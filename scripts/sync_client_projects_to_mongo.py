#!/usr/bin/env python3
"""Sync project definitions from local client folders into MySQL client records."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice_sales_agent.clients import (  # noqa: E402
    get_client_editor_payload,
    list_client_ids,
    save_client_editor_payload,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync local client project definitions into MySQL.")
    parser.add_argument("--client", help="Sync one client ID. If omitted, syncs all local file-backed clients.")
    args = parser.parse_args()

    client_ids = [args.client] if args.client else list_client_ids(backend="file")
    for client_id in client_ids:
        file_payload = get_client_editor_payload(client_id, backend="file")
        try:
            mysql_payload = get_client_editor_payload(client_id, backend="mysql")
        except FileNotFoundError:
            mysql_payload = file_payload
        mysql_payload["projects"] = file_payload.get("projects") or []
        if file_payload.get("active_project_id"):
            mysql_payload["active_project_id"] = file_payload["active_project_id"]
        save_client_editor_payload(client_id, mysql_payload, backend="mysql")
        print(f"Synced projects for {client_id}")


if __name__ == "__main__":
    main()
