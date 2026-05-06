#!/usr/bin/env python3
"""Generate a full sample Marathi conversation flow for the current client."""

from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from google import genai

from voice_sales_agent.clients import load_client
from voice_sales_agent.config import load_settings


def main() -> None:
    settings = load_settings()
    client = load_client(settings.default_client_id)
    sdk = genai.Client(api_key=settings.gemini_api_key)

    prompt = f"""
Generate a realistic multi-turn sales call in Marathi between:
- AGENT: a sales representative for {client.config.display_name}
- USER: a prospect named Salman Shaik

Requirements:
- The agent must start the conversation first.
- The first question must ask for the prospect's name naturally.
- Keep the conversation fully in Marathi.
- Keep the flow natural and phone-like, not robotic.
- Cover greeting, name capture, role/company discovery, pain points, objection handling, value proposition, and a next step.
- Make the user an operations-focused clinic contact with scheduling pain points.
- Keep it to 10-14 turns total.
- Do not include meta commentary, stage directions, or analysis.
- Format strictly as alternating lines starting with "AGENT:" and "USER:".
""".strip()

    response = sdk.models.generate_content(model=settings.structured_model, contents=prompt)
    print(response.text or "")


if __name__ == "__main__":
    main()
