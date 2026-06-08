"""Sarvam-backed live adapters using turn-based STT/LLM/TTS behind the live client interface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import contextlib
import logging
from pathlib import Path
import tempfile
import wave

from .constants import INPUT_SAMPLE_RATE
from .gemini_api import GeminiStructuredClient, LiveEvent
from .sarvam_api import SarvamRecordingTranscriber, SarvamSpeechClient, SarvamStructuredClient

logger = logging.getLogger(__name__)


class SarvamLiveVoiceClient:
    """Live client compatibility shim for Sarvam using per-turn processing."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        chat_model: str,
        tts_model: str,
        tts_speaker: str,
        tts_target_language_code: str,
        stt_model: str,
        stt_mode: str,
        stt_language_code: str,
    ) -> None:
        self._structured = SarvamStructuredClient(api_key, base_url=base_url, model=chat_model)
        self._speech = SarvamSpeechClient(
            api_key,
            base_url=base_url,
            model=tts_model,
            speaker=tts_speaker,
            target_language_code=tts_target_language_code,
        )
        self._stt = SarvamRecordingTranscriber(
            api_key,
            base_url=base_url,
            model=stt_model,
            mode=stt_mode,
            language_code=stt_language_code,
        )
        self._queue: asyncio.Queue[LiveEvent | None] = asyncio.Queue()
        self._audio_buffer = bytearray()
        self._system_prompt = ""
        self._voice_name = ""
        self._history: list[dict[str, str]] = []
        self._response_mode = "text_tts"
        self._connected = False

    @property
    def response_mode(self) -> str:
        return self._response_mode

    async def connect(
        self,
        system_prompt: str,
        voice_name: str,
        generation_settings=None,
        response_mode: str = "text_tts",
        explicit_vad: bool = False,
    ) -> None:
        del generation_settings, explicit_vad
        self._system_prompt = system_prompt
        self._voice_name = voice_name
        self._response_mode = response_mode
        self._connected = True
        logger.info("Connected Sarvam live adapter in %s mode.", response_mode)

    async def close(self) -> None:
        self._connected = False
        self._audio_buffer.clear()
        with contextlib.suppress(Exception):
            await self._queue.put(None)

    async def send_audio(self, pcm_chunk: bytes) -> None:
        if not self._connected or not pcm_chunk:
            return
        self._audio_buffer.extend(pcm_chunk)

    async def send_text_turn(self, text: str, role: str = "user", turn_complete: bool = True) -> None:
        del role
        if not self._connected:
            raise RuntimeError("Sarvam live adapter is not connected.")
        normalized = text.strip()
        if not normalized:
            return
        deterministic_line = self._extract_deterministic_line(normalized)
        if deterministic_line:
            await self._emit_agent_line(deterministic_line, turn_complete=turn_complete)
            return
        await self._emit_user_and_reply(normalized, turn_complete=turn_complete)

    @staticmethod
    def _extract_deterministic_line(text: str) -> str | None:
        marker = "Speak exactly the following line and nothing else. Do not add any introduction or explanation:"
        if marker in text:
            value = text.split(marker, 1)[1].strip()
            return " ".join(value.split()).strip() or None
        return None

    async def _emit_agent_line(self, agent_text: str, turn_complete: bool) -> None:
        self._history.append({"role": "assistant", "text": agent_text})
        await self._queue.put(
            LiveEvent(kind="agent_text", text=agent_text, is_final=True, turn_complete=False, source="sarvam")
        )
        audio = await self._speech.synthesize(agent_text, self._voice_name)
        if audio:
            await self._queue.put(
                LiveEvent(kind="audio", audio=audio, is_final=True, turn_complete=False, source="sarvam")
            )
        await self._queue.put(LiveEvent(kind="turn_complete", turn_complete=turn_complete, source="sarvam"))

    async def signal_activity_end(self) -> None:
        if not self._connected:
            return
        if not self._audio_buffer:
            return

        transcript = await self._transcribe_buffer(bytes(self._audio_buffer))
        self._audio_buffer.clear()
        if not transcript:
            return
        await self._emit_user_and_reply(transcript, turn_complete=True)

    async def _emit_user_and_reply(self, user_text: str, turn_complete: bool) -> None:
        await self._queue.put(
            LiveEvent(kind="user_text", text=user_text, is_final=True, turn_complete=False, source="sarvam")
        )
        self._history.append({"role": "user", "text": user_text})
        transcript = "\n".join(
            f"{'User' if turn['role'] == 'user' else 'Agent'}: {turn['text']}"
            for turn in self._history[-12:]
            if turn.get("text")
        )
        reply_prompt = (
            f"{self._system_prompt}\n\nConversation so far:\n{transcript}\n\n"
            "Respond naturally to the latest user message."
        )
        agent_text = await self._structured.generate_chat_reply(
            system_prompt=reply_prompt,
            user_text=user_text,
            generation_settings=None,
        )
        agent_text = " ".join(str(agent_text or "").split()).strip()
        if not agent_text:
            agent_text = "धन्यवाद. कृपया पुन्हा एकदा थोडक्यात सांगा."
        self._history.append({"role": "assistant", "text": agent_text})

        await self._queue.put(
            LiveEvent(kind="agent_text", text=agent_text, is_final=True, turn_complete=False, source="sarvam")
        )

        if self._response_mode == "live_audio":
            audio = await self._speech.synthesize(agent_text, self._voice_name)
            if audio:
                await self._queue.put(
                    LiveEvent(kind="audio", audio=audio, is_final=True, turn_complete=False, source="sarvam")
                )
        await self._queue.put(LiveEvent(kind="turn_complete", turn_complete=turn_complete, source="sarvam"))

    async def _transcribe_buffer(self, pcm_bytes: bytes) -> str:
        if not pcm_bytes:
            return ""
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="sarvam_live_", suffix=".wav", delete=False) as handle:
                temp_path = Path(handle.name)
            with wave.open(str(temp_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(INPUT_SAMPLE_RATE)
                wav_file.writeframes(pcm_bytes)
            text, _meta = await self._stt.transcribe_async(temp_path, language_hint=None)
            return " ".join(str(text or "").split()).strip()
        except Exception as exc:
            logger.warning("Sarvam live transcription failed: %s", exc)
            return ""
        finally:
            if temp_path is not None:
                with contextlib.suppress(Exception):
                    temp_path.unlink(missing_ok=True)

    async def receive(self) -> AsyncIterator[LiveEvent]:
        while self._connected:
            event = await self._queue.get()
            if event is None:
                return
            yield event


class SarvamGeminiHybridLiveClient(SarvamLiveVoiceClient):
    """Sarvam STT/TTS with Gemini handling text reasoning between turns."""

    def __init__(
        self,
        *,
        sarvam_api_key: str,
        sarvam_base_url: str,
        tts_model: str,
        tts_speaker: str,
        tts_target_language_code: str,
        stt_model: str,
        stt_mode: str,
        stt_language_code: str,
        gemini_api_key: str,
        gemini_model: str,
    ) -> None:
        self._structured = GeminiStructuredClient(gemini_api_key, gemini_model)
        self._speech = SarvamSpeechClient(
            sarvam_api_key,
            base_url=sarvam_base_url,
            model=tts_model,
            speaker=tts_speaker,
            target_language_code=tts_target_language_code,
        )
        self._stt = SarvamRecordingTranscriber(
            sarvam_api_key,
            base_url=sarvam_base_url,
            model=stt_model,
            mode=stt_mode,
            language_code=stt_language_code,
        )
        self._queue: asyncio.Queue[LiveEvent | None] = asyncio.Queue()
        self._audio_buffer = bytearray()
        self._system_prompt = ""
        self._voice_name = ""
        self._history: list[dict[str, str]] = []
        self._response_mode = "live_audio"
        self._connected = False
