#!/usr/bin/env python3
"""Run the Piopiy voice agent worker."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice_sales_agent.piopiy_agent import run_piopiy_agent


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_piopiy_agent())


if __name__ == "__main__":
    main()

