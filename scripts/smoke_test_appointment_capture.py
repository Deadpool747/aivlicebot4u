"""Smoke test for appointment date/time capture from saved call artifacts.

Usage:
  python scripts/smoke_test_appointment_capture.py
  python scripts/smoke_test_appointment_capture.py --client ganpati_hospital
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice_sales_agent.analytics import _extract_appointment_snapshot, _resolve_lead_name, _resolve_provider_usage, _resolve_to_number


MARKER_PATTERN = re.compile(
    r"\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm|बजे|वाजता)|"
    r"(?:tomorrow|day after tomorrow|कल|परसों|परसो|उद्या|परवा|parwa|parva|udya|udhya)",
    re.IGNORECASE,
)


def _load_artifact(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _has_schedule_marker(transcript: list[dict]) -> bool:
    for turn in transcript:
        text = " ".join(str(turn.get("text") or "").split()).strip()
        if text and MARKER_PATTERN.search(text):
            return True
    return False


def run_smoke_test(root: Path, client_filter: str | None = None) -> int:
    artifact_paths = sorted(root.glob("*/*/artifacts.json"))
    total_sessions = 0
    marker_sessions = 0
    captured_sessions = 0
    critical_complete = 0
    marker_with_phone_source = 0
    critical_complete_with_phone_source = 0
    misses: list[tuple[str, str, str, str]] = []

    for artifact_path in artifact_paths:
        payload = _load_artifact(artifact_path)
        if not payload:
            continue
        if client_filter and str(payload.get("client_id") or "").strip() != client_filter:
            continue
        transcript = payload.get("transcript") or []
        total_sessions += 1
        if not _has_schedule_marker(transcript):
            continue
        marker_sessions += 1
        details, appointment_date, appointment_time = _extract_appointment_snapshot(payload)
        provider_usage = _resolve_provider_usage(payload)
        lead_name = _resolve_lead_name(payload, provider_usage)
        phone = _resolve_to_number(payload, provider_usage)
        has_phone_source = bool(phone and phone != "-")
        if has_phone_source:
            marker_with_phone_source += 1
        if (
            lead_name
            and lead_name != "Unknown"
            and phone
            and phone != "-"
            and appointment_date != "-"
            and appointment_time != "-"
        ):
            critical_complete += 1
            if has_phone_source:
                critical_complete_with_phone_source += 1
        if appointment_date != "-" or appointment_time != "-":
            captured_sessions += 1
            continue
        snippet = ""
        for turn in transcript[-6:]:
            text = " ".join(str(turn.get("text") or "").split()).strip()
            if text and MARKER_PATTERN.search(text):
                snippet = text
        misses.append(
            (
                str(payload.get("client_id") or "-"),
                str(payload.get("session_id") or artifact_path.parent.name),
                snippet or "-",
                details or "-",
            )
        )

    print(f"Total sessions scanned: {total_sessions}")
    print(f"Sessions with schedule markers: {marker_sessions}")
    print(f"Captured appointment date/time: {captured_sessions}")
    rate = (captured_sessions / marker_sessions * 100.0) if marker_sessions else 0.0
    print(f"Capture rate: {rate:.1f}%")
    critical_rate = (critical_complete / marker_sessions * 100.0) if marker_sessions else 0.0
    print(f"Critical fields complete (name+phone+date+time): {critical_complete}")
    print(f"Critical completeness rate: {critical_rate:.1f}%")
    print(f"Marker sessions with phone source: {marker_with_phone_source}")
    with_phone_rate = (
        critical_complete_with_phone_source / marker_with_phone_source * 100.0
        if marker_with_phone_source
        else 0.0
    )
    print(
        "Critical completeness rate (where phone source exists): "
        f"{with_phone_rate:.1f}% ({critical_complete_with_phone_source}/{marker_with_phone_source})"
    )
    if misses:
        print("\nMisses (up to 20):")
        for client_id, session_id, snippet, details in misses[:20]:
            print(f"- {client_id} / {session_id}")
            print(f"  marker_snippet: {snippet}")
            print(f"  extracted_details: {details}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test appointment slot extraction from session artifacts.")
    parser.add_argument("--sessions-root", default="sessions", help="Path to sessions root folder (default: sessions)")
    parser.add_argument("--client", default=None, help="Optional client_id filter")
    args = parser.parse_args()
    return run_smoke_test(Path(args.sessions_root), client_filter=args.client)


if __name__ == "__main__":
    raise SystemExit(main())
