"""CLI entry point for the local Gemini voice sales demo."""

from __future__ import annotations

import argparse
import asyncio
import logging

from rich.console import Console

from .clients import list_client_ids
from .config import load_settings
from .session import VoiceSalesSession


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Gemini voice sales demo.")
    parser.add_argument("--client", help="Client ID to load", default=None)
    parser.add_argument(
        "--list-clients",
        action="store_true",
        help="List configured clients and exit.",
    )
    return parser


async def _main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    console = Console()

    if args.list_clients:
        for client_id in list_client_ids():
            console.print(client_id)
        return

    settings = load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    client_id = args.client or settings.default_client_id
    session = VoiceSalesSession(settings=settings, client_id=client_id, console=console)
    await session.run()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
