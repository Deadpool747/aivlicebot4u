#!/usr/bin/env python3
"""Validate one or all client folders."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice_sales_agent.clients import list_client_ids, validate_client_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate client config bundles.")
    parser.add_argument("--client", help="Validate one client ID. If omitted, validates all clients.")
    args = parser.parse_args()

    client_ids = [args.client] if args.client else list_client_ids()
    has_errors = False

    for client_id in client_ids:
        issues = validate_client_bundle(client_id)
        if issues:
            has_errors = True
            print(f"{client_id}: INVALID")
            for issue in issues:
                print(f"  - {issue}")
        else:
            print(f"{client_id}: OK")

    sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main()
