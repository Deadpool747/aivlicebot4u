"""Piopiy TTS adapters for the live worker."""

from __future__ import annotations

import logging
import os
import time
from typing import AsyncGenerator, Optional

from piopiy.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame
from piopiy.services.tts_service import TTSService

from .gemini_api import GeminiSpeechClient

logger = logging.getLogger(__name__)


def _normalize_gemini_tts_model(model: str) -> str:
    value = (model or "").strip()
    aliases = {
        "gemini-2.5-flash-tts": "gemini-2.5-flash-preview-tts",
        "gemini-2.5-pro-tts": "gemini-2.5-pro-preview-tts",
    }
    value = aliases.get(value, value)
    if not value.startswith("models/"):
        value = f"models/{value}"
    return value


class GeminiApiKeyTTSService(TTSService):
    """Gemini API-key-backed TTS for Piopiy.

    The installed Piopiy GeminiTTSService expects Google Cloud service-account
    credentials. This adapter uses the repo's Gemini API-key speech client so we
    can keep the live worker on Gemini without provisioning separate creds.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.5-flash-tts",
        voice_id: str = "Aoede",
        sample_rate: Optional[int] = 24_000,
        call_session_id: str | None = None,
        disable_fallback: bool = False,
        fallback_model: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._voice_id = (voice_id or "Aoede").strip() or "Aoede"
        self._model = _normalize_gemini_tts_model(model)
        self._speech = GeminiSpeechClient(api_key, self._model)
        fallback_model = fallback_model or (
            os.getenv("PIOPIY_GEMINI_TTS_FALLBACK_MODEL")
            or os.getenv("GEMINI_TTS_FALLBACK_MODEL")
            or "gemini-2.5-pro-preview-tts"
        )
        self._fallback_model = _normalize_gemini_tts_model(fallback_model)
        self._fallback_speech = (
            GeminiSpeechClient(api_key, self._fallback_model)
            if self._fallback_model and self._fallback_model != self._model
            else None
        )
        self._quota_error_count = 0
        self._circuit_open = False
        self._circuit_threshold = int(os.getenv("PIOPIY_TTS_CIRCUIT_BREAKER_THRESHOLD", "2") or "2")
        self._call_session_id = (
            call_session_id
            or
            os.getenv("PIOPIY_CURRENT_CALL_SESSION_ID")
            or os.getenv("PIOPIY_SESSION_ID")
            or "unknown"
        )
        self._active_provider = "gemini_tts"
        self._disable_fallback = disable_fallback
        self._fallback_switch_count = 0
        self._audio_writer_count = 1
        self._quota_cooldown_until = 0.0
        self._quota_cooldown_seconds = float(os.getenv("PIOPIY_TTS_QUOTA_COOLDOWN_SECONDS", "8") or "8")

    @property
    def voice_id(self) -> str:
        return self._voice_id

    @staticmethod
    def _is_quota_error(exc: Exception) -> bool:
        text = f"{type(exc).__name__}: {exc}"
        return "429" in text or "RESOURCE_EXHAUSTED" in text

    def _log_tts_event(
        self,
        *,
        provider: str,
        model: str,
        error_code: str | None = None,
        fallback_triggered: bool = False,
        fallback_reason: str | None = None,
    ) -> None:
        logger.info(
            "piopiy_tts_event session_id=%s tts_provider_used=%s tts_model_used=%s voice_name=%s "
            "tts_error_code=%s fallback_triggered=%s fallback_reason=%s audio_writer_count=%s audio_queue_size=%s",
            self._call_session_id,
            provider,
            model,
            self._voice_id,
            error_code or "",
            bool(fallback_triggered),
            fallback_reason or "",
            self._audio_writer_count,
            0,
        )

    def _log_audio_path(
        self,
        *,
        provider: str,
        model: str,
        started_at: float,
        first_audio_at: float | None = None,
        duplicate_audio_path_detected: bool = False,
    ) -> None:
        latency_ms = None
        if first_audio_at is not None:
            latency_ms = round((first_audio_at - started_at) * 1000, 1)
        logger.info(
            "piopiy_audio_path active_voice_name=%s active_tts_provider=%s fallback_voice_name=%s "
            "audio_path_active=%s duplicate_audio_path_detected=%s audio_path_locked=true voice_locked=true fallback_switch_count=%s tts_started_at=%.6f "
            "first_audio_chunk_ready_time=%s first_audio_sent_to_piopiy_at=%s total_response_latency_ms=%s "
            "call_session_id=%s session_id=%s tts_model_used=%s audio_writer_count=%s audio_queue_size=%s",
            self._voice_id,
            provider,
            self._voice_id,
            "text_tts",
            bool(duplicate_audio_path_detected),
            self._fallback_switch_count,
            started_at,
            f"{first_audio_at:.6f}" if first_audio_at is not None else "",
            f"{first_audio_at:.6f}" if first_audio_at is not None else "",
            latency_ms if latency_ms is not None else "",
            self._call_session_id,
            self._call_session_id,
            model,
            self._audio_writer_count,
            0,
        )

    async def _synthesize_with_fallback(self, text: str, *, reason: str) -> bytes:
        if self._disable_fallback:
            raise RuntimeError(
                "Gemini TTS fallback disabled for this call; refusing mid-call voice/model switch."
            )
        if self._fallback_speech is None:
            raise RuntimeError("Gemini TTS circuit is open and no fallback model is configured.")
        self._fallback_switch_count += 1
        self._active_provider = "gemini_tts_fallback"
        self._log_tts_event(
            provider="gemini_tts_fallback",
            model=self._fallback_model,
            error_code="primary_circuit_open" if self._circuit_open else "primary_429",
            fallback_triggered=True,
            fallback_reason=reason,
        )
        return await self._fallback_speech.synthesize(text, self._voice_id)

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        tts_started_at = time.monotonic()
        active_provider = self._active_provider
        active_model = self._fallback_model if self._circuit_open else self._model
        try:
            yield TTSStartedFrame()
            if self._disable_fallback and self._quota_cooldown_until > tts_started_at:
                logger.warning(
                    "piopiy_tts_quota_cooldown session_id=%s voice_name=%s tts_model=%s "
                    "cooldown_remaining_ms=%.1f fallback_triggered=false audio_writer_count=%s audio_queue_size=%s",
                    self._call_session_id,
                    self._voice_id,
                    self._model,
                    (self._quota_cooldown_until - tts_started_at) * 1000,
                    self._audio_writer_count,
                    0,
                )
                yield TTSStoppedFrame()
                return
            if self._circuit_open:
                if self._disable_fallback:
                    self._quota_cooldown_until = time.monotonic() + self._quota_cooldown_seconds
                    logger.warning(
                        "piopiy_tts_circuit_open_no_fallback session_id=%s voice_name=%s tts_model=%s "
                        "fallback_triggered=false fallback_reason=disabled_to_prevent_voice_switch audio_writer_count=%s audio_queue_size=%s",
                        self._call_session_id,
                        self._voice_id,
                        self._model,
                        self._audio_writer_count,
                        0,
                    )
                    yield TTSStoppedFrame()
                    return
                audio = await self._synthesize_with_fallback(text, reason="primary_circuit_open")
                active_provider = "gemini_tts_fallback"
                active_model = self._fallback_model
            else:
                try:
                    self._log_tts_event(
                        provider="gemini_tts",
                        model=self._model,
                        fallback_triggered=False,
                    )
                    gemini_request_start_time = time.monotonic()
                    audio = await self._speech.synthesize(text, self._voice_id)
                    active_provider = "gemini_tts"
                    active_model = self._model
                    gemini_text_response_time = time.monotonic()
                except Exception as exc:
                    if not self._is_quota_error(exc):
                        raise
                    self._quota_error_count += 1
                    self._log_tts_event(
                        provider="gemini_tts",
                        model=self._model,
                        error_code="429_RESOURCE_EXHAUSTED",
                        fallback_triggered=self._quota_error_count > self._circuit_threshold,
                        fallback_reason="primary_429",
                    )
                    if self._disable_fallback:
                        self._quota_cooldown_until = time.monotonic() + self._quota_cooldown_seconds
                        logger.warning(
                            "piopiy_tts_quota_no_fallback session_id=%s voice_name=%s tts_model=%s "
                            "fallback_triggered=false fallback_reason=disabled_to_prevent_voice_switch "
                            "quota_error_count=%s audio_writer_count=%s audio_queue_size=%s",
                            self._call_session_id,
                            self._voice_id,
                            self._model,
                            self._quota_error_count,
                            self._audio_writer_count,
                            0,
                        )
                        if self._quota_error_count > self._circuit_threshold:
                            self._circuit_open = True
                        yield TTSStoppedFrame()
                        return
                    if self._quota_error_count > self._circuit_threshold:
                        self._circuit_open = True
                    audio = await self._synthesize_with_fallback(text, reason="primary_429")
                    active_provider = "gemini_tts_fallback"
                    active_model = self._fallback_model
                    gemini_text_response_time = time.monotonic()
            if not audio:
                raise RuntimeError("Gemini TTS returned no audio.")
            first_audio_at = time.monotonic()
            self._log_audio_path(
                provider=active_provider,
                model=active_model,
                started_at=tts_started_at,
                first_audio_at=first_audio_at,
                duplicate_audio_path_detected=False,
            )
            logger.info(
                "piopiy_turn_latency session_id=%s user_speech_end_time=%s gemini_request_start_time=%.6f "
                "gemini_text_response_time=%.6f tts_start_time=%.6f first_audio_chunk_ready_time=%.6f "
                "first_audio_sent_to_piopiy_time=%.6f total_turn_latency_ms=%.1f voice_name=%s tts_model=%s "
                "fallback_triggered=%s audio_writer_count=%s audio_queue_size=%s",
                self._call_session_id,
                "",
                locals().get("gemini_request_start_time", tts_started_at),
                locals().get("gemini_text_response_time", first_audio_at),
                tts_started_at,
                first_audio_at,
                first_audio_at,
                (first_audio_at - tts_started_at) * 1000,
                self._voice_id,
                active_model,
                active_provider == "gemini_tts_fallback",
                self._audio_writer_count,
                0,
            )
            yield TTSAudioRawFrame(audio, self.sample_rate, 1)
            yield TTSStoppedFrame()
        except Exception as exc:
            logger.exception("Gemini API-key TTS failed")
            if not self._disable_fallback:
                yield ErrorFrame(error=f"Gemini API-key TTS failed: {exc}")
            yield TTSStoppedFrame()
