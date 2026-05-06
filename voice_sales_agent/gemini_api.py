"""Gemini Live API and structured text generation wrappers."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from google import genai
from google.genai.errors import ClientError
from google.genai import types

from .constants import INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE
from .extraction import build_extraction_prompt, parse_summary_payload
from .models import ClientBundle, GenerationSettings, PostCallSummary, SessionArtifacts

logger = logging.getLogger(__name__)


def build_generation_config(settings: GenerationSettings | None) -> types.GenerationConfig | None:
    """Convert file-based generation settings into SDK config."""
    if settings is None:
        return None
    payload = settings.model_dump(exclude_none=True)
    if not payload:
        return None
    return types.GenerationConfig(**payload)


def build_live_connect_kwargs(settings: GenerationSettings | None) -> dict[str, Any]:
    """Convert file-based generation settings into LiveConnectConfig kwargs."""
    if settings is None:
        return {}
    payload = settings.model_dump(exclude_none=True)
    return {
        "temperature": payload.get("temperature"),
        "topP": payload.get("top_p"),
        "topK": payload.get("top_k"),
        "maxOutputTokens": payload.get("max_output_tokens"),
    }


@dataclass(slots=True)
class LiveEvent:
    kind: str
    text: str | None = None
    audio: bytes | None = None
    turn_complete: bool = False
    interrupted: bool = False
    latency_ms: float | None = None
    raw: Any | None = None
    is_final: bool = True
    source: str | None = None


class GeminiStructuredClient:
    """Thin wrapper for Gemini text generation used for summaries and extraction."""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def summarize_call(self, client_bundle: ClientBundle, artifacts: SessionArtifacts) -> PostCallSummary:
        """Generate a structured post-call summary."""
        prompt = build_extraction_prompt(client_bundle, artifacts)
        config_payload = client_bundle.config.structured_generation.model_dump(exclude_none=True)
        config_payload["response_mime_type"] = "application/json"
        generation_config = types.GenerateContentConfig(**config_payload)
        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=self._model,
            contents=prompt,
            config=generation_config,
        )
        return parse_summary_payload(response.text or "{}")

    async def generate_chat_reply(
        self,
        *,
        system_prompt: str,
        user_text: str,
        generation_settings: GenerationSettings | None = None,
    ) -> str:
        """Generate a short plain-text reply for WhatsApp chat turns."""
        config_payload: dict[str, Any] = {"response_mime_type": "text/plain"}
        if generation_settings is not None:
            config_payload.update(generation_settings.model_dump(exclude_none=True))
        generation_config = types.GenerateContentConfig(**config_payload)
        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=self._model,
            contents=[
                types.Content(role="user", parts=[types.Part(text=system_prompt)]),
                types.Content(role="user", parts=[types.Part(text=user_text)]),
            ],
            config=generation_config,
        )
        return (response.text or "").strip()

    async def generate_json(
        self,
        *,
        prompt: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Generate strict JSON from a prompt using the structured model."""
        generation_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=temperature,
        )
        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=self._model,
            contents=prompt,
            config=generation_config,
        )
        text = (response.text or "").strip()
        if not text:
            return {}
        try:
            import json

            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"raw": parsed}
        except Exception:
            from .live_preview_pipeline import parse_json_text

            return parse_json_text(text)


class GeminiSpeechClient:
    """Generate deterministic spoken lines using Gemini TTS."""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._cache: dict[tuple[str, str], bytes] = {}

    async def synthesize(self, text: str, voice_name: str) -> bytes:
        """Synthesize one exact line of speech."""
        cache_key = (voice_name, text.strip())
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=self._model,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                    )
                ),
            ),
        )

        for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            if content is None:
                continue
            for part in getattr(content, "parts", []) or []:
                inline_data = getattr(part, "inline_data", None)
                if inline_data and getattr(inline_data, "data", None):
                    data = inline_data.data
                    if isinstance(data, str):
                        data = base64.b64decode(data)
                    self._cache[cache_key] = data
                    return data
        raise RuntimeError("No audio returned from Gemini TTS.")


