"""Backfill call outcome SQLite DB from saved session artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice_sales_agent.call_outcomes_store import SqliteCallOutcomeStore
from voice_sales_agent.models import SessionArtifacts


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill call outcomes DB from sessions/*/*/artifacts.json")
    parser.add_argument("--sessions-root", default="sessions", help="Sessions root directory")
    parser.add_argument("--db-path", default="sessions/call_outcomes.db", help="SQLite DB path")
    parser.add_argument("--client", default=None, help="Optional client_id filter")
    args = parser.parse_args()

    sessions_root = Path(args.sessions_root)
    store = SqliteCallOutcomeStore(Path(args.db_path))
    artifact_paths = sorted(sessions_root.glob("*/*/artifacts.json"))
    processed = 0
    skipped = 0
    failed = 0

    for artifact_path in artifact_paths:
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            if args.client and str(payload.get("client_id") or "").strip() != args.client:
                skipped += 1
                continue
            artifacts = SessionArtifacts.model_validate(payload)
            store.upsert_outcome(artifacts, payload.get("telephony_context") if isinstance(payload, dict) else None)
            processed += 1
        except Exception:
            failed += 1

    print(f"Processed: {processed}")
    print(f"Skipped: {skipped}")
    print(f"Failed: {failed}")
    print(f"DB: {Path(args.db_path).resolve()}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

