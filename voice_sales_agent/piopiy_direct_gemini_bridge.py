"""Direct Piopiy <-> Gemini Live bridge.

This path keeps Piopiy responsible only for telephony transport while our
backend talks to Gemini Live directly for audio-in/audio-out conversation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from .clients import load_client
from .config import AppSettings
from .gemini_api import GeminiLiveVoiceClient, LiveEvent
from .language import infer_opening_language
from .models import ClientBundle, ProjectRuntimeConfig
from .prompt_builder import PromptBuilder
from .telephony import PendingCall

logger = logging.getLogger(__name__)

TraceHook = Callable[[str], None]
StageHook = Callable[[str], None]


def _resolve_runtime_api_key(settings: AppSettings, project_runtime: ProjectRuntimeConfig | None) -> str:
    return (
        (project_runtime.gemini_api_key if project_runtime else None)
        or (
            os.getenv(project_runtime.gemini_api_key_env or "")
            if project_runtime and project_runtime.gemini_api_key_env
            else None
        )
        or settings.gemini_api_key
    )


def _render_opening_text(client: ClientBundle, customer_name: str) -> str:
    display_name = client.config.display_name
    opening_language = (
        client.config.default_opening_language
        or infer_opening_language(customer_name)
        or "english"
    )
    configured_opening = client.config.opening_script.get(opening_language)
    if configured_opening:
        return configured_opening.format(
            customer_name=customer_name,
            hospital_name=display_name,
            project_name=client.active_project.name if client.active_project else display_name,
        ).strip()

    if client.config.conversation_mode == "appointment_booking":
        if opening_language == "marathi":
            return f"नमस्कार, मी {display_name} मधून बोलते आहे. तुमचं नाव {customer_name} आहे का?"
        if opening_language == "hindi":
            return f"नमस्कार, मैं {display_name} से बोल रही हूँ। क्या आपका नाम {customer_name} है?"
        return f"Hello, this is {display_name}. Am I speaking with {customer_name}?"

    persona_gender = client.config.voice.persona_gender
    if opening_language == "marathi":
        verb = "बोलते आहे" if persona_gender == "female" else "बोलत आहे"
        return f"नमस्कार, मी {display_name} कडून {verb}. तुमचं नाव {customer_name} आहे का?"
    if opening_language == "hindi":
        verb = "बोल रही हूँ" if persona_gender == "female" else "बोल रहा हूँ"
        return f"नमस्कार, मैं {display_name} से {verb}। क्या आपका नाम {customer_name} है?"
    return f"Hello, am I speaking with {customer_name}?"


class PiopiyDirectGeminiBridgeSession:
    """Run one direct audio bridge session for a Piopiy streaming call."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        pending_call: PendingCall,
        audio,
        trace_hook: TraceHook | None = None,
        stage_hook: StageHook | None = None,
    ) -> None:
        self.settings = settings
        self.pending_call = pending_call
        self.audio = audio
        self.trace_hook = trace_hook or (lambda _message: None)
        self.stage_hook = stage_hook or (lambda _stage: None)

        project_id = (pending_call.metadata or {}).get("project_id")
        self.client = load_client(pending_call.client_id, project_id=project_id)
        self.project_runtime = self.client.active_project.runtime if self.client.active_project else None
        self.runtime_api_key = _resolve_runtime_api_key(settings, self.project_runtime)
        self.runtime_live_model = (
            (self.project_runtime.live_model if self.project_runtime else None)
            or settings.live_model
        )
        self.prompt_builder = PromptBuilder()
        self.live = GeminiLiveVoiceClient(self.runtime_api_key, self.runtime_live_model)
        self._stopped = asyncio.Event()
        self._caller_audio_received_at: float | None = None
        self._gemini_audio_send_started_at: float | None = None
        self._gemini_first_audio_at: float | None = None
        self._first_audio_sent_to_piopiy_at: float | None = None
        self._audio_chunks_received = 0
        self._audio_chunks_buffered_before_send = 0

    async def run(self) -> None:
        contact_details = {
            key: str(value).strip()
            for key, value in (self.pending_call.metadata or {}).items()
            if isinstance(value, (str, int, float)) and str(value).strip()
        }
        opening_language = (
            self.client.config.default_opening_language
            or infer_opening_language(self.pending_call.customer_name)
            or "english"
        )
        system_prompt = self.prompt_builder.build(
            self.client,
            customer_name=self.pending_call.customer_name,
            opening_language=opening_language,
            contact_details=contact_details,
        )
        voice_name = self.client.config.voice.voice_name
        sender: asyncio.Task[None] | None = None
        receiver: asyncio.Task[None] | None = None
        try:
            self.stage_hook("direct_gemini_connecting")
            await self.live.connect(
                system_prompt=system_prompt,
                voice_name=voice_name,
                generation_settings=self.client.config.live_generation,
                response_mode="live_audio",
                explicit_vad=False,
            )
            self.trace_hook(
                f"Direct Gemini bridge connected model={self.runtime_live_model} voice={voice_name} client={self.pending_call.client_id} "
                "selected_agent_class=PiopiyDirectGeminiBridgeSession selected_audio_path=NATIVE_GEMINI_LIVE_AUDIO "
                "gemini_live_native_audio_enabled=true separate_tts_enabled=false audio_path_locked=true voice_locked=true"
            )
            self.stage_hook("direct_gemini_connected")

            if not _env_bool("PIOPIY_DIRECT_GEMINI_SKIP_OPENING", False):
                opening_text = _render_opening_text(self.client, self.pending_call.customer_name)
                await self._speak_exact_line(opening_text)
                self.trace_hook(f"Direct Gemini opening delivered: {opening_text}")

            sender = asyncio.create_task(self._send_loop(), name="piopiy_direct_gemini_send")
            receiver = asyncio.create_task(self._receive_loop(), name="piopiy_direct_gemini_recv")
            done, pending = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        finally:
            for task in (sender, receiver):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            await self.live.close()
            self.stage_hook("direct_gemini_closed")

    async def _speak_exact_line(self, text: str) -> None:
        prompt = (
            "Speak exactly the following line and nothing else. "
            f"Do not add any introduction or explanation: {text}"
        )
        await self.live.send_text_turn(prompt, role="user", turn_complete=True)

    async def _send_loop(self) -> None:
        async for chunk in self.audio.mic_chunks():
            now = time.monotonic()
            self._audio_chunks_received += 1
            if self._caller_audio_received_at is None:
                self._caller_audio_received_at = now
                self.trace_hook(
                    "latency caller_audio_received_at=%.6f vad_speech_start_at=%.6f audio_chunk_buffer_size=%s"
                    % (now, now, len(chunk))
                )
            if self._gemini_audio_send_started_at is None:
                self._gemini_audio_send_started_at = now
                self.trace_hook(
                    "latency gemini_audio_send_started_at=%.6f number_of_audio_chunks_buffered_before_send=%s "
                    "waits_for_full_tts=false response_audio_streaming=chunk_by_chunk"
                    % (now, self._audio_chunks_buffered_before_send)
                )
            await self.live.send_audio(chunk)
        self._stopped.set()

    async def _receive_loop(self) -> None:
        async for event in self.live.receive():
            await self._handle_event(event)
            if self._stopped.is_set():
                return

    async def _handle_event(self, event: LiveEvent) -> None:
        if event.kind == "audio" and event.audio:
            now = time.monotonic()
            if self._gemini_first_audio_at is None:
                self._gemini_first_audio_at = now
                total_latency_ms = None
                if self._caller_audio_received_at is not None:
                    total_latency_ms = round((now - self._caller_audio_received_at) * 1000, 1)
                self.trace_hook(
                    "latency gemini_first_token_or_audio_at=%.6f first_audio_chunk_ready_at=%.6f total_turn_latency_ms=%s "
                    "audio_chunk_buffer_size=%s number_of_audio_chunks_buffered_before_send=%s waits_for_full_tts=false response_audio_streaming=chunk_by_chunk"
                    % (
                        now,
                        now,
                        total_latency_ms if total_latency_ms is not None else "",
                        len(event.audio),
                        self._audio_chunks_buffered_before_send,
                    )
                )
            await self.audio.play(event.audio)
            if self._first_audio_sent_to_piopiy_at is None:
                self._first_audio_sent_to_piopiy_at = time.monotonic()
                total_latency_ms = None
                if self._caller_audio_received_at is not None:
                    total_latency_ms = round((self._first_audio_sent_to_piopiy_at - self._caller_audio_received_at) * 1000, 1)
                self.trace_hook(
                    "latency first_audio_sent_to_piopiy_at=%.6f total_turn_latency_ms=%s audio_path_active=gemini_live_audio_in_audio_out"
                    % (self._first_audio_sent_to_piopiy_at, total_latency_ms if total_latency_ms is not None else "")
                )
            if event.latency_ms is not None:
                self.trace_hook(f"Direct Gemini first audio latency_ms={round(event.latency_ms, 1)}")
            return

        if event.kind == "interrupted":
            await self.audio.flush_playback()
            self.trace_hook("Direct Gemini interruption detected; playback flushed.")
            return

        if event.kind == "user_text" and event.text:
            if event.is_final:
                self.trace_hook(f"Caller: {event.text}")
            return

        if event.kind == "agent_text" and event.text and event.is_final:
            self.trace_hook(f"Agent: {event.text}")
            return

        if event.kind == "turn_complete":
            if self._caller_audio_received_at is not None:
                self.trace_hook("latency vad_speech_end_at=%.6f" % time.monotonic())
            self.trace_hook("Direct Gemini turn complete.")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default
