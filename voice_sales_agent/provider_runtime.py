"""Provider-agnostic runtime factories for live, TTS, STT, and structured models."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from .config import AppSettings
from .faster_stt import FasterWhisperTranscriber
from .gemini_api import GeminiLiveVoiceClient, GeminiSpeechClient, GeminiStructuredClient
from .recording_stt import RecordingSpeechRecognitionTranscriber


class StructuredClientProtocol(Protocol):
    async def summarize_call(self, client_bundle: Any, artifacts: Any) -> Any: ...
    async def generate_chat_reply(self, *, system_prompt: str, user_text: str, generation_settings: Any = None) -> str: ...
    async def generate_json(self, *, prompt: str, temperature: float = 0.2) -> dict[str, Any]: ...


class SpeechClientProtocol(Protocol):
    async def synthesize(self, text: str, voice_name: str) -> bytes: ...


class RecordingSttProtocol(Protocol):
    async def transcribe_async(self, wav_path: Any, language_hint: str | None = None) -> tuple[str, dict[str, str]]: ...


class LiveClientProtocol(Protocol):
    response_mode: str

    async def connect(
        self,
        system_prompt: str,
        voice_name: str,
        generation_settings: Any = None,
        response_mode: str = "text_tts",
        explicit_vad: bool = False,
    ) -> None: ...
    async def close(self) -> None: ...
    async def send_audio(self, pcm_chunk: bytes) -> None: ...
    async def receive(self): ...
    async def signal_activity_end(self) -> None: ...
    async def send_text_turn(self, text: str, role: str = "user", turn_complete: bool = True) -> None: ...


@dataclass(slots=True)
class ProviderRuntime:
    live: LiveClientProtocol
    structured: StructuredClientProtocol
    structured_fallback: StructuredClientProtocol | None
    speech: SpeechClientProtocol
    recording_stt: RecordingSttProtocol
    faster_stt: FasterWhisperTranscriber


def _normalize_provider(value: str | None, default: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1].strip()
    normalized = normalized.lower()
    return normalized or default


def build_provider_runtime(
    settings: AppSettings,
    *,
    runtime_api_key: str,
    runtime_structured_api_key: str,
    runtime_live_model: str,
    runtime_structured_model: str,
    runtime_structured_fallback_model: str | None,
    runtime_tts_model: str,
) -> ProviderRuntime:
    provider_default = _normalize_provider(settings.speech_provider, "gemini")
    live_provider = _normalize_provider(os.getenv("LIVE_PROVIDER"), "gemini")
    structured_provider = _normalize_provider(os.getenv("STRUCTURED_PROVIDER"), provider_default)
    tts_provider = _normalize_provider(os.getenv("TTS_PROVIDER"), provider_default)
    recording_stt_provider = _normalize_provider(os.getenv("RECORDING_STT_PROVIDER"), provider_default)

    if live_provider == "gemini":
        live = GeminiLiveVoiceClient(runtime_api_key, runtime_live_model)
    else:
        raise RuntimeError(f"LIVE_PROVIDER='{live_provider}' is unsupported. Use: gemini.")

    structured = GeminiStructuredClient(runtime_structured_api_key, runtime_structured_model)
    structured_fallback: StructuredClientProtocol | None = None
    if runtime_structured_fallback_model and runtime_structured_fallback_model != runtime_structured_model:
        structured_fallback = GeminiStructuredClient(runtime_structured_api_key, runtime_structured_fallback_model)

    speech = GeminiSpeechClient(runtime_api_key, runtime_tts_model)

    recording_stt = RecordingSpeechRecognitionTranscriber(
        language_candidates=["mr-IN", "hi-IN", "en-IN"],
        chunk_seconds=settings.recording_stt_chunk_seconds,
    )

    faster_stt = FasterWhisperTranscriber(settings)
    return ProviderRuntime(
        live=live,
        structured=structured,
        structured_fallback=structured_fallback,
        speech=speech,
        recording_stt=recording_stt,
        faster_stt=faster_stt,
    )


def build_structured_client(
    settings: AppSettings,
    *,
    api_key: str,
    model: str,
) -> StructuredClientProtocol:
    structured_provider = _normalize_provider(os.getenv("STRUCTURED_PROVIDER"), _normalize_provider(settings.speech_provider, "gemini"))
    if structured_provider != "gemini":
        raise RuntimeError(f"STRUCTURED_PROVIDER='{structured_provider}' is unsupported. Use: gemini.")
    return GeminiStructuredClient(api_key, model)