class GeminiLiveVoiceClient:
    """Manage an audio-in/audio-out Gemini Live API session."""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._session_cm = None
        self._session = None
        self._response_started_at: float | None = None
        self._turn_started_at: float | None = None
        self._response_mode = "live_audio"
        self._activity_open = False
        self._explicit_vad_enabled = False

    async def __aenter__(self) -> "GeminiLiveVoiceClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(
        self,
        system_prompt: str,
        voice_name: str,
        generation_settings: GenerationSettings | None = None,
        response_mode: str = "text_tts",
        explicit_vad: bool = False,
    ) -> None:
        """Open the Gemini Live session."""
        if response_mode == "text_tts" and "native-audio-preview" in self._model:
            logger.warning(
                "Response mode 'text_tts' is not supported by %s; falling back to live audio.",
                self._model,
            )
            response_mode = "live_audio"
        self._response_mode = response_mode
        # The current Gemini Live API surface does not accept explicit_vad_signal.
        # Keep this disabled until the SDK/API adds stable support.
        if explicit_vad:
            logger.warning(
                "Explicit VAD was requested but is not supported by the current Gemini API surface. Falling back."
            )
        self._explicit_vad_enabled = False
        response_modalities = ["TEXT"] if response_mode == "text_tts" else ["AUDIO"]
        config_kwargs: dict[str, Any] = {
            "response_modalities": response_modalities,
            "input_audio_transcription": {},
            "system_instruction": types.Content(
                role="user",
                parts=[types.Part(text=system_prompt)],
            ),
            "thinking_config": types.ThinkingConfig(thinking_budget=0),
            **build_live_connect_kwargs(generation_settings),
        }
        if response_mode != "text_tts":
            config_kwargs["speech_config"] = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                )
            )
            config_kwargs["output_audio_transcription"] = {}
        config = types.LiveConnectConfig(
            **config_kwargs,
        )
        self._session_cm = self._client.aio.live.connect(model=self._model, config=config)
        self._session = await self._session_cm.__aenter__()
        logger.info("Connected to Gemini Live with model %s", self._model)

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """Push raw microphone audio to Gemini."""
        if self._session is None:
            raise RuntimeError("Live session is not connected.")
        if self._turn_started_at is None:
            self._turn_started_at = perf_counter()
            self._response_started_at = None
        if self._explicit_vad_enabled and not self._activity_open:
            await self._session.send_realtime_input(activity_start=types.ActivityStart())
            self._activity_open = True
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm_chunk, mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}")
        )

    async def signal_activity_end(self) -> None:
        """Explicitly tell Gemini that the current realtime audio turn has ended."""
        if self._session is None or not self._explicit_vad_enabled or not self._activity_open:
            return
        await self._session.send_realtime_input(activity_end=types.ActivityEnd())
        self._activity_open = False

    async def send_text_turn(self, text: str, role: str = "user", turn_complete: bool = True) -> None:
        """Send a text turn into the Live session."""
        if self._session is None:
            raise RuntimeError("Live session is not connected.")
        await self._session.send_client_content(
            turns=types.Content(role=role, parts=[types.Part(text=text)]),
            turn_complete=turn_complete,
        )

    async def receive(self) -> AsyncIterator[LiveEvent]:
        """Yield normalized live session events from Gemini."""
        if self._session is None:
            raise RuntimeError("Live session is not connected.")

        while self._session is not None:
            async for response in self._session.receive():
                server_content = getattr(response, "server_content", None)
                if server_content is None:
                    yield LiveEvent(kind="raw", raw=response)
                    continue

                model_turn = getattr(server_content, "model_turn", None)
                if model_turn is not None:
                    for part in getattr(model_turn, "parts", []) or []:
                        inline_data = getattr(part, "inline_data", None)
                        if inline_data and getattr(inline_data, "data", None):
                            if self._response_started_at is None and self._turn_started_at is not None:
                                self._response_started_at = perf_counter()
                            audio_data = inline_data.data
                            if isinstance(audio_data, str):
                                audio_data = base64.b64decode(audio_data)
                            yield LiveEvent(
                                kind="audio",
                                audio=audio_data,
                                latency_ms=self._compute_latency_ms(),
                                raw=response,
                            )
                        text_value = getattr(part, "text", None)
                        if text_value:
                            yield LiveEvent(
                                kind="agent_text",
                                text=text_value,
                                raw=response,
                                source="model_turn",
                            )

                input_transcription = getattr(server_content, "input_transcription", None)
                if input_transcription and getattr(input_transcription, "text", None):
                    yield LiveEvent(
                        kind="user_text",
                        text=input_transcription.text,
                        raw=response,
                        is_final=bool(getattr(input_transcription, "finished", True)),
                    )

                output_transcription = getattr(server_content, "output_transcription", None)
                if output_transcription and getattr(output_transcription, "text", None):
                    yield LiveEvent(
                        kind="agent_text",
                        text=output_transcription.text,
                        raw=response,
                        is_final=bool(getattr(output_transcription, "finished", True)),
                        source="output_transcription",
                    )

                if getattr(server_content, "interrupted", False):
                    yield LiveEvent(kind="interrupted", interrupted=True, raw=response)

                if getattr(server_content, "turn_complete", False):
                    yield LiveEvent(
                        kind="turn_complete",
                        turn_complete=True,
                        latency_ms=self._compute_latency_ms(final=True),
                        raw=response,
                    )
                    self._turn_started_at = None
                    self._response_started_at = None
                    self._activity_open = False
                    break

    def _compute_latency_ms(self, final: bool = False) -> float | None:
        if self._turn_started_at is None:
            return None
        end_time = perf_counter() if final or self._response_started_at is None else self._response_started_at
        return max((end_time - self._turn_started_at) * 1000, 0.0)

    async def close(self) -> None:
        """Close the live session safely."""
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception as exc:
                message = str(exc)
                if "keepalive ping timeout" in message or "abnormal closure" in message or "timed out while closing connection" in message:
                    logger.warning("Suppressing Gemini Live close transport error during shutdown: %s", exc)
                else:
                    raise
            self._session_cm = None
            self._session = None
            self._activity_open = False
            self._explicit_vad_enabled = False
            logger.info("Closed Gemini Live session")

    @property
    def output_sample_rate(self) -> int:
        return OUTPUT_SAMPLE_RATE

    @property
    def response_mode(self) -> str:
        return self._response_mode
