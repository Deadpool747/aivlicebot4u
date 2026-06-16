#!/usr/bin/env python3
"""Standalone Piopiy Gemini Live smoke test.

This script is intentionally isolated from the main app runtime. It helps us
verify what the installed Piopiy SDK actually supports for Gemini Live.

Modes:
  - ``shape``: validate the API surface with fake Agent room context.
               This proves whether llm-only Gemini Live can start without
               separate STT/TTS in the installed SDK version.
  - ``agent``: run a real Piopiy Agent process with the installed SDK.

Environment:
  AGENT_ID
  AGENT_TOKEN
  GOOGLE_API_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from dotenv import load_dotenv

from piopiy.agent import Agent, ROOM_CTX, TOKEN_CTX, URL_CTX
from piopiy.services.google.tts import GeminiTTSService
from piopiy.services.google.gemini_live.llm import (
    GeminiLiveLLMService,
    GeminiModalities,
    InputParams,
)
from piopiy.speech_agent import SpeechAgent
from piopiy.voice_agent import VoiceAgent

load_dotenv()


def build_gemini_live() -> GeminiLiveLLMService:
    api_key = (os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Missing GOOGLE_API_KEY.")
    return GeminiLiveLLMService(
        api_key=api_key,
        model=os.getenv(
            "VOICE_AGENT_MODEL",
            "models/gemini-2.5-flash-native-audio-preview-12-2025",
        ).strip(),
        system_instruction=(
            os.getenv(
                "VOICE_AGENT_INSTRUCTIONS",
                "You are a helpful and energetic voice assistant. "
                "Keep your responses concise and conversational.",
            ).strip()
        ),
        params=InputParams(
            modalities=GeminiModalities.AUDIO,
            temperature=float(os.getenv("VOICE_AGENT_TEMPERATURE", "0.7").strip() or "0.7"),
        ),
    )


def build_voice_agent() -> VoiceAgent:
    return VoiceAgent(
        instructions=os.getenv(
            "VOICE_AGENT_AGENT_INSTRUCTIONS",
            "You are a professional assistant.",
        ).strip(),
        greeting=os.getenv(
            "VOICE_AGENT_GREETING",
            "Hi there! This is Gemini Live. How can I help you today?",
        ).strip(),
    )


def build_speech_agent() -> SpeechAgent:
    return SpeechAgent(
        instructions=os.getenv(
            "VOICE_AGENT_AGENT_INSTRUCTIONS",
            "You are a professional assistant.",
        ).strip(),
        greeting=os.getenv(
            "VOICE_AGENT_GREETING",
            "Hi there! This is Gemini Live. How can I help you today?",
        ).strip(),
    )


def build_gemini_tts() -> GeminiTTSService:
    api_key = (os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Missing GOOGLE_API_KEY.")
    return GeminiTTSService(
        api_key=api_key,
        model=os.getenv("VOICE_AGENT_TTS_MODEL", "gemini-2.5-flash-tts").strip(),
        voice_id=os.getenv("VOICE_AGENT_TTS_VOICE", "Kore").strip(),
    )


async def create_session(
    agent_id: str | None = None,
    call_id: str | None = None,
    from_number: str | None = None,
    to_number: str | None = None,
    metadata: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    print(f"Incoming call: {from_number} -> {to_number}")
    print(f"Call ID: {call_id}")
    if agent_id:
        print(f"Agent ID: {agent_id}")
    if metadata:
        print(f"Metadata keys: {sorted(metadata.keys())}")

    gemini_live = build_gemini_live()
    voice_agent = build_voice_agent()

    # This is the real SDK API in Piopiy AI 0.6.1.
    await voice_agent.Action(
        llm=gemini_live,
        allow_interruptions=True,
    )

    print("Action configured. Starting voice agent...")
    await voice_agent.start()
    print(f"Session ended: {call_id}")


async def create_speech_session(
    agent_id: str | None = None,
    call_id: str | None = None,
    from_number: str | None = None,
    to_number: str | None = None,
    metadata: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    print(f"Incoming call: {from_number} -> {to_number}")
    print(f"Call ID: {call_id}")
    if agent_id:
        print(f"Agent ID: {agent_id}")
    if metadata:
        print(f"Metadata keys: {sorted(metadata.keys())}")

    speech_agent = build_speech_agent()
    gemini_live = build_gemini_live()
    gemini_tts = build_gemini_tts()

    await speech_agent.Action(
        omni=gemini_live,
        tts=gemini_tts,
        allow_interruptions=True,
    )

    print("SpeechAgent Action configured. Starting speech agent...")
    await speech_agent.start()
    print(f"Speech session ended: {call_id}")


async def run_shape_mode() -> None:
    print("Running SDK shape test with fake room context...")
    tok_url = URL_CTX.set("wss://example.invalid")
    tok_token = TOKEN_CTX.set("fake-token")
    tok_room = ROOM_CTX.set("fake-room")
    try:
        await create_session(
            agent_id="shape-test-agent",
            call_id="shape-test-call",
            from_number="100",
            to_number="200",
        )
    finally:
        ROOM_CTX.reset(tok_room)
        TOKEN_CTX.reset(tok_token)
        URL_CTX.reset(tok_url)


async def run_speech_shape_mode() -> None:
    print("Running SpeechAgent SDK shape test with fake room context...")
    tok_url = URL_CTX.set("wss://example.invalid")
    tok_token = TOKEN_CTX.set("fake-token")
    tok_room = ROOM_CTX.set("fake-room")
    try:
        await create_speech_session(
            agent_id="speech-shape-test-agent",
            call_id="speech-shape-test-call",
            from_number="100",
            to_number="200",
        )
    finally:
        ROOM_CTX.reset(tok_room)
        TOKEN_CTX.reset(tok_token)
        URL_CTX.reset(tok_url)


async def run_agent_mode() -> None:
    agent_id = (os.getenv("AGENT_ID") or "").strip()
    agent_token = (os.getenv("AGENT_TOKEN") or "").strip()
    missing = [
        name
        for name, value in (
            ("AGENT_ID", agent_id),
            ("AGENT_TOKEN", agent_token),
            ("GOOGLE_API_KEY", (os.getenv("GOOGLE_API_KEY") or "").strip()),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    agent = Agent(
        agent_id=agent_id,
        agent_token=agent_token,
        create_session=create_session,
        debug=True,
    )
    print("Starting real Piopiy Agent connection...")
    await agent.connect()


async def run_speech_agent_mode() -> None:
    agent_id = (os.getenv("AGENT_ID") or "").strip()
    agent_token = (os.getenv("AGENT_TOKEN") or "").strip()
    missing = [
        name
        for name, value in (
            ("AGENT_ID", agent_id),
            ("AGENT_TOKEN", agent_token),
            ("GOOGLE_API_KEY", (os.getenv("GOOGLE_API_KEY") or "").strip()),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    agent = Agent(
        agent_id=agent_id,
        agent_token=agent_token,
        create_session=create_speech_session,
        debug=True,
    )
    print("Starting real Piopiy SpeechAgent connection...")
    await agent.connect()


async def amain(mode: str) -> None:
    if mode == "shape":
        await run_shape_mode()
        return
    if mode == "speech-shape":
        await run_speech_shape_mode()
        return
    if mode == "agent":
        await run_agent_mode()
        return
    if mode == "speech-agent":
        await run_speech_agent_mode()
        return
    raise RuntimeError(f"Unsupported mode: {mode}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone Piopiy Gemini Live smoke test")
    parser.add_argument(
        "--mode",
        choices=("shape", "speech-shape", "agent", "speech-agent"),
        default="shape",
        help="Use 'shape' or 'speech-shape' for isolated SDK validation, or 'agent' / 'speech-agent' for a real Piopiy agent run.",
    )
    args = parser.parse_args()
    asyncio.run(amain(args.mode))


if __name__ == "__main__":
    main()
