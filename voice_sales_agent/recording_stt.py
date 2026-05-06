"""Recording-based STT using SpeechRecognition for post-call corpus extraction."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
import tempfile
import time
import wave

logger = logging.getLogger(__name__)


class RecordingSpeechRecognitionTranscriber:
    """Build full-call text corpus from a saved WAV recording."""

    def __init__(
        self,
        language_candidates: list[str] | None = None,
        chunk_seconds: int = 8,
        request_retries: int = 3,
        retry_backoff_seconds: float = 0.8,
    ) -> None:
        self.language_candidates = [item.strip() for item in (language_candidates or ["mr-IN", "hi-IN", "en-IN"]) if item.strip()]
        self.chunk_seconds = max(int(chunk_seconds or 8), 3)
        self.request_retries = max(int(request_retries or 1), 1)
        self.retry_backoff_seconds = max(float(retry_backoff_seconds or 0.2), 0.2)

    def _recognize_google_with_retries(self, recognizer: object, audio: object, language: str) -> str:
        # Import locally so class remains import-safe when dependency is missing.
        import speech_recognition as sr  # type: ignore

        last_error: Exception | None = None
        for attempt in range(1, self.request_retries + 1):
            try:
                return recognizer.recognize_google(audio, language=language)
            except sr.UnknownValueError:
                # Audio decoded but no text recognized; retry won't help.
                return ""
            except Exception as exc:
                last_error = exc
                if attempt >= self.request_retries:
                    break
                time.sleep(self.retry_backoff_seconds * attempt)
        if last_error is not None:
            logger.debug("SpeechRecognition request failed language=%s error=%s", language, last_error)
        return ""

    def _recognize_full(self, wav_path: Path) -> tuple[str, str | None]:
        try:
            import speech_recognition as sr  # type: ignore
        except Exception as exc:
            logger.warning("SpeechRecognition unavailable for recording STT: %s", exc)
            return "", None

        recognizer = sr.Recognizer()
        for language in self.language_candidates:
            try:
                with sr.AudioFile(str(wav_path)) as source:
                    audio = recognizer.record(source)
                text = self._recognize_google_with_retries(recognizer, audio, language)
                normalized = " ".join(str(text or "").split()).strip()
                if normalized:
                    return normalized, language
            except Exception:
                continue
        return "", None

    def _recognize_chunked(self, wav_path: Path) -> tuple[str, list[str]]:
        parts: list[str] = []
        langs: list[str] = []
        with wave.open(str(wav_path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            frame_rate = source.getframerate()
            total_frames = source.getnframes()
            chunk_frames = int(frame_rate * self.chunk_seconds)
            offset = 0
            while offset < total_frames:
                frames = source.readframes(min(chunk_frames, total_frames - offset))
                if not frames:
                    break
                offset += chunk_frames
                with tempfile.NamedTemporaryFile(prefix="recording_stt_chunk_", suffix=".wav", delete=False) as temp:
                    temp_path = Path(temp.name)
                try:
                    with wave.open(str(temp_path), "wb") as out:
                        out.setnchannels(channels)
                        out.setsampwidth(sample_width)
                        out.setframerate(frame_rate)
                        out.writeframes(frames)
                    text, language = self._recognize_full(temp_path)
                    if text:
                        parts.append(text)
                        if language:
                            langs.append(language)
                finally:
                    with contextlib.suppress(Exception):
                        temp_path.unlink(missing_ok=True)
        return " ".join(parts).strip(), langs

    def transcribe(self, wav_path: Path) -> tuple[str, dict[str, str]]:
        if not wav_path.exists():
            return "", {}
        full_text, full_language = self._recognize_full(wav_path)
        if full_text:
            return full_text, {"mode": "full_recording", "language": full_language or ""}
        chunked_text, chunked_languages = self._recognize_chunked(wav_path)
        if chunked_text:
            return chunked_text, {"mode": "chunked_recording", "languages": ",".join(chunked_languages)}
        return "", {}

    async def transcribe_async(self, wav_path: Path) -> tuple[str, dict[str, str]]:
        return await asyncio.to_thread(self.transcribe, wav_path)
