"""SQLite-backed storage for finalized call outcomes."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analytics import (
    _coerce_duration,
    _extract_appointment_snapshot,
    _extract_call_date_time,
    _resolve_lead_name,
    _resolve_provider,
    _resolve_provider_usage,
    _resolve_source,
    _resolve_to_number,
    _resolve_call_direction,
    _session_result,
)
from .models import SessionArtifacts


class SqliteCallOutcomeStore:
    """Persist one normalized row per session for downstream actions."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS call_outcomes (
                    session_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    project_id TEXT,
                    project_name TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    call_date TEXT NOT NULL,
                    call_time TEXT NOT NULL,
                    lead_name TEXT NOT NULL,
                    to_number TEXT NOT NULL,
                    appointment_date TEXT NOT NULL,
                    appointment_time TEXT NOT NULL,
                    appointment_details TEXT NOT NULL,
                    follow_up TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    suggested_next_action TEXT NOT NULL,
                    qualification_status TEXT NOT NULL,
                    result TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    lead_source TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    critical_fields_complete INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_call_outcomes_client_started
                ON call_outcomes(client_id, started_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_call_outcomes_phone
                ON call_outcomes(to_number)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_call_outcomes_appt_slot
                ON call_outcomes(appointment_date, appointment_time)
                """
            )
            conn.commit()

    def upsert_outcome(self, artifacts: SessionArtifacts, telephony_context: dict[str, Any] | None = None) -> None:
        payload = artifacts.model_dump(mode="json")
        session_dict = dict(payload)
        if telephony_context:
            session_dict["telephony_context"] = telephony_context

        provider_usage = _resolve_provider_usage(session_dict)
        direction = _resolve_call_direction(session_dict, provider_usage)
        provider = _resolve_provider(session_dict, provider_usage)
        lead_source = _resolve_source(provider, direction, provider_usage)
        appointment_details, appointment_date, appointment_time = _extract_appointment_snapshot(session_dict)
        lead_name = _resolve_lead_name(session_dict, provider_usage)
        to_number = _resolve_to_number(session_dict, provider_usage)
        result = _session_result(session_dict)
        duration_seconds = round(_coerce_duration(session_dict), 1)
        call_date, call_time = _extract_call_date_time(str(session_dict.get("started_at") or ""))
        summary = session_dict.get("summary") or {}
        memory = session_dict.get("memory") or {}
        next_step = str(memory.get("next_step") or summary.get("suggested_next_action") or "").strip()
        critical_fields_complete = int(
            bool(lead_name and lead_name != "Unknown")
            and bool(to_number and to_number != "-")
            and bool(appointment_date != "-" and appointment_time != "-")
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        row = {
            "session_id": str(session_dict.get("session_id") or ""),
            "client_id": str(session_dict.get("client_id") or ""),
            "project_id": str(session_dict.get("project_id") or "") or None,
            "project_name": str(session_dict.get("project_name") or "") or None,
            "started_at": str(session_dict.get("started_at") or ""),
            "ended_at": str(session_dict.get("ended_at") or "") or None,
            "call_date": call_date,
            "call_time": call_time,
            "lead_name": lead_name or "Unknown",
            "to_number": to_number or "-",
            "appointment_date": appointment_date or "-",
            "appointment_time": appointment_time or "-",
            "appointment_details": appointment_details or "-",
            "follow_up": next_step or "-",
            "summary": str(summary.get("summary") or "").strip() or "-",
            "suggested_next_action": str(summary.get("suggested_next_action") or "").strip() or "-",
            "qualification_status": str((summary.get("qualification") or {}).get("status") or "unknown"),
            "result": result,
            "provider": provider,
            "lead_source": lead_source,
            "duration_seconds": float(duration_seconds),
            "critical_fields_complete": critical_fields_complete,
            "error_count": len(session_dict.get("errors") or []),
            "raw_json": json.dumps(payload, ensure_ascii=False),
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        if not row["session_id"] or not row["client_id"] or not row["started_at"]:
            return

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM call_outcomes WHERE session_id = ? LIMIT 1",
                (row["session_id"],),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now_iso
            conn.execute(
                """
                INSERT INTO call_outcomes(
                    session_id, client_id, project_id, project_name, started_at, ended_at,
                    call_date, call_time, lead_name, to_number, appointment_date, appointment_time,
                    appointment_details, follow_up, summary, suggested_next_action,
                    qualification_status, result, provider, lead_source, duration_seconds,
                    critical_fields_complete, error_count, raw_json, created_at, updated_at
                )
                VALUES(
                    :session_id, :client_id, :project_id, :project_name, :started_at, :ended_at,
                    :call_date, :call_time, :lead_name, :to_number, :appointment_date, :appointment_time,
                    :appointment_details, :follow_up, :summary, :suggested_next_action,
                    :qualification_status, :result, :provider, :lead_source, :duration_seconds,
                    :critical_fields_complete, :error_count, :raw_json, :created_at, :updated_at
                )
                ON CONFLICT(session_id) DO UPDATE SET
                    client_id = excluded.client_id,
                    project_id = excluded.project_id,
                    project_name = excluded.project_name,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    call_date = excluded.call_date,
                    call_time = excluded.call_time,
                    lead_name = excluded.lead_name,
                    to_number = excluded.to_number,
                    appointment_date = excluded.appointment_date,
                    appointment_time = excluded.appointment_time,
                    appointment_details = excluded.appointment_details,
                    follow_up = excluded.follow_up,
                    summary = excluded.summary,
                    suggested_next_action = excluded.suggested_next_action,
                    qualification_status = excluded.qualification_status,
                    result = excluded.result,
                    provider = excluded.provider,
                    lead_source = excluded.lead_source,
                    duration_seconds = excluded.duration_seconds,
                    critical_fields_complete = excluded.critical_fields_complete,
                    error_count = excluded.error_count,
                    raw_json = excluded.raw_json,
                    created_at = :created_at,
                    updated_at = excluded.updated_at
                """,
                {**row, "created_at": created_at},
            )
            conn.commit()
