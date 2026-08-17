"""Optional Faster-Whisper transcription fallback for low-quality live transcripts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
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

    @staticmethod
    def _normalize_text(text: str | None) -> str:
        return " ".join(str(text or "").split()).strip()

    @staticmethod
    def _quality_score(text: str, language_probability: float | None = None) -> float:
        normalized = FasterWhisperTranscriber._normalize_text(text)
        if not normalized:
            return float("-inf")
        tokens = [token for token in normalized.split() if token]
        token_count = len(tokens)
        unique_token_ratio = len(set(tokens)) / max(token_count, 1)
        alpha_token_count = sum(1 for token in tokens if any(char.isalpha() for char in token))
        repeated_word_runs = len(re.findall(r"\b(\w+)(?:\s+\1\b){2,}", normalized, flags=re.IGNORECASE))
        repeated_char_runs = len(re.findall(r"(.)\1{4,}", normalized))
        punctuation_runs = len(re.findall(r"[۔.]{4,}", normalized))
        weird_symbol_count = len(re.findall(r"[^\w\s\u0900-\u097F\u0600-\u06FF,.!?।،;:'\"()\-\u2019]", normalized))
        language_bonus = float(language_probability or 0.0) * 10.0
        return (
            token_count * 1.5
            + unique_token_ratio * 20.0
            + alpha_token_count
            + language_bonus
            - repeated_word_runs * 18.0
            - repeated_char_runs * 12.0
            - punctuation_runs * 4.0
            - weird_symbol_count * 2.0
        )

    def _transcribe_wav_pass(self, wav_path: Path, language_hint: str | None = None) -> tuple[str, dict[str, Any]]:
        model = self._ensure_model()
        if model is None or not wav_path.exists():
            return "", {
                "language": None,
                "language_probability": None,
                "language_hint": language_hint,
                "model": self._settings.faster_stt_model,
                "provider": "faster_whisper",
            }
        language = self._language_code(language_hint)
        try:
            segments, info = model.transcribe(
                str(wav_path),
                beam_size=self._settings.faster_stt_beam_size,
                language=language,
                # Keep VAD off for post-call recordings so quieter caller speech
                # and overlaps are not dropped before transcription.
                vad_filter=False,
                condition_on_previous_text=False,
                temperature=0.0,
            )
            parts: list[str] = []
            for segment in segments:
                text = self._normalize_text(getattr(segment, "text", "") or "")
                if text:
                    parts.append(text)
            transcript_text = self._normalize_text(" ".join(parts))
            return transcript_text, {
                "language": self._normalize_text(getattr(info, "language", None)) or None,
                "language_probability": getattr(info, "language_probability", None),
                "language_hint": language_hint,
                "model": self._settings.faster_stt_model,
                "provider": "faster_whisper",
            }
        except Exception as exc:
            logger.warning("Faster-Whisper transcription failed for %s: %s", wav_path, exc)
            return "", {
                "language": None,
                "language_probability": None,
                "language_hint": language_hint,
                "model": self._settings.faster_stt_model,
                "provider": "faster_whisper",
            }

    def _wav_duration_seconds(self, wav_path: Path) -> float | None:
        try:
            with wave.open(str(wav_path), "rb") as wav_file:
                frame_rate = float(wav_file.getframerate() or 0)
                total_frames = float(wav_file.getnframes() or 0)
            if frame_rate <= 0:
                return None
            return total_frames / frame_rate
        except Exception:
            return None

    def _transcribe_chunked_wav(
        self,
        wav_path: Path,
        language_hints: list[str | None],
        chunk_seconds: int = 15,
    ) -> tuple[str, dict[str, Any]]:
        if not wav_path.exists():
            return "", {
                "language": None,
                "language_probability": None,
                "language_hint": None,
                "model": self._settings.faster_stt_model,
                "provider": "faster_whisper",
                "strategy": "chunked",
                "chunks": [],
            }
        try:
            with wave.open(str(wav_path), "rb") as source:
                channels = source.getnchannels()
                sample_width = source.getsampwidth()
                frame_rate = source.getframerate()
                total_frames = source.getnframes()
                if frame_rate <= 0 or total_frames <= 0:
                    return "", {
                        "language": None,
                        "language_probability": None,
                        "language_hint": None,
                        "model": self._settings.faster_stt_model,
                        "provider": "faster_whisper",
                        "strategy": "chunked",
                        "chunks": [],
                    }
                frames_per_chunk = max(int(frame_rate * max(chunk_seconds, 4)), frame_rate)
                step_frames = frames_per_chunk
                offset = 0
                chunk_index = 0
                chunk_texts: list[str] = []
                chunk_meta: list[dict[str, Any]] = []
                with tempfile.TemporaryDirectory(prefix="faster_whisper_chunks_") as temp_dir:
                    temp_root = Path(temp_dir)
                    while offset < total_frames:
                        source.setpos(offset)
                        frames = source.readframes(min(frames_per_chunk, total_frames - offset))
                        if not frames:
                            break
                        chunk_index += 1
                        chunk_path = temp_root / f"chunk_{chunk_index:03d}.wav"
                        with wave.open(str(chunk_path), "wb") as out:
                            out.setnchannels(channels)
                            out.setsampwidth(sample_width)
                            out.setframerate(frame_rate)
                            out.writeframes(frames)
                        best_chunk_text = ""
                        best_chunk_meta: dict[str, Any] = {
                            "language": None,
                            "language_probability": None,
                            "language_hint": None,
                            "score": float("-inf"),
                            "chunk_index": chunk_index,
                        }
                        for hint in language_hints:
                            candidate_text, candidate_meta = self._transcribe_wav_pass(chunk_path, hint)
                            candidate_score = self._quality_score(
                                candidate_text,
                                candidate_meta.get("language_probability"),
                            )
                            if candidate_score > float(best_chunk_meta["score"]):
                                best_chunk_text = candidate_text
                                best_chunk_meta = {
                                    **candidate_meta,
                                    "score": candidate_score,
                                    "chunk_index": chunk_index,
                                }
                        if best_chunk_text:
                            chunk_texts.append(best_chunk_text)
                        chunk_meta.append(best_chunk_meta)
                        offset += step_frames
                transcript_text = self._normalize_text(" ".join(chunk_texts))
                best_chunk_meta = max(chunk_meta, key=lambda item: float(item.get("score") or float("-inf")), default={})
                return transcript_text, {
                    "language": best_chunk_meta.get("language"),
                    "language_probability": best_chunk_meta.get("language_probability"),
                    "language_hint": best_chunk_meta.get("language_hint"),
                    "model": self._settings.faster_stt_model,
                    "provider": "faster_whisper",
                    "strategy": "chunked",
                    "chunk_seconds": chunk_seconds,
                    "chunks": chunk_meta,
                }
        except Exception as exc:
            logger.warning("Faster-Whisper chunked transcription failed for %s: %s", wav_path, exc)
            return "", {
                "language": None,
                "language_probability": None,
                "language_hint": None,
                "model": self._settings.faster_stt_model,
                "provider": "faster_whisper",
                "strategy": "chunked",
                "chunks": [],
            }

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
        text, _ = self._transcribe_wav_pass(wav_path, language_hint)
        return text

    def transcribe_wav_best(
        self,
        wav_path: Path,
        language_hints: list[str | None] | None = None,
        chunk_seconds: int = 15,
    ) -> tuple[str, dict[str, Any]]:
        model = self._ensure_model()
        if model is None or not wav_path.exists():
            return "", {
                "language": None,
                "language_probability": None,
                "language_hint": None,
                "model": self._settings.faster_stt_model,
                "provider": "faster_whisper",
                "strategy": "unavailable",
                "candidates": [],
            }
        hints = [hint for hint in (language_hints or [None, "hindi", "marathi"]) if hint not in {""}]
        if None not in hints:
            hints.append(None)

        candidates: list[dict[str, Any]] = []
        for hint in hints:
            text, meta = self._transcribe_wav_pass(wav_path, hint)
            candidates.append(
                {
                    "strategy": "full",
                    "language_hint": hint,
                    "text": text,
                    "language": meta.get("language"),
                    "language_probability": meta.get("language_probability"),
                    "score": self._quality_score(text, meta.get("language_probability")),
                }
            )

        duration_seconds = self._wav_duration_seconds(wav_path)
        if duration_seconds is not None and duration_seconds >= max(float(chunk_seconds), 4.0):
            chunked_text, chunked_meta = self._transcribe_chunked_wav(wav_path, hints, chunk_seconds=chunk_seconds)
            candidates.append(
                {
                    "strategy": "chunked",
                    "language_hint": chunked_meta.get("language_hint"),
                    "text": chunked_text,
                    "language": chunked_meta.get("language"),
                    "language_probability": chunked_meta.get("language_probability"),
                    "score": self._quality_score(chunked_text, chunked_meta.get("language_probability")),
                    "chunk_seconds": chunk_seconds,
                    "chunks": chunked_meta.get("chunks") or [],
                }
            )

        best_candidate = max(candidates, key=lambda item: float(item.get("score") or float("-inf")), default=None)
        if not best_candidate:
            return "", {
                "language": None,
                "language_probability": None,
                "language_hint": None,
                "model": self._settings.faster_stt_model,
                "provider": "faster_whisper",
                "strategy": "none",
                "candidates": [],
            }
        return best_candidate.get("text") or "", {
            "language": best_candidate.get("language"),
            "language_probability": best_candidate.get("language_probability"),
            "language_hint": best_candidate.get("language_hint"),
            "model": self._settings.faster_stt_model,
            "provider": "faster_whisper",
            "strategy": best_candidate.get("strategy"),
            "chunk_seconds": best_candidate.get("chunk_seconds"),
            "candidates": candidates,
        }

    async def transcribe_wav_async(self, wav_path: Path, language_hint: str | None = None) -> str:
        return await asyncio.to_thread(self.transcribe_wav, wav_path, language_hint)

    async def transcribe_wav_best_async(
        self,
        wav_path: Path,
        language_hints: list[str | None] | None = None,
        chunk_seconds: int = 15,
    ) -> tuple[str, dict[str, Any]]:
        return await asyncio.to_thread(self.transcribe_wav_best, wav_path, language_hints, chunk_seconds)

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
