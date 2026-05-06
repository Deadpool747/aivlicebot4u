"""Internal actual-cost estimation for telephony and Gemini usage."""

from __future__ import annotations

from typing import Any

from .config import AppSettings
from .models import ActualCostLedger, CostLineItem, SessionArtifacts


def _round_money(value: float) -> float:
    return round(value, 6)


class SessionCostTracker:
    """Track internal provider usage and estimate actual costs per session."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.live_input_audio_bytes = 0
        self.live_output_audio_bytes = 0
        self.tts_output_audio_bytes = 0
        self.tts_output_chars = 0
        self.agent_text_chars = 0
        self.structured_input_chars = 0
        self.structured_output_chars = 0

    def record_live_input_audio(self, payload: bytes) -> None:
        self.live_input_audio_bytes += len(payload)

    def record_live_output_audio(self, payload: bytes) -> None:
        self.live_output_audio_bytes += len(payload)

    def record_agent_text(self, text: str) -> None:
        self.agent_text_chars += len(text.strip())

    def record_tts(self, text: str, audio_bytes: bytes) -> None:
        self.tts_output_chars += len(text.strip())
        self.tts_output_audio_bytes += len(audio_bytes)

    def record_structured_request(self, prompt_text: str) -> None:
        self.structured_input_chars += len(prompt_text.strip())

    def record_structured_response(self, response_text: str) -> None:
        self.structured_output_chars += len(response_text.strip())

    def build_ledger(self, artifacts: SessionArtifacts, telephony_context: dict[str, Any] | None) -> ActualCostLedger:
        currency = self.settings.cost_currency
        telephony_context = telephony_context or {}
        telephony_items: list[CostLineItem] = []
        gemini_items: list[CostLineItem] = []
        notes: list[str] = []

        provider = str(telephony_context.get("provider") or "").strip() or None
        provider_call_sid = str(telephony_context.get("provider_call_sid") or "").strip() or None
        stream_duration_seconds = self._resolve_telephony_duration_seconds(artifacts, telephony_context)

        if provider == "exotel":
            telephony_items.append(
                self._build_duration_item(
                    provider="exotel",
                    category="telephony",
                    duration_seconds=stream_duration_seconds,
                    rate_per_minute=self.settings.exotel_cost_per_minute,
                    currency=currency,
                    metadata={
                        "call_status": telephony_context.get("call_status"),
                        "stream_status": telephony_context.get("stream_status"),
                        "disconnected_by": telephony_context.get("stream_disconnected_by"),
                    },
                )
            )
        elif provider == "twilio":
            telephony_items.append(
                self._build_duration_item(
                    provider="twilio",
                    category="telephony",
                    duration_seconds=stream_duration_seconds,
                    rate_per_minute=self.settings.twilio_cost_per_minute,
                    currency=currency,
                    metadata={"call_status": telephony_context.get("call_status")},
                )
            )
        elif provider == "airtel_iq":
            telephony_items.append(
                self._build_duration_item(
                    provider="airtel_iq",
                    category="telephony",
                    duration_seconds=stream_duration_seconds,
                    rate_per_minute=self.settings.airtel_iq_cost_per_minute,
                    currency=currency,
                    metadata={"call_status": telephony_context.get("call_status")},
                )
            )
        elif provider == "tata":
            telephony_items.append(
                self._build_duration_item(
                    provider="tata",
                    category="telephony",
                    duration_seconds=stream_duration_seconds,
                    rate_per_minute=self.settings.tata_cost_per_minute,
                    currency=currency,
                    metadata={"call_status": telephony_context.get("call_status")},
                )
            )
        elif provider == "meta_whatsapp":
            telephony_items.append(
                self._build_duration_item(
                    provider="meta_whatsapp",
                    category="telephony",
                    duration_seconds=stream_duration_seconds,
                    rate_per_minute=self.settings.meta_whatsapp_cost_per_minute,
                    currency=currency,
                    metadata={"call_status": telephony_context.get("call_status")},
                )
            )

        live_input_seconds = self.live_input_audio_bytes / (16_000 * 2)
        live_output_seconds = self.live_output_audio_bytes / (24_000 * 2)
        tts_output_seconds = self.tts_output_audio_bytes / (24_000 * 2)

        if live_input_seconds > 0:
            gemini_items.append(
                self._build_duration_item(
                    provider="gemini",
                    category="live_input_audio",
                    duration_seconds=live_input_seconds,
                    rate_per_minute=self.settings.gemini_live_input_cost_per_minute,
                    currency=currency,
                    metadata={"model": self.settings.live_model},
                )
            )
        if live_output_seconds > 0:
            gemini_items.append(
                self._build_duration_item(
                    provider="gemini",
                    category="live_output_audio",
                    duration_seconds=live_output_seconds,
                    rate_per_minute=self.settings.gemini_live_output_cost_per_minute,
                    currency=currency,
                    metadata={"model": self.settings.live_model},
                )
            )
        if self.tts_output_chars > 0:
            gemini_items.append(
                self._build_char_item(
                    provider="gemini",
                    category="tts_text",
                    chars=self.tts_output_chars,
                    rate_per_1k_chars=self.settings.gemini_tts_cost_per_1k_chars,
                    currency=currency,
                    metadata={
                        "model": self.settings.tts_model,
                        "audio_seconds": round(tts_output_seconds, 3),
                    },
                )
            )
        if self.structured_input_chars > 0 or self.structured_output_chars > 0:
            total_structured_chars = self.structured_input_chars + self.structured_output_chars
            gemini_items.append(
                self._build_char_item(
                    provider="gemini",
                    category="structured_generation",
                    chars=total_structured_chars,
                    rate_per_1k_chars=self.settings.gemini_structured_cost_per_1k_chars,
                    currency=currency,
                    metadata={
                        "model": self.settings.structured_model,
                        "input_chars": self.structured_input_chars,
                        "output_chars": self.structured_output_chars,
                    },
                )
            )

        if provider and not telephony_items:
            notes.append(f"No telephony pricing configured for provider '{provider}'.")
        if self.settings.gemini_live_input_cost_per_minute <= 0 and live_input_seconds > 0:
            notes.append("Gemini live input audio rate is unset; output uses a 0 cost placeholder.")
        if self.settings.gemini_live_output_cost_per_minute <= 0 and live_output_seconds > 0:
            notes.append("Gemini live output audio rate is unset; output uses a 0 cost placeholder.")
        if self.settings.gemini_tts_cost_per_1k_chars <= 0 and self.tts_output_chars > 0:
            notes.append("Gemini TTS rate is unset; output uses a 0 cost placeholder.")
        if self.settings.gemini_structured_cost_per_1k_chars <= 0 and (self.structured_input_chars or self.structured_output_chars):
            notes.append("Gemini structured-generation rate is unset; output uses a 0 cost placeholder.")

        total_estimated_cost = _round_money(
            sum(item.estimated_cost for item in telephony_items) + sum(item.estimated_cost for item in gemini_items)
        )

        return ActualCostLedger(
            status="estimated",
            currency=currency,
            telephony=telephony_items,
            gemini=gemini_items,
            total_estimated_cost=total_estimated_cost,
            notes=notes,
            provider_call_sid=provider_call_sid,
            telephony_provider=provider,
            raw_provider_usage=self._normalize_context(telephony_context),
            raw_model_usage={
                "live_model": self.settings.live_model,
                "structured_model": self.settings.structured_model,
                "tts_model": self.settings.tts_model,
                "live_input_audio_seconds": round(live_input_seconds, 3),
                "live_output_audio_seconds": round(live_output_seconds, 3),
                "tts_output_audio_seconds": round(tts_output_seconds, 3),
                "tts_output_chars": self.tts_output_chars,
                "agent_text_chars": self.agent_text_chars,
                "structured_input_chars": self.structured_input_chars,
                "structured_output_chars": self.structured_output_chars,
            },
        )

    def _resolve_telephony_duration_seconds(
        self, artifacts: SessionArtifacts, telephony_context: dict[str, Any]
    ) -> float:
        stream_duration = telephony_context.get("stream_duration_seconds")
        if isinstance(stream_duration, str):
            try:
                stream_duration = float(stream_duration)
            except ValueError:
                stream_duration = None
        if isinstance(stream_duration, int | float) and stream_duration >= 0:
            return float(stream_duration)

        if artifacts.ended_at is not None:
            return max((artifacts.ended_at - artifacts.started_at).total_seconds(), 0.0)
        return 0.0

    def _build_duration_item(
        self,
        *,
        provider: str,
        category: str,
        duration_seconds: float,
        rate_per_minute: float,
        currency: str,
        metadata: dict[str, Any],
    ) -> CostLineItem:
        minutes = duration_seconds / 60 if duration_seconds > 0 else 0.0
        return CostLineItem(
            provider=provider,
            category=category,
            unit="minute",
            quantity=round(minutes, 6),
            rate=rate_per_minute,
            currency=currency,
            estimated_cost=_round_money(minutes * rate_per_minute),
            metadata=self._normalize_context({"duration_seconds": round(duration_seconds, 3), **metadata}),
        )

    def _build_char_item(
        self,
        *,
        provider: str,
        category: str,
        chars: int,
        rate_per_1k_chars: float,
        currency: str,
        metadata: dict[str, Any],
    ) -> CostLineItem:
        quantity = chars / 1000 if chars > 0 else 0.0
        return CostLineItem(
            provider=provider,
            category=category,
            unit="1k_chars",
            quantity=round(quantity, 6),
            rate=rate_per_1k_chars,
            currency=currency,
            estimated_cost=_round_money(quantity * rate_per_1k_chars),
            metadata=self._normalize_context({"chars": chars, **metadata}),
        )

    @staticmethod
    def _normalize_context(payload: dict[str, Any]) -> dict[str, str | float | int | bool | None]:
        normalized: dict[str, str | float | int | bool | None] = {}
        for key, value in payload.items():
            if value is None or isinstance(value, str | int | float | bool):
                normalized[str(key)] = value
            else:
                normalized[str(key)] = str(value)
        return normalized
