"""Optional Faster-Whisper transcription fallback for low-quality live transcripts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
import tempfile
from typing import Any
import wave

from .config import AppSettings

logger = logging.getLogger(__name__)


class FasterWhisperTranscriber:
    """Lazily load Faster-Whisper and transcribe caller audio on demand."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._model: Any | None = None
        self._unavailable = False

    @property
    def enabled(self) -> bool:
        return bool(self._settings.faster_stt_enabled)

    def _language_code(self, language_hint: str | None) -> str | None:
        value = str(language_hint or "").strip().lower()
        if value == "marathi":
            return "mr"
        if value == "hindi":
            return "hi"
        if value == "english":
            return "en"
        return None

    def _ensure_model(self) -> Any | None:
        if self._unavailable or not self.enabled:
            return None
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as exc:
            self._unavailable = True
            logger.warning("Faster-Whisper unavailable; fallback STT disabled: %s", exc)
            return None
        try:
            self._model = WhisperModel(
                self._settings.faster_stt_model,
                device=self._settings.faster_stt_device,
                compute_type=self._settings.faster_stt_compute_type,
            )
            logger.info(
                "Loaded Faster-Whisper model=%s device=%s compute_type=%s",
                self._settings.faster_stt_model,
                self._settings.faster_stt_device,
                self._settings.faster_stt_compute_type,
            )
        except Exception as exc:
            self._unavailable = True
            logger.warning("Failed to initialize Faster-Whisper; fallback STT disabled: %s", exc)
            return None
        return self._model

    def transcribe_wav(self, wav_path: Path, language_hint: str | None = None) -> str:
        model = self._ensure_model()
        if model is None or not wav_path.exists():
            return ""
        language = self._language_code(language_hint)
        try:
            segments, _ = model.transcribe(
                str(wav_path),
                beam_size=self._settings.faster_stt_beam_size,
                language=language,
                vad_filter=True,
                condition_on_previous_text=False,
                temperature=0.0,
            )
            parts: list[str] = []
            for segment in segments:
                text = " ".join(str(getattr(segment, "text", "") or "").split()).strip()
                if text:
                    parts.append(text)
            return " ".join(parts).strip()
        except Exception as exc:
            logger.warning("Faster-Whisper transcription failed for %s: %s", wav_path, exc)
            return ""

    async def transcribe_wav_async(self, wav_path: Path, language_hint: str | None = None) -> str:
        return await asyncio.to_thread(self.transcribe_wav, wav_path, language_hint)

    def transcribe_pcm16_mono(self, pcm_bytes: bytes, sample_rate: int = 16_000, language_hint: str | None = None) -> str:
        if not pcm_bytes:
            return ""
        model = self._ensure_model()
        if model is None:
            return ""

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="parallel_stt_", suffix=".wav", delete=False) as handle:
                temp_path = Path(handle.name)
            with wave.open(str(temp_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(pcm_bytes)
            return self.transcribe_wav(temp_path, language_hint)
        except Exception as exc:
            logger.warning("Faster-Whisper PCM transcription failed: %s", exc)
            return ""
        finally:
            if temp_path is not None:
                with contextlib.suppress(Exception):
                    temp_path.unlink(missing_ok=True)

    async def transcribe_pcm16_mono_async(
        self,
        pcm_bytes: bytes,
        sample_rate: int = 16_000,
        language_hint: str | None = None,
    ) -> str:
        return await asyncio.to_thread(self.transcribe_pcm16_mono, pcm_bytes, sample_rate, language_hint)
