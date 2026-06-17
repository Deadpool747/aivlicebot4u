"""Piopiy agent worker integration for the live voice sales flow.

This worker follows the Piopiy quickstart pattern:
- AGENT_ID
- AGENT_TOKEN
- create_session(...)

It keeps the live voice conversation separate from the dashboard web app so
the agent can run as its own long-lived process.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json

from .clients import list_client_ids, load_client
from .config import AppSettings, load_settings
from .prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)
TRACE_FILE = Path(os.getenv("PIOPIY_TRACE_FILE", "/opt/new_voice_agent/runtime/piopiy_agent_trace.jsonl"))


@dataclass(slots=True)
class PiopiyProviderConfig:
    factory: str
    api_key: str | None = None
    model: str | None = None
    voice_name: str | None = None
    language_code: str | None = None
    base_url: str | None = None


def _use_gemini_live() -> bool:
    value = os.getenv("PIOPIY_PIPELINE_MODE", "gemini_live").strip().lower()
    return value in {"gemini_live", "speech_to_speech", "speech", "realtime", "live"}


def _pipeline_mode() -> str:
    return os.getenv("PIOPIY_PIPELINE_MODE", "gemini_live").strip().lower() or "gemini_live"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _load_factory(spec: str) -> Any:
    module_name, attr_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def _instantiate(factory: Any, **kwargs: Any) -> Any:
    signature = inspect.signature(factory)
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    filtered: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value is None or value == "":
            continue
        if accepts_kwargs or key in signature.parameters:
            filtered[key] = value
    return factory(**filtered)


def _resolve_customer_name(from_number: str, metadata: dict[str, Any] | None) -> str:
    candidates = (
        (metadata or {}).get("customer_name"),
        (metadata or {}).get("name"),
        (metadata or {}).get("full_name"),
        from_number,
        "Caller",
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return "Caller"


def _resolve_contact_details(from_number: str, to_number: str, metadata: dict[str, Any] | None) -> dict[str, str]:
    details: dict[str, str] = {}
    if from_number.strip():
        details["from_number"] = from_number.strip()
    if to_number.strip():
        details["to_number"] = to_number.strip()
    for key in ("email", "phone", "company", "customer_name", "name", "full_name"):
        value = str((metadata or {}).get(key) or "").strip()
        if value:
            details[key] = value
    return details


def _build_prompt(
    *,
    settings: AppSettings,
    client_id: str,
    project_id: str | None,
    customer_name: str,
    contact_details: dict[str, str],
) -> tuple[str, str]:
    client = load_client(client_id, project_id=project_id)
    prompt_builder = PromptBuilder()
    instructions = prompt_builder.build(
        client,
        customer_name=customer_name,
        opening_language=client.config.default_opening_language,
        contact_details=contact_details,
    )
    greeting = (
        client.config.closure_examples.guidance
        or client.config.primary_offer
        or f"Hello {customer_name}, thanks for calling."
    )
    if not greeting.strip():
        greeting = f"Hello {customer_name}, thanks for calling."
    logger.info(
        "Prepared Piopiy session prompt for client_id=%s project_id=%s",
        client_id,
        project_id or "",
    )
    opening_language = (client.config.default_opening_language or "english").strip().lower()
    opening_template = client.config.opening_script.get(opening_language) or client.config.opening_script.get("english")
    project_name = (project_id or client.config.display_name or "demo").replace("_", " ").strip()
    del settings
    if opening_template:
        greeting = opening_template.format(
            customer_name=customer_name,
            project_name=project_name,
        ).strip()
    return instructions, greeting


def _build_provider_kwargs(config: PiopiyProviderConfig) -> dict[str, Any]:
    return {
        "api_key": config.api_key,
        "model": config.model,
        "voice_name": config.voice_name,
        "voice_id": config.voice_name,
        "language_code": config.language_code,
        "base_url": config.base_url,
    }


def _resolve_sarvam_api_key() -> str | None:
    return (
        os.getenv("PIOPIY_SARVAM_API_KEY")
        or os.getenv("SARVAM_API_KEY")
        or ""
    ).strip() or None


def _resolve_sarvam_openai_base_url() -> str | None:
    base_url = (
        os.getenv("PIOPIY_LLM_BASE_URL")
        or os.getenv("SARVAM_OPENAI_BASE_URL")
        or os.getenv("SARVAM_BASE_URL")
        or ""
    ).strip()
    if not base_url:
        return None
    if base_url.endswith("/v1"):
        return base_url
    return f"{base_url.rstrip('/')}/v1"


def _append_trace(event: str, **fields: Any) -> None:
    try:
        TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        with TRACE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logger.exception("Failed to append Piopiy trace event %s", event)


async def run_piopiy_agent() -> None:
    """Start the Piopiy agent worker and keep it connected for incoming calls."""
    settings = load_settings()
    agent_id = (os.getenv("AGENT_ID") or settings.piopiy_agent_id or "ai_agent").strip()
    agent_token = (os.getenv("AGENT_TOKEN") or settings.piopiy_api_token or "").strip()
    if not agent_token:
        raise RuntimeError("Missing AGENT_TOKEN / PIOPIY API token.")
    client_id = (os.getenv("PIOPIY_CLIENT_ID") or "aivoicebot4u_guest_demo").strip()
    if not client_id:
        available_clients = list_client_ids()
        if available_clients:
            client_id = available_clients[0]
    if not client_id:
        raise RuntimeError("Missing PIOPIY_CLIENT_ID and no client templates are available.")
    _append_trace("worker_boot", agent_id=agent_id, token_present=bool(agent_token), client_id=client_id or None)

    project_id = (os.getenv("PIOPIY_PROJECT_ID") or "real_estate_english_demo").strip() or None

    pipeline_mode = _pipeline_mode()
    use_gemini_native = pipeline_mode in {
        "gemini_live",
        "speech_to_speech",
        "speech",
        "realtime",
        "live",
        "gemini_native_simple",
        "native_simple",
        "gemini_native",
    }
    gemini_live_factory = _load_factory(
        os.getenv(
            "PIOPIY_GEMINI_LIVE_FACTORY",
            "piopiy.services.google.gemini_live.llm:GeminiLiveLLMService",
        ).strip()
    )
    gemini_tts_factory = _load_factory(
        os.getenv(
            "PIOPIY_GEMINI_TTS_FACTORY",
            "piopiy.services.google.tts:GeminiTTSService",
        ).strip()
    )
    gemini_input_params = _load_factory("piopiy.services.google.gemini_live.llm:InputParams")
    gemini_modalities = _load_factory("piopiy.services.google.gemini_live.llm:GeminiModalities")
    gemini_live_config = PiopiyProviderConfig(
        factory=os.getenv(
            "PIOPIY_GEMINI_LIVE_FACTORY",
            "piopiy.services.google.gemini_live.llm:GeminiLiveLLMService",
        ).strip(),
        api_key=(
            os.getenv("PIOPIY_GEMINI_API_KEY")
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or ""
        ).strip() or None,
        model=(
            os.getenv("PIOPIY_GEMINI_LIVE_MODEL")
            or os.getenv("GEMINI_LIVE_MODEL")
            or "models/gemini-2.5-flash-native-audio-preview-12-2025"
        ).strip() or None,
    )
    gemini_tts_config = PiopiyProviderConfig(
        factory=os.getenv(
            "PIOPIY_GEMINI_TTS_FACTORY",
            "piopiy.services.google.tts:GeminiTTSService",
        ).strip(),
        api_key=(
            os.getenv("PIOPIY_GEMINI_API_KEY")
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or ""
        ).strip() or None,
        model=(
            os.getenv("PIOPIY_GEMINI_TTS_MODEL")
            or os.getenv("GEMINI_TTS_MODEL")
            or "gemini-2.5-flash-tts"
        ).strip() or None,
        voice_name=(
            os.getenv("PIOPIY_GEMINI_TTS_VOICE_ID")
            or os.getenv("PIOPIY_TTS_VOICE_NAME")
            or os.getenv("PIOPIY_TTS_VOICE_ID")
            or "Kore"
        ).strip() or None,
    )

    async def create_session(
        agent_id: str,
        call_id: str,
        from_number: str,
        to_number: str,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        logger.info(
            "Piopiy create_session started agent_id=%s call_id=%s from=%s to=%s metadata_keys=%s",
            agent_id,
            call_id,
            from_number,
            to_number,
            sorted((metadata or {}).keys()),
        )
        _append_trace(
            "create_session_started",
            agent_id=agent_id,
            call_id=call_id,
            from_number=from_number,
            to_number=to_number,
            metadata_keys=sorted((metadata or {}).keys()),
        )
        del kwargs
        try:
            client = load_client(client_id, project_id=project_id)
            customer_name = _resolve_customer_name(from_number, metadata)
            contact_details = _resolve_contact_details(from_number, to_number, metadata)
            instructions, greeting = _build_prompt(
                settings=settings,
                client_id=client_id,
                project_id=project_id,
                customer_name=customer_name,
                contact_details=contact_details,
            )

            client_voice_name = (client.config.voice.voice_name or "").strip() or None
            if use_gemini_native:
                from piopiy.speech_agent import SpeechAgent

                speech_agent = SpeechAgent(
                    instructions=instructions,
                    greeting=greeting,
                )
                omni = _instantiate(
                    gemini_live_factory,
                    api_key=gemini_live_config.api_key,
                    model=gemini_live_config.model,
                    system_instruction=instructions,
                    params=gemini_input_params(
                        modalities=gemini_modalities.AUDIO,
                    ),
                )
                tts = _instantiate(
                    gemini_tts_factory,
                    api_key=gemini_tts_config.api_key,
                    model=gemini_tts_config.model,
                    voice_id=gemini_tts_config.voice_name or client_voice_name or "Kore",
                )
                await speech_agent.Action(
                    omni=omni,
                    tts=tts,
                    allow_interruptions=_env_bool("PIOPIY_ALLOW_INTERRUPTIONS", True),
                )
                _append_trace("session_configured", call_id=call_id, mode="gemini_native_no_stt")
                await speech_agent.start()
                _append_trace("session_started", call_id=call_id)
                logger.info("Piopiy Gemini-native no-STT session started for call_id=%s", call_id)
                return

            raise RuntimeError(
                f"PIOPIY_PIPELINE_MODE={pipeline_mode!r} is not supported by the no-STT worker. "
                "Use gemini_live or gemini_native_simple."
            )
        except Exception as exc:
            _append_trace("create_session_error", call_id=call_id, error=repr(exc))
            raise

    from piopiy.agent import Agent

    agent = Agent(
        agent_id=agent_id,
        agent_token=agent_token,
        create_session=create_session,
        debug=_env_bool("PIOPIY_DEBUG", True),
    )
    logger.info("Piopiy agent %s online. Waiting for calls...", agent_id)
    _append_trace("agent_connecting", agent_id=agent_id)
    try:
        await agent.connect()
        _append_trace("agent_connected", agent_id=agent_id)
    except Exception as exc:
        _append_trace("agent_connect_error", agent_id=agent_id, error=repr(exc))
        raise
