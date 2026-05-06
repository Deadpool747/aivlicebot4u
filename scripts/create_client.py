#!/usr/bin/env python3
"""Create a new client scaffold with all required config and prompt files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice_sales_agent.constants import CLIENTS_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new client scaffold.")
    parser.add_argument("--client", required=True, help="Client ID, for example acme_health")
    args = parser.parse_args()

    client_id = args.client.strip().lower().replace("-", "_")
    client_dir = CLIENTS_DIR / client_id
    client_dir.mkdir(parents=True, exist_ok=False)

    files: dict[str, str] = {
        "config.json": json.dumps(
            {
                "client_id": client_id,
                "display_name": client_id.replace("_", " ").title(),
                "industry": "Replace me",
                "website": None,
                "primary_offer": "Replace me",
                "target_audience": ["Replace me"],
                "tone": ["helpful", "confident"],
                "disallowed_claims": ["Do not add claims until approved."],
                "allowed_contact_fields": ["name", "email", "phone"],
                "voice": {"voice_name": "Aoede", "speaking_rate": 1.0},
                "live_generation": {
                    "temperature": 0.8,
                    "top_p": 0.95,
                    "top_k": 40,
                    "max_output_tokens": 256
                },
                "structured_generation": {
                    "temperature": 0.2,
                    "top_p": 0.8,
                    "top_k": 20,
                    "max_output_tokens": 512
                },
            },
            indent=2,
        )
        + "\n",
        "system_prompt.txt": (
            "Describe how this sales agent should behave for the client.\n"
            "Include greeting style, discovery questions, qualification style, and objection handling tone.\n"
        ),
        "knowledge.md": (
            "# Product Summary\n\n"
            "Add the approved product/service facts here.\n\n"
            "# Proof Points\n\n"
            "Add approved outcomes, integrations, and differentiators.\n"
        ),
        "objections.json": json.dumps(
            {
                "common": [
                    {"objection": "We already have a solution.", "response": "Acknowledge and probe for gaps."},
                    {"objection": "Send me an email.", "response": "Offer a brief summary and confirm interest."},
                ]
            },
            indent=2,
        )
        + "\n",
        "qualification.json": json.dumps(
            {
                "fields": {
                    "role_fit": "Is the contact relevant to the buying process?",
                    "pain_urgency": "How important is the problem right now?",
                    "timeline": "Any timing or project deadline mentioned?",
                }
            },
            indent=2,
        )
        + "\n",
        "cta.json": json.dumps(
            {
                "primary_cta": "Book a demo",
                "fallback_cta": "Collect contact info for follow-up",
                "success_criteria": ["A clear next step is agreed."],
            },
            indent=2,
        )
        + "\n",
    }

    for filename, content in files.items():
        (client_dir / filename).write_text(content, encoding="utf-8")

    print(f"Created new client scaffold at {client_dir}")


if __name__ == "__main__":
    main()
