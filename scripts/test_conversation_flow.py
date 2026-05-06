#!/usr/bin/env python3
"""Run a scripted Gemini Live text conversation to validate call flow."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice_sales_agent.clients import load_client
from voice_sales_agent.config import load_settings
from voice_sales_agent.gemini_api import GeminiLiveVoiceClient
from voice_sales_agent.prompt_builder import PromptBuilder


SCRIPTED_USER_TURNS = [
    "हॅलो.",
    "माझं नाव सलमान शेख आहे.",
    "मी एका दोन-लोकेशन क्लिनिकचा ऑपरेशन्स मॅनेजर आहे.",
    "आमच्याकडे अपॉइंटमेंट शेड्युलिंग आणि पेशंट कॉल्समध्ये खूप मॅन्युअल काम होतं.",
    "फ्रंट डेस्क टीम वारंवार व्यस्त असते आणि कॉल्स मिस होतात.",
    "जर उपयोगी असेल तर छोटा डेमो बघायला मला हरकत नाही.",
]


async def collect_agent_text(live: GeminiLiveVoiceClient, timeout_seconds: float = 20.0) -> str:
    chunks: list[str] = []
    async def _collect() -> None:
        async for event in live.receive():
            if event.kind == "agent_text" and event.text:
                text = " ".join(event.text.split()).strip()
                if text and (not chunks or chunks[-1] != text):
                    chunks.append(text)
            if event.kind == "turn_complete":
                break

    await asyncio.wait_for(_collect(), timeout=timeout_seconds)
    return " ".join(chunks).strip()


async def main() -> None:
    settings = load_settings()
    client = load_client(settings.default_client_id)
    prompt = PromptBuilder().build(client)

    live = GeminiLiveVoiceClient(settings.gemini_api_key, settings.live_model)
    await live.connect(system_prompt=prompt, voice_name=client.config.voice.voice_name)
    try:
        await live.send_realtime_text(
            (
                "Start the sales call now in Marathi. Introduce yourself briefly, ask for the prospect's "
                "name first, and speak naturally without meta commentary. If a sample name is needed, "
                "use Salman Shaik."
            )
        )
        opening = await collect_agent_text(live)
        print(f"AGENT: {opening}")

        for turn in SCRIPTED_USER_TURNS:
            print(f"USER: {turn}")
            await live.send_realtime_text(turn)
            reply = await collect_agent_text(live)
            print(f"AGENT: {reply}")
    finally:
        await live.close()


if __name__ == "__main__":
    asyncio.run(main())
