#!/usr/bin/env python3
"""Run an isolated old-SDK Piopiy Gemini Live agent.

This runner is intentionally separate from the main app runtime. It loads an
older TeleCMI/Piopiy source tree from ``PIOPIY_OLD_SDK_SRC`` and starts the
Gemini Live sample using the older ``VoiceAgent.configure(...)`` API.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OLD_SDK_SRC = Path(
    os.getenv("PIOPIY_OLD_SDK_SRC", "/opt/telecmi_agents_oldtest_remote/src")
).resolve()

if str(OLD_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(OLD_SDK_SRC))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from piopiy.agent import Agent
from piopiy.services.google.gemini_live.llm import (  # type: ignore[import-not-found]
    GeminiLiveLLMService,
    GeminiModalities,
    InputParams,
)
from piopiy.voice_agent import VoiceAgent  # type: ignore[import-not-found]
from voice_sales_agent.clients import load_client


TRACE_FILE = Path(
    os.getenv("PIOPIY_OLD_SDK_TRACE_FILE", str(PROJECT_ROOT / "runtime" / "piopiy_old_sdk_trace.jsonl"))
)
DEFAULT_CLIENT_ID = "user_janjal_voicebot_12c92bbc"
DEFAULT_PROJECT_ID = "janjal_ward22_inbound_918065254654"


def _append_trace(event: str, **fields: Any) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    try:
        TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TRACE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logging.exception("Failed to append old SDK trace event %s", event)


def _resolve_call_context(from_number: str | None) -> dict[str, Any]:
    client_id = (os.getenv("PIOPIY_OLD_SDK_CLIENT_ID") or DEFAULT_CLIENT_ID).strip()
    project_id = (os.getenv("PIOPIY_OLD_SDK_PROJECT_ID") or DEFAULT_PROJECT_ID).strip()
    customer_name = (os.getenv("PIOPIY_OLD_SDK_CUSTOMER_NAME") or "नागरिक").strip()
    if from_number:
        customer_name = from_number.strip()

    client = load_client(client_id, project_id=project_id or None)
    opening_language = (
        os.getenv("PIOPIY_OLD_SDK_OPENING_LANGUAGE")
        or client.config.default_opening_language
        or "marathi"
    ).strip().lower()
    contact_details = {"phone": from_number.strip()} if from_number else {}
    system_instruction = _build_live_system_instruction(
        client=client,
        customer_name=customer_name,
        opening_language=opening_language,
        contact_details=contact_details,
    )
    greeting = (
        client.config.opening_script.get(opening_language)
        or client.config.opening_script.get("marathi")
        or client.config.opening_script.get("english")
        or "नमस्कार! मी कार्यालयातून बोलत आहे. आपल्याला कशी मदत करू शकतो?"
    ).strip()
    voice_id = (
        os.getenv("PIOPIY_OLD_SDK_VOICE_ID")
        or client.config.voice.voice_name
        or "Kore"
    ).strip()
    temperature = str(
        client.config.live_generation.temperature
        if client.config.live_generation.temperature is not None
        else os.getenv("PIOPIY_OLD_SDK_TEMPERATURE", "0.35").strip()
    )
    return {
        "client": client,
        "client_id": client_id,
        "project_id": project_id,
        "customer_name": customer_name,
        "opening_language": opening_language,
        "system_instruction": system_instruction,
        "greeting": greeting,
        "voice_id": voice_id,
        "temperature": float(str(temperature)),
    }


def _build_live_system_instruction(
    *,
    client: Any,
    customer_name: str,
    opening_language: str,
    contact_details: dict[str, str],
) -> str:
    project_instruction = (
        client.active_project.prompt_instruction.strip()
        if client.active_project and client.active_project.prompt_instruction
        else ""
    )
    lines = [
        f"You are {client.config.display_name}.",
        f"Primary role: {client.config.primary_offer}.",
        f"Opening language: {opening_language}.",
        "Stay in one language per reply. Default to Marathi unless the caller clearly switches.",
        "Keep replies short, natural, and phone-friendly.",
        "Respond quickly. Ask only one question at a time.",
        "Do not remain silent after the caller speaks.",
        "Acknowledge short replies immediately and continue.",
        "Never say you are from a real estate team.",
        f"Voice persona: {client.config.voice.persona_gender}.",
    ]
    if customer_name:
        lines.append(f"Caller reference: {customer_name}.")
    if contact_details:
        lines.append(f"Known contact details: {json.dumps(contact_details, ensure_ascii=False)}.")
    if client.config.disallowed_claims:
        lines.append("Do not make these claims: " + " | ".join(client.config.disallowed_claims))
    if project_instruction:
        lines.append("Project rules:")
        lines.append(project_instruction)
    return "\n".join(lines).strip()


async def create_session(
    agent_id: str | None = None,
    call_id: str | None = None,
    from_number: str | None = None,
    to_number: str | None = None,
    metadata: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    context = _resolve_call_context(from_number)
    _append_trace(
        "create_session_started",
        agent_id=agent_id,
        call_id=call_id,
        from_number=from_number,
        to_number=to_number,
        metadata_keys=sorted(metadata.keys()) if isinstance(metadata, dict) else [],
        client_id=context["client_id"],
        project_id=context["project_id"],
        opening_language=context["opening_language"],
        voice_id=context["voice_id"],
    )

    api_key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Missing GOOGLE_API_KEY / GEMINI_API_KEY.")

    gemini_live = GeminiLiveLLMService(
        api_key=api_key,
        model=os.getenv("PIOPIY_OLD_SDK_MODEL", "models/gemini-2.0-flash-exp").strip(),
        voice_id=context["voice_id"],
        start_audio_paused=False,
        inference_on_context_initialization=True,
        params=InputParams(
            modalities=GeminiModalities.AUDIO,
            temperature=context["temperature"],
        ),
    )

    voice_agent = VoiceAgent(
        instructions=context["system_instruction"],
        greeting=context["greeting"],
    )

    await voice_agent.configure(
        llm=gemini_live,
        allow_interruptions=True,
    )
    _append_trace("session_configured", call_id=call_id, mode="old_sdk_gemini_live_s2s")
    await voice_agent.start()
    _append_trace("session_finished", call_id=call_id)


async def amain() -> None:
    agent_id = (
        os.getenv("PIOPIY_OLD_SDK_AGENT_ID")
        or os.getenv("AGENT_ID")
        or ""
    ).strip()
    agent_token = (os.getenv("AGENT_TOKEN") or os.getenv("PIOPIY_API_TOKEN") or "").strip()
    if not agent_id:
        raise RuntimeError("Missing AGENT_ID.")
    if not agent_token:
        raise RuntimeError("Missing AGENT_TOKEN / PIOPIY_API_TOKEN.")

    _append_trace(
        "worker_boot",
        agent_id=agent_id,
        old_sdk_src=str(OLD_SDK_SRC),
        token_present=bool(agent_token),
    )

    agent = Agent(
        agent_id=agent_id,
        agent_token=agent_token,
        create_session=create_session,
        debug=True,
    )

    _append_trace("agent_connecting", agent_id=agent_id)
    try:
        await agent.connect()
        _append_trace("agent_connected", agent_id=agent_id)
    except Exception as exc:
        _append_trace("agent_connect_error", agent_id=agent_id, error=repr(exc))
        raise


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(amain())


if __name__ == "__main__":
    main()
