"""Lightweight lead intake and call analytics for the dashboard."""

from __future__ import annotations

import json
import re
import contextlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .constants import PROJECT_ROOT

LEAD_STORE_PATH = PROJECT_ROOT / "logs" / "lead_pipeline.json"
DEFAULT_RESULTS_BASE_TEMPLATE = "default_v1"
IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_RESULTS_COLUMNS: list[tuple[str, str]] = [
    ("lead_name", "Lead"),
    ("to_number", "Phone"),
    ("appointment_details", "Appointment Details"),
    ("appointment_date", "Appt Date"),
    ("appointment_time", "Appt Time"),
    ("important_questions_asked", "Questions Asked"),
    ("gemini_status", "Gemini Status"),
    ("call_date", "Call Date"),
    ("call_time", "Call Time"),
    ("result", "Result"),
    ("duration_seconds", "Duration"),
    ("follow_up", "Follow-Up"),
]
LEGACY_PROVIDER_HINTS = ("tata", "sarvam", "savaram", "telecmi")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_phone(value: str) -> str:
    return "".join(ch for ch in str(value or "").strip() if ch in "+0123456789")


def _normalize_name(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_candidate_lead_name(value: str) -> str:
    candidate = _normalize_name(value).strip(" .,!?:;")
    if not candidate:
        return ""
    lowered = candidate.lower()
    if len(lowered) < 3:
        return ""
    if len(candidate.split()) > 4:
        return ""
    if lowered in {"unknown", "unknown lead", "inbound caller", "caller", "prospect", "customer"}:
        return ""
    if lowered in {"yes", "yeah", "yep", "no", "ok", "okay", "haan", "han", "ha", "ho", "ji", "jee", "speaking"}:
        return ""
    if any(
        token in lowered
        for token in (
            "hospital",
            "helpdesk",
            "helpline",
            "clinic",
            "caller",
            "agent",
            "inbound",
            "outbound",
            "स्वामी",
            "हॉस्पिटल",
            "हेल्पडेस्क",
        )
    ):
        return ""
    if re.search(r"\d", candidate):
        return ""
    return candidate


def _read_store() -> dict[str, Any]:
    LEAD_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LEAD_STORE_PATH.exists():
        return {"leads": []}
    try:
        return json.loads(LEAD_STORE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"leads": []}


def _write_store(payload: dict[str, Any]) -> None:
    LEAD_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEAD_STORE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def record_leads(
    *,
    client_id: str,
    source: str,
    leads: list[dict[str, str]],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = _read_store()
    existing = store.setdefault("leads", [])
    now = _utc_now()
    created = 0
    updated = 0

    for lead in leads:
        customer_name = _normalize_name(lead.get("customer_name", ""))
        to_number = _normalize_phone(lead.get("to_number", ""))
        if not to_number:
            continue
        lead_key = f"{client_id}:{source}:{to_number}:{customer_name.lower() or 'unknown'}"
        found = next((item for item in existing if item.get("lead_key") == lead_key), None)
        payload = {
            "lead_key": lead_key,
            "client_id": client_id,
            "source": source,
            "customer_name": customer_name or "Unknown Lead",
            "to_number": to_number,
            "first_seen_at": now,
            "last_seen_at": now,
            "metadata": metadata or {},
        }
        if found is None:
            existing.append(payload)
            created += 1
        else:
            found.update(
                {
                    "client_id": client_id,
                    "customer_name": customer_name or found.get("customer_name") or "Unknown Lead",
                    "to_number": to_number,
                    "last_seen_at": now,
                    "metadata": {**found.get("metadata", {}), **(metadata or {})},
                }
            )
            updated += 1

    _write_store(store)
    return {"created": created, "updated": updated, "total": len(existing)}


def _load_artifact(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _session_result(session: dict[str, Any]) -> str:
    transcript = session.get("transcript") or []
    user_turns = [turn for turn in transcript if turn.get("speaker") == "user" and turn.get("text")]
    errors = session.get("errors") or []
    error_texts = [str(item or "").strip() for item in errors if str(item or "").strip()]
    non_blocking_prefixes = (
        "Recording details extraction failed:",
        "Recording details extraction timed out",
        "Post-call summary generation timed out",
        "Post-call summary generation failed:",
    )
    has_blocking_errors = any(not msg.startswith(non_blocking_prefixes) for msg in error_texts)

    provider_usage = _resolve_provider_usage(session)
    telephony_context = session.get("telephony_context") or {}
    metadata = telephony_context.get("metadata") if isinstance(telephony_context.get("metadata"), dict) else {}
    status = str(
        provider_usage.get("call_status")
        or provider_usage.get("stream_status")
        or telephony_context.get("call_status")
        or telephony_context.get("stream_status")
        or metadata.get("call_status")
        or metadata.get("stream_status")
        or ""
    ).strip().lower()
    answered_statuses = {
        "answered",
        "in-progress",
        "in_progress",
        "connected",
        "completed",
        "success",
    }
    not_answered_statuses = {
        "not_answered",
        "unanswered",
        "no-answer",
        "no_answer",
        "busy",
        "failed",
        "canceled",
        "cancelled",
    }

    if has_blocking_errors:
        return "unsuccessful"
    if user_turns:
        return "successful"
    if status in not_answered_statuses:
        return "unsuccessful"
    if status in answered_statuses:
        return "successful"
    if _coerce_duration(session) >= 20.0:
        return "successful"
    return "unsuccessful"


def _coerce_duration(session: dict[str, Any]) -> float:
    actual_cost = session.get("actual_cost") or {}
    provider_usage = actual_cost.get("raw_provider_usage") or {}
    duration = provider_usage.get("stream_duration_seconds")
    if duration not in (None, ""):
        try:
            parsed = float(duration)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
    metrics = session.get("metrics") or {}
    try:
        parsed = float(metrics.get("caller_audio_seconds") or 0.0)
        if parsed > 0:
            return parsed
    except (TypeError, ValueError):
        parsed = 0.0
    started_at = _parse_iso_datetime(str(session.get("started_at") or ""))
    ended_at = _parse_iso_datetime(str(session.get("ended_at") or ""))
    if started_at and ended_at and ended_at >= started_at:
        return max((ended_at - started_at).total_seconds(), 0.0)
    telephony_context = session.get("telephony_context") or {}
    for candidate in (
        telephony_context.get("cdr_duration_seconds"),
        telephony_context.get("duration_seconds"),
        telephony_context.get("call_duration_seconds"),
    ):
        try:
            parsed = float(candidate)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            continue
    return 0.0


def _resolve_provider_usage(session: dict[str, Any]) -> dict[str, Any]:
    actual_cost = session.get("actual_cost") or {}
    from_actual = actual_cost.get("raw_provider_usage") or {}
    telephony_context = session.get("telephony_context") or {}
    from_context = telephony_context.get("raw_provider_usage") or {}
    merged: dict[str, Any] = {}
    if isinstance(from_context, dict):
        merged.update(from_context)
    if isinstance(from_actual, dict):
        merged.update(from_actual)
    return merged


def _resolve_lead_name(session: dict[str, Any], provider_usage: dict[str, Any]) -> str:
    memory = session.get("memory") or {}
    summary = session.get("summary") or {}
    recording_details = session.get("recording_llm_details") or {}
    telephony_context = session.get("telephony_context") or {}
    transcript = session.get("transcript") or []
    direction = _resolve_call_direction(session, provider_usage)
    recording_name = _normalize_candidate_lead_name(str(recording_details.get("name") or ""))
    if recording_name:
        return recording_name
    summary_name = _normalize_candidate_lead_name(str(summary.get("lead_name") or ""))
    memory_name = _normalize_candidate_lead_name(str(memory.get("lead_name") or ""))
    if summary_name:
        return summary_name
    if memory_name:
        return memory_name

    parallel_stt_text = " ".join(str(session.get("parallel_stt_transcript") or "").split()).strip()
    if parallel_stt_text:
        for pattern in (
            re.compile(r"\b(?:my name is|i am|i'm|this is)\s+([A-Za-z][A-Za-z' -]{1,50})\b", re.IGNORECASE),
            re.compile(r"(?:मेरा नाम|माझं नाव|माझे नाव)\s+([^\d.,!?]{2,50})", re.IGNORECASE),
        ):
            match = pattern.search(parallel_stt_text)
            if not match:
                continue
            candidate = _normalize_candidate_lead_name(match.group(1))
            if candidate:
                return candidate

    for turn in transcript:
        if str(turn.get("speaker") or "").lower() != "user":
            continue
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        for pattern in (
            re.compile(r"\b(?:my name is|i am|i'm|this is)\s+([A-Za-z][A-Za-z' -]{1,50})\b", re.IGNORECASE),
            re.compile(r"(?:मेरा नाम|माझं नाव|माझे नाव)\s+([^\d.,!?]{2,50})", re.IGNORECASE),
        ):
            match = pattern.search(text)
            if not match:
                continue
            candidate = _normalize_candidate_lead_name(match.group(1))
            if candidate:
                return candidate

    # If the agent just asked for identity, treat a short user reply as possible name.
    prompt_markers = (
        "your name",
        "who am i speaking",
        "आपका नाम",
        "तुमचं नाव",
        "तुमचे नाव",
        "कोणाशी बोलत",
        "किससे बात",
    )
    stop_words = {
        "yes",
        "yeah",
        "yep",
        "no",
        "ok",
        "okay",
        "haan",
        "han",
        "ha",
        "ho",
        "ji",
        "jee",
        "nahi",
        "नाही",
        "हो",
        "हाँ",
        "हां",
        "जी",
    }
    for index, turn in enumerate(transcript):
        if str(turn.get("speaker") or "").lower() != "agent":
            continue
        agent_text = " ".join(str(turn.get("text") or "").split()).strip().lower()
        if not agent_text or not any(marker in agent_text for marker in prompt_markers):
            continue
        if index + 1 >= len(transcript):
            continue
        next_turn = transcript[index + 1]
        if str(next_turn.get("speaker") or "").lower() != "user":
            continue
        user_text = " ".join(str(next_turn.get("text") or "").split()).strip()
        if not user_text:
            continue
        lowered_user = user_text.lower()
        if lowered_user in stop_words:
            continue
        if len(lowered_user.split()) > 4:
            continue
        candidate = _normalize_candidate_lead_name(user_text)
        if candidate:
            return candidate

    fallback = _normalize_candidate_lead_name(
        str(provider_usage.get("customer_name") or telephony_context.get("customer_name") or "")
    )
    if fallback:
        return fallback
    caller_number = _resolve_from_number(session, provider_usage)
    if caller_number and caller_number != "-":
        return "Inbound Caller"
    return "Unknown"


def _resolve_call_direction(session: dict[str, Any], provider_usage: dict[str, Any]) -> str:
    telephony_context = session.get("telephony_context") or {}
    metadata = telephony_context.get("metadata") if isinstance(telephony_context.get("metadata"), dict) else {}
    direction = str(
        provider_usage.get("direction")
        or telephony_context.get("direction")
        or telephony_context.get("call_direction")
        or metadata.get("direction")
        or metadata.get("call_direction")
        or ""
    ).strip().lower()
    if direction in {"inbound", "outbound"}:
        return direction
    has_from = any(
        _normalize_phone(str(value or ""))
        for value in (
            provider_usage.get("from_number"),
            telephony_context.get("from_number"),
            telephony_context.get("from"),
            telephony_context.get("fromNumber"),
            telephony_context.get("customer_no_with_prefix"),
            telephony_context.get("customer_number_with_prefix"),
            metadata.get("from_number"),
            metadata.get("customer_no_with_prefix"),
            metadata.get("customer_number_with_prefix"),
        )
    )
    has_to = any(
        _normalize_phone(str(value or ""))
        for value in (
            provider_usage.get("to_number"),
            telephony_context.get("to_number"),
            telephony_context.get("to"),
            telephony_context.get("toNumber"),
            metadata.get("to_number"),
            metadata.get("to"),
            metadata.get("toNumber"),
        )
    )
    if has_from and has_to:
        return "inbound"
    if has_to:
        return "outbound"
    return "unknown"


def _normalize_provider_name(provider: str) -> str:
    return str(provider or "").strip().lower()


def _normalize_source_name(source: str) -> str:
    return str(source or "").strip().lower()


def _is_legacy_provider(provider: str) -> bool:
    normalized = _normalize_provider_name(provider)
    return any(hint in normalized for hint in LEGACY_PROVIDER_HINTS)


def _resolve_provider(session: dict[str, Any], provider_usage: dict[str, Any]) -> str:
    actual_cost = session.get("actual_cost") or {}
    telephony_context = session.get("telephony_context") or {}
    metadata = telephony_context.get("metadata") if isinstance(telephony_context.get("metadata"), dict) else {}
    provider = _normalize_provider_name(
        str(
        actual_cost.get("telephony_provider")
        or provider_usage.get("provider")
        or telephony_context.get("provider")
        or metadata.get("provider")
        or ""
        ).strip().lower()
    )
    if provider:
        return provider
    if telephony_context.get("provider") in {"local", "browser"}:
        return str(telephony_context.get("provider"))
    return "piopiy"


def _resolve_source(provider: str, direction: str, provider_usage: dict[str, Any]) -> str:
    explicit = str(provider_usage.get("lead_source") or "").strip()
    if explicit:
        return _normalize_source_name(explicit)
    provider = _normalize_provider_name(provider)
    if provider == "piopiy":
        return "piopiy_inbound_stream" if direction == "inbound" else "piopiy_outbound"
    return "unknown"


def _resolve_to_number(session: dict[str, Any], provider_usage: dict[str, Any]) -> str:
    memory = session.get("memory") or {}
    summary = session.get("summary") or {}
    telephony_context = session.get("telephony_context") or {}
    metadata = telephony_context.get("metadata") if isinstance(telephony_context.get("metadata"), dict) else {}
    direction = _resolve_call_direction(session, provider_usage)
    inbound_candidates = [
        provider_usage.get("from_number"),
        telephony_context.get("from_number"),
        provider_usage.get("customer_no_with_prefix"),
        provider_usage.get("customer_number_with_prefix"),
        provider_usage.get("caller_id_number"),
        telephony_context.get("caller_id_number"),
        telephony_context.get("caller_id"),
        provider_usage.get("caller_id"),
        telephony_context.get("customer_no_with_prefix"),
        telephony_context.get("customer_number_with_prefix"),
        telephony_context.get("from"),
        telephony_context.get("fromNumber"),
        telephony_context.get("customer_number"),
        telephony_context.get("customer_no"),
        telephony_context.get("caller_number"),
        telephony_context.get("phone"),
        telephony_context.get("mobile"),
        metadata.get("from_number"),
        metadata.get("caller_id_number"),
        metadata.get("customer_no_with_prefix"),
        metadata.get("customer_number_with_prefix"),
        metadata.get("customer_number"),
        metadata.get("customer_no"),
        metadata.get("caller_number"),
        metadata.get("phone"),
        metadata.get("mobile"),
        provider_usage.get("to_number"),
        telephony_context.get("to_number"),
    ]
    outbound_candidates = [
        provider_usage.get("to_number"),
        telephony_context.get("to_number"),
        provider_usage.get("from_number"),
        telephony_context.get("from_number"),
    ]
    candidates = (inbound_candidates if direction == "inbound" else outbound_candidates) + [
        provider_usage.get("customer_number"),
        provider_usage.get("customer_no"),
        provider_usage.get("caller_number"),
        provider_usage.get("phone"),
        provider_usage.get("mobile"),
        telephony_context.get("to"),
        telephony_context.get("toNumber"),
        metadata.get("to_number"),
        metadata.get("to"),
        metadata.get("toNumber"),
        (memory.get("contact_details") or {}).get("phone"),
        (summary.get("contact_details") or {}).get("phone"),
    ]
    for candidate in candidates:
        normalized = _normalize_phone(str(candidate or ""))
        if normalized:
            return normalized
    for blob in (
        provider_usage.get("status_callback_payload"),
        provider_usage.get("stream_endpoint_payload"),
        telephony_context.get("status_callback_payload"),
        telephony_context.get("stream_endpoint_payload"),
        metadata.get("status_callback_payload"),
        metadata.get("stream_endpoint_payload"),
    ):
        normalized = _extract_phone_from_payload_blob(blob, direction=direction)
        if normalized:
            return normalized
    return "-"


def _resolve_from_number(session: dict[str, Any], provider_usage: dict[str, Any]) -> str:
    telephony_context = session.get("telephony_context") or {}
    metadata = telephony_context.get("metadata") if isinstance(telephony_context.get("metadata"), dict) else {}
    candidates = [
        provider_usage.get("from_number"),
        telephony_context.get("from_number"),
        provider_usage.get("caller_id_number"),
        telephony_context.get("caller_id_number"),
        telephony_context.get("caller_id"),
        provider_usage.get("caller_id"),
        telephony_context.get("caller_number"),
        telephony_context.get("from"),
        telephony_context.get("fromNumber"),
        metadata.get("from_number"),
        metadata.get("caller_id_number"),
        metadata.get("caller_number"),
        provider_usage.get("customer_number"),
        provider_usage.get("customer_no"),
    ]
    for candidate in candidates:
        normalized = _normalize_phone(str(candidate or ""))
        if normalized:
            return normalized
    return "-"


def _parse_iso_datetime(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _extract_phone_from_payload_blob(blob: Any, *, direction: str) -> str:
    payload: dict[str, Any] = {}
    if isinstance(blob, dict):
        payload = blob
    elif isinstance(blob, str):
        raw = blob.strip()
        if not raw:
            return ""
        with contextlib.suppress(Exception):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
    if not payload:
        return ""
    inbound_keys = (
        "customer_no_with_prefix",
        "customer_number_with_prefix",
        "caller_id_number",
        "from_number",
        "fromNumber",
        "from",
        "customer_number",
        "customer_no",
    )
    outbound_keys = (
        "to_number",
        "toNumber",
        "to",
        "call_to_number",
        "answered_agent_number",
        "destination_number",
    )
    keys = inbound_keys if direction == "inbound" else outbound_keys
    for key in keys:
        normalized = _normalize_phone(str(payload.get(key) or ""))
        if normalized:
            return normalized
    return ""


def _extract_call_date_time(started_at: str) -> tuple[str, str]:
    parsed = _parse_iso_datetime(started_at)
    if parsed is None:
        return "-", "-"
    ist_time = parsed.astimezone(IST)
    return parsed.strftime("%Y-%m-%d"), ist_time.strftime("%H:%M:%S IST")


def _dedupe_sentences(text: str) -> str:
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        return ""
    chunks = [
        part.strip()
        for part in re.split(r"(?<=[.!?।])\s+|\s*\|\s*|\n+", normalized)
        if part.strip()
    ]
    kept: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        key = chunk.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(chunk)
    return " | ".join(kept) if kept else normalized


def _looks_like_appointment_detail(text: str) -> bool:
    lowered = str(text or "").lower()
    if not lowered:
        return False
    markers = (
        "appointment",
        "slot",
        "schedule",
        "scheduled",
        "callback",
        "meeting",
        "visit",
        "booked",
        "reschedule",
        "tomorrow",
        "day after",
        "कल",
        "परसों",
        "उद्या",
        "परवा",
        "अपॉइंटमेंट",
        "कॉलबॅक",
        "मीटिंग",
    )
    if any(marker in lowered for marker in markers):
        return True
    return bool(
        re.search(
            r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\b|\b\d{1,2}(:\d{2})?\s*(am|pm)\b",
            lowered,
            flags=re.IGNORECASE,
        )
    )


def _trim_for_table(text: str, limit: int = 220) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _to_24h_time(hour: int, minute: int, meridiem: str | None) -> str:
    if meridiem:
        marker = meridiem.upper()
        if marker == "AM":
            hour = 0 if hour == 12 else hour
        elif marker == "PM":
            hour = 12 if hour == 12 else hour + 12
    return f"{hour:02d}:{minute:02d}"


def _extract_date_from_text(text: str, started_at: str) -> str:
    value = str(text or "")
    if not value:
        return "-"
    explicit_iso = re.search(r"\b\d{4}-\d{2}-\d{2}\b", value)
    if explicit_iso:
        return explicit_iso.group(0)
    explicit_short = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", value)
    if explicit_short:
        day = int(explicit_short.group(1))
        month = int(explicit_short.group(2))
        year = int(explicit_short.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day, tzinfo=timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            pass
    explicit_named = re.search(r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\b", value)
    if explicit_named:
        with contextlib.suppress(ValueError):
            parsed = datetime.strptime(explicit_named.group(1), "%d %b %Y").replace(tzinfo=timezone.utc)
            return parsed.strftime("%Y-%m-%d")

    started = _parse_iso_datetime(started_at)
    if started is None:
        return "-"
    lowered = value.lower()
    if any(token in lowered for token in ("day after tomorrow", "परसों", "परसो", "परवा", "parwa", "parva")):
        return (started + timedelta(days=2)).strftime("%Y-%m-%d")
    if any(token in lowered for token in ("tomorrow", "कल", "उद्या", "udya", "udhya")):
        return (started + timedelta(days=1)).strftime("%Y-%m-%d")
    if any(token in lowered for token in ("today", "आज", "aaj")):
        return started.strftime("%Y-%m-%d")
    weekday_map = {
        "monday": 0,
        "सोमवार": 0,
        "सोमवारी": 0,
        "somvar": 0,
        "tuesday": 1,
        "मंगळवार": 1,
        "मंगलवार": 1,
        "मंगळवारी": 1,
        "mangalvar": 1,
        "wednesday": 2,
        "बुधवार": 2,
        "बुधवारी": 2,
        "budhvar": 2,
        "thursday": 3,
        "गुरुवार": 3,
        "गुरुवारी": 3,
        "guruvar": 3,
        "friday": 4,
        "शुक्रवार": 4,
        "शुक्रवारी": 4,
        "shukrvar": 4,
        "saturday": 5,
        "शनिवार": 5,
        "शनिवारी": 5,
        "shanivar": 5,
        "sunday": 6,
        "रविवार": 6,
        "रविवारी": 6,
        "ravivar": 6,
    }
    for token, target_weekday in weekday_map.items():
        if token in lowered:
            days_ahead = (target_weekday - started.weekday()) % 7
            return (started + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    return "-"


def _extract_time_from_text(text: str) -> str:
    value = str(text or "")
    if not value:
        return "-"
    meridiem_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b", value, flags=re.IGNORECASE)
    if meridiem_match:
        hour = int(meridiem_match.group(1))
        minute = int(meridiem_match.group(2) or "0")
        return _to_24h_time(hour, minute, meridiem_match.group(3))
    local_time = re.search(r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(बजे|वाजता)", value)
    if local_time:
        hour = int(local_time.group(1))
        minute = int(local_time.group(2) or "0")
        lowered = value.lower()
        if any(token in lowered for token in ("evening", "शाम", "सायंकाळ", "pm")) and 1 <= hour <= 11:
            hour += 12
        return _to_24h_time(hour, minute, None)
    twenty_four = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", value)
    if twenty_four:
        return f"{int(twenty_four.group(1)):02d}:{int(twenty_four.group(2)):02d}"
    return "-"


def _extract_slot_from_transcript(session: dict[str, Any]) -> tuple[str, str]:
    transcript = list(session.get("transcript") or [])
    parallel_stt_text = " ".join(str(session.get("parallel_stt_transcript") or "").split()).strip()
    if parallel_stt_text:
        transcript.append({"speaker": "user", "text": parallel_stt_text})
    started_at = str(session.get("started_at") or "")
    appointment_date = "-"
    appointment_time = "-"
    for turn in reversed(transcript):
        if not isinstance(turn, dict):
            continue
        text = " ".join(str(turn.get("text") or "").split()).strip()
        if not text:
            continue
        if appointment_date == "-":
            date_value = _extract_date_from_text(text, started_at)
            if date_value != "-":
                appointment_date = date_value
        if appointment_time == "-":
            time_value = _extract_time_from_text(text)
            if time_value != "-":
                appointment_time = time_value
        if appointment_date != "-" and appointment_time != "-":
            return appointment_date, appointment_time

    if appointment_date == "-" or appointment_time == "-":
        combined_tail = " | ".join(
            " ".join(str(turn.get("text") or "").split()).strip()
            for turn in transcript[-10:]
            if isinstance(turn, dict) and str(turn.get("text") or "").strip()
        )
        if combined_tail:
            if appointment_date == "-":
                appointment_date = _extract_date_from_text(combined_tail, started_at)
            if appointment_time == "-":
                appointment_time = _extract_time_from_text(combined_tail)
    return appointment_date, appointment_time


def _extract_appointment_snapshot(session: dict[str, Any]) -> tuple[str, str, str]:
    memory = session.get("memory") or {}
    summary = session.get("summary") or {}
    recording_details = session.get("recording_llm_details") or {}
    llm_date = " ".join(str(recording_details.get("appointment_date") or "").split()).strip()
    llm_time = " ".join(str(recording_details.get("appointment_time") or "").split()).strip()
    candidate_details = [
        str(memory.get("appointment_details") or "").strip(),
        str(memory.get("next_step") or "").strip(),
        str(summary.get("suggested_next_action") or "").strip(),
        str(summary.get("summary") or "").strip(),
        str(session.get("parallel_stt_transcript") or "").strip(),
        str(session.get("recording_stt_full_corpus") or "").strip(),
        str(session.get("recording_stt_corpus") or "").strip(),
    ]
    candidate_details = [item for item in candidate_details if item]
    deduped_candidates: list[str] = []
    seen: set[str] = set()
    for item in candidate_details:
        cleaned = _dedupe_sentences(item)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped_candidates.append(cleaned)

    details = next((item for item in deduped_candidates if _looks_like_appointment_detail(item)), "")
    if not details and deduped_candidates:
        details = deduped_candidates[0]
    if not details and (llm_date or llm_time):
        if llm_date and llm_time:
            details = f"Appointment noted for {llm_date} at {llm_time}"
        elif llm_date:
            details = f"Appointment date noted: {llm_date}"
        elif llm_time:
            details = f"Appointment time noted: {llm_time}"
    details = _trim_for_table(details)

    appointment_date = llm_date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", llm_date) else _extract_date_from_text(
        details, str(session.get("started_at") or "")
    )
    appointment_time = (
        f"{int(llm_time.split(':', 1)[0]):02d}:{llm_time.split(':', 1)[1]}"
        if re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", llm_time)
        else _extract_time_from_text(details)
    )
    if appointment_date == "-" or appointment_time == "-":
        transcript_date, transcript_time = _extract_slot_from_transcript(session)
        if appointment_date == "-":
            appointment_date = transcript_date
        if appointment_time == "-":
            appointment_time = transcript_time
    return (details or "-", appointment_date, appointment_time)


def _extract_important_questions(session: dict[str, Any]) -> str:
    recording_details = session.get("recording_llm_details") or {}
    raw_questions = recording_details.get("important_questions_asked")
    if not isinstance(raw_questions, list):
        return "-"
    cleaned = [
        " ".join(str(item or "").split()).strip()
        for item in raw_questions
        if " ".join(str(item or "").split()).strip()
    ]
    cleaned = [item for item in cleaned if len(item) >= 6 and len(item.split()) >= 2]
    if not cleaned:
        return "-"
    deduped: list[str] = []
    seen: set[str] = set()
    for question in cleaned:
        key = question.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(question)
    return " | ".join(deduped[:5]) if deduped else "-"


def _extract_gemini_status(session: dict[str, Any]) -> str:
    metrics = session.get("metrics") or {}
    if float(metrics.get("recording_llm_available") or 0.0) >= 1.0:
        return "working"

    errors = [str(item or "").strip() for item in (session.get("errors") or []) if str(item or "").strip()]
    error_blob = " | ".join(errors).lower()
    if any(
        marker in error_blob
        for marker in (
            "gemini structured extraction unavailable",
            "recording details extraction failed",
            "recording details extraction timed out",
            "503",
            "unavailable",
            "resource_exhausted",
            "deadline_exceeded",
        )
    ):
        return "not_working"
    return "-"


def _normalize_extra_fields(extra_fields: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for field in extra_fields or []:
        key = str(field or "").strip()
        if not key:
            continue
        if key in normalized:
            continue
        normalized.append(key)
    return normalized[:20]


def _column_label(field_key: str) -> str:
    compact = str(field_key or "").strip()
    if not compact:
        return "Field"
    return compact.replace("_", " ").replace(".", " ").title()


def _resolve_path_value(session: dict[str, Any], field_key: str) -> str:
    current: Any = session
    for part in str(field_key or "").split("."):
        token = part.strip()
        if not token:
            return "-"
        if not isinstance(current, dict):
            return "-"
        current = current.get(token)
    if current in (None, ""):
        return "-"
    if isinstance(current, (dict, list)):
        with contextlib.suppress(Exception):
            return json.dumps(current, ensure_ascii=False)
        return str(current)
    return str(current)


def _load_call_outcome_rows(session_output_dir: Path, scoped_client_ids: set[str]) -> list[dict[str, Any]]:
    db_path = session_output_dir / "call_outcomes.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT session_id, client_id, project_id, project_name, started_at, ended_at,
                   call_date, call_time, lead_name, to_number, appointment_date, appointment_time,
                   appointment_details, follow_up, summary, suggested_next_action,
                   qualification_status, result, provider, lead_source, duration_seconds,
                   raw_json
            FROM call_outcomes
            ORDER BY started_at DESC
            """
        ).fetchall()
    except Exception:
        return []
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    items: list[dict[str, Any]] = []
    for row in rows:
        client_id = str(row["client_id"] or "").strip()
        if scoped_client_ids and client_id not in scoped_client_ids:
            continue
        session_payload = json.loads(row["raw_json"] or "{}") if row["raw_json"] else {}
        provider_usage = _resolve_provider_usage(session_payload) if isinstance(session_payload, dict) else {}
        from_number = _resolve_from_number(session_payload, provider_usage) if isinstance(session_payload, dict) else "-"
        items.append(
            {
                "session_id": row["session_id"],
                "client_id": client_id,
                "project_id": row["project_id"],
                "project_name": row["project_name"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "lead_name": row["lead_name"],
                "to_number": row["to_number"],
                "from_number": from_number,
                "appointment_details": row["appointment_details"],
                "appointment_date": row["appointment_date"],
                "appointment_time": row["appointment_time"],
                "important_questions_asked": [],
                "gemini_status": "unknown",
                "call_date": row["call_date"],
                "call_time": row["call_time"],
                "provider": row["provider"],
                "lead_source": row["lead_source"],
                "result": row["result"],
                "duration_seconds": float(row["duration_seconds"] or 0.0),
                "qualification": row["qualification_status"] or "unknown",
                "follow_up": row["follow_up"],
                "critical_fields_complete": False,
                "started_at": row["started_at"],
                "summary": {
                    "summary": row["summary"] or "",
                    "suggested_next_action": row["suggested_next_action"] or "",
                },
                "memory": {},
                "metrics": {},
                "telephony_context": {},
                "raw_json": row["raw_json"],
            }
        )
    return items


def build_dashboard_analytics(
    session_output_dir: Path,
    client_ids: set[str] | None = None,
    results_base_template: str = DEFAULT_RESULTS_BASE_TEMPLATE,
    results_extra_fields: list[str] | None = None,
) -> dict[str, Any]:
    store = _read_store()
    raw_lead_records = store.get("leads") or []
    scoped_client_ids = {str(item).strip() for item in (client_ids or set()) if str(item).strip()}
    lead_records = [
        item
        for item in raw_lead_records
        if not scoped_client_ids or str(item.get("client_id") or "").strip() in scoped_client_ids
    ]
    sessions: list[dict[str, Any]] = []
    for artifact_path in session_output_dir.glob("*/*/artifacts.json"):
        payload = _load_artifact(artifact_path)
        if payload:
            payload_client_id = str(payload.get("client_id") or "").strip()
            if scoped_client_ids and payload_client_id not in scoped_client_ids:
                continue
            sessions.append(payload)
    fallback_rows = _load_call_outcome_rows(session_output_dir, scoped_client_ids)
    seen_session_ids = {str(session.get("session_id") or "").strip() for session in sessions}
    for fallback in fallback_rows:
        session_id = str(fallback.get("session_id") or "").strip()
        if not session_id or session_id in seen_session_ids:
            continue
        sessions.append(fallback)
        seen_session_ids.add(session_id)

    def started_at(session: dict[str, Any]) -> str:
        return str(session.get("started_at") or "")

    sessions.sort(key=started_at, reverse=True)

    calls_made = len(sessions)
    successful_calls = 0
    unsuccessful_calls = 0
    follow_up_required = 0
    critical_fields_complete = 0
    source_breakdown: dict[str, int] = {}
    provider_breakdown: dict[str, int] = {}
    recent_calls: list[dict[str, Any]] = []
    called_numbers: set[str] = set()
    normalized_extra_fields = _normalize_extra_fields(results_extra_fields)
    base_columns = DEFAULT_RESULTS_COLUMNS if str(results_base_template or "").strip() else DEFAULT_RESULTS_COLUMNS
    table_columns = [{"key": key, "label": label} for key, label in base_columns]
    for field_key in normalized_extra_fields:
        if any(str(column["key"]) == field_key for column in table_columns):
            continue
        table_columns.append({"key": field_key, "label": _column_label(field_key)})

    for session in sessions:
        result = _session_result(session)
        provider_usage = _resolve_provider_usage(session)
        provider = _resolve_provider(session, provider_usage)
        if _is_legacy_provider(provider):
            continue
        if result == "successful":
            successful_calls += 1
        else:
            unsuccessful_calls += 1

        memory = session.get("memory") or {}
        summary = session.get("summary") or {}
        next_step = memory.get("next_step") or summary.get("suggested_next_action") or ""
        if next_step:
            follow_up_required += 1

        direction = _resolve_call_direction(session, provider_usage)
        source = _resolve_source(provider, direction, provider_usage)
        source_breakdown[source] = source_breakdown.get(source, 0) + 1
        provider_breakdown[provider] = provider_breakdown.get(provider, 0) + 1

        to_number = _resolve_to_number(session, provider_usage)
        if to_number:
            called_numbers.add(to_number)

        appointment_details, appointment_date, appointment_time = _extract_appointment_snapshot(session)
        important_questions_asked = _extract_important_questions(session)
        gemini_status = _extract_gemini_status(session)
        call_date, call_time = _extract_call_date_time(str(session.get("started_at") or ""))
        lead_name = _resolve_lead_name(session, provider_usage)
        has_critical_fields = (
            bool(lead_name and lead_name != "Unknown")
            and bool(to_number and to_number != "-")
            and bool(appointment_date != "-" and appointment_time != "-")
        )
        if has_critical_fields:
            critical_fields_complete += 1

        row = {
            "session_id": session.get("session_id"),
            "client_id": session.get("client_id"),
            "lead_name": lead_name,
            "to_number": to_number or "-",
            "from_number": _resolve_from_number(session, provider_usage),
            "appointment_details": appointment_details,
            "appointment_date": appointment_date,
            "appointment_time": appointment_time,
            "important_questions_asked": important_questions_asked,
            "gemini_status": gemini_status,
            "call_date": call_date,
            "call_time": call_time,
            "provider": provider,
            "lead_source": source,
            "result": result,
            "duration_seconds": round(_coerce_duration(session), 1),
            "qualification": ((summary.get("qualification") or {}).get("status")) or "unknown",
            "follow_up": next_step or "-",
            "critical_fields_complete": has_critical_fields,
            "started_at": session.get("started_at"),
        }
        for field_key in normalized_extra_fields:
            if field_key in row:
                continue
            row[field_key] = _resolve_path_value(session, field_key)
        recent_calls.append(row)

    lead_source_breakdown: dict[str, int] = {}
    unique_arrived_numbers: set[str] = set()
    for lead in lead_records:
        source = str(lead.get("source") or "unknown")
        lead_source_breakdown[source] = lead_source_breakdown.get(source, 0) + 1
        normalized = _normalize_phone(str(lead.get("to_number") or ""))
        if normalized:
            unique_arrived_numbers.add(normalized)

    return {
        "summary": {
            "leads_arrived": len(lead_records),
            "unique_leads_arrived": len(unique_arrived_numbers),
            "calls_made": calls_made,
            "successful_calls": successful_calls,
            "unsuccessful_calls": unsuccessful_calls,
            "follow_up_required": follow_up_required,
            "not_called_yet": max(len(unique_arrived_numbers - called_numbers), 0),
            "critical_fields_complete": critical_fields_complete,
        },
        "source_breakdown": lead_source_breakdown,
        "call_source_breakdown": source_breakdown,
        "provider_breakdown": provider_breakdown,
        "table_columns": table_columns,
        "recent_calls": recent_calls[:20],
    }


def build_workspace_billing_summary(
    session_output_dir: Path,
    client_ids: set[str] | None = None,
) -> dict[str, Any]:
    scoped_client_ids = {str(item).strip() for item in (client_ids or set()) if str(item).strip()}
    sessions: list[dict[str, Any]] = []
    for artifact_path in session_output_dir.glob("*/*/artifacts.json"):
        payload = _load_artifact(artifact_path)
        if not payload:
            continue
        payload_client_id = str(payload.get("client_id") or "").strip()
        if scoped_client_ids and payload_client_id not in scoped_client_ids:
            continue
        sessions.append(payload)

    total_estimated_cost = 0.0
    total_minute_units = 0.0
    total_minute_charges = 0.0
    currency = ""

    for session in sessions:
        actual_cost = session.get("actual_cost") or {}
        if not isinstance(actual_cost, dict):
            continue
        currency = currency or str(actual_cost.get("currency") or "")
        try:
            total_estimated_cost += float(actual_cost.get("total_estimated_cost") or 0.0)
        except (TypeError, ValueError):
            pass

        for group in ("telephony", "gemini"):
            items = actual_cost.get(group) or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("unit") or "").strip() != "minute":
                    continue
                try:
                    quantity = float(item.get("quantity") or 0.0)
                except (TypeError, ValueError):
                    quantity = 0.0
                try:
                    estimated_cost = float(item.get("estimated_cost") or 0.0)
                except (TypeError, ValueError):
                    estimated_cost = 0.0
                total_minute_units += max(quantity, 0.0)
                total_minute_charges += max(estimated_cost, 0.0)

    total_estimated_cost = round(total_estimated_cost, 6)
    total_minute_units = round(total_minute_units, 6)
    total_minute_charges = round(total_minute_charges, 6)
    non_minute_charges = round(max(total_estimated_cost - total_minute_charges, 0.0), 6)
    combined_effective_rate = round((total_minute_charges / total_minute_units), 6) if total_minute_units > 0 else 0.0

    return {
        "currency": currency or "INR",
        "total_sessions": len(sessions),
        "total_estimated_cost": total_estimated_cost,
        "minute_charges_total": total_minute_charges,
        "non_minute_charges_total": non_minute_charges,
        "total_minute_units": total_minute_units,
        "combined_effective_rate_per_minute": combined_effective_rate,
    }
