"""Client-scoped CRM integration hooks."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import request

from .models import ClientBundle, SessionArtifacts

logger = logging.getLogger(__name__)


def trigger_post_call_integrations(
    client: ClientBundle,
    artifacts: SessionArtifacts,
    telephony_context: dict[str, Any] | None,
) -> list[str]:
    """Run enabled CRM integrations for a specific client."""
    cfg = _load_integration_config(client.base_dir / "crm_integration.json")
    if not cfg:
        return []
    if not bool(cfg.get("enabled")):
        return []
    provider = str(cfg.get("provider") or "").strip().lower()
    if provider != "cardiolab":
        return [f"Unsupported CRM provider: {provider or 'unknown'}"]
    return _trigger_cardiolab_appointment(client, artifacts, telephony_context, cfg)


def _load_integration_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to parse CRM integration config at %s", path)
        return {}


def _is_appointment_confirmed(agent_text: str) -> bool:
    lowered = agent_text.lower()
    markers = (
        "appointment is noted for",
        "appointment is set for",
        "your appointment is",
        "अपॉइंटमेंट",
        "appointment has been confirmed",
        "appointment has been booked",
        "नोंदवली आहे",
        "कन्फर्म",
        "बुक",
        "निश्चित",
        "तय",
    )
    return any(marker in lowered for marker in markers)


def _extract_confirmed_agent_line(artifacts: SessionArtifacts) -> str:
    for turn in reversed(artifacts.transcript):
        if turn.speaker != "agent":
            continue
        text = (turn.text or "").strip()
        if _is_appointment_confirmed(text):
            return text
    return ""


def _extract_any_date_time_from_agent_turns(artifacts: SessionArtifacts) -> tuple[str | None, str | None]:
    for turn in reversed(artifacts.transcript):
        if turn.speaker != "agent":
            continue
        text = (turn.text or "").strip()
        if not text:
            continue
        date_value, time_value = _extract_appointment_date_and_time(text)
        if date_value and time_value:
            return date_value, time_value
    return None, None


def _extract_date_time_from_free_text(text: str) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    return _extract_appointment_date_and_time(text)


def _resolve_confirmed_slot(artifacts: SessionArtifacts) -> tuple[str | None, str | None]:
    # 1) Ideal case: explicit confirmation line in agent response.
    confirmed_line = _extract_confirmed_agent_line(artifacts)
    date_value, time_value = _extract_appointment_date_and_time(confirmed_line)
    if date_value and time_value:
        return date_value, time_value

    # 2) Structured memory fallback.
    mem_text = str(artifacts.memory.appointment_details or "").strip()
    date_value, time_value = _extract_date_time_from_free_text(mem_text)
    if date_value and time_value:
        return date_value, time_value

    # 3) Suggested next-step fallback.
    summary = artifacts.summary
    next_step = str(summary.suggested_next_action if summary is not None else "").strip()
    date_value, time_value = _extract_date_time_from_free_text(next_step)
    if date_value and time_value:
        return date_value, time_value

    # 4) Last resort: any agent line that includes a date+time tuple.
    return _extract_any_date_time_from_agent_turns(artifacts)


def _extract_appointment_date_and_time(agent_text: str) -> tuple[str | None, str | None]:
    if not agent_text:
        return None, None
    text = " ".join(agent_text.split())
    date_value: str | None = None
    time_value: str | None = None

    date_match = re.search(r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\b", text)
    if date_match:
        with_date = date_match.group(1)
        try:
            date_value = datetime.strptime(with_date, "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            date_value = None

    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b", text, flags=re.IGNORECASE)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or "0")
        ampm = (time_match.group(3) or "").upper()
        if ampm == "AM":
            hour = 0 if hour == 12 else hour
        elif ampm == "PM":
            hour = 12 if hour == 12 else hour + 12
        time_value = f"{hour:02d}:{minute:02d}"
    else:
        # Hindi/Marathi pattern: "11 वाजता" / "11 बजे"
        local_match = re.search(r"\b(\d{1,2})\s*(वाजता|बजे)\b", text)
        if local_match:
            hour = int(local_match.group(1))
            # Without AM/PM marker we keep a practical daytime default.
            if 1 <= hour <= 7:
                hour += 12
            time_value = f"{hour:02d}:00"

    return date_value, time_value


def _normalize_phone_for_cardiolab(number: str) -> str:
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if len(digits) == 10:
        return f"91{digits}"
    return digits


def _extract_age_from_transcript(artifacts: SessionArtifacts) -> int | None:
    for turn in artifacts.transcript:
        if turn.speaker != "user":
            continue
        text = (turn.text or "").strip()
        if not text:
            continue
        for token in re.findall(r"\b([1-9][0-9]{0,2})\b", text):
            value = int(token)
            if 1 <= value <= 120:
                return value
    return None


def _resolve_patient_name(artifacts: SessionArtifacts, telephony_context: dict[str, Any] | None) -> str:
    memory_name = (artifacts.memory.lead_name or "").strip()
    if memory_name and not any(ch.isdigit() for ch in memory_name):
        return memory_name
    if isinstance(telephony_context, dict):
        provider_usage = (telephony_context.get("raw_provider_usage") or {}) if isinstance(
            telephony_context.get("raw_provider_usage"), dict
        ) else {}
        name = str(provider_usage.get("customer_name") or telephony_context.get("customer_name") or "").strip()
        if name and not any(ch.isdigit() for ch in name):
            return name
    return "Unknown Patient"


def _resolve_patient_phone(telephony_context: dict[str, Any] | None) -> str:
    if not isinstance(telephony_context, dict):
        return ""
    provider_usage = (telephony_context.get("raw_provider_usage") or {}) if isinstance(
        telephony_context.get("raw_provider_usage"), dict
    ) else {}
    direction = str(
        provider_usage.get("direction")
        or telephony_context.get("direction")
        or telephony_context.get("call_direction")
        or ""
    ).strip().lower()
    inbound_candidates = [
        provider_usage.get("from_number"),
        telephony_context.get("from_number"),
        provider_usage.get("customer_no_with_prefix"),
        provider_usage.get("customer_number_with_prefix"),
        provider_usage.get("caller_id_number"),
        telephony_context.get("customer_no_with_prefix"),
        telephony_context.get("customer_number_with_prefix"),
        telephony_context.get("caller_id_number"),
        provider_usage.get("to_number"),
        telephony_context.get("to_number"),
        telephony_context.get("phone"),
    ]
    outbound_candidates = [
        provider_usage.get("to_number"),
        telephony_context.get("to_number"),
        provider_usage.get("from_number"),
        telephony_context.get("from_number"),
        telephony_context.get("phone"),
    ]
    for candidate in inbound_candidates if direction == "inbound" else outbound_candidates:
        number = _normalize_phone_for_cardiolab(str(candidate or "").strip())
        if number:
            return number
    number = ""
    return number


def _get_env_value(name: str) -> str:
    return (os.getenv(name, "") or "").strip()


def _trigger_cardiolab_appointment(
    client: ClientBundle,
    artifacts: SessionArtifacts,
    telephony_context: dict[str, Any] | None,
    cfg: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    event_mode = str(cfg.get("event") or "appointment_confirmed").strip().lower()
    if event_mode != "appointment_confirmed":
        return errors

    appointment_date, appointment_time = _resolve_confirmed_slot(artifacts)
    if not appointment_date or not appointment_time:
        errors.append(
            "CRM sync skipped: could not parse confirmed appointment date/time from transcript or summary context."
        )
        return errors

    base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
    username_env = str(cfg.get("username_env") or "").strip()
    password_env = str(cfg.get("password_env") or "").strip()
    if not base_url or not username_env or not password_env:
        errors.append("CRM sync skipped: missing base_url/username_env/password_env in crm_integration.json.")
        return errors

    username = _get_env_value(username_env)
    password = _get_env_value(password_env)
    if not username or not password:
        errors.append(f"CRM sync skipped: missing credentials in env ({username_env}/{password_env}).")
        return errors

    patient_phone = _resolve_patient_phone(telephony_context)
    if not patient_phone:
        errors.append("CRM sync skipped: missing patient phone number.")
        return errors

    patient_name = _resolve_patient_name(artifacts, telephony_context)
    patient_city = str(cfg.get("default_patient_city") or "Nashik").strip()
    patient_age = _extract_age_from_transcript(artifacts) or int(cfg.get("default_patient_age") or 35)
    patient_gender = str(cfg.get("default_patient_gender") or "Male").strip()
    package_id = int(cfg.get("package_id") or 1)
    hospital_id = int(cfg.get("hospital_id") or 1)
    notes_template = str(cfg.get("notes_template") or "Booked by AI call flow for {client_id}")
    notes = notes_template.format(client_id=client.config.client_id, project_id=artifacts.project_id or "")

    try:
        login_payload = json.dumps({"username": username, "password": password}).encode("utf-8")
        login_req = request.Request(
            f"{base_url}/api/auth/login",
            data=login_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(login_req, timeout=15) as response:
            login_data = json.loads(response.read().decode("utf-8"))
        token = str(login_data.get("token") or "").strip()
        if not token:
            errors.append("CRM sync failed: login response did not include token.")
            return errors

        appointment_payload = {
            "patient_phone": patient_phone,
            "patient_name": patient_name,
            "patient_city": patient_city,
            "patient_age": patient_age,
            "patient_gender": patient_gender,
            "package_id": package_id,
            "hospital_id": hospital_id,
            "appointment_date": appointment_date,
            "appointment_time": appointment_time,
            "notes": notes,
        }
        book_req = request.Request(
            f"{base_url}/api/appointments",
            data=json.dumps(appointment_payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with request.urlopen(book_req, timeout=20) as response:
            appointment_data = json.loads(response.read().decode("utf-8"))

        apt_code = str(appointment_data.get("apt_code") or "").strip()
        if apt_code:
            artifacts.memory.next_step = (
                artifacts.memory.next_step
                or f"CRM appointment synced successfully with reference {apt_code}."
            )
        logger.info(
            "CRM appointment sync successful client_id=%s apt_code=%s patient_phone=%s",
            client.config.client_id,
            apt_code or "<missing>",
            patient_phone,
        )
    except Exception as exc:
        logger.exception("CRM appointment sync failed for client_id=%s", client.config.client_id)
        errors.append(f"CRM sync failed: {exc}")
    return errors
