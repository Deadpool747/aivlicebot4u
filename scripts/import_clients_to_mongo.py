#!/usr/bin/env python3
"""Import local client folders into MySQL."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice_sales_agent.clients import (  # noqa: E402
    export_file_client_payload,
    list_client_ids,
    save_client_editor_payload,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import local client folders into MySQL.")
    parser.add_argument("--client", help="Import one client ID. If omitted, imports all local clients.")
    args = parser.parse_args()

    client_ids = [args.client] if args.client else list_client_ids(backend="file")
    for client_id in client_ids:
        payload = export_file_client_payload(client_id)
        save_client_editor_payload(client_id, payload, backend="mysql")
        print(f"Imported {client_id}")


if __name__ == "__main__":
    main()
