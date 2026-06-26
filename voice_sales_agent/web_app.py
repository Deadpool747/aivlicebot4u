"""Minimal local browser UI for running the voice sales demo."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import csv
import hashlib
import hmac
from http.cookies import SimpleCookie
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from datetime import timedelta
from typing import Any
import urllib.parse
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
import httpx
from pydantic import BaseModel
from openpyxl import Workbook, load_workbook
try:
    from aiortc import RTCPeerConnection, RTCSessionDescription
except Exception:  # pragma: no cover - optional dependency path
    RTCPeerConnection = None  # type: ignore[assignment]
    RTCSessionDescription = None  # type: ignore[assignment]

from .analytics import build_dashboard_analytics, build_workspace_billing_summary, record_leads
from .auth_backend import SqliteAuthStore
from .call_outcomes_store import SqliteCallOutcomeStore
from .clients import (
    get_client_editor_payload,
    list_client_ids,
    list_client_projects,
    load_client,
    save_client_editor_payload,
)
from .config import AppSettings, load_settings
from .call_state import InMemoryCallStateStore, RedisCallStateStore
from .provider_runtime import build_structured_client
from .live_preview_pipeline import (
    DEFAULT_LIVE_PREVIEW_FIELD_CONFIG,
    build_stage1_prompt,
    build_stage2_prompt,
    fallback_stage2_mapping,
)
from .session import VoiceSalesSession
from .telephony import (
    AirtelIQCallClient,
    AirtelIQMediaBridge,
    ExotelCallClient,
    ExotelMediaBridge,
    MetaWhatsAppCallClient,
    MetaWhatsAppMediaBridge,
    PiopiyMediaBridge,
    PiopiyCallClient,
    PendingCall,
    SmartfloMediaBridge,
    TwilioCallClient,
    TwilioMediaBridge,
    _resample_linear,
    build_ws_url,
    validate_public_base_url,
)
from .piopiy_direct_gemini_bridge import PiopiyDirectGeminiBridgeSession
from .constants import INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE, PROJECT_ROOT
from .models import SessionArtifacts
from .transcripts import SessionLogger
from .web_state import DashboardState

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "oswell_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 14
GUEST_VISITOR_COOKIE_NAME = "aivoicebot4u_guest_visitor_id"
GUEST_VISITOR_MAX_AGE_SECONDS = 60 * 60 * 24 * 365
DEFAULT_GUEST_DEMO_NOTIFICATION_EMAIL = "support@aivoicebot4u.com"
ALLOWED_RECORDING_SUFFIXES = {".wav", ".mp3", ".mpeg"}
PIOPIY_PLAYBACK_SUFFIX = ".playback.wav"


class AuthSignupRequest(BaseModel):
    name: str
    email: str
    password: str


class AuthLoginRequest(BaseModel):
    email: str
    password: str


class AuthForgotPasswordRequest(BaseModel):
    email: str


class LivePreviewTurn(BaseModel):
    speaker: str
    text: str


class LivePreviewFieldConfigItem(BaseModel):
    key: str
    label: str
    description: str
    required: bool = False
    allow_inference: bool = True
    fallback_behavior: str = "not_mentioned"
    output_format: str = "short_text"


class LivePreviewExtractRequest(BaseModel):
    session_key: str | None = None
    turns: list[LivePreviewTurn] = []
    field_config: list[LivePreviewFieldConfigItem] | None = None


class AuthManager:
    """SQLite-backed auth service with signed cookie sessions."""

    def __init__(self, store: SqliteAuthStore, secret_seed: str) -> None:
        self._store = store
        self._secret = hashlib.sha256(secret_seed.encode("utf-8")).digest()

    @staticmethod
    def _normalize_email(email: str) -> str:
        return email.strip().lower()

    @staticmethod
    def _normalize_name(name: str) -> str:
        return " ".join((name or "").strip().split())

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        return "".join(ch for ch in str(phone or "").strip() if ch in "+0123456789")

    @staticmethod
    def _normalize_guest_visitor_id(visitor_id: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "", str(visitor_id or "").strip().lower())
        return normalized[:48]

    @staticmethod
    def _hash_password(password: str, salt_hex: str) -> str:
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            180_000,
        )
        return digest.hex()

    def signup(self, name: str, email: str, password: str) -> dict[str, str]:
        normalized_email = self._normalize_email(email)
        normalized_name = self._normalize_name(name)
        if len(normalized_name) < 2:
            raise ValueError("Enter your full name.")
        if "@" not in normalized_email or "." not in normalized_email.rsplit("@", 1)[-1]:
            raise ValueError("Enter a valid email address.")
        if len(password.strip()) < 8:
            raise ValueError("Use at least 8 characters for your password.")
        salt_hex = os.urandom(16).hex()
        now_iso = datetime.now(timezone.utc).isoformat()
        return self._store.create_user(
            user_id=str(uuid4()),
            name=normalized_name,
            email=normalized_email,
            password_hash=self._hash_password(password, salt_hex),
            password_salt=salt_hex,
            created_at=now_iso,
        )

    def bootstrap_admin(self, *, name: str, email: str, password: str) -> dict[str, str]:
        normalized_email = self._normalize_email(email)
        existing = self._store.get_user_by_email(normalized_email)
        if existing is not None:
            if existing.get("is_admin") == "1":
                return existing
            raise ValueError("The configured admin email already exists as a non-admin user.")
        salt_hex = os.urandom(16).hex()
        now_iso = datetime.now(timezone.utc).isoformat()
        return self._store.create_user(
            user_id=str(uuid4()),
            name=self._normalize_name(name) or "Admin",
            email=normalized_email,
            password_hash=self._hash_password(password, salt_hex),
            password_salt=salt_hex,
            created_at=now_iso,
            is_admin=True,
        )

    def login(self, email: str, password: str) -> dict[str, str]:
        normalized_email = self._normalize_email(email)
        user = self._store.get_user_by_email(normalized_email)
        if user is None:
            raise ValueError("Invalid email or password.")
        expected = self._hash_password(password, user["password_salt"])
        if not hmac.compare_digest(expected, user["password_hash"]):
            raise ValueError("Invalid email or password.")
        return user

    def create_guest_demo_lead(
        self,
        *,
        user: dict[str, str] | None,
        workspace_client_id: str,
        project_id: str | None,
        project_name: str | None,
        full_name: str,
        phone: str,
        email: str,
        source: str = "website_guest_demo",
        status: str = "submitted",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_name = self._normalize_name(full_name)
        normalized_phone = self._normalize_phone(phone)
        normalized_email = self._normalize_email(email)
        if len(normalized_name) < 2:
            raise ValueError("Enter a valid full name.")
        if len(normalized_phone) < 7:
            raise ValueError("Enter a valid phone number.")
        if "@" not in normalized_email or "." not in normalized_email.rsplit("@", 1)[-1]:
            raise ValueError("Enter a valid email address.")
        now_iso = datetime.now(timezone.utc).isoformat()
        return self._store.create_guest_demo_lead(
            user_id=str((user or {}).get("id") or "") or None,
            workspace_client_id=workspace_client_id,
            project_id=project_id,
            project_name=project_name,
            full_name=normalized_name,
            phone=normalized_phone,
            email=normalized_email,
            source=source,
            status=status,
            created_at=now_iso,
            updated_at=now_iso,
            metadata=metadata,
        )

    def update_guest_demo_lead_notification(
        self,
        *,
        lead_id: str,
        notification_status: str,
        notification_sent_at: str | None = None,
        notification_error: str | None = None,
    ) -> None:
        self._store.update_guest_demo_lead_notification(
            lead_id=lead_id,
            notification_status=notification_status,
            notification_sent_at=notification_sent_at,
            notification_error=notification_error,
        )

    def update_guest_demo_lead_summary(
        self,
        *,
        lead_id: str,
        last_session_id: str | None,
        testing_summary: str | None,
        asked_questions: list[str],
        summary_updated_at: str,
        status: str | None = None,
    ) -> None:
        self._store.update_guest_demo_lead_summary(
            lead_id=lead_id,
            last_session_id=last_session_id,
            testing_summary=testing_summary,
            asked_questions=asked_questions,
            summary_updated_at=summary_updated_at,
            status=status,
        )

    def get_or_create_demo_user(self) -> dict[str, str]:
        """Return a stable local demo user used for no-login Start Demo flows."""
        demo_email = "guest@aivoicebot4u.local"
        existing = self._store.get_user_by_email(demo_email)
        if existing is not None:
            return existing
        salt_hex = os.urandom(16).hex()
        now_iso = datetime.now(timezone.utc).isoformat()
        return self._store.create_user(
            user_id=str(uuid4()),
            name="Demo User",
            email=demo_email,
            password_hash=self._hash_password(f"demo_{uuid4().hex}", salt_hex),
            password_salt=salt_hex,
            created_at=now_iso,
        )

    def get_or_create_public_guest_user(self, visitor_id: str | None) -> dict[str, str]:
        normalized_visitor_id = self._normalize_guest_visitor_id(visitor_id or "")
        if not normalized_visitor_id:
            return self.get_or_create_demo_user()
        demo_email = f"guest_{normalized_visitor_id}@aivoicebot4u.local"
        existing = self._store.get_user_by_email(demo_email)
        if existing is not None:
            return existing
        salt_hex = os.urandom(16).hex()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            return self._store.create_user(
                user_id=str(uuid4()),
                name="Demo User",
                email=demo_email,
                password_hash=self._hash_password(f"demo_{uuid4().hex}", salt_hex),
                password_salt=salt_hex,
                created_at=now_iso,
            )
        except ValueError:
            existing = self._store.get_user_by_email(demo_email)
            if existing is not None:
                return existing
            raise

    @staticmethod
    def is_admin(user: dict[str, str] | None) -> bool:
        return bool(user and str(user.get("is_admin") or "") == "1")

    @staticmethod
    def is_guest_user(user: dict[str, str] | None) -> bool:
        if not user:
            return False
        email = str(user.get("email") or "").strip().lower()
        return email == "guest@aivoicebot4u.local" or (
            email.startswith("guest_") and email.endswith("@aivoicebot4u.local")
        )

    @staticmethod
    def _b64url_encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _b64url_decode(raw: str) -> bytes | None:
        try:
            padding = "=" * (-len(raw) % 4)
            return base64.urlsafe_b64decode(f"{raw}{padding}".encode("ascii"))
        except Exception:
            return None

    def issue_session_token(self, user: dict[str, str]) -> str:
        payload = {
            "uid": user["id"],
            "iat": int(time.time()),
            "exp": int(time.time()) + SESSION_MAX_AGE_SECONDS,
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        encoded_payload = self._b64url_encode(payload_bytes)
        signature = hmac.new(self._secret, payload_bytes, hashlib.sha256).hexdigest()
        return f"{encoded_payload}.{signature}"

    def _resolve_user_from_token(self, token: str) -> dict[str, str] | None:
        token = (token or "").strip()
        if "." not in token:
            return None
        encoded_payload, signature = token.split(".", 1)
        if not encoded_payload or not signature:
            return None
        payload_bytes = self._b64url_decode(encoded_payload)
        if payload_bytes is None:
            return None
        expected_signature = hmac.new(self._secret, payload_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            return None
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        expires_at = int(payload.get("exp") or 0)
        if expires_at <= int(time.time()):
            return None
        user_id = str(payload.get("uid") or "")
        if not user_id:
            return None
        return self._store.get_user_by_id(user_id)

    def migrate_legacy_users(self, users_path: Path) -> int:
        imported = self._store.migrate_from_legacy_json(users_path)
        if imported > 0:
            logger.info("Imported %s legacy auth users into SQLite auth store.", imported)
        return imported

    def current_user_from_request(self, request: Request) -> dict[str, str] | None:
        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        return self._resolve_user_from_token(token)

    def current_user_from_cookie_header(self, cookie_header: str) -> dict[str, str] | None:
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        with contextlib.suppress(Exception):
            cookie.load(cookie_header)
        if SESSION_COOKIE_NAME not in cookie:
            return None
        return self._resolve_user_from_token(str(cookie[SESSION_COOKIE_NAME].value))

    @staticmethod
    def public_user(user: dict[str, str]) -> dict[str, str]:
        return {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "created_at": user["created_at"],
            "is_admin": "1" if str(user.get("is_admin") or "") == "1" else "0",
        }


def _can_use_local_microphone() -> bool:
    """Best-effort check for local mic availability on the running host."""
    try:
        import pyaudio  # type: ignore
    except Exception:
        return False

    pa = None
    try:
        pa = pyaudio.PyAudio()
        device_count = pa.get_device_count()
        if device_count <= 0:
            return False
        for index in range(device_count):
            info = pa.get_device_info_by_index(index)
            if float(info.get("maxInputChannels", 0) or 0) > 0:
                return True
        return False
    except Exception:
        return False
    finally:
        if pa is not None:
            with contextlib.suppress(Exception):
                pa.terminate()


async def _notify_guest_demo_lead_via_formspree(lead: dict[str, Any]) -> tuple[str, str | None]:
    endpoint = (os.getenv("FORMSPREE_ENDPOINT") or "").strip()
    if not endpoint:
        return "skipped", "FORMSPREE_ENDPOINT is not configured."

    target_email = (os.getenv("FORMSPREE_TO_EMAIL") or DEFAULT_GUEST_DEMO_NOTIFICATION_EMAIL).strip()
    project_name = str(lead.get("project_name") or lead.get("project_id") or "Guest Demo").strip()
    metadata = lead.get("metadata") if isinstance(lead.get("metadata"), dict) else {}
    payload = {
        "lead_id": str(lead.get("id") or ""),
        "name": str(lead.get("full_name") or ""),
        "phone": str(lead.get("phone") or ""),
        "email": str(lead.get("email") or ""),
        "project_name": project_name,
        "project_id": str(lead.get("project_id") or ""),
        "workspace_client_id": str(lead.get("workspace_client_id") or ""),
        "source": str(lead.get("source") or "website_guest_demo"),
        "target_email": target_email,
        "_replyto": str(lead.get("email") or ""),
        "_subject": f"New guest demo lead: {project_name}",
        "message": (
            f"New guest demo lead received.\n\n"
            f"Name: {lead.get('full_name')}\n"
            f"Phone: {lead.get('phone')}\n"
            f"Email: {lead.get('email')}\n"
            f"Project: {project_name}\n"
            f"Workspace Client: {lead.get('workspace_client_id')}\n"
            f"Source: {lead.get('source')}\n"
            f"Transport: {metadata.get('transport', '')}\n"
            f"Session Key: {metadata.get('session_key', '')}\n"
        ),
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
        if 200 <= response.status_code < 300:
            return "sent", None
        return "failed", f"Formspree returned HTTP {response.status_code}: {response.text[:300]}"
    except Exception as exc:
        return "failed", str(exc)


def _build_guest_demo_leads_workbook(leads: list[dict[str, Any]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Guest Demo Leads"
    worksheet.append(
        [
            "Created At",
            "Name",
            "Phone",
            "Email",
            "Project",
            "Status",
            "Notification",
            "Testing Summary",
            "Asked Questions",
            "Session ID",
            "Source",
        ]
    )
    for lead in leads:
        worksheet.append(
            [
                str(lead.get("created_at") or ""),
                str(lead.get("full_name") or ""),
                str(lead.get("phone") or ""),
                str(lead.get("email") or ""),
                str(lead.get("project_name") or lead.get("project_id") or ""),
                str(lead.get("status") or ""),
                str(lead.get("notification_status") or ""),
                str(lead.get("testing_summary") or ""),
                "\n".join(str(item) for item in (lead.get("asked_questions") or []) if str(item).strip()),
                str(lead.get("last_session_id") or ""),
                str(lead.get("source") or ""),
            ]
        )
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _extract_guest_demo_asked_questions(artifacts: SessionArtifacts) -> list[str]:
    prefixes = (
        "what",
        "which",
        "when",
        "where",
        "why",
        "how",
        "can",
        "could",
        "would",
        "will",
        "do",
        "does",
        "is",
        "are",
        "should",
        "i want",
        "i need",
        "i am looking",
        "i'm looking",
        "looking for",
        "tell me",
        "price",
        "pricing",
        "cost",
    )
    seen: set[str] = set()
    results: list[str] = []
    for turn in artifacts.transcript:
        if turn.speaker != "user":
            continue
        text = " ".join(str(turn.text or "").split()).strip()
        if len(text) < 4:
            continue
        normalized = text.lower()
        if "?" not in text and not any(normalized.startswith(prefix) for prefix in prefixes):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        results.append(text)
        if len(results) >= 8:
            break
    return results


def _build_guest_demo_testing_summary(artifacts: SessionArtifacts) -> str | None:
    if artifacts.summary and artifacts.summary.summary:
        return str(artifacts.summary.summary).strip()
    user_turns = [
        " ".join(str(turn.text or "").split()).strip()
        for turn in artifacts.transcript
        if turn.speaker == "user" and str(turn.text or "").strip()
    ]
    if not user_turns:
        return None
    project_name = str(artifacts.project_name or artifacts.project_id or "guest demo").strip()
    snippet = " | ".join(user_turns[:3]).strip()
    return f"User tested the {project_name} flow. Key conversation points: {snippet}"


def _is_terminal_call_status(status: str) -> bool:
    normalized = (status or "").strip().lower()
    return normalized in {
        "completed",
        "complete",
        "disconnected",
        "terminated",
        "canceled",
        "cancelled",
        "failed",
        "busy",
        "no-answer",
        "no answer",
        "not_answered",
        "unanswered",
        "hangup",
        "ended",
    }


def _is_terminal_event(event_type: str) -> bool:
    normalized = (event_type or "").strip().lower()
    return normalized in {
        "call_disconnected",
        "call_ended",
        "call_end",
        "call_terminated",
        "hangup",
        "stop",
    }


def _extract_exotel_stream_metadata(params: dict[str, str]) -> dict[str, str | bool | None]:
    stream_payload = params.get("Stream")
    parsed_stream: dict[str, str] = {}
    if stream_payload:
        try:
            raw_stream = json.loads(stream_payload)
            if isinstance(raw_stream, dict):
                parsed_stream = {str(key): str(value) for key, value in raw_stream.items() if value is not None}
        except json.JSONDecodeError:
            logger.warning("Could not parse Exotel Stream JSON payload: %s", stream_payload)

    def value(*keys: str) -> str | None:
        for key in keys:
            direct = params.get(key)
            if direct not in {None, ""}:
                return direct
            parsed = parsed_stream.get(key)
            if parsed not in {None, ""}:
                return parsed
        return None

    return {
        "stream_sid": value("Stream[StreamSID]", "StreamSID"),
        "stream_status": value("Stream[Status]", "Status"),
        "stream_duration_seconds": value("Stream[Duration]", "Duration"),
        "stream_disconnected_by": value("Stream[DisconnectedBy]", "DisconnectedBy"),
        "recording_url": value("Stream[RecordingUrl]", "RecordingUrl"),
        "stream_detailed_status": value("Stream[DetailedStatus]", "DetailedStatus"),
        "stream_error": value("Stream[Error]", "Error"),
        "passthru_called": True,
    }


def _extract_airtel_iq_payload(raw_payload: object) -> dict[str, str]:
    if not isinstance(raw_payload, dict):
        return {}
    return {str(key): str(value) for key, value in raw_payload.items() if value is not None}


def _payload_value(payload: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""
def _extract_meta_whatsapp_payload(raw_payload: object) -> dict[str, str]:
    if not isinstance(raw_payload, dict):
        return {}
    if "sample" in raw_payload and isinstance(raw_payload.get("sample"), dict):
        sample = raw_payload.get("sample") or {}
        value = sample.get("value") if isinstance(sample, dict) else None
        if isinstance(value, dict):
            calls = value.get("calls")
            first_call = calls[0] if isinstance(calls, list) and calls and isinstance(calls[0], dict) else {}
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            return {
                "eventType": str(first_call.get("event") or ""),
                "call_id": str(first_call.get("id") or ""),
                "to": str(first_call.get("to") or ""),
                "from": str(first_call.get("from") or ""),
                "timestamp": str(first_call.get("timestamp") or ""),
                "phone_number_id": str(metadata.get("phone_number_id") or ""),
                "display_phone_number": str(metadata.get("display_phone_number") or ""),
                "messaging_product": str(value.get("messaging_product") or ""),
            }
    return {str(key): str(value) for key, value in raw_payload.items() if value is not None}


def _extract_twilio_payload(form_data: Any) -> dict[str, str]:
    if not hasattr(form_data, "multi_items"):
        return {}
    return {str(key): str(value) for key, value in form_data.multi_items()}


class StartSessionRequest(BaseModel):
    client_id: str
    project_id: str | None = None
    customer_name: str
    contact_phone: str | None = None
    contact_email: str | None = None
    transport: str | None = None
    session_key: str | None = None


class ClientEditorRequest(BaseModel):
    config: dict
    system_prompt: str
    knowledge: str
    objections: dict
    qualification: dict
    cta: dict
    projects: list[dict[str, Any]] | None = None
    active_project_id: str | None = None
    import_metadata: dict[str, Any] | None = None


class ActiveProjectRequest(BaseModel):
    project_id: str


class AppSettingsUpdateRequest(BaseModel):
    telephony_max_concurrent_sessions: int


class ClientBuilderRequest(BaseModel):
    business_name: str = ""
    business_type: str = ""
    website: str = ""
    business_description: str = ""
    why_build_agent: str = ""
    agent_goal: str = ""
    project_name: str = ""
    call_direction: str = "inbound"
    target_audience: str = ""
    preferred_languages: str = ""
    services_products: str = ""
    service_area: str = ""
    voice_tone: str = ""
    pricing_notes: str = ""
    do_not_say: str = ""
    end_user_details: str = ""
    business_narrative: str = ""
    additional_notes: str = ""
    agent_type: str = "sales"


class StartPhoneCallRequest(BaseModel):
    client_id: str
    project_id: str | None = None
    customer_name: str
    to_number: str
    lead_source: str | None = None


class BatchCallLead(BaseModel):
    customer_name: str
    to_number: str


class BatchCallRequest(BaseModel):
    client_id: str
    project_id: str | None = None
    leads: list[BatchCallLead]
    lead_source: str | None = None
    max_concurrent_calls: int | None = None


class BatchLeadImportRequest(BaseModel):
    leads: list[dict[str, str]]


class AirtelIQActionRequest(BaseModel):
    pending_id: str
    action: str
    vm_session_id: str | None = None
    audio_url: str | None = None
    timeout: int | None = None
    max_digits: int | None = None


class MetaWhatsAppActionRequest(BaseModel):
    pending_id: str
    action: str
    to_number: str | None = None
    call_id: str | None = None
    sdp_type: str | None = None
    sdp: str | None = None


def _builder_slug(value: str, fallback: str = "custom") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return normalized[:48] or fallback


def _builder_list(value: str) -> list[str]:
    return [
        item
        for item in (part.strip() for part in re.split(r"[\n,;]+", str(value or "")))
        if item
    ]


def _builder_supported_languages(value: str) -> list[str]:
    normalized: list[str] = []
    for item in _builder_list(value):
        lang = item.strip().lower()
        if lang in {"marathi", "hindi", "english"} and lang not in normalized:
            normalized.append(lang)
    return normalized


def _builder_sentences(value: str) -> list[str]:
    return [
        item
        for item in (part.strip(" -\t") for part in re.split(r"[\n;]+", str(value or "")))
        if item
    ]


def _builder_primary_language(languages: list[str]) -> str | None:
    for item in languages:
        normalized = item.strip().lower()
        if normalized in {"marathi", "hindi", "english"}:
            return normalized
    return languages[0].strip().lower() if languages else None


def _builder_project_type(goal_text: str, direction_text: str) -> str:
    normalized = f"{goal_text} {direction_text}".lower()
    if "appointment" in normalized or "booking" in normalized:
        return "appointment_booking"
    if "qualif" in normalized:
        return "lead_qualification"
    if "follow" in normalized:
        return "followup"
    if any(token in normalized for token in {"sales", "sell", "lead", "enquiry", "inquiry", "outbound"}):
        return "sales"
    return "custom"


def _builder_project_type_from_agent_type(agent_type: str, fallback_goal: str, fallback_direction: str) -> str:
    normalized = str(agent_type or "").strip().lower()
    mapping = {
        "sales": "sales",
        "customer_support": "custom",
        "customer support": "custom",
        "appointment_booking": "appointment_booking",
        "appointment booking": "appointment_booking",
        "lead_qualification": "lead_qualification",
        "lead qualification": "lead_qualification",
        "followup": "followup",
        "follow-up": "followup",
        "follow up": "followup",
    }
    return mapping.get(normalized) or _builder_project_type(fallback_goal, fallback_direction)


def _builder_conversation_mode(project_type: str, goal_text: str) -> str:
    if project_type == "appointment_booking" or any(
        token in goal_text.lower() for token in {"appointment", "booking", "schedule"}
    ):
        return "appointment_booking"
    return "sales_discovery"


def _builder_opening_script(display_name: str, primary_language: str | None, goal_text: str) -> dict[str, str]:
    opening_goal = goal_text.strip() or "तुमच्या गरजेबद्दल"
    scripts = {
        "marathi": f"नमस्कार {{customer_name}}, मी {display_name} कडून बोलते आहे. {opening_goal} मला थोडक्यात सांगाल का?",
        "hindi": f"नमस्कार {{customer_name}}, मैं {display_name} से बोल रही हूँ। कृपया अपनी जरूरत के बारे में संक्षेप में बताएंगे?",
        "english": f"Hello {{customer_name}}, this is {display_name}. Could you briefly tell me what you need help with today?",
    }
    if primary_language in {"marathi", "hindi", "english"}:
        return {primary_language: scripts[primary_language], **{k: v for k, v in scripts.items() if k != primary_language}}
    return scripts


def _build_client_builder_payload(request: ClientBuilderRequest, workspace_client_id: str) -> dict[str, Any]:
    business_name = request.business_name.strip() or "New Business"
    project_name = request.project_name.strip() or f"{business_name} Agent"
    languages = _builder_supported_languages(request.preferred_languages) or ["marathi", "hindi", "english"]
    primary_language = _builder_primary_language(languages) or "marathi"
    services = _builder_list(request.services_products)
    locations = _builder_list(request.service_area)
    target_audience = _builder_list(request.target_audience)
    tone = _builder_list(request.voice_tone)
    pricing_points = _builder_sentences(request.pricing_notes)
    guardrails = _builder_sentences(request.do_not_say)
    end_user_details = _builder_sentences(request.end_user_details)
    additional_notes = _builder_sentences(request.additional_notes)
    project_type = _builder_project_type_from_agent_type(
        request.agent_type,
        request.agent_goal,
        request.call_direction,
    )
    conversation_mode = _builder_conversation_mode(project_type, request.agent_goal)
    normalized_direction = request.call_direction.strip().lower() or "inbound"
    project_id = _builder_slug(
        request.project_name or f"{business_name}_{project_type}_{normalized_direction}",
        fallback="custom_agent",
    )

    primary_offer = request.agent_goal.strip() or request.business_description.strip() or "AI voice agent workflow"
    description_parts = [
        request.business_description.strip(),
        request.why_build_agent.strip(),
        request.business_narrative.strip(),
    ]
    business_context = "\n".join(part for part in description_parts if part)

    knowledge_sections: list[str] = [
        "# Business Overview",
        "",
        f"{business_name} operates in {request.business_type.strip() or 'its stated business category'}.",
    ]
    if business_context:
        knowledge_sections.extend(["", business_context])
    if services:
        knowledge_sections.extend(["", "# Products / Services", ""])
        knowledge_sections.extend(f"- {item}" for item in services)
    if target_audience:
        knowledge_sections.extend(["", "# Target Audience", ""])
        knowledge_sections.extend(f"- {item}" for item in target_audience)
    if pricing_points:
        knowledge_sections.extend(["", "# Pricing / Offer Notes", ""])
        knowledge_sections.extend(f"- {item}" for item in pricing_points)
    if end_user_details:
        knowledge_sections.extend(["", "# End-User Details", ""])
        knowledge_sections.extend(f"- {item}" for item in end_user_details)
    if additional_notes:
        knowledge_sections.extend(["", "# Additional Notes", ""])
        knowledge_sections.extend(f"- {item}" for item in additional_notes)

    system_prompt = "\n".join(
        part
        for part in [
            f"You represent {business_name}.",
            "",
            f"Business type: {request.business_type.strip() or 'general business'}.",
            f"Primary goal: {request.agent_goal.strip() or 'handle customer conversations professionally'}.",
            f"Direction: {normalized_direction}.",
            "",
            "Preferred style:",
            "- Sound clear, practical, and helpful.",
            f"- Start in {primary_language} unless the customer clearly prefers another supported language.",
            "- Ask one clear question at a time.",
            "- Use only approved business, pricing, and policy details from knowledge.",
            "- Capture the next step cleanly before closing.",
            "",
            "Guardrails:",
            *([f"- {item}" for item in guardrails] or ["- Do not invent pricing, promises, or policies not present in the approved knowledge."]),
        ]
    )

    qualification_fields = {
        "fit": "What is the caller trying to achieve or buy?",
        "timing": "How soon do they want this handled?",
        "contact": "What is the best callback or follow-up path?",
        "scope": "What size, type, service, or use-case details matter most?",
    }
    if project_type == "appointment_booking":
        qualification_fields["availability"] = "Which day or time works best for them?"

    prompt_instruction_parts = [
        f"Run this workflow for {business_name}.",
        f"Business type: {request.business_type.strip() or 'general business'}.",
        f"Goal: {request.agent_goal.strip() or 'assist the caller and capture the next step'}.",
        f"Direction: {normalized_direction}.",
    ]
    if request.why_build_agent.strip():
        prompt_instruction_parts.append(f"Why this agent exists: {request.why_build_agent.strip()}.")
    if services:
        prompt_instruction_parts.append(f"Focus areas: {', '.join(services)}.")
    if pricing_points:
        prompt_instruction_parts.append("Use the approved pricing notes where relevant, but do not invent missing commercial details.")
    if guardrails:
        prompt_instruction_parts.append("Respect the restricted claims and do not say anything beyond approved details.")
    prompt_instruction_parts.append("Keep the conversation natural, short, and action-oriented.")

    payload = {
        "client_id": workspace_client_id,
        "config": {
            "client_id": workspace_client_id,
            "display_name": business_name,
            "industry": request.business_type.strip() or "Custom business workflow",
            "status": "draft",
            "website": request.website.strip() or None,
            "primary_offer": primary_offer,
            "target_audience": target_audience,
            "tone": tone,
            "languages_supported": languages,
            "services": services,
            "locations": locations,
            "tags": [
                _builder_slug(request.business_type, fallback="business"),
                _builder_slug(normalized_direction, fallback="workflow"),
                "user_workspace",
            ],
            "conversation_mode": conversation_mode,
            "workflow_mode": "model_led",
            "closure_mode": "appointment_booking" if project_type == "appointment_booking" else "default",
            "default_opening_language": primary_language,
            "opening_script": _builder_opening_script(business_name, primary_language, request.agent_goal),
            "closure_examples": {
                "positive": [
                    "Thank you. We have captured your requirement and the team can follow up on the details shared.",
                ],
                "negative": [],
                "non_closing_questions": [],
                "guidance": "Close by confirming the need captured and the next follow-up step.",
            },
            "disallowed_claims": guardrails or [
                "Do not promise exact pricing, timelines, or policies unless they are explicitly provided in approved knowledge."
            ],
            "allowed_contact_fields": ["name", "email", "phone"],
            "booking_rules": {},
            "handoff_rules": {},
            "intent_overrides": {},
            "import_metadata": {
                "schema_version": 2,
                "source": "client_builder_ui",
                "external_id": None,
                "imported_at": None,
                "imported_from": workspace_client_id,
                "notes": [
                    "Generated from the dashboard client builder.",
                ],
            },
            "voice": {
                "voice_name": "Kore",
                "speaking_rate": 1.0,
            },
            "live_generation": {
                "temperature": 0.7,
                "top_p": 0.9,
                "top_k": 32,
                "max_output_tokens": 2000,
            },
            "structured_generation": {
                "temperature": 0.2,
                "top_p": 0.8,
                "top_k": 20,
                "max_output_tokens": 2000,
            },
        },
        "system_prompt": system_prompt,
        "knowledge": "\n".join(knowledge_sections).strip(),
        "objections": {
            "common": [
                {
                    "objection": "I just want the details quickly.",
                    "response": "Share the concise approved details, then confirm the one most important requirement so the follow-up is accurate.",
                },
                {
                    "objection": "Send this on WhatsApp or email.",
                    "response": "Confirm the best contact detail and capture any final requirement so the follow-up message is useful.",
                },
            ]
        },
        "qualification": {
            "fields": qualification_fields,
        },
        "cta": {
            "primary_cta": "Confirm the next business step with the customer",
            "fallback_cta": "Capture contact details and the key requirement for follow-up",
            "success_criteria": [
                "The user's core need is captured",
                "A follow-up path is identified",
                "Important business or service details are recorded",
            ],
        },
        "projects": [
            {
                "project_id": project_id,
                "name": project_name,
                "project_type": project_type,
                "status": "active",
                "description": request.agent_goal.strip() or f"{business_name} voice agent workflow.",
                "prompt_instruction": " ".join(prompt_instruction_parts),
                "runtime": {
                    "gemini_api_key_env": None,
                    "live_model": None,
                    "structured_model": None,
                    "tts_model": None,
                    "outreach_mode": "call_only",
                    "outbound_call_provider": settings.telephony_provider,
                    "whatsapp_consent_message": None,
                    "whatsapp_chat_opening_message": None,
                },
                "intent_overrides": {},
            }
        ],
        "active_project_id": project_id,
        "summary": {
            "display_name": business_name,
            "project_name": project_name,
            "project_type": project_type,
            "direction": normalized_direction,
            "primary_language": primary_language,
        },
    }
    return payload


class BrowserAudioBridge:
    """Bridge browser microphone/audio over WebSocket to the session audio interface."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self._incoming_audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._playback_active = False
        self._playback_idle = asyncio.Event()
        self._playback_idle.set()
        self._playback_clear_task: asyncio.Task[None] | None = None
        self._incoming_chunk_count = 0

    def open(self) -> None:
        """WebSocket transport is already open."""
        return

    async def handle_ws_message(self, message: dict[str, Any]) -> None:
        event = str(message.get("event") or "").strip().lower()
        if event == "audio_chunk":
            payload = str(message.get("payload") or "").strip()
            if not payload:
                return
            sample_rate = int(message.get("sample_rate") or INPUT_SAMPLE_RATE)
            raw = base64.b64decode(payload)
            if sample_rate != INPUT_SAMPLE_RATE:
                raw = _resample_linear(raw, sample_rate, INPUT_SAMPLE_RATE)
            if raw:
                self._incoming_chunk_count += 1
                if self._incoming_chunk_count == 1:
                    logger.info(
                        "Browser audio bridge received first mic chunk bytes=%s sample_rate=%s",
                        len(raw),
                        sample_rate,
                    )
                    async with self._send_lock:
                        await self.websocket.send_text(
                            json.dumps(
                                {
                                    "event": "mic_chunk_received",
                                    "sample_rate": sample_rate,
                                    "bytes": len(raw),
                                }
                            )
                        )
                await self._incoming_audio.put(raw)
            return
        if event == "stop":
            await self._incoming_audio.put(None)
            self._closed = True
            return

    async def mic_chunks(self):
        while True:
            chunk = await self._incoming_audio.get()
            if chunk is None:
                return
            yield chunk

    async def play(self, audio_bytes: bytes) -> None:
        if self._closed or not audio_bytes:
            return
        self._playback_active = True
        self._playback_idle.clear()
        async with self._send_lock:
            await self.websocket.send_text(
                json.dumps(
                    {
                        "event": "audio_chunk",
                        "sample_rate": OUTPUT_SAMPLE_RATE,
                        "payload": base64.b64encode(audio_bytes).decode("ascii"),
                    }
                )
            )
        if self._playback_clear_task is not None:
            self._playback_clear_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._playback_clear_task
        duration_seconds = max(0.08, len(audio_bytes) / (2 * OUTPUT_SAMPLE_RATE))
        self._playback_clear_task = asyncio.create_task(self._clear_playback_after(duration_seconds))

    async def _clear_playback_after(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        self._playback_active = False
        self._playback_idle.set()

    async def flush_playback(self) -> None:
        self._playback_active = False
        self._playback_idle.set()
        if self._closed:
            return
        async with self._send_lock:
            await self.websocket.send_text(json.dumps({"event": "flush"}))

    def is_playing(self) -> bool:
        return self._playback_active

    def should_drop_input_while_playing(self) -> bool:
        # Browser sessions rely on the client's echo cancellation and server-side
        # turn detection. Dropping all input during playback can swallow the
        # user's first response right after the greeting.
        return False

    async def wait_for_playback_idle(self) -> None:
        await self._playback_idle.wait()

    async def set_processing_ambience(self, enabled: bool) -> None:
        return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._playback_clear_task is not None:
            self._playback_clear_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._playback_clear_task
            self._playback_clear_task = None
        self._playback_active = False
        self._playback_idle.set()
        await self._incoming_audio.put(None)
        with contextlib.suppress(Exception):
            await self.websocket.close()


def _meta_whatsapp_signature_valid(settings: AppSettings, body: bytes, signature_header: str | None) -> bool:
    app_secret = settings.meta_whatsapp_app_secret or ""
    if not app_secret:
        return True
    if not signature_header:
        return False
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    provided = signature_header[len(prefix) :].strip()
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


def _extract_meta_whatsapp_call_events(raw_payload: object) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    if not isinstance(raw_payload, dict):
        return events

    # Sample payload support used in dashboard/webhook test tools.
    if "sample" in raw_payload and isinstance(raw_payload.get("sample"), dict):
        sample_payload = _extract_meta_whatsapp_payload(raw_payload)
        if sample_payload:
            events.append(sample_payload)
        return events

    entries = raw_payload.get("entry")
    if not isinstance(entries, list):
        return events

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            calls = value.get("calls")
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    events.append(
                        {
                            "eventType": str(call.get("event") or call.get("status") or ""),
                            "callStatus": str(call.get("status") or ""),
                            "call_id": str(call.get("id") or ""),
                            "to": str(call.get("to") or ""),
                            "from": str(call.get("from") or ""),
                            "timestamp": str(call.get("timestamp") or ""),
                            "phone_number_id": str(metadata.get("phone_number_id") or ""),
                            "display_phone_number": str(metadata.get("display_phone_number") or ""),
                            "messaging_product": str(value.get("messaging_product") or ""),
                            "pending_id": str(
                                call.get("pending_id")
                                or call.get("biz_opaque_callback_data")
                                or call.get("client_ref")
                                or ""
                            ),
                            "session_sdp_type": str(
                                (call.get("session") or {}).get("sdp_type")
                                if isinstance(call.get("session"), dict)
                                else ""
                            ),
                            "session_sdp": str(
                                (call.get("session") or {}).get("sdp")
                                if isinstance(call.get("session"), dict)
                                else ""
                            ),
                            "call_errors": json.dumps(call.get("errors"), ensure_ascii=True)
                            if isinstance(call.get("errors"), list)
                            else "",
                            "raw_call": json.dumps(call, ensure_ascii=True),
                        }
                    )
            messages = value.get("messages")
            contacts = value.get("contacts")
            if isinstance(messages, list):
                contact_wa_id = ""
                if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict):
                    contact_wa_id = str(contacts[0].get("wa_id") or "")
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    text_obj = message.get("text") if isinstance(message.get("text"), dict) else {}
                    events.append(
                        {
                            "eventType": "inbound_message",
                            "message_id": str(message.get("id") or ""),
                            "message_type": str(message.get("type") or ""),
                            "message_text": str(text_obj.get("body") or ""),
                            "from": str(message.get("from") or contact_wa_id or ""),
                            "to": str(metadata.get("display_phone_number") or ""),
                            "timestamp": str(message.get("timestamp") or ""),
                            "phone_number_id": str(metadata.get("phone_number_id") or ""),
                            "display_phone_number": str(metadata.get("display_phone_number") or ""),
                            "messaging_product": str(value.get("messaging_product") or ""),
                        }
                    )
    return events


def _normalize_phone_for_match(value: str | None) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def _is_guest_demo_workspace_client(client_id: str | None) -> bool:
    normalized = str(client_id or "").strip()
    return normalized.startswith("user_guest") or normalized == "aivoicebot4u_guest_demo"


def _validate_guest_demo_contact(
    *,
    customer_name: str,
    contact_phone: str | None,
    contact_email: str | None,
) -> dict[str, str]:
    normalized_name = " ".join(str(customer_name or "").split()).strip()
    normalized_phone = "".join(ch for ch in str(contact_phone or "").strip() if ch in "+0123456789")
    normalized_email = str(contact_email or "").strip().lower()
    if len(normalized_name) < 2:
        raise ValueError("Enter your full name before starting the demo.")
    if not re.fullmatch(r"\+?\d{7,15}", normalized_phone):
        raise ValueError("Enter a valid phone number before starting the demo.")
    if normalized_phone == "+10000000000":
        raise ValueError("Enter your real phone number before starting the demo.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized_email):
        raise ValueError("Enter a valid email address before starting the demo.")
    if normalized_email.endswith("@aivoicebot4u.local"):
        raise ValueError("Enter your real email address before starting the demo.")
    return {
        "name": normalized_name,
        "phone": normalized_phone,
        "email": normalized_email,
    }


def _is_affirmative_reply(text: str) -> bool:
    normalized = " ".join((text or "").lower().split())
    yes_tokens = {
        "yes",
        "y",
        "ok",
        "okay",
        "haan",
        "ha",
        "han",
        "h",
        "ji",
        "sure",
        "call",
        "करो",
        "हाँ",
        "हां",
        "जी",
        "हो",
    }
    return normalized in yes_tokens or normalized.startswith("yes ") or normalized.startswith("haan ")


def _is_negative_reply(text: str) -> bool:
    normalized = " ".join((text or "").lower().split())
    no_tokens = {
        "no",
        "n",
        "nah",
        "nahi",
        "nahin",
        "ना",
        "नहीं",
        "मत",
    }
    return normalized in no_tokens or normalized.startswith("no ") or normalized.startswith("nahi ")


def _is_chat_closing_turn(user_text: str, agent_text: str) -> bool:
    user_normalized = " ".join((user_text or "").lower().split())
    agent_normalized = " ".join((agent_text or "").lower().split())
    if _is_negative_reply(user_text):
        return True
    closing_user_tokens = {
        "thanks",
        "thank you",
        "thx",
        "ok thanks",
        "thanku",
        "dhanyavad",
        "shukriya",
        "bas",
        "enough",
        "done",
    }
    if user_normalized in closing_user_tokens:
        return True
    return (
        "thank" in agent_normalized
        and (
            "call you back" in agent_normalized
            or "we will call" in agent_normalized
            or "good day" in agent_normalized
            or "take care" in agent_normalized
            or "bye" in agent_normalized
        )
    )


def _sanitize_meta_sdp_offer(raw_sdp: str) -> str:
    lines = [line.strip() for line in raw_sdp.replace("\r\n", "\n").split("\n") if line.strip()]
    sanitized: list[str] = []
    saw_sha256_fingerprint = False
    for line in lines:
        if line.startswith("c=IN IP6 "):
            sanitized.append("c=IN IP4 0.0.0.0")
            continue
        if line.startswith("a=candidate:"):
            candidate_parts = line.split()
            if len(candidate_parts) >= 6 and ":" in candidate_parts[4]:
                continue
        if line.startswith("a=fingerprint:"):
            if line.startswith("a=fingerprint:sha-256 "):
                if saw_sha256_fingerprint:
                    continue
                saw_sha256_fingerprint = True
                sanitized.append(line)
            continue
        sanitized.append(line)
    return "\r\n".join(sanitized) + "\r\n"


def _lead_source_catalog() -> list[dict[str, str | bool]]:
    return [
        {
            "id": "manual_single",
            "label": "Manual Single Number",
            "description": "Best for testing from the UI. Enter one name and one phone number and place the call immediately.",
            "status": "ready",
            "supports_direct_call": True,
            "fields": [
                {"id": "customer_name", "label": "Customer Name", "type": "text", "required": True},
                {"id": "to_number", "label": "Mobile Number", "type": "tel", "required": True},
            ],
        },
        {
            "id": "meta",
            "label": "Meta Lead Ads",
            "description": "Planned connector for pulling lead name and phone details from Meta forms after credentials are configured.",
            "status": "planned",
            "supports_direct_call": False,
            "fields": [
                {"id": "access_token", "label": "Access Token", "type": "password", "required": True},
                {"id": "form_id", "label": "Form ID", "type": "text", "required": True},
                {"id": "page_id", "label": "Page ID", "type": "text", "required": False},
            ],
        },
        {
            "id": "youtube",
            "label": "YouTube Leads",
            "description": "Planned connector for leads collected from YouTube campaigns or landing-form workflows.",
            "status": "planned",
            "supports_direct_call": False,
            "fields": [
                {"id": "campaign_id", "label": "Campaign ID", "type": "text", "required": True},
                {"id": "sheet_url", "label": "Lead Sheet URL", "type": "url", "required": False},
            ],
        },
        {
            "id": "indiamart",
            "label": "IndiaMART",
            "description": "Planned connector for buyer enquiry leads from IndiaMART once API/export access is configured.",
            "status": "planned",
            "supports_direct_call": False,
            "fields": [
                {"id": "api_key", "label": "API Key", "type": "password", "required": True},
                {"id": "glusr_crm_key", "label": "GLUSR CRM Key", "type": "text", "required": True},
            ],
        },
        {
            "id": "justdial",
            "label": "Justdial",
            "description": "Planned connector for Justdial lead ingestion with name and phone number mapping.",
            "status": "planned",
            "supports_direct_call": False,
            "fields": [
                {"id": "vendor_code", "label": "Vendor Code", "type": "text", "required": True},
                {"id": "email", "label": "Registered Email", "type": "email", "required": False},
            ],
        },
        {
            "id": "excel",
            "label": "Excel Upload",
            "description": "Planned batch import path for name and mobile columns from Excel/CSV sheets.",
            "status": "ready",
            "supports_direct_call": False,
            "fields": [
                {"id": "file", "label": "Upload File", "type": "file", "required": True},
            ],
        },
    ]


def _source_by_id(source_id: str) -> dict[str, object] | None:
    for source in _lead_source_catalog():
        if source["id"] == source_id:
            return source
    return None


def _normalize_lead_name(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_phone_number(value: object) -> str:
    return "".join(ch for ch in str(value or "").strip() if ch in "+0123456789")


def _extract_rows_from_csv(contents: bytes) -> list[dict[str, str]]:
    text = contents.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [{str(key): str(value or "") for key, value in row.items()} for row in reader]


def _extract_rows_from_excel(contents: bytes) -> list[dict[str, str]]:
    workbook = load_workbook(io.BytesIO(contents), read_only=True, data_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(cell or "").strip() for cell in rows[0]]
    normalized_rows: list[dict[str, str]] = []
    for row in rows[1:]:
        normalized_rows.append(
            {
                headers[index]: str(value or "")
                for index, value in enumerate(row)
                if index < len(headers) and headers[index]
            }
        )
    return normalized_rows


def _extract_leads_from_tabular_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not rows:
        return []

    def pick(row: dict[str, str], candidates: tuple[str, ...]) -> str:
        lowered = {str(key).strip().lower(): str(value or "").strip() for key, value in row.items()}
        for candidate in candidates:
            if candidate in lowered and lowered[candidate]:
                return lowered[candidate]
        return ""

    leads: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        name = _normalize_lead_name(
            pick(row, ("name", "full name", "customer name", "lead name", "prospect name"))
        )
        phone = _normalize_phone_number(
            pick(row, ("phone", "mobile", "phone number", "mobile number", "number", "contact number"))
        )
        if not phone:
            continue
        leads.append(
            {
                "row_number": str(index),
                "customer_name": name or f"Lead {index}",
                "to_number": phone,
            }
        )
    return leads


class SessionController:
    """Multi-session controller keyed by session id/call id."""

    def __init__(self, settings: AppSettings, on_session_finished=None) -> None:
        self.settings = settings
        self._max_concurrent_sessions = max(1, int(settings.telephony_max_concurrent_sessions))
        self._on_session_finished = on_session_finished
        self.state = DashboardState()
        self._states: dict[str, DashboardState] = {"dashboard": self.state}
        self._sessions: dict[str, VoiceSalesSession] = {}
        self._tasks: dict[str, asyncio.Task[Path]] = {}
        self._alias_to_primary: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _resolve_session_key(self, session_key: str) -> str:
        resolved = self._alias_to_primary.get(session_key, session_key)
        # Guard against accidental loops from future refactors.
        if resolved == session_key:
            return resolved
        return self._alias_to_primary.get(resolved, resolved)

    async def start(
        self,
        client_id: str,
        customer_name: str,
        project_id: str | None = None,
        contact_details: dict[str, str] | None = None,
        session_key: str = "dashboard",
        session_id: str | None = None,
    ) -> None:
        await self.start_with_audio(
            client_id,
            customer_name,
            project_id=project_id,
            contact_details=contact_details,
            audio=None,
            telephony_context=None,
            defer_initial_prompt=False,
            session_key=session_key,
            session_id=session_id,
        )

    async def start_with_audio(
        self,
        client_id: str,
        customer_name: str,
        audio,
        project_id: str | None = None,
        contact_details: dict[str, str] | None = None,
        telephony_context: dict | None = None,
        defer_initial_prompt: bool = False,
        session_key: str = "dashboard",
        session_aliases: list[str] | None = None,
        session_id: str | None = None,
    ) -> None:
        async with self._lock:
            primary_key = self._resolve_session_key(session_key)
            existing_task = self._tasks.get(primary_key)
            if existing_task is not None and not existing_task.done():
                raise RuntimeError("A session is already running.")
            active_sessions = sum(
                1
                for task in self._tasks.values()
                if task is not None and not task.done()
            )
            if active_sessions >= self._max_concurrent_sessions:
                raise RuntimeError(
                    f"Max concurrent sessions reached ({self._max_concurrent_sessions})."
                )
            state = self._states.setdefault(primary_key, DashboardState())
            await state.reset_for_start(client_id, customer_name, project_id=project_id)
            session = VoiceSalesSession(
                settings=self.settings,
                client_id=client_id,
                project_id=project_id,
                customer_name=customer_name,
                contact_details=contact_details,
                event_handler=state.apply,
                audio=audio,
                telephony_context=telephony_context,
                defer_initial_prompt=defer_initial_prompt,
                session_id=session_id,
            )
            state.session_id = session.session_id
            task = asyncio.create_task(session.run())
            self._sessions[primary_key] = session
            self._tasks[primary_key] = task
            for alias in session_aliases or []:
                normalized_alias = str(alias or "").strip()
                if not normalized_alias or normalized_alias == primary_key:
                    continue
                self._states[normalized_alias] = state
                self._alias_to_primary[normalized_alias] = primary_key
            task.add_done_callback(
                lambda completed_task, key=primary_key: asyncio.create_task(self._clear_finished_task(key, completed_task))
            )

    async def release_initial_prompt(self, session_key: str = "dashboard") -> None:
        async with self._lock:
            primary_key = self._resolve_session_key(session_key)
            session = self._sessions.get(primary_key)
            if session is not None:
                session.release_initial_prompt()

    async def stop(self, session_key: str = "dashboard") -> None:
        task: asyncio.Task[Path] | None
        async with self._lock:
            primary_key = self._resolve_session_key(session_key)
            session = self._sessions.get(primary_key)
            if session is not None:
                session.stop()
            task = self._tasks.get(primary_key)
        if task is not None:
            try:
                # Keep finalization alive after the 15s API wait window so
                # post-call corpus extraction can still complete.
                await asyncio.wait_for(asyncio.shield(task), timeout=15)
            except TimeoutError:
                logger.warning(
                    "Session finalize exceeded 15s; cancelling stale task so a new session can start session_key=%s",
                    session_key,
                )
                task.cancel()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except Exception:
                logger.exception("Session task raised while stopping session_key=%s", session_key)
        if task is None or task.done():
            await self._clear_finished_task(primary_key, task)
        else:
            await self._force_clear_session(primary_key, task)

    async def merge_telephony_context(
        self,
        session_key: str,
        updates: dict[str, str | float | int | bool | None] | None,
    ) -> None:
        if not updates:
            return
        async with self._lock:
            primary_key = self._resolve_session_key(session_key)
            session = self._sessions.get(primary_key)
            if session is None:
                return
            existing = session.telephony_context if isinstance(session.telephony_context, dict) else {}
            merged: dict[str, Any] = dict(existing)
            for key, value in updates.items():
                if value is None:
                    continue
                merged[str(key)] = value
            session.telephony_context = merged
            session.artifacts.telephony_context = dict(merged)

    async def merge_piopiy_recording(
        self,
        session_key: str,
        updates: dict[str, Any] | None,
    ) -> None:
        if not updates:
            return
        async with self._lock:
            primary_key = self._resolve_session_key(session_key)
            session = self._sessions.get(primary_key)
            if session is None:
                return
            for key, value in updates.items():
                if value is None:
                    continue
                if hasattr(session.artifacts, str(key)):
                    setattr(session.artifacts, str(key), value)
            existing = session.telephony_context if isinstance(session.telephony_context, dict) else {}
            merged: dict[str, Any] = dict(existing)
            for key, value in updates.items():
                if value is None:
                    continue
                merged[str(key)] = value
            session.telephony_context = merged
            session.artifacts.telephony_context = dict(merged)

    async def snapshot(self, session_key: str = "dashboard") -> dict:
        async with self._lock:
            state = self._states.get(session_key)
            if state is None:
                state = self._states.get(self._resolve_session_key(session_key))
            state = state or self.state
        return await state.snapshot()

    async def _clear_finished_task(self, session_key: str, task: asyncio.Task[Path] | None) -> None:
        async with self._lock:
            tracked = self._tasks.get(session_key)
            if task is not None and tracked is not task:
                return
            if tracked is not None and not tracked.done():
                return
            session = self._sessions.get(session_key)
            self._tasks.pop(session_key, None)
            self._sessions.pop(session_key, None)
            aliases = [alias for alias, primary in self._alias_to_primary.items() if primary == session_key]
            for alias in aliases:
                self._alias_to_primary.pop(alias, None)
        if session is not None and self._on_session_finished is not None:
            maybe_result = self._on_session_finished(session)
            if asyncio.iscoroutine(maybe_result):
                await maybe_result

    async def _force_clear_session(self, session_key: str, task: asyncio.Task[Path] | None) -> None:
        """Drop a stale task reference after stop() has already been requested.

        This prevents a hung shutdown from blocking future starts in the same
        browser session key. The task is cancelled first, then the controller
        forgets it so the UI can recover.
        """
        async with self._lock:
            tracked = self._tasks.get(session_key)
            if task is not None and tracked is not task:
                return
            self._tasks.pop(session_key, None)
            self._sessions.pop(session_key, None)
            aliases = [alias for alias, primary in self._alias_to_primary.items() if primary == session_key]
            for alias in aliases:
                self._alias_to_primary.pop(alias, None)
            state = self._states.get(session_key)
            if state is not None:
                state.running = False
                state.status = "finished"
                state.detail = "Session stopped."
        logger.warning("Force-cleared stale session_key=%s after shutdown timeout.", session_key)

    async def is_busy(self, session_key: str | None = None) -> bool:
        async with self._lock:
            if session_key is not None:
                task = self._tasks.get(self._resolve_session_key(session_key))
                return task is not None and not task.done()
            return any(task is not None and not task.done() for task in self._tasks.values())

    async def update_max_concurrent_sessions(self, value: int) -> int:
        normalized = max(1, int(value))
        async with self._lock:
            self._max_concurrent_sessions = normalized
        return normalized

    async def max_concurrent_sessions(self) -> int:
        async with self._lock:
            return self._max_concurrent_sessions


class TelephonyController:
    """Manage pending outbound calls and bridge them into the session engine."""

    def __init__(self, settings: AppSettings, session_controller: SessionController) -> None:
        self.settings = settings
        self.session_controller = session_controller
        self.twilio = TwilioCallClient(settings)
        self.exotel = ExotelCallClient(settings)
        self.airtel_iq = AirtelIQCallClient(settings)
        self.meta_whatsapp = MetaWhatsAppCallClient(settings)
        self.piopiy = PiopiyCallClient(settings)
        self._meta_peer_connections: dict[str, Any] = {}
        self._meta_media_bridges: dict[str, MetaWhatsAppMediaBridge] = {}
        if settings.shared_state_backend == "redis" or (
            settings.shared_state_backend == "auto" and settings.redis_url
        ):
            if not settings.redis_url:
                raise RuntimeError("REDIS_URL is required when SHARED_STATE_BACKEND=redis.")
            self._store = RedisCallStateStore(
                redis_url=settings.redis_url,
                prefix=settings.redis_prefix,
                call_state_ttl_seconds=settings.call_state_ttl_seconds,
            )
            logger.info("Using Redis call-state store: prefix=%s", settings.redis_prefix)
        else:
            self._store = InMemoryCallStateStore(call_state_ttl_seconds=settings.call_state_ttl_seconds)
            logger.info("Using in-memory call-state store.")

    @staticmethod
    def _serialize_pending_call(pending_call: PendingCall) -> dict[str, Any]:
        return {
            "session_id": pending_call.session_id,
            "client_id": pending_call.client_id,
            "customer_name": pending_call.customer_name,
            "to_number": pending_call.to_number,
            "provider": pending_call.provider,
            "provider_call_sid": pending_call.provider_call_sid,
            "metadata": pending_call.metadata or {},
        }

    @staticmethod
    def _deserialize_pending_call(payload: dict[str, Any] | None) -> PendingCall | None:
        if payload is None:
            return None
        return PendingCall(
            session_id=str(payload.get("session_id", "")),
            client_id=str(payload.get("client_id", "")),
            customer_name=str(payload.get("customer_name", "")),
            to_number=str(payload.get("to_number", "")),
            provider=str(payload.get("provider", "")),
            provider_call_sid=str(payload.get("provider_call_sid") or "").strip() or None,
            metadata=(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}) or {},
        )

    async def create_inbound_call(
        self,
        *,
        provider: str,
        client_id: str,
        customer_name: str,
        from_number: str,
        to_number: str,
        project_id: str | None = None,
        provider_call_sid: str | None = None,
        lead_source: str = "twilio_inbound",
        context_metadata: dict[str, str | float | int | bool | None] | None = None,
    ) -> tuple[str, PendingCall]:
        client_bundle = load_client(client_id, project_id=project_id)
        active_project = client_bundle.active_project
        pending_id = uuid4().hex
        metadata: dict[str, str | float | int | bool | None] = {
            "provider": provider,
            "direction": "inbound",
            "client_id": client_id,
            "project_id": active_project.project_id if active_project else project_id,
            "project_name": active_project.name if active_project else None,
            "customer_name": customer_name,
            "from_number": from_number,
            "to_number": to_number,
            "lead_source": lead_source,
            "call_status": "in_progress",
            "call_requested_at_epoch": time.time(),
            "provider_call_sid": provider_call_sid,
        }
        if context_metadata:
            for key, value in context_metadata.items():
                if value is None:
                    continue
                metadata[str(key)] = value
        pending_call = PendingCall(
            session_id=pending_id,
            client_id=client_id,
            customer_name=customer_name,
            to_number=to_number,
            provider=provider,
            provider_call_sid=provider_call_sid,
            metadata=metadata,
        )
        await self._store.put_call(
            pending_id=pending_id,
            pending_payload=self._serialize_pending_call(pending_call),
            context=metadata,
        )
        if provider_call_sid:
            await self.update_pending_call_provider_sid(pending_id, provider_call_sid)
        record_leads(
            client_id=client_id,
            source=lead_source,
            leads=[{"customer_name": customer_name, "to_number": from_number}],
            metadata={
                "provider": provider,
                "direction": "inbound",
                "project_id": active_project.project_id if active_project else project_id,
                "project_name": active_project.name if active_project else "",
                "called_number": to_number,
                "provider_call_sid": provider_call_sid,
            },
        )
        return pending_id, pending_call

    async def create_outbound_call(
        self,
        client_id: str,
        customer_name: str,
        to_number: str,
        project_id: str | None = None,
        lead_source: str = "manual_single",
        context_metadata: dict[str, str | float | int | bool | None] | None = None,
    ) -> dict[str, str]:
        client_bundle = load_client(client_id, project_id=project_id)
        active_project = client_bundle.active_project
        runtime = active_project.runtime if active_project is not None else None
        outreach_mode = (runtime.outreach_mode if runtime is not None else "call_only") or "call_only"
        selected_provider = (
            (runtime.outbound_call_provider if runtime is not None else None)
            or self.settings.telephony_provider
        )
        provider = selected_provider if outreach_mode != "chat_only" else "meta_whatsapp"
        self._ensure_provider_configured(provider)
        pending_id = uuid4().hex
        metadata: dict[str, str | float | int | bool | None] = {
            "provider": provider,
            "outreach_mode": outreach_mode,
            "selected_provider": selected_provider,
            "client_id": client_id,
            "project_id": active_project.project_id if active_project else None,
            "project_name": active_project.name if active_project else None,
            "customer_name": customer_name,
            "to_number": to_number,
            "lead_source": lead_source,
            "call_status": "queued",
            "call_requested_at_epoch": time.time(),
        }
        if context_metadata:
            for key, value in context_metadata.items():
                if value is None:
                    continue
                metadata[str(key)] = value
        pending_call = PendingCall(
            session_id=pending_id,
            client_id=client_id,
            customer_name=customer_name,
            to_number=to_number,
            provider=provider,
            metadata=metadata,
        )
        await self._store.put_call(
            pending_id=pending_id,
            pending_payload=self._serialize_pending_call(pending_call),
            context=metadata,
        )
        record_leads(
            client_id=client_id,
            source=lead_source,
            leads=[{"customer_name": customer_name, "to_number": to_number}],
            metadata={
                "provider": provider,
                "project_id": active_project.project_id if active_project else None,
                "project_name": active_project.name if active_project else None,
            },
        )

        if outreach_mode == "chat_only":
            message = (
                (runtime.whatsapp_chat_opening_message if runtime is not None else None)
                or f"Hi {customer_name}, we are here on WhatsApp to help. How can we assist you today?"
            )
            response = await self.meta_whatsapp.send_text_message(to_number=to_number, body_text=message)
            await self.update_call_context(
                pending_id,
                {
                    "call_status": "chat_started",
                    "meta_whatsapp_message_sid": str(response.get("sid", "")).strip() or None,
                },
            )
            return {
                "provider": "meta_whatsapp",
                "status": "chat_started",
                "call_sid": "",
                "pending_id": pending_id,
                "to_number": to_number,
                "lead_source": lead_source,
                "project_id": active_project.project_id if active_project else "",
                "project_name": active_project.name if active_project else "",
            }

        if outreach_mode == "consent_then_call":
            consent_message = (
                (runtime.whatsapp_consent_message if runtime is not None else None)
                or "Can we call you now? Please reply YES or NO."
            )
            await self.meta_whatsapp.send_text_message(to_number=to_number, body_text=consent_message)
            await self.update_call_context(
                pending_id,
                {
                    "provider": "meta_whatsapp_consent",
                    "call_status": "awaiting_consent",
                    "consent_status": "pending",
                    "target_call_provider": selected_provider,
                },
            )
            pending_payload = await self._store.get_call(pending_id)
            if pending_payload is not None:
                pending_payload["provider"] = "meta_whatsapp_consent"
                pending_metadata = pending_payload.get("metadata")
                if isinstance(pending_metadata, dict):
                    pending_metadata["provider"] = "meta_whatsapp_consent"
                    pending_metadata["consent_status"] = "pending"
                    pending_metadata["target_call_provider"] = selected_provider
                await self._store.set_call(pending_id, pending_payload)
            return {
                "provider": "meta_whatsapp",
                "status": "awaiting_consent",
                "call_sid": "",
                "pending_id": pending_id,
                "to_number": to_number,
                "lead_source": lead_source,
                "project_id": active_project.project_id if active_project else "",
                "project_name": active_project.name if active_project else "",
            }

        try:
            call = await self._place_provider_call(
                provider=provider,
                pending_id=pending_id,
                client_id=client_id,
                project_id=active_project.project_id if active_project else None,
                to_number=to_number,
                runtime=runtime,
            )
        except Exception:
            await self._store.pop_call(pending_id)
            raise

        await self.update_call_context(
            pending_id,
            {
                "provider_call_sid": str(call.get("sid", "")).strip() or None,
                "call_status": str(call.get("status", "")).strip() or "queued",
            },
        )

        return {
            "provider": provider,
            "status": str(call.get("status", "queued")),
            "call_sid": str(call.get("sid", "")),
            "pending_id": pending_id,
            "to_number": to_number,
            "lead_source": lead_source,
            "project_id": active_project.project_id if active_project else "",
            "project_name": active_project.name if active_project else "",
        }

    def _ensure_provider_configured(self, provider: str) -> None:
        if provider == "exotel":
            self.exotel.ensure_configured()
            return
        if provider == "airtel_iq":
            self.airtel_iq.ensure_configured()
            return
        if provider == "meta_whatsapp":
            self.meta_whatsapp.ensure_configured()
            return
        if provider == "piopiy":
            self.piopiy.ensure_configured()
            return
        if provider == "twilio":
            self.twilio.ensure_configured()
            return
        raise RuntimeError(f"Unsupported provider '{provider}'.")

    async def _place_provider_call(
        self,
        *,
        provider: str,
        pending_id: str,
        client_id: str,
        project_id: str | None,
        to_number: str,
        runtime=None,
    ) -> dict[str, Any]:
        self._ensure_provider_configured(provider)
        if provider == "exotel":
            base_url = validate_public_base_url(self.settings.public_base_url or "")
            status_callback_url = urllib.parse.urljoin(
                base_url.geturl().rstrip("/") + "/",
                f"exotel/status/{pending_id}",
            )
            call = await self.exotel.create_call(to_number=to_number, status_callback_url=status_callback_url)
        elif provider == "airtel_iq":
            base_url = validate_public_base_url(self.settings.public_base_url or "")
            status_callback_url = urllib.parse.urljoin(
                base_url.geturl().rstrip("/") + "/",
                f"airtel-iq/status/{pending_id}",
            )
            events_callback_url = urllib.parse.urljoin(
                base_url.geturl().rstrip("/") + "/",
                f"airtel-iq/events/{pending_id}",
            )
            cdr_callback_url = urllib.parse.urljoin(
                base_url.geturl().rstrip("/") + "/",
                f"airtel-iq/cdr/{pending_id}",
            )
            ws_url = build_ws_url(self.settings.public_base_url or "", f"/airtel-iq/media/{pending_id}")
            call = await self.airtel_iq.create_call(
                to_number=to_number,
                status_callback_url=status_callback_url,
                ws_url=ws_url,
                events_callback_url=events_callback_url,
                cdr_callback_url=cdr_callback_url,
            )
        elif provider == "meta_whatsapp":
            base_url = validate_public_base_url(self.settings.public_base_url or "")
            status_callback_url = urllib.parse.urljoin(
                base_url.geturl().rstrip("/") + "/",
                f"meta-whatsapp/status/{pending_id}",
            )
            sdp_type = ""
            sdp = ""
            try:
                sdp_type, sdp = await self.create_meta_offer(pending_id)
            except Exception as exc:
                logger.warning("Dynamic Meta SDP offer generation failed for pending_id=%s: %s", pending_id, exc)
                sdp_type = (self.settings.meta_whatsapp_default_sdp_type or "offer").strip() or "offer"
                sdp = (self.settings.meta_whatsapp_default_sdp or "").strip()
                if not sdp:
                    raise RuntimeError(
                        "Dynamic Meta SDP generation failed and META_WHATSAPP_DEFAULT_SDP is not set."
                    ) from exc
            call = await self.meta_whatsapp.create_call(
                to_number=to_number,
                status_callback_url=status_callback_url,
                pending_id=pending_id,
                client_id=client_id,
                project_id=project_id,
                sdp_type=sdp_type,
                sdp=sdp,
            )
        elif provider == "piopiy":
            call = await self.piopiy.create_call(
                to_number=to_number,
                agent_id=(runtime.piopiy_agent_id if runtime is not None else None),
                caller_id=(runtime.piopiy_caller_id if runtime is not None else None),
                app_id=(runtime.piopiy_app_id if runtime is not None else None),
            )
        else:
            base_url = validate_public_base_url(self.settings.public_base_url or "")
            twiml_url = urllib.parse.urljoin(
                base_url.geturl().rstrip("/") + "/",
                f"twilio/voice/outbound/{pending_id}",
            )
            call = await self.twilio.create_call(to_number=to_number, twiml_url=twiml_url)

        provider_call_sid = str(call.get("sid", "")).strip()
        if provider_call_sid:
            await self.update_pending_call_provider_sid(pending_id, provider_call_sid)
        return call

    async def find_pending_consent_by_number(self, to_number: str) -> tuple[str, PendingCall] | None:
        target = _normalize_phone_for_match(to_number)
        if not target:
            return None
        pending_ids = await self._store.list_pending_ids(provider="meta_whatsapp_consent")
        for pending_id in reversed(pending_ids):
            payload = await self._store.get_call(pending_id)
            pending_call = self._deserialize_pending_call(payload)
            if pending_call is None:
                continue
            if _normalize_phone_for_match(pending_call.to_number) == target:
                return pending_id, pending_call
        return None

    async def start_inbound_whatsapp_consent_flow(
        self,
        *,
        from_number: str,
        initial_message: str,
        client_id: str,
        project_id: str | None,
    ) -> tuple[str, PendingCall]:
        """Create a consent-first WhatsApp lead session for inbound messages."""
        client_bundle = load_client(client_id, project_id=project_id)
        active_project = client_bundle.active_project
        project_runtime = active_project.runtime if active_project is not None else None

        pending_id = uuid4().hex
        customer_name = from_number
        metadata: dict[str, str | float | int | bool | None] = {
            "provider": "meta_whatsapp_consent",
            "outreach_mode": "consent_then_call",
            "selected_provider": "meta_whatsapp",
            "target_call_provider": "twilio",
            "client_id": client_id,
            "project_id": active_project.project_id if active_project else project_id,
            "project_name": active_project.name if active_project else None,
            "customer_name": customer_name,
            "to_number": from_number,
            "lead_source": "meta_whatsapp_inbound",
            "call_status": "awaiting_consent",
            "consent_status": "pending",
            "call_requested_at_epoch": time.time(),
            "inbound_first_message": initial_message.strip(),
        }
        pending_call = PendingCall(
            session_id=pending_id,
            client_id=client_id,
            customer_name=customer_name,
            to_number=from_number,
            provider="meta_whatsapp_consent",
            metadata=metadata,
        )
        await self._store.put_call(
            pending_id=pending_id,
            pending_payload=self._serialize_pending_call(pending_call),
            context=metadata,
        )
        record_leads(
            client_id=client_id,
            source="meta_whatsapp_inbound",
            leads=[{"customer_name": customer_name, "to_number": from_number}],
            metadata={
                "provider": "meta_whatsapp",
                "project_id": active_project.project_id if active_project else project_id,
                "project_name": active_project.name if active_project else "",
            },
        )
        consent_message = (
            (project_runtime.whatsapp_consent_message if project_runtime is not None else None)
            or "Namaste. Kya hum aapko abhi Twilio number se call karein? Reply YES for call now or NO to continue booking on chat."
        )
        await self.meta_whatsapp.send_text_message(to_number=from_number, body_text=consent_message)
        return pending_id, pending_call

    async def find_chat_pending_by_number(self, to_number: str) -> tuple[str, PendingCall] | None:
        target = _normalize_phone_for_match(to_number)
        if not target:
            return None
        pending_ids = await self._store.list_pending_ids(provider="meta_whatsapp")
        for pending_id in reversed(pending_ids):
            payload = await self._store.get_call(pending_id)
            pending_call = self._deserialize_pending_call(payload)
            if pending_call is None:
                continue
            if _normalize_phone_for_match(pending_call.to_number) != target:
                continue
            context = await self.get_call_context(pending_id) or {}
            if str(context.get("outreach_mode") or "").strip() != "chat_only":
                continue
            call_status = str(context.get("call_status") or "").strip().lower()
            if call_status in {"chat_started", "chat_active", "queued"}:
                return pending_id, pending_call
        return None

    async def find_latest_chat_by_number(self, to_number: str) -> tuple[str, PendingCall, str] | None:
        target = _normalize_phone_for_match(to_number)
        if not target:
            return None
        pending_ids = await self._store.list_pending_ids(provider="meta_whatsapp")
        for pending_id in reversed(pending_ids):
            payload = await self._store.get_call(pending_id)
            pending_call = self._deserialize_pending_call(payload)
            if pending_call is None:
                continue
            if _normalize_phone_for_match(pending_call.to_number) != target:
                continue
            context = await self.get_call_context(pending_id) or {}
            if str(context.get("outreach_mode") or "").strip() != "chat_only":
                continue
            call_status = str(context.get("call_status") or "").strip().lower()
            return pending_id, pending_call, call_status
        return None

    async def send_whatsapp_chat_reply(self, pending_id: str, pending_call: PendingCall, user_text: str) -> str:
        client_bundle = load_client(
            pending_call.client_id,
            project_id=str((pending_call.metadata or {}).get("project_id") or "") or None,
        )
        active_project = client_bundle.active_project
        runtime = active_project.runtime if active_project is not None else None
        runtime_key = (runtime.gemini_api_key if runtime is not None else None) or ""
        runtime_key_env = (runtime.gemini_api_key_env if runtime is not None else None) or ""
        api_key = runtime_key.strip() or (os.getenv(runtime_key_env, "").strip() if runtime_key_env else "")
        if not api_key:
            api_key = (self.settings.gemini_api_key or "").strip()
        model = (
            ((runtime.structured_model if runtime is not None else None) or self.settings.structured_model).strip()
        )
        context = await self.get_call_context(pending_id) or {}
        history: list[dict[str, str]] = []
        raw_history = str(context.get("whatsapp_chat_history_json") or "").strip()
        if raw_history:
            try:
                parsed = json.loads(raw_history)
                if isinstance(parsed, list):
                    history = [
                        {
                            "role": str(item.get("role") or "").strip(),
                            "text": str(item.get("text") or "").strip(),
                        }
                        for item in parsed
                        if isinstance(item, dict)
                    ]
            except json.JSONDecodeError:
                history = []

        history.append({"role": "user", "text": user_text.strip()})
        compact_history = history[-10:]
        transcript = "\n".join(
            f"{'User' if turn.get('role') == 'user' else 'Agent'}: {turn.get('text', '').strip()}"
            for turn in compact_history
            if turn.get("text")
        )
        reply_text = ""
        if api_key and model:
            resolved_assets = PromptBuilder.resolve_project_playbook_assets(client_bundle)
            chat_prompt = (
                "You are a concise WhatsApp sales assistant for outbound follow-up.\n"
                "Rules:\n"
                "- Keep replies short, natural, and actionable (1-2 lines).\n"
                "- Understand short Roman Marathi/Hindi/English replies such as ho, nahi, udya, parwa, sakali.\n"
                "- If user says no only for phone call preference, continue the appointment booking in chat.\n"
                "- Close politely only when user clearly says they are not interested in heart checkup itself.\n"
                "- If user asks for callback timing, confirm exact slot and keep it brief.\n"
                "- Do not ask the user to repeat simple yes/no intent.\n"
                "- Stay aligned with this business context:\n"
                f"{str(resolved_assets.get('system_prompt') or '').strip()}\n\n"
                f"Conversation so far:\n{transcript}\n\n"
                "Respond to the latest user message only."
            )
            try:
                generator = build_structured_client(self.settings, api_key=api_key, model=model)
                reply_text = await generator.generate_chat_reply(
                    system_prompt=chat_prompt,
                    user_text=user_text.strip(),
                    generation_settings=client_bundle.config.structured_generation,
                )
            except Exception:
                logger.exception("WhatsApp chat reply generation failed for pending_id=%s", pending_id)
        if not reply_text:
            reply_text = "Thanks for the update. We can continue here, or I can arrange a quick callback if you prefer."

        reply_text = " ".join(reply_text.split())
        if len(reply_text) > 600:
            reply_text = f"{reply_text[:597].rstrip()}..."
        await self.meta_whatsapp.send_text_message(to_number=pending_call.to_number, body_text=reply_text)
        logger.info(
            "WhatsApp chat reply sent pending_id=%s to=%s user=%s agent=%s",
            pending_id,
            pending_call.to_number,
            user_text.strip(),
            reply_text,
        )
        closing_turn = _is_chat_closing_turn(user_text, reply_text)
        history.append({"role": "assistant", "text": reply_text})
        await self.update_call_context(
            pending_id,
            {
                "call_status": "chat_closed" if closing_turn else "chat_active",
                "last_user_message": user_text.strip(),
                "last_agent_message": reply_text,
                "whatsapp_chat_history_json": json.dumps(history[-12:], ensure_ascii=True),
                "chat_last_reply_at_epoch": time.time(),
                "chat_closed_at_epoch": time.time() if closing_turn else None,
            },
        )
        return reply_text

    async def switch_consent_to_chat(self, pending_id: str, pending_call: PendingCall) -> None:
        """Convert consent flow to chat booking when user declines phone call."""
        opening = (
            "ठीक आहे. आपण इथेच अपॉइंटमेंट बुकिंग सुरू करूया. कृपया रुग्णाचं पूर्ण नाव सांगा."
        )
        pending_payload = await self._store.get_call(pending_id)
        if pending_payload is not None:
            pending_payload["provider"] = "meta_whatsapp"
            pending_metadata = pending_payload.get("metadata")
            if isinstance(pending_metadata, dict):
                pending_metadata["provider"] = "meta_whatsapp"
                pending_metadata["outreach_mode"] = "chat_only"
                pending_metadata["consent_status"] = "declined"
                pending_metadata["call_status"] = "chat_started"
                pending_metadata["target_call_provider"] = "twilio"
            await self._store.set_call(pending_id, pending_payload)
        await self.update_call_context(
            pending_id,
            {
                "provider": "meta_whatsapp",
                "outreach_mode": "chat_only",
                "consent_status": "declined",
                "call_status": "chat_started",
                "target_call_provider": "twilio",
                "whatsapp_chat_history_json": json.dumps(
                    [{"role": "assistant", "text": opening}],
                    ensure_ascii=True,
                ),
                "chat_started_at_epoch": time.time(),
            },
        )
        await self.meta_whatsapp.send_text_message(to_number=pending_call.to_number, body_text=opening)

    async def activate_consent_call(self, pending_id: str, pending_call: PendingCall) -> dict[str, Any]:
        context = await self.get_call_context(pending_id) or {}
        target_provider = str(
            context.get("target_call_provider")
            or (pending_call.metadata or {}).get("target_call_provider")
            or self.settings.telephony_provider
        ).strip() or self.settings.telephony_provider
        if target_provider not in {"twilio", "exotel", "airtel_iq", "meta_whatsapp", "piopiy"}:
            target_provider = self.settings.telephony_provider

        pending_payload = await self._store.get_call(pending_id)
        if pending_payload is not None:
            pending_payload["provider"] = target_provider
            metadata = pending_payload.get("metadata")
            if isinstance(metadata, dict):
                metadata["provider"] = target_provider
                metadata["consent_status"] = "approved"
                metadata["call_status"] = "queued"
            await self._store.set_call(pending_id, pending_payload)
        await self.update_call_context(
            pending_id,
            {
                "provider": target_provider,
                "consent_status": "approved",
                "call_status": "queued",
                "call_requested_at_epoch": time.time(),
            },
        )
        return await self._place_provider_call(
            provider=target_provider,
            pending_id=pending_id,
            client_id=pending_call.client_id,
            project_id=str((pending_call.metadata or {}).get("project_id") or "") or None,
            to_number=pending_call.to_number,
        )

    async def decline_consent_call(self, pending_id: str) -> None:
        await self.update_call_context(
            pending_id,
            {
                "consent_status": "declined",
                "call_status": "consent_declined",
            },
        )
        await self.consume_pending_call(pending_id)

    async def get_pending_call(self, pending_id: str) -> PendingCall | None:
        return self._deserialize_pending_call(await self._store.get_call(pending_id))

    async def consume_pending_call(self, pending_id: str) -> PendingCall | None:
        return self._deserialize_pending_call(await self._store.pop_call(pending_id))

    async def consume_first_pending_call(self, provider: str) -> PendingCall | None:
        pending_ids = await self._store.list_pending_ids(provider=provider)
        for pending_id in pending_ids:
            pending_call = self._deserialize_pending_call(await self._store.pop_call(pending_id))
            if pending_call is not None:
                return pending_call
        return None

    async def update_pending_call_provider_sid(self, pending_id: str, provider_call_sid: str) -> None:
        pending_payload = await self._store.get_call(pending_id)
        if pending_payload is not None:
            existing_aliases = pending_payload.get("provider_call_sids")
            if not isinstance(existing_aliases, list):
                existing_aliases = []
            aliases = [str(item).strip() for item in existing_aliases if str(item or "").strip()]
            if provider_call_sid and provider_call_sid not in aliases:
                aliases.append(provider_call_sid)
            pending_payload["provider_call_sid"] = provider_call_sid
            pending_payload["provider_call_sids"] = aliases
            metadata = pending_payload.get("metadata")
            if isinstance(metadata, dict):
                metadata["provider_call_sid"] = provider_call_sid
                metadata["provider_call_sids"] = aliases
            await self._store.set_call(pending_id, pending_payload)
        context = await self._store.get_context(pending_id)
        if context is not None:
            context["provider_call_sid"] = provider_call_sid
            existing_aliases = context.get("provider_call_sids")
            if not isinstance(existing_aliases, list):
                existing_aliases = []
            aliases = [str(item).strip() for item in existing_aliases if str(item or "").strip()]
            if provider_call_sid and provider_call_sid not in aliases:
                aliases.append(provider_call_sid)
            context["provider_call_sids"] = aliases
            await self._store.set_context(pending_id, context)

    async def consume_pending_call_by_provider_sid(self, provider: str, provider_call_sid: str) -> PendingCall | None:
        match = await self._store.find_call_by_provider_sid(provider, provider_call_sid)
        if match is None:
            return None
        pending_id, _ = match
        return self._deserialize_pending_call(await self._store.pop_call(pending_id))

    async def find_pending_id_by_provider_sid(self, provider: str, provider_call_sid: str) -> str | None:
        match = await self._store.find_call_by_provider_sid(provider, provider_call_sid)
        if match is None:
            return None
        pending_id, _ = match
        return pending_id

    async def get_call_context(self, pending_id: str) -> dict[str, str | float | int | bool | None] | None:
        context = await self._store.get_context(pending_id)
        if context is None:
            return None
        return {str(key): value for key, value in context.items()}

    async def update_call_context(
        self, pending_id: str, updates: dict[str, str | float | int | bool | None]
    ) -> None:
        context = await self._store.get_context(pending_id)
        if context is None:
            return
        context.update(updates)
        await self._store.set_context(pending_id, context)
        pending_payload = await self._store.get_call(pending_id)
        if pending_payload is not None:
            metadata = pending_payload.get("metadata")
            if isinstance(metadata, dict):
                metadata.update(updates)
            await self._store.set_call(pending_id, pending_payload)

    async def update_call_context_by_provider_sid(
        self, provider: str, provider_call_sid: str, updates: dict[str, str | float | int | bool | None]
    ) -> None:
        match = await self._store.find_call_by_provider_sid(provider, provider_call_sid)
        if match is None:
            return
        pending_id, _ = match
        await self.update_call_context(pending_id, updates)

    async def list_pending_call_ids(self, provider: str | None = None) -> list[str]:
        return await self._store.list_pending_ids(provider=provider)

    async def create_meta_offer(self, pending_id: str) -> tuple[str, str]:
        if RTCPeerConnection is None:
            raise RuntimeError("aiortc is not installed.")
        await self.close_meta_peer(pending_id)
        peer = RTCPeerConnection()
        # Keep a single audio m-line while attaching a bidirectional media bridge track.
        bridge = MetaWhatsAppMediaBridge(
            outbound_source_rate=self.settings.meta_whatsapp_outbound_source_rate,
            outbound_preroll_ms=self.settings.meta_whatsapp_outbound_preroll_ms,
        )
        peer.addTrack(bridge.outgoing_track)
        bridge.bind_peer_connection(peer)
        offer = await peer.createOffer()
        await peer.setLocalDescription(offer)
        await self._wait_for_meta_ice_complete(peer)
        local = peer.localDescription
        if local is None or not local.sdp:
            await bridge.close()
            await peer.close()
            raise RuntimeError("Meta WebRTC local SDP generation failed.")
        sanitized_sdp = _sanitize_meta_sdp_offer(str(local.sdp))
        await peer.setLocalDescription(RTCSessionDescription(sdp=sanitized_sdp, type=str(local.type)))
        self._meta_peer_connections[pending_id] = peer
        self._meta_media_bridges[pending_id] = bridge
        return str(local.type), sanitized_sdp

    async def apply_meta_answer(self, pending_id: str, sdp_type: str, sdp: str) -> None:
        if RTCSessionDescription is None:
            return
        peer = self._meta_peer_connections.get(pending_id)
        if peer is None:
            return
        await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))

    async def close_meta_peer(self, pending_id: str) -> None:
        peer = self._meta_peer_connections.pop(pending_id, None)
        if peer is not None:
            with contextlib.suppress(Exception):
                await peer.close()
        bridge = self._meta_media_bridges.pop(pending_id, None)
        if bridge is not None:
            with contextlib.suppress(Exception):
                await bridge.close()

    def get_meta_media_bridge(self, pending_id: str) -> MetaWhatsAppMediaBridge | None:
        return self._meta_media_bridges.get(pending_id)

    @staticmethod
    async def _wait_for_meta_ice_complete(peer: Any, timeout_seconds: float = 2.0) -> None:
        start = time.monotonic()
        while getattr(peer, "iceGatheringState", "") != "complete":
            if time.monotonic() - start > timeout_seconds:
                return
            await asyncio.sleep(0.05)

    async def claim_webhook_event(
        self,
        provider: str,
        pending_id: str,
        event_scope: str,
        payload: dict[str, str],
    ) -> bool:
        normalized_payload = {str(key): str(value) for key, value in payload.items()}
        fingerprint = hashlib.sha1(
            json.dumps(normalized_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        event_key = f"{provider}:{pending_id}:{event_scope}:{fingerprint}"
        return await self._store.claim_webhook_event(
            event_key=event_key,
            ttl_seconds=self.settings.webhook_idempotency_ttl_seconds,
        )


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="Gemini Voice Sales Agent Dashboard")

    def _trusted_hosts() -> list[str]:
        env_value = os.getenv("TRUSTED_HOSTS", "").strip()
        hosts: list[str] = []
        if env_value:
            hosts.extend(host.strip() for host in env_value.split(",") if host.strip())
        elif settings.public_base_url:
            parsed = urllib.parse.urlparse(settings.public_base_url)
            if parsed.hostname:
                hosts.append(parsed.hostname)
        for fallback in ("localhost", "127.0.0.1", "[::1]"):
            if fallback not in hosts:
                hosts.append(fallback)
        return hosts

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_trusted_hosts())
    live_preview_structured = build_structured_client(
        settings,
        api_key=settings.gemini_api_key,
        model=settings.structured_model,
    )
    async def _handle_finished_session(session: VoiceSalesSession) -> None:
        lead_id = str((session.telephony_context or {}).get("guest_demo_lead_id") or "").strip()
        if not lead_id:
            return
        auth_manager.update_guest_demo_lead_summary(
            lead_id=lead_id,
            last_session_id=session.session_id,
            testing_summary=_build_guest_demo_testing_summary(session.artifacts),
            asked_questions=_extract_guest_demo_asked_questions(session.artifacts),
            summary_updated_at=datetime.now(timezone.utc).isoformat(),
            status="completed",
        )

    controller = SessionController(settings, on_session_finished=_handle_finished_session)
    telephony = TelephonyController(settings, controller)
    piopiy_capture: dict[str, Any] = {
        "last_request": None,
        "last_answer": None,
        "last_debug": None,
        "last_cdr": None,
        "last_events": None,
        "last_catcher": None,
    }
    piopiy_recording_retry_keys: set[tuple[str, int]] = set()
    piopiy_trace_file = Path(os.getenv("PIOPIY_TRACE_FILE", "/opt/new_voice_agent/runtime/piopiy_agent_trace.jsonl"))
    piopiy_debug_state_file = Path(
        os.getenv("PIOPIY_DEBUG_STATE_FILE", "/opt/new_voice_agent/runtime/piopiy_debug_state.json")
    )
    piopiy_latest_runtime_file = Path(
        os.getenv("PIOPIY_LATEST_RUNTIME_FILE", "/opt/new_voice_agent/runtime/piopiy_latest_call_runtime.json")
    )
    piopiy_debug_state: dict[str, Any] = {
        "stage": "idle",
        "updated_at": None,
        "history": [],
    }
    browser_audio_bridges: dict[str, BrowserAudioBridge] = {}

    async def _capture_piopiy_request(request: Request) -> dict[str, Any]:
        raw_body = await request.body()
        try:
            payload: Any = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
        except Exception:
            payload = raw_body.decode("utf-8", errors="replace")
        headers = {str(key).lower(): str(value) for key, value in request.headers.items()}
        captured = {
            "method": request.method,
            "url": str(request.url),
            "headers": headers,
            "payload": payload,
        }
        piopiy_capture["last_request"] = captured
        return captured

    async def _capture_piopiy_catcher_request(request: Request) -> dict[str, Any]:
        raw_body = await request.body()
        try:
            payload: Any = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
        except Exception:
            payload = raw_body.decode("utf-8", errors="replace")
        headers = {str(key).lower(): str(value) for key, value in request.headers.items()}
        captured = {
            "method": request.method,
            "url": str(request.url),
            "headers": headers,
            "payload": payload,
        }
        piopiy_capture["last_request"] = captured
        piopiy_capture["last_catcher"] = captured
        return captured

    def _append_piopiy_trace(event: str, **data: Any) -> None:
        record: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
        for key, value in data.items():
            if value is None:
                continue
            try:
                json.dumps(value)
                record[str(key)] = value
            except Exception:
                record[str(key)] = str(value)
        try:
            piopiy_trace_file.parent.mkdir(parents=True, exist_ok=True)
            with piopiy_trace_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True) + "\n")
        except Exception:
            logger.exception("Failed to append Piopiy trace event=%s", event)

    def _persist_piopiy_debug_state() -> None:
        try:
            piopiy_debug_state_file.parent.mkdir(parents=True, exist_ok=True)
            with piopiy_debug_state_file.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(piopiy_debug_state, ensure_ascii=True, default=str, indent=2) + "\n")
        except Exception:
            logger.exception("Failed to persist Piopiy debug state")

    def _record_piopiy_stage(stage: str, **fields: Any) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        entry: dict[str, Any] = {"ts": stamp, "stage": stage}
        for key, value in fields.items():
            if value is None:
                continue
            try:
                json.dumps(value)
                entry[str(key)] = value
            except Exception:
                entry[str(key)] = str(value)
        piopiy_debug_state["stage"] = stage
        piopiy_debug_state["updated_at"] = stamp
        history = piopiy_debug_state.setdefault("history", [])
        if isinstance(history, list):
            history.append(entry)
            del history[:-25]
        piopiy_debug_state["last_entry"] = entry
        _append_piopiy_trace(f"stage:{stage}", **fields)
        _persist_piopiy_debug_state()

    def _read_piopiy_trace_entries(limit: int = 100, pending_id: str | None = None) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if not piopiy_trace_file.exists():
            return entries
        try:
            lines = piopiy_trace_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            return entries
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except Exception:
                continue
            if not isinstance(parsed, dict):
                continue
            if pending_id and str(parsed.get("pending_id") or "").strip() != pending_id:
                continue
            entries.append(parsed)
            if len(entries) >= max(1, limit):
                break
        entries.reverse()
        return entries

    def _write_latest_call_runtime(updates: dict[str, Any]) -> None:
        existing: dict[str, Any] = {}
        if piopiy_latest_runtime_file.exists():
            with contextlib.suppress(Exception):
                parsed = json.loads(piopiy_latest_runtime_file.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    existing = parsed
        existing.update({key: value for key, value in updates.items() if value is not None})
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            piopiy_latest_runtime_file.parent.mkdir(parents=True, exist_ok=True)
            piopiy_latest_runtime_file.write_text(
                json.dumps(existing, indent=2, ensure_ascii=True, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Failed to write latest Piopiy runtime debug file")

    def _read_latest_call_runtime() -> dict[str, Any]:
        if not piopiy_latest_runtime_file.exists():
            return {}
        with contextlib.suppress(Exception):
            parsed = json.loads(piopiy_latest_runtime_file.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _capture_runtime_trace(pending_id: str, message: str) -> None:
        updates: dict[str, Any] = {
            "latest_session_id": pending_id,
            "session_id": pending_id,
            "last_runtime_trace": message,
        }
        if "Direct Gemini bridge connected" in message:
            updates.update(
                {
                    "selected_audio_path": "NATIVE_GEMINI_LIVE_AUDIO",
                    "selected_agent_class": "PiopiyDirectGeminiBridgeSession",
                    "gemini_live_native_audio_enabled": True,
                    "separate_tts_enabled": False,
                    "audio_path_locked": True,
                    "voice_locked": True,
                    "fallback_triggered": False,
                    "duplicate_audio_path_detected": False,
                }
            )
            voice_match = re.search(r"\bvoice=([^\s]+)", message)
            if voice_match:
                updates["voice_used"] = voice_match.group(1)
                updates["active_voice_name"] = voice_match.group(1)
        for key in (
            "caller_audio_received_at",
            "vad_speech_start_at",
            "vad_speech_end_at",
            "gemini_audio_send_started_at",
            "gemini_first_token_or_audio_at",
            "first_audio_chunk_ready_at",
            "first_audio_sent_to_piopiy_at",
            "total_turn_latency_ms",
            "audio_chunk_buffer_size",
            "number_of_audio_chunks_buffered_before_send",
        ):
            match = re.search(rf"\b{re.escape(key)}=([^\s]+)", message)
            if match:
                raw_value = match.group(1)
                with contextlib.suppress(Exception):
                    updates[key] = float(raw_value)
                if key not in updates:
                    updates[key] = raw_value
        if "waits_for_full_tts=false" in message:
            updates["waits_for_full_tts"] = False
        if "response_audio_streaming=chunk_by_chunk" in message:
            updates["response_audio_streaming"] = "chunk_by_chunk"
        if "Direct Gemini first audio latency_ms=" in message:
            match = re.search(r"latency_ms=([0-9.]+)", message)
            if match:
                with contextlib.suppress(Exception):
                    updates["first_response_latency_ms"] = float(match.group(1))
        _write_latest_call_runtime(updates)

    def _build_piopiy_ws_url(request: Request, pending_id: str) -> str:
        explicit_ws_base = str(os.getenv("PIOPIY_WS_PUBLIC_BASE_URL") or "").strip()
        if explicit_ws_base:
            parsed_explicit = urllib.parse.urlparse(explicit_ws_base)
            if parsed_explicit.scheme in {"ws", "wss"} and parsed_explicit.netloc:
                base_path = parsed_explicit.path.rstrip("/")
                joined_path = f"{base_path}/piopiy/stream/{pending_id}" if base_path else f"/piopiy/stream/{pending_id}"
                return urllib.parse.urlunparse(
                    (parsed_explicit.scheme, parsed_explicit.netloc, joined_path, "", "", "")
                )
            return build_ws_url(explicit_ws_base, f"/piopiy/stream/{pending_id}")
        public_base_url = str(settings.public_base_url or "").strip()
        if public_base_url:
            try:
                return build_ws_url(public_base_url, f"/piopiy/stream/{pending_id}")
            except Exception as exc:
                logger.warning(
                    "Piopiy ws URL build from PUBLIC_BASE_URL failed; falling back to request host. base_url=%s error=%s",
                    public_base_url,
                    exc,
                )
        forced_scheme = str(os.getenv("PIOPIY_WS_SCHEME") or "").strip().lower()
        forwarded_proto = str(request.headers.get("x-forwarded-proto") or request.url.scheme or "").strip().lower()
        if forced_scheme in {"ws", "wss"}:
            scheme = forced_scheme
        else:
            scheme = "wss" if forwarded_proto == "https" else "ws"
        host = (
            str(request.headers.get("x-forwarded-host") or "").strip()
            or str(request.headers.get("host") or "").strip()
            or request.url.netloc
        )
        if not host:
            raise RuntimeError("Unable to resolve Piopiy websocket host.")
        return f"{scheme}://{host}/piopiy/stream/{pending_id}"

    def _bootstrap_piopiy_dashboard_session(
        *,
        client_id: str,
        project_id: str | None,
        project_name: str | None,
        pending_id: str,
        telephony_context: dict[str, Any],
    ) -> Path:
        """Create an early session artifact so Piopiy calls appear on the dashboard immediately.

        We reuse the Piopiy pending ID as the session ID so the eventual live
        stream writes back into the same session directory.
        """
        session_logger = SessionLogger(settings.session_output_dir)
        session_dir = session_logger.create_session_dir(client_id, pending_id)
        artifacts = SessionArtifacts(
            client_id=client_id,
            project_id=project_id,
            project_name=project_name,
            session_id=pending_id,
            started_at=datetime.now(timezone.utc),
            telephony_context=dict(telephony_context),
        )
        session_logger.save(artifacts, session_dir)
        return session_dir

    def _parse_piopiy_timestamp_ms(value: object) -> datetime | None:
        try:
            timestamp_ms = float(value)
        except (TypeError, ValueError):
            return None
        if timestamp_ms <= 0:
            return None
        return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)

    def _extract_piopiy_started_at(payload: dict[str, Any]) -> datetime | None:
        timestamps = payload.get("timestamps") if isinstance(payload.get("timestamps"), dict) else {}
        start_block = timestamps.get("start") if isinstance(timestamps.get("start"), dict) else {}
        for candidate in (
            start_block.get("utc_iso"),
            payload.get("started_at"),
            payload.get("start_time"),
            payload.get("answered_at"),
        ):
            parsed = _parse_iso_datetime(candidate)
            if parsed is not None:
                return parsed
        for candidate in (
            start_block.get("utc_ms"),
            timestamps.get("start_utc_ms"),
            payload.get("timestamp_utc_ms"),
            payload.get("start_utc_ms"),
            payload.get("started_at_utc_ms"),
        ):
            parsed = _parse_piopiy_timestamp_ms(candidate)
            if parsed is not None:
                return parsed
        return None

    def _extract_piopiy_ended_at(payload: dict[str, Any]) -> datetime | None:
        timestamps = payload.get("timestamps") if isinstance(payload.get("timestamps"), dict) else {}
        end_block = timestamps.get("end") if isinstance(timestamps.get("end"), dict) else {}
        for candidate in (
            end_block.get("utc_iso"),
            payload.get("ended_at"),
            payload.get("end_time"),
            payload.get("hangup_at"),
        ):
            parsed = _parse_iso_datetime(candidate)
            if parsed is not None:
                return parsed
        for candidate in (
            end_block.get("utc_ms"),
            timestamps.get("end_utc_ms"),
            payload.get("end_utc_ms"),
            payload.get("ended_at_utc_ms"),
        ):
            parsed = _parse_piopiy_timestamp_ms(candidate)
            if parsed is not None:
                return parsed
        return None

    def _piopiy_recording_api_url(file_name: str) -> str:
        return f"https://api.piopiy.com/sip/app/call/recording/play/{urllib.parse.quote(file_name)}"

    def _piopiy_recording_request_headers(recording_url: str) -> dict[str, str]:
        parsed = urllib.parse.urlparse(str(recording_url or "").strip())
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if "api.piopiy.com" in host and (
            "/sip/app/call/recording/play/" in path or path.startswith("/play/")
        ):
            token = (settings.piopiy_api_token or os.getenv("PIOPIY_API_TOKEN", "")).strip()
            if token:
                return {"Authorization": f"Bearer {token}"}
        return {}

    def _resolve_project_piopiy_app_id(client_id: str | None, project_id: str | None) -> str | None:
        resolved_client_id = str(client_id or "").strip()
        resolved_project_id = str(project_id or "").strip()
        if not resolved_client_id:
            return None
        with contextlib.suppress(Exception):
            bundle = load_client(resolved_client_id, resolved_project_id or None)
            active_project = bundle.active_project
            runtime = active_project.runtime if active_project is not None else None
            value = str(runtime.piopiy_app_id or "").strip() if runtime is not None else ""
            if value:
                return value
        return None

    def _extract_piopiy_cdr_file_name(cdr_payload: Any) -> str | None:
        if not isinstance(cdr_payload, dict):
            return None
        for key in ("file_name", "recording_file_name", "recording_filename"):
            value = str(cdr_payload.get(key) or "").strip()
            if value:
                return value
        recording = cdr_payload.get("recording")
        if isinstance(recording, dict):
            for key in ("file_name", "recording_file_name", "recording_filename"):
                value = str(recording.get(key) or "").strip()
                if value:
                    return value
        return None

    async def _fetch_piopiy_recording_from_cdr(
        *,
        client_id: str,
        session_dir: Path,
        app_id: str | None,
        call_id_candidates: list[str],
        caller_id: str | None,
        to_number: str | None,
        started_at: datetime | None,
        ended_at: datetime | None,
        direction: str | None,
    ) -> dict[str, Any] | None:
        api_token = (settings.piopiy_api_token or os.getenv("PIOPIY_API_TOKEN", "")).strip()
        app_id = (app_id or settings.piopiy_app_id or os.getenv("PIOPIY_APP_ID", "")).strip()
        if not api_token or not app_id:
            return None
        window_start = started_at or ended_at or datetime.now(timezone.utc)
        window_end = ended_at or started_at or datetime.now(timezone.utc)
        if window_end < window_start:
            window_start, window_end = window_end, window_start
        start_time = int((window_start - timedelta(hours=12)).timestamp() * 1000)
        end_time = int((window_end + timedelta(hours=12)).timestamp() * 1000)
        headers = {"Authorization": f"Bearer {api_token}"}
        request_body = {
            "app_id": app_id,
            "start_time": start_time,
            "end_time": end_time,
            "page": 1,
            "limit": 50,
        }
        if direction:
            request_body["call_type"] = direction
        if caller_id:
            request_body["search_number"] = caller_id
        api_url = "https://api.piopiy.com/sip/app/cdr/get"
        _append_piopiy_trace(
            "recording_lookup_started",
            pending_id=session_dir.name,
            client_id=client_id,
            app_id=app_id,
            request_body=request_body,
            call_id_candidates=call_id_candidates,
            caller_id=caller_id,
            to_number=to_number,
            direction=direction,
        )
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=45.0) as client:
                response = await client.post(api_url, headers=headers, json=request_body)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            logger.warning(
                "Piopiy CDR lookup failed client_id=%s session_id=%s error=%s",
                client_id,
                session_dir.name,
                exc,
            )
            _append_piopiy_trace(
                "recording_lookup_failed",
                pending_id=session_dir.name,
                client_id=client_id,
                app_id=app_id,
                request_body=request_body,
                error=repr(exc),
            )
            return None

        cdr_rows = payload.get("cdr") if isinstance(payload, dict) else None
        if not isinstance(cdr_rows, list):
            _append_piopiy_trace(
                "recording_lookup_invalid_payload",
                pending_id=session_dir.name,
                client_id=client_id,
                app_id=app_id,
                request_body=request_body,
                response_payload=payload if isinstance(payload, dict) else str(type(payload)),
            )
            return None
        _append_piopiy_trace(
            "recording_lookup_response",
            pending_id=session_dir.name,
            client_id=client_id,
            app_id=app_id,
            request_body=request_body,
            cdr_count=len(cdr_rows),
        )
        matched_rows: list[tuple[int, dict[str, Any], str]] = []
        for row in cdr_rows:
            if not isinstance(row, dict):
                continue
            row_call_id = str(row.get("call_id") or "").strip()
            row_conversation_id = str(row.get("conversation_id") or "").strip()
            row_caller = str(row.get("caller_id") or row.get("from_number") or "").strip()
            row_did = str(row.get("to_number") or row.get("to") or "").strip()
            if call_id_candidates and row_call_id and row_call_id not in call_id_candidates and row_conversation_id not in call_id_candidates:
                continue
            if caller_id and row_caller and caller_id != row_caller:
                continue
            if to_number and row_did and to_number != row_did:
                continue
            file_name = _extract_piopiy_cdr_file_name(row)
            if not file_name and not bool(row.get("recording")):
                continue
            if not file_name:
                continue
            leg = str(row.get("leg") or "").strip().lower()
            try:
                row_duration = int(float(row.get("duration") or 0))
            except (TypeError, ValueError):
                row_duration = 0
            recording_type = (
                "ai_leg" if leg == "ai" else "caller_leg" if leg in {"a", "caller"} else "full_or_unknown"
            )
            type_score = 100 if recording_type == "full_or_unknown" else 10
            matched_rows.append((type_score + min(row_duration, 10_000), row, recording_type))

        if matched_rows:
            _score, row, recording_type = sorted(matched_rows, key=lambda item: item[0], reverse=True)[0]
            file_name = _extract_piopiy_cdr_file_name(row) or ""
            row_call_id = str(row.get("call_id") or "").strip()
            row_conversation_id = str(row.get("conversation_id") or "").strip()
            recording_url = _piopiy_recording_api_url(file_name)
            _append_piopiy_trace(
                "recording_lookup_matched",
                pending_id=session_dir.name,
                client_id=client_id,
                app_id=app_id,
                recording_filename=file_name,
                row_call_id=row_call_id or None,
                row_conversation_id=row_conversation_id or None,
                selected_recording_type=recording_type,
            )
            return {
                "recording_url": recording_url,
                "recording_filename": file_name,
                "recording_source": "cdr_lookup",
                "recording_cdr_row": row,
                "selected_recording_type": recording_type,
            }
        _append_piopiy_trace(
            "recording_lookup_no_match",
            pending_id=session_dir.name,
            client_id=client_id,
            app_id=app_id,
            request_body=request_body,
            cdr_count=len(cdr_rows),
        )
        return None

    def _extract_piopiy_recording_info(payload: dict[str, Any]) -> tuple[str | None, str | None]:
        play_url_pattern = re.compile(r"https?://api\.piopiy\.com/play/[^\s\"'<>]+", re.IGNORECASE)

        def _walk(node: Any, parents: tuple[str, ...] = ()) -> tuple[str | None, str | None]:
            if isinstance(node, dict):
                for key, value in node.items():
                    key_text = str(key or "").strip().lower()
                    url_candidate = str(value or "").strip() if isinstance(value, str) else ""
                    if key_text in {
                        "recording_url",
                        "recordingurl",
                        "recording_file_url",
                        "file_url",
                        "download_url",
                        "url",
                    } and url_candidate:
                        return (url_candidate, None)
                    if key_text in {
                        "recording_filename",
                        "recording_file_name",
                        "filename",
                        "file_name",
                    } and url_candidate:
                        return (None, url_candidate)
                    if isinstance(value, dict) or isinstance(value, list):
                        nested_url, nested_name = _walk(value, parents + (key_text,))
                        if nested_url or nested_name:
                            return (nested_url, nested_name)
                    if isinstance(value, str):
                        candidate = value.strip()
                        match = play_url_pattern.search(candidate)
                        if match:
                            return (match.group(0), None)
                        if candidate.startswith(("http://", "https://")) and any(
                            marker in key_text for marker in ("record", "audio", "file", "media", "url")
                        ):
                            return (candidate, None)
                return (None, None)
            if isinstance(node, list):
                for value in node:
                    nested_url, nested_name = _walk(value, parents)
                    if nested_url or nested_name:
                        return (nested_url, nested_name)
            if isinstance(node, str):
                match = play_url_pattern.search(node.strip())
                if match:
                    return (match.group(0), None)
            return (None, None)

        recording_url, file_name = _walk(payload)
        if not recording_url:
            recording = payload.get("recording") if isinstance(payload.get("recording"), dict) else {}
            recording_url_candidates = [
                (recording or {}).get("file_url"),
                (recording or {}).get("download_url"),
                (recording or {}).get("url"),
                (recording or {}).get("recording_url"),
                payload.get("recording_url"),
                payload.get("RecordingUrl"),
                payload.get("recordingUrl"),
                payload.get("file_url"),
                payload.get("url"),
            ]
            recording_url = next((str(value).strip() for value in recording_url_candidates if str(value or "").strip()), "")
        if not file_name:
            recording = payload.get("recording") if isinstance(payload.get("recording"), dict) else {}
            file_name_candidates = [
                (recording or {}).get("file_name"),
                (recording or {}).get("filename"),
                (recording or {}).get("recording_filename"),
                payload.get("recording_filename"),
                payload.get("filename"),
                payload.get("file_name"),
            ]
            file_name = next((str(value).strip() for value in file_name_candidates if str(value or "").strip()), "")
        if recording_url and not file_name:
            parsed = urllib.parse.urlparse(recording_url)
            file_name = Path(parsed.path).name
        return (recording_url or None, file_name or None)

    async def _bootstrap_piopiy_dashboard_from_cdr(
        *,
        pending_id: str,
        payload: dict[str, Any],
    ) -> None:
        context = await telephony.get_call_context(pending_id) or {}
        client_id = str(payload.get("client_id") or context.get("client_id") or "").strip()
        if not client_id:
            return
        project_id = str(payload.get("project_id") or context.get("project_id") or "").strip() or None
        project_name = str(payload.get("project_name") or context.get("project_name") or "").strip() or None
        session_logger = SessionLogger(settings.session_output_dir)
        session_dir = session_logger.create_session_dir(client_id, pending_id)
        artifacts_path = session_dir / "artifacts.json"
        existing_payload = _read_json_file(artifacts_path)
        if isinstance(existing_payload, dict):
            try:
                artifacts = SessionArtifacts.model_validate(existing_payload)
            except Exception:
                artifacts = SessionArtifacts(
                    client_id=client_id,
                    project_id=project_id,
                    project_name=project_name,
                    session_id=pending_id,
                    started_at=_extract_piopiy_started_at(payload) or datetime.now(timezone.utc),
                )
        else:
            artifacts = SessionArtifacts(
                client_id=client_id,
                project_id=project_id,
                project_name=project_name,
                session_id=pending_id,
                started_at=_extract_piopiy_started_at(payload) or datetime.now(timezone.utc),
            )

        started_at = _extract_piopiy_started_at(payload)
        ended_at = _extract_piopiy_ended_at(payload)
        if started_at is not None:
            artifacts.started_at = started_at
        if ended_at is not None:
            artifacts.ended_at = ended_at

        existing_context = artifacts.telephony_context if isinstance(artifacts.telephony_context, dict) else {}
        merged_context: dict[str, Any] = dict(existing_context)
        merged_context.update(
            {
                "provider": "piopiy",
                "client_id": client_id,
                "project_id": project_id,
                "project_name": project_name,
                "provider_call_sid": str(
                    payload.get("cmiuuid")
                    or payload.get("callSid")
                    or payload.get("call_id")
                    or payload.get("conversation_id")
                    or payload.get("request_id")
                    or pending_id
                ).strip(),
                "call_status": str(payload.get("status") or payload.get("event") or "").strip() or None,
                "call_direction": str(payload.get("direction") or "").strip() or None,
                "piopiy_last_cdr_json": json.dumps(payload, ensure_ascii=True),
                "piopiy_last_cdr_at_epoch": time.time(),
            }
        )
        if payload.get("caller_id"):
            merged_context["caller_id"] = payload.get("caller_id")
        if payload.get("from"):
            merged_context["from_number"] = payload.get("from")
        if payload.get("to"):
            merged_context["to_number"] = payload.get("to")
        recording_url, recording_filename = _extract_piopiy_recording_info(payload)
        if recording_url:
            merged_context["piopiy_recording_url"] = recording_url
        if recording_filename:
            merged_context["piopiy_recording_filename"] = recording_filename
        artifacts.telephony_context = merged_context
        session_logger.save(artifacts, session_dir)

        with contextlib.suppress(Exception):
            SqliteCallOutcomeStore(settings.call_outcomes_db_path).upsert_outcome(artifacts, merged_context)

        with contextlib.suppress(Exception):
            await controller.merge_telephony_context(pending_id, merged_context)

    def _looks_like_piopiy_payload(payload: Any) -> bool:
        if not isinstance(payload, dict) or not payload:
            return False
        expected_keys = {
            "appid",
            "callSid",
            "cmiuuid",
            "direction",
            "did",
            "event",
            "from",
            "request_id",
            "status",
            "to",
        }
        return any(str(key) in expected_keys for key in payload.keys())
    static_dir = Path(__file__).resolve().parent / "web_static"
    auth_db_path = Path(
        os.getenv("OSWELL_AUTH_DB_PATH", str(settings.session_output_dir / "oswell_auth.db"))
    ).expanduser()
    auth_store = SqliteAuthStore(auth_db_path)
    auth_manager = AuthManager(
        store=auth_store,
        secret_seed=f"{settings.gemini_api_key}:{settings.public_base_url or 'local'}",
    )
    auth_manager.migrate_legacy_users(settings.session_output_dir / ".oswell_users.json")
    admin_seed_email = (os.getenv("ADMIN_EMAIL") or "admin@aivoicebot4u.com").strip().lower()
    admin_seed_password = (os.getenv("ADMIN_PASSWORD") or "Admin@12345").strip()
    admin_seed_name = (os.getenv("ADMIN_NAME") or "AI Voice Admin").strip() or "AI Voice Admin"
    try:
        auth_manager.bootstrap_admin(
            name=admin_seed_name,
            email=admin_seed_email,
            password=admin_seed_password,
        )
    except ValueError:
        logger.warning("Admin bootstrap skipped because configured admin email belongs to a non-admin user.")

    def _slug_token(value: str, fallback: str = "user", max_length: int = 24) -> str:
        token = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower())
        token = token.strip("_")
        if not token:
            token = fallback
        return token[:max_length]

    def _workspace_client_id_for_user(user: dict[str, str]) -> str:
        email_local = str(user.get("email") or "").split("@", 1)[0]
        uid_tail = _slug_token(str(user.get("id") or ""), fallback="uid", max_length=8)
        return f"user_{_slug_token(email_local, fallback='workspace', max_length=24)}_{uid_tail}"

    def _workspace_session_key_for_user(user: dict[str, str]) -> str:
        return f"dashboard_{_slug_token(str(user.get('id') or ''), fallback='session', max_length=32)}"

    def _resolve_inbound_whatsapp_target() -> tuple[str, str | None]:
        """Pick client/project for inbound WhatsApp leads."""
        configured_client_id = (os.getenv("INBOUND_WHATSAPP_CLIENT_ID", "").strip() or None)
        configured_project_id = (os.getenv("INBOUND_WHATSAPP_PROJECT_ID", "").strip() or None)
        if configured_client_id:
            return configured_client_id, configured_project_id

        # Magnum-first fallback for this deployment.
        for candidate in list_client_ids(base_dir=Path(__file__).resolve().parent.parent / "clients"):
            with contextlib.suppress(Exception):
                bundle = load_client(candidate)
                if any(project.project_id == "kamna_magnum_appointment_booking" for project in bundle.projects):
                    return candidate, "kamna_magnum_appointment_booking"
        return settings.default_client_id, None

    def _resolve_inbound_twilio_target() -> tuple[str, str | None]:
        configured_client_id = settings.twilio_inbound_client_id or settings.default_client_id
        configured_project_id = settings.twilio_inbound_project_id
        return configured_client_id, configured_project_id

    def _resolve_inbound_piopiy_target(to_number: str | None) -> tuple[str, str | None]:
        """Pick the Piopiy client/project from the inbound DID.

        We keep a number-specific route for Janjal's public inbound line so calls
        to that DID always open the exact Janjal grievance script instead of the
        generic Piopiy fallback project. The helper also supports future
        number-to-client overrides via `PIOPIY_INBOUND_ROUTING_JSON`.
        """
        normalized_to = "".join(ch for ch in str(to_number or "").strip() if ch.isdigit())
        routing_overrides_raw = os.getenv("PIOPIY_INBOUND_ROUTING_JSON", "").strip()
        if routing_overrides_raw:
            with contextlib.suppress(Exception):
                routing_overrides = json.loads(routing_overrides_raw)
                if isinstance(routing_overrides, dict):
                    for candidate_key in (
                        normalized_to,
                        normalized_to[-12:] if len(normalized_to) >= 12 else "",
                        normalized_to[-10:] if len(normalized_to) >= 10 else "",
                    ):
                        route = routing_overrides.get(candidate_key)
                        if isinstance(route, dict):
                            client_id = str(route.get("client_id") or "").strip()
                            project_id = str(route.get("project_id") or "").strip() or None
                            if client_id:
                                return client_id, project_id
                        elif isinstance(route, str) and route.strip():
                            return route.strip(), None

        janjal_did = "917943446880"
        if normalized_to == janjal_did or normalized_to.endswith(janjal_did):
            return "user_janjal_voicebot_12c92bbc", "janjal_ward22_inbound_918065254654"

        configured_client_id = os.getenv("PIOPIY_INBOUND_CLIENT_ID", "").strip() or os.getenv("PIOPIY_DEFAULT_CLIENT_ID", "").strip() or settings.default_client_id
        configured_project_id = os.getenv("PIOPIY_INBOUND_PROJECT_ID", "").strip() or os.getenv("PIOPIY_DEFAULT_PROJECT_ID", "").strip() or None
        return configured_client_id, configured_project_id

    def _ensure_user_workspace_client(user: dict[str, str]) -> tuple[str, bool]:
        workspace_client_id = _workspace_client_id_for_user(user)
        existing_ids = list_client_ids()
        if workspace_client_id in existing_ids:
            try:
                load_client(workspace_client_id)
                return workspace_client_id, False
            except Exception as exc:
                logger.warning(
                    "Workspace client '%s' exists but failed validation/load; attempting repair. error=%s",
                    workspace_client_id,
                    exc,
                )
        if not existing_ids:
            raise RuntimeError("No base client template exists. Add at least one client template first.")

        user_email = str(user.get("email") or "").strip().lower()
        template_client_id: str | None = None
        if user_email == "guest@aivoicebot4u.local" and "nova_security" in existing_ids:
            template_client_id = "nova_security"
        else:
            resolved_template = _resolve_safe_template_client_id(
                existing_ids,
                preferred=[
                    settings.default_client_id,
                    "nova_security",
                    "led_arts",
                ],
            )
            if resolved_template != "acme_health":
                template_client_id = resolved_template

        if template_client_id is not None:
            payload = get_client_editor_payload(template_client_id)
        else:
            # Avoid seeding new authenticated workspaces from domain-specific healthcare defaults.
            payload = _build_client_builder_payload(
                ClientBuilderRequest(
                    business_name=f"{str(user.get('name') or 'User').strip() or 'User'} Workflow",
                    business_type="Custom",
                    project_name="Sales Agent",
                    preferred_languages="marathi,hindi,english",
                    agent_type="sales",
                ),
                workspace_client_id=workspace_client_id,
            )
        config = dict(payload.get("config") or {})
        config["display_name"] = f"{user.get('name', 'User')} Workflow"
        config["status"] = "draft"
        existing_tags = list(config.get("tags") or [])
        if "user_workspace" not in existing_tags:
            existing_tags.append("user_workspace")
        config["tags"] = existing_tags
        config["import_metadata"] = {
            **dict(config.get("import_metadata") or {}),
            "source": "user_workspace_bootstrap",
            "external_id": str(user.get("id") or ""),
            "imported_from": template_client_id or "generated_blank_workspace",
            "notes": [
                "Workspace client auto-provisioned per authenticated user.",
            ],
        }
        payload["config"] = config
        try:
            save_client_editor_payload(workspace_client_id, payload)
        except FileNotFoundError:
            target_dir = Path(__file__).resolve().parent.parent / "clients" / workspace_client_id
            if template_client_id:
                source_dir = Path(__file__).resolve().parent.parent / "clients" / template_client_id
                if source_dir.exists():
                    if not target_dir.exists():
                        shutil.copytree(source_dir, target_dir)
                    else:
                        for filename in (
                            "config.json",
                            "system_prompt.txt",
                            "knowledge.md",
                            "objections.json",
                            "qualification.json",
                            "cta.json",
                        ):
                            src = source_dir / filename
                            dst = target_dir / filename
                            if src.exists() and not dst.exists():
                                shutil.copy2(src, dst)
            elif not target_dir.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
            save_client_editor_payload(workspace_client_id, payload)
        return workspace_client_id, True

    def _parse_iso_datetime(value: object) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    def _read_json_file(path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _probe_audio_duration_seconds(path: Path) -> float | None:
        ffprobe = shutil.which("ffprobe")
        if not path.exists() or not path.is_file():
            return None
        if ffprobe:
            try:
                result = subprocess.run(
                    [
                        ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                value = float((result.stdout or "").strip())
                if value > 0:
                    return value
            except Exception:
                pass
        if path.suffix.lower() in {".mp3", ".mpeg"}:
            return _estimate_mp3_duration_seconds(path)
        return None

    def _playback_recording_path(source_path: Path) -> Path:
        return source_path.with_name(f"{source_path.stem}{PIOPIY_PLAYBACK_SUFFIX}")

    def _ensure_browser_playback_recording(source_path: Path) -> tuple[Path | None, dict[str, Any]]:
        """Create a browser-friendly WAV copy for telephony MP3s without touching the source."""
        details: dict[str, Any] = {
            "recording_playback_source_path": str(source_path),
            "recording_playback_status": "unavailable",
        }
        if not source_path.exists() or not source_path.is_file() or source_path.stat().st_size <= 0:
            details["recording_playback_error"] = "source_missing"
            return None, details
        if source_path.suffix.lower() == ".wav":
            details.update(
                {
                    "recording_playback_status": "source",
                    "recording_playback_filename": source_path.name,
                    "recording_playback_path": str(source_path),
                    "recording_playback_duration_seconds": _probe_audio_duration_seconds(source_path),
                }
            )
            return source_path, details

        target_path = _playback_recording_path(source_path)
        if target_path.exists() and target_path.is_file() and target_path.stat().st_size > 0:
            details.update(
                {
                    "recording_playback_status": "ready",
                    "recording_playback_filename": target_path.name,
                    "recording_playback_path": str(target_path),
                    "recording_playback_size_bytes": target_path.stat().st_size,
                    "recording_playback_duration_seconds": _probe_audio_duration_seconds(target_path),
                }
            )
            return target_path, details

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            details["recording_playback_error"] = "ffmpeg_not_available"
            return None, details

        temp_path = target_path.with_name(f"{target_path.stem}.download{target_path.suffix}")
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(temp_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if not temp_path.exists() or temp_path.stat().st_size <= 0:
                details["recording_playback_error"] = "conversion_empty"
                return None, details
            temp_path.replace(target_path)
            details.update(
                {
                    "recording_playback_status": "ready",
                    "recording_playback_filename": target_path.name,
                    "recording_playback_path": str(target_path),
                    "recording_playback_size_bytes": target_path.stat().st_size,
                    "recording_playback_duration_seconds": _probe_audio_duration_seconds(target_path),
                }
            )
            return target_path, details
        except Exception as exc:
            with contextlib.suppress(Exception):
                temp_path.unlink()
            details["recording_playback_error"] = str(exc)
            return None, details

    def _estimate_mp3_duration_seconds(path: Path) -> float | None:
        bitrate_table = {
            (3, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
            (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0],
            (3, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0],
            (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
            (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
            (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0],
        }
        sample_rate_table = {
            3: [44100, 48000, 32000, 0],
            2: [22050, 24000, 16000, 0],
            0: [11025, 12000, 8000, 0],
        }
        try:
            data = path.read_bytes()
        except Exception:
            return None
        if len(data) < 4:
            return None
        offset = 0
        if data[:3] == b"ID3" and len(data) >= 10:
            tag_size = (
                ((data[6] & 0x7F) << 21)
                | ((data[7] & 0x7F) << 14)
                | ((data[8] & 0x7F) << 7)
                | (data[9] & 0x7F)
            )
            offset = min(len(data), 10 + tag_size)
        duration = 0.0
        frames = 0
        i = offset
        while i + 4 <= len(data):
            header = int.from_bytes(data[i : i + 4], "big")
            if (header & 0xFFE00000) != 0xFFE00000:
                i += 1
                continue
            version_id = (header >> 19) & 0x3
            layer_id = (header >> 17) & 0x3
            bitrate_idx = (header >> 12) & 0xF
            sample_idx = (header >> 10) & 0x3
            padding = (header >> 9) & 0x1
            if version_id == 1 or layer_id == 0 or bitrate_idx in {0, 15} or sample_idx == 3:
                i += 1
                continue
            version_key = 3 if version_id == 3 else 2
            bitrate_kbps = bitrate_table.get((version_key, layer_id), [0] * 16)[bitrate_idx]
            sample_rate = sample_rate_table.get(version_id, [0] * 4)[sample_idx]
            if not bitrate_kbps or not sample_rate:
                i += 1
                continue
            bitrate = bitrate_kbps * 1000
            if layer_id == 3:
                samples_per_frame = 384
                frame_length = int(((12 * bitrate / sample_rate) + padding) * 4)
            elif layer_id == 2:
                samples_per_frame = 1152
                frame_length = int((144 * bitrate / sample_rate) + padding)
            else:
                samples_per_frame = 1152 if version_id == 3 else 576
                coefficient = 144 if version_id == 3 else 72
                frame_length = int((coefficient * bitrate / sample_rate) + padding)
            if frame_length <= 4:
                i += 1
                continue
            duration += samples_per_frame / sample_rate
            frames += 1
            i += frame_length
        return duration if frames > 0 else None

    def _is_partial_recording(
        *,
        audio_duration_seconds: float | None,
        expected_duration_seconds: float | None,
        content_length: int | None = None,
        file_size: int | None = None,
    ) -> bool:
        if content_length and file_size is not None and file_size < content_length:
            return True
        if audio_duration_seconds is None or expected_duration_seconds is None or expected_duration_seconds <= 10:
            return False
        return audio_duration_seconds < max(5.0, expected_duration_seconds * 0.75)

    def _serve_audio_file_with_range(path: Path, request: Request, filename: str) -> Response:
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="Recording not found.")
        file_size = path.stat().st_size
        media_type = "audio/mpeg" if path.suffix.lower() in {".mp3", ".mpeg"} else "audio/wav"
        common_headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": media_type,
            "Cache-Control": "private, max-age=60",
        }
        range_header = str(request.headers.get("range") or "").strip()
        if not range_header:
            headers = {
                **common_headers,
                "Content-Length": str(file_size),
                "Content-Disposition": f'inline; filename="{filename}"',
            }
            return FileResponse(path, media_type=media_type, filename=filename, headers=headers)

        match = re.match(r"bytes=(\d*)-(\d*)$", range_header)
        if not match:
            return Response(status_code=416, headers={**common_headers, "Content-Range": f"bytes */{file_size}"})
        start_text, end_text = match.groups()
        if start_text == "" and end_text == "":
            return Response(status_code=416, headers={**common_headers, "Content-Range": f"bytes */{file_size}"})
        if start_text == "":
            suffix_length = int(end_text)
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
        if start >= file_size or end < start:
            return Response(status_code=416, headers={**common_headers, "Content-Range": f"bytes */{file_size}"})
        end = min(end, file_size - 1)
        chunk_size = end - start + 1

        def _iter_file() -> Any:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    chunk = handle.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            **common_headers,
            "Content-Length": str(chunk_size),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Disposition": f'inline; filename="{filename}"',
        }
        return StreamingResponse(_iter_file(), status_code=206, media_type=media_type, headers=headers)

    def _build_recording_entry(client_id: str, session_dir: Path) -> dict[str, Any] | None:
        artifacts_path = session_dir / "artifacts.json"
        artifacts = _read_json_file(artifacts_path)
        if not isinstance(artifacts, dict):
            return None
        piopiy_recording_meta = _read_json_file(session_dir / "piopiy_recording.json")
        memory = artifacts.get("memory") if isinstance(artifacts.get("memory"), dict) else {}
        telephony_context = artifacts.get("telephony_context") if isinstance(artifacts.get("telephony_context"), dict) else {}
        metrics = artifacts.get("metrics") if isinstance(artifacts.get("metrics"), dict) else {}
        actual_cost = artifacts.get("actual_cost") if isinstance(artifacts.get("actual_cost"), dict) else {}
        provider_usage = actual_cost.get("raw_provider_usage") if isinstance(actual_cost.get("raw_provider_usage"), dict) else {}
        started_at = _parse_iso_datetime(artifacts.get("started_at"))
        ended_at = _parse_iso_datetime(artifacts.get("ended_at"))
        lead_name = (
            str(memory.get("lead_name") or "").strip()
            or str(telephony_context.get("customer_name") or "").strip()
            or str(telephony_context.get("from_number") or "").strip()
            or str(telephony_context.get("caller_id") or "").strip()
            or "Unknown"
        )
        provider = (
            str(actual_cost.get("telephony_provider") or "").strip()
            or str(provider_usage.get("provider") or "").strip()
            or str(telephony_context.get("provider") or "").strip()
            or "unknown"
        )
        def _coerce_positive_duration(*values: Any) -> float:
            for value in values:
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    return parsed
            return 0.0

        duration_seconds = _coerce_positive_duration(
            metrics.get("conversation_audio_seconds"),
            metrics.get("caller_audio_seconds"),
            provider_usage.get("stream_duration_seconds"),
            metrics.get("session_seconds"),
            telephony_context.get("cdr_duration_seconds"),
            telephony_context.get("duration_seconds"),
        )
        if not duration_seconds and started_at and ended_at and ended_at >= started_at:
            duration_seconds = (ended_at - started_at).total_seconds()
        files: list[dict[str, str]] = []
        seen_filenames: set[str] = set()

        def _looks_like_audio_file(path: Path) -> bool:
            if path.suffix.lower() not in ALLOWED_RECORDING_SUFFIXES:
                return False
            if not path.exists() or not path.is_file():
                return False
            try:
                if path.stat().st_size <= 0:
                    return False
                with path.open("rb") as handle:
                    prefix = handle.read(32).lstrip()
                return not prefix.startswith((b"{", b"["))
            except Exception:
                return False

        def _add_recording_file(label: str, filename: str) -> None:
            if not filename or filename in seen_filenames:
                return
            audio_path = session_dir / filename
            if not _looks_like_audio_file(audio_path):
                return
            seen_filenames.add(filename)
            files.append(
                {
                    "label": label,
                    "filename": filename,
                    "url": f"/api/recordings/{urllib.parse.quote(client_id)}/{urllib.parse.quote(session_dir.name)}/{urllib.parse.quote(filename)}",
                }
            )

        for filename, label in (
            ("conversation_audio.wav", "Full Conversation"),
            ("caller_audio.wav", "Caller"),
            ("agent_audio.wav", "Agent"),
        ):
            _add_recording_file(label, filename)
        recording_url = ""
        piopiy_filename = ""
        recording_status = ""
        recording_error = ""
        recording_duration_seconds = None
        if isinstance(piopiy_recording_meta, dict):
            recording_url = str(piopiy_recording_meta.get("recording_url") or "").strip()
            piopiy_filename = str(piopiy_recording_meta.get("recording_filename") or "").strip()
            recording_status = str(piopiy_recording_meta.get("recording_status") or "").strip()
            recording_error = str(piopiy_recording_meta.get("recording_error") or "").strip()
            selected_recording_type = str(piopiy_recording_meta.get("selected_recording_type") or "").strip()
            partial_reason = str(piopiy_recording_meta.get("recording_partial_reason") or "").strip()
            if recording_status == "partial":
                if partial_reason:
                    recording_error = partial_reason
                elif selected_recording_type == "ai_leg" and recording_error == "recording_duration_shorter_than_call":
                    recording_error = "ai_leg_only"
                elif selected_recording_type == "caller_leg" and recording_error == "recording_duration_shorter_than_call":
                    recording_error = "caller_leg_only"
            try:
                recording_duration_seconds = float(piopiy_recording_meta.get("recording_duration_seconds") or 0) or None
            except (TypeError, ValueError):
                recording_duration_seconds = None
            try:
                playback_duration_seconds = (
                    float(piopiy_recording_meta.get("recording_playback_duration_seconds") or 0) or None
                )
            except (TypeError, ValueError):
                playback_duration_seconds = None
            if playback_duration_seconds and (
                recording_duration_seconds is None or playback_duration_seconds > recording_duration_seconds
            ):
                recording_duration_seconds = playback_duration_seconds
        if not recording_url:
            recording_url = str(telephony_context.get("piopiy_recording_url") or "").strip()
        if not piopiy_filename:
            piopiy_filename = str(telephony_context.get("piopiy_recording_filename") or "").strip()
        if not recording_status:
            recording_status = str(telephony_context.get("recording_status") or "").strip()
        if not recording_error:
            recording_error = str(telephony_context.get("recording_error") or "").strip()
        if recording_url and not piopiy_filename:
            parsed = urllib.parse.urlparse(recording_url)
            piopiy_filename = Path(parsed.path).name
        if piopiy_filename:
            playback_filename = ""
            if isinstance(piopiy_recording_meta, dict):
                playback_filename = str(piopiy_recording_meta.get("recording_playback_filename") or "").strip()
            source_path = session_dir / piopiy_filename
            if not playback_filename and source_path.exists():
                playback_path, playback_meta = _ensure_browser_playback_recording(source_path)
                if playback_path is not None:
                    playback_filename = playback_path.name
                if isinstance(piopiy_recording_meta, dict) and playback_meta:
                    with contextlib.suppress(Exception):
                        piopiy_recording_meta.update(
                            {key: value for key, value in playback_meta.items() if value is not None}
                        )
                        (session_dir / "piopiy_recording.json").write_text(
                            json.dumps(piopiy_recording_meta, indent=2, ensure_ascii=True),
                            encoding="utf-8",
                        )
            if playback_filename:
                _add_recording_file("Piopiy Recording", playback_filename)
                seen_filenames.add(piopiy_filename)
            else:
                _add_recording_file("Piopiy Recording", piopiy_filename)
        for audio_path in sorted(session_dir.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
            if audio_path.name in seen_filenames:
                continue
            if audio_path.name in {"piopiy_recording.wav"}:
                continue
            _add_recording_file("Piopiy Recording", audio_path.name)
        if files:
            recording_status = "saved"
            recording_error = ""
            if recording_duration_seconds is None and piopiy_filename:
                recording_duration_seconds = _probe_audio_duration_seconds(session_dir / piopiy_filename)
            if _is_partial_recording(
                audio_duration_seconds=recording_duration_seconds,
                expected_duration_seconds=duration_seconds,
            ):
                recording_status = "partial"
                recording_error = recording_error or "recording_duration_shorter_than_call"
                if isinstance(piopiy_recording_meta, dict):
                    selected_recording_type = str(piopiy_recording_meta.get("selected_recording_type") or "").strip()
                    if selected_recording_type == "ai_leg" and recording_error == "recording_duration_shorter_than_call":
                        recording_error = "ai_leg_only"
                    elif selected_recording_type == "caller_leg" and recording_error == "recording_duration_shorter_than_call":
                        recording_error = "caller_leg_only"
            if isinstance(piopiy_recording_meta, dict) and piopiy_filename:
                with contextlib.suppress(Exception):
                    piopiy_recording_meta["recording_status"] = recording_status
                    piopiy_recording_meta["recording_error"] = recording_error or None
                    if recording_duration_seconds is not None:
                        piopiy_recording_meta["recording_duration_seconds"] = round(recording_duration_seconds, 3)
                    (session_dir / "piopiy_recording.json").write_text(
                        json.dumps(piopiy_recording_meta, indent=2, ensure_ascii=True),
                        encoding="utf-8",
                    )
        resolved_project_app_id = _resolve_project_piopiy_app_id(
            client_id,
            str(artifacts.get("project_id") or telephony_context.get("project_id") or "").strip() or None,
        )
        recording_pending = bool(
            provider == "piopiy"
            and not files
            and resolved_project_app_id
            and recording_status not in {"failed", "unavailable"}
        )
        recording_message = None
        if recording_pending:
            recording_message = "Recording metadata is present, but Piopiy has not returned a playable recording file yet."
        elif provider == "piopiy" and files and recording_status == "partial":
            recording_message = "Recording saved, but duration looks shorter than the call."
        elif provider == "piopiy" and not files and recording_status in {"failed", "unavailable"}:
            recording_message = f"Recording unavailable: {recording_error or recording_status}."
        return {
            "client_id": client_id,
            "session_id": str(artifacts.get("session_id") or session_dir.name),
            "project_id": str(artifacts.get("project_id") or "").strip() or None,
            "project_name": str(artifacts.get("project_name") or "").strip() or None,
            "lead_name": lead_name,
            "provider": provider,
            "from_number": str(
                telephony_context.get("from_number")
                or telephony_context.get("caller_id")
                or telephony_context.get("caller_number")
                or telephony_context.get("from")
                or ""
            ).strip() or None,
            "to_number": str(telephony_context.get("to_number") or "").strip() or None,
            "started_at": started_at.isoformat() if started_at else None,
            "ended_at": ended_at.isoformat() if ended_at else None,
            "duration_seconds": round(float(duration_seconds or 0.0), 1),
            "session_dir": str(session_dir),
            "files": files,
            "recording_status": recording_status or ("saved" if files else None),
            "recording_error": recording_error or None,
            "recording_duration_seconds": round(recording_duration_seconds, 3) if recording_duration_seconds is not None else None,
            "recording_pending": recording_pending,
            "recording_pending_message": recording_message,
        }

    def _list_workspace_recordings(client_id: str) -> list[dict[str, Any]]:
        client_dir = (settings.session_output_dir / client_id).resolve()
        if not client_dir.exists() or not client_dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for session_dir in client_dir.iterdir():
            if not session_dir.is_dir():
                continue
            item = _build_recording_entry(client_id, session_dir)
            if item is not None:
                items.append(item)
        items.sort(
            key=lambda item: (
                str(item.get("started_at") or ""),
                str(item.get("session_id") or ""),
            ),
            reverse=True,
        )
        return items

    GUEST_DEMO_TEMPLATE_CLIENT_ID = "aivoicebot4u_guest_demo"

    def _demo_project_language(project_id: str | None) -> str:
        normalized = str(project_id or "").strip().lower()
        if normalized in {"magnum_hospital_marathi_demo", "janardan_swami_cancer_helpdesk_demo", "led_arts_marathi_demo"}:
            return "marathi"
        if normalized == "car_dealer_hindi_demo":
            return "hindi"
        return "english"

    def _sync_guest_demo_workspace(user: dict[str, str]) -> str:
        workspace_client_id = _workspace_client_id_for_user(user)
        existing_ids = list_client_ids()
        template_client_id = _resolve_safe_template_client_id(
            existing_ids,
            preferred=[
                GUEST_DEMO_TEMPLATE_CLIENT_ID,
                "acme_health",
                "nova_security",
                "led_arts",
                settings.default_client_id,
            ],
        )
        payload = get_client_editor_payload(template_client_id)
        config = dict(payload.get("config") or {})
        config["display_name"] = "AI Voice Bot 4 U Demo Studio"
        config["status"] = "draft"
        existing_tags = list(config.get("tags") or [])
        for tag in ("guest_demo", "user_workspace"):
            if tag not in existing_tags:
                existing_tags.append(tag)
        config["tags"] = existing_tags
        config["import_metadata"] = {
            **dict(config.get("import_metadata") or {}),
            "source": "guest_demo_workspace",
            "external_id": str(user.get("id") or ""),
            "imported_from": template_client_id,
            "notes": [
                "Public guest demo workspace synced from the guest demo template.",
            ],
        }
        payload["config"] = config
        try:
            save_client_editor_payload(workspace_client_id, payload)
        except FileNotFoundError:
            source_dir = Path(__file__).resolve().parent.parent / "clients" / template_client_id
            target_dir = Path(__file__).resolve().parent.parent / "clients" / workspace_client_id
            if source_dir.exists() and not target_dir.exists():
                shutil.copytree(source_dir, target_dir)
            save_client_editor_payload(workspace_client_id, payload)
        return workspace_client_id

    def _html(filename: str) -> str:
        return (static_dir / filename).read_text(encoding="utf-8")

    def _sanitize_guest_visitor_id(raw_value: str | None) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "", str(raw_value or "").strip().lower())
        if 12 <= len(normalized) <= 48:
            return normalized
        return ""

    def _new_guest_visitor_id() -> str:
        return f"{int(time.time()):x}{uuid4().hex}"[:40]

    def _resolve_guest_visitor_id(request: Request) -> str:
        header_value = request.headers.get("x-guest-visitor-id")
        query_value = request.query_params.get("visitor_id")
        cookie_value = request.cookies.get(GUEST_VISITOR_COOKIE_NAME)
        resolved = _sanitize_guest_visitor_id(header_value or query_value or cookie_value)
        return resolved or _new_guest_visitor_id()

    def _resolve_existing_guest_visitor_id(request: Request) -> str:
        header_value = request.headers.get("x-guest-visitor-id")
        query_value = request.query_params.get("visitor_id")
        cookie_value = request.cookies.get(GUEST_VISITOR_COOKIE_NAME)
        return _sanitize_guest_visitor_id(header_value or query_value or cookie_value)

    def _resolve_guest_demo_user_from_request(request: Request) -> dict[str, str] | None:
        visitor_id = _resolve_existing_guest_visitor_id(request)
        if not visitor_id:
            return None
        return auth_manager.get_or_create_public_guest_user(visitor_id)

    def _cookie_secure_enabled() -> bool:
        force_secure = os.getenv("SESSION_COOKIE_SECURE", "").strip().lower()
        if force_secure in {"1", "true", "yes", "on"}:
            return True
        if force_secure in {"0", "false", "no", "off"}:
            return False
        base = str(settings.public_base_url or "").strip().lower()
        return base.startswith("https://")

    def _set_auth_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure_enabled(),
            path="/",
        )

    def _set_guest_visitor_cookie(response: Response, visitor_id: str) -> None:
        normalized = _sanitize_guest_visitor_id(visitor_id)
        if not normalized:
            return
        response.set_cookie(
            key=GUEST_VISITOR_COOKIE_NAME,
            value=normalized,
            max_age=GUEST_VISITOR_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure_enabled(),
            path="/",
        )

    def _clear_auth_cookie(response: Response) -> None:
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")

    def _is_user_workspace_client_id(client_id: str | None) -> bool:
        normalized = str(client_id or "").strip().lower()
        return normalized.startswith("user_")

    def _resolve_safe_template_client_id(
        existing_ids: list[str],
        preferred: list[str] | None = None,
    ) -> str:
        preferred_ids = [candidate for candidate in (preferred or []) if candidate]
        for candidate in preferred_ids:
            if candidate in existing_ids and not _is_user_workspace_client_id(candidate):
                return candidate
        for candidate in existing_ids:
            if not _is_user_workspace_client_id(candidate):
                return candidate
        return existing_ids[0]

    def _is_admin_page_path(path: str) -> bool:
        return path in {"/admin", "/admin/", "/admin/leads"}

    def _is_admin_api_path(path: str) -> bool:
        return path.startswith("/api/admin/")

    def _is_telephony_callback_path(path: str) -> bool:
        return (
            path.startswith("/twilio/")
            or path.startswith("/exotel/")
            or path.startswith("/airtel-iq/")
            or path.startswith("/piopiy/")
            or path.startswith("/meta-whatsapp/")
        )

    def _is_protected_page_path(path: str) -> bool:
        return path in {"/app", "/dashboard"} or _is_admin_page_path(path)

    def _is_protected_api_path(path: str) -> bool:
        return (
            (path.startswith("/api/") or _is_admin_api_path(path))
            and not path.startswith("/api/auth/")
            and path != "/api/demo/options"
        )

    def _is_guest_demo_api_path(path: str) -> bool:
        return path in {
            "/api/session",
            "/api/session/start",
            "/api/session/stop",
            "/api/live-preview/extract",
        }

    def _require_admin_user(request: Request) -> dict[str, str]:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        if not auth_manager.is_admin(user):
            raise HTTPException(status_code=403, detail="Admin access required.")
        return user

    def _persist_env_setting(key: str, value: str) -> None:
        env_path = PROJECT_ROOT / ".env"
        desired_line = f"{key}={value}"
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
        else:
            lines = []
        replaced = False
        updated_lines: list[str] = []
        for line in lines:
            if line.strip().startswith("#") or "=" not in line:
                updated_lines.append(line)
                continue
            existing_key = line.split("=", 1)[0].strip()
            if existing_key == key:
                updated_lines.append(desired_line)
                replaced = True
            else:
                updated_lines.append(line)
        if not replaced:
            updated_lines.append(desired_line)
        env_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")

    def _is_start_demo_request(request: Request) -> bool:
        value = (request.query_params.get("start_demo") or "").strip().lower()
        return value in {"1", "true", "yes"}

    async def _safe_stop_session(session_key: str) -> None:
        try:
            latest_context = await telephony.get_call_context(session_key)
            if latest_context:
                await controller.merge_telephony_context(session_key, latest_context)
            await controller.stop(session_key=session_key)
        except Exception:
            logger.exception("Failed to stop session cleanly for session_key=%s", session_key)

    @app.middleware("http")
    async def log_http_requests(request: Request, call_next):
        if _is_telephony_callback_path(request.url.path):
            logger.info(
                "Telephony HTTP request: method=%s path=%s query=%s user_agent=%s",
                request.method,
                request.url.path,
                request.url.query,
                request.headers.get("user-agent", ""),
            )
        response = await call_next(request)
        if _is_telephony_callback_path(request.url.path):
            logger.info(
                "Telephony HTTP response: path=%s status=%s",
                request.url.path,
                response.status_code,
            )
        return response

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(self), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if _cookie_secure_enabled():
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response

    @app.middleware("http")
    async def enforce_authentication(request: Request, call_next):
        path = request.url.path
        user = auth_manager.current_user_from_request(request)
        request.state.auth_user = user
        user_is_guest = auth_manager.is_guest_user(user)
        if _is_protected_page_path(path) or _is_protected_api_path(path):
            if user is None:
                guest_user = None
                if path in {
                    "/api/session",
                    "/api/session/start",
                    "/api/session/stop",
                    "/api/live-preview/extract",
                }:
                    guest_user = _resolve_guest_demo_user_from_request(request)
                if guest_user is not None:
                    request.state.auth_user = guest_user
                    response = await call_next(request)
                    return response
                if path == "/app" and _is_start_demo_request(request):
                    visitor_id = _resolve_guest_visitor_id(request)
                    demo_user = auth_manager.get_or_create_public_guest_user(visitor_id)
                    demo_token = auth_manager.issue_session_token(demo_user)
                    request.state.auth_user = demo_user
                    response = await call_next(request)
                    _set_auth_cookie(response, demo_token)
                    _set_guest_visitor_cookie(response, visitor_id)
                    return response
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Authentication required."}, status_code=401)
                next_target = path
                if request.url.query:
                    next_target = f"{path}?{request.url.query}"
                login_url = f"/login?next={urllib.parse.quote(next_target, safe='')}"
                return RedirectResponse(url=login_url, status_code=303)
            if user_is_guest:
                if _is_protected_page_path(path):
                    return RedirectResponse(url="/login", status_code=303)
                if _is_protected_api_path(path) and not _is_guest_demo_api_path(path):
                    return JSONResponse(
                        {"detail": "Authentication required. Please log in to access dashboard APIs."},
                        status_code=401,
                    )
            if (_is_admin_page_path(path) or _is_admin_api_path(path)) and not auth_manager.is_admin(user):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Admin access required."}, status_code=403)
                return RedirectResponse(url="/admin/login", status_code=303)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def landing_page() -> str:
        return _html("landing.html")

    @app.get("/logo.png")
    async def logo_asset() -> Response:
        logo_path = static_dir / "logo.png"
        if not logo_path.exists():
            raise HTTPException(status_code=404, detail="Logo not found.")
        return FileResponse(path=str(logo_path), media_type="image/png")

    @app.get("/static/{asset_name}")
    async def static_asset(asset_name: str) -> Response:
        safe_name = Path(asset_name).name
        asset_path = static_dir / safe_name
        if not asset_path.exists() or not asset_path.is_file():
            raise HTTPException(status_code=404, detail="Asset not found.")
        media_type, _ = mimetypes.guess_type(str(asset_path))
        return FileResponse(path=str(asset_path), media_type=media_type or "application/octet-stream")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        user = getattr(request.state, "auth_user", None)
        if user is not None and not auth_manager.is_guest_user(user):
            return RedirectResponse(url="/app", status_code=303)
        return HTMLResponse(content=_html("login.html"))

    @app.get("/signup", response_class=HTMLResponse)
    async def signup_page(request: Request) -> Response:
        user = getattr(request.state, "auth_user", None)
        if user is not None and not auth_manager.is_guest_user(user):
            return RedirectResponse(url="/app", status_code=303)
        return HTMLResponse(content=_html("signup.html"))

    @app.get("/forgot-password", response_class=HTMLResponse)
    async def forgot_password_page(request: Request) -> Response:
        user = getattr(request.state, "auth_user", None)
        if user is not None and not auth_manager.is_guest_user(user):
            return RedirectResponse(url="/app", status_code=303)
        return HTMLResponse(content=_html("forgot-password.html"))

    @app.get("/admin/login", response_class=HTMLResponse)
    async def admin_login_page(request: Request) -> Response:
        if auth_manager.is_admin(getattr(request.state, "auth_user", None)):
            return RedirectResponse(url="/admin/leads", status_code=303)
        return HTMLResponse(content=_html("admin_login.html"))

    @app.get("/admin")
    async def admin_redirect() -> Response:
        return RedirectResponse(url="/admin/leads", status_code=307)

    @app.get("/admin/leads", response_class=HTMLResponse)
    async def admin_leads_page(request: Request) -> Response:
        _require_admin_user(request)
        return HTMLResponse(content=_html("admin_leads.html"))

    @app.get("/app", response_class=HTMLResponse)
    async def app_page() -> str:
        return _html("index.html")

    @app.get("/dashboard")
    async def dashboard_redirect() -> Response:
        return RedirectResponse(url="/app", status_code=307)

    @app.get("/api/auth/me")
    async def auth_me(request: Request) -> dict[str, object]:
        user = getattr(request.state, "auth_user", None)
        if user is None or auth_manager.is_guest_user(user):
            raise HTTPException(status_code=401, detail="Authentication required.")
        return {"authenticated": True, "user": auth_manager.public_user(user)}

    @app.post("/api/auth/signup")
    async def auth_signup(payload: AuthSignupRequest) -> Response:
        try:
            user = auth_manager.signup(name=payload.name, email=payload.email, password=payload.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        token = auth_manager.issue_session_token(user)
        response = JSONResponse(
            {
                "status": "ok",
                "message": "Account created successfully.",
                "user": auth_manager.public_user(user),
            }
        )
        _set_auth_cookie(response, token)
        return response

    @app.post("/api/auth/login")
    async def auth_login(payload: AuthLoginRequest) -> Response:
        try:
            user = auth_manager.login(email=payload.email, password=payload.password)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        token = auth_manager.issue_session_token(user)
        response = JSONResponse(
            {
                "status": "ok",
                "message": "Signed in successfully.",
                "user": auth_manager.public_user(user),
            }
        )
        _set_auth_cookie(response, token)
        return response

    @app.post("/api/auth/logout")
    async def auth_logout() -> Response:
        response = JSONResponse({"status": "ok"})
        _clear_auth_cookie(response)
        return response

    @app.post("/api/auth/demo-start")
    async def auth_demo_start(request: Request) -> Response:
        visitor_id = _resolve_guest_visitor_id(request)
        user = auth_manager.get_or_create_public_guest_user(visitor_id)
        token = auth_manager.issue_session_token(user)
        response = JSONResponse(
            {
                "status": "ok",
                "message": "Demo session ready.",
                "user": auth_manager.public_user(user),
            }
        )
        _set_auth_cookie(response, token)
        _set_guest_visitor_cookie(response, visitor_id)
        return response

    @app.get("/api/demo/options")
    async def demo_options(request: Request) -> Response:
        visitor_id = _resolve_guest_visitor_id(request)
        user = auth_manager.get_or_create_public_guest_user(visitor_id)
        workspace_client_id = _sync_guest_demo_workspace(user)
        token = auth_manager.issue_session_token(user)
        projects = list_client_projects(workspace_client_id)
        response = JSONResponse(
            {
                "status": "ok",
                "user": auth_manager.public_user(user),
                "workspace_client_id": workspace_client_id,
                "workspace_session_key": _workspace_session_key_for_user(user),
                "projects": [
                    {
                        **project,
                        "demo_language": _demo_project_language(project.get("project_id")),
                    }
                    for project in projects
                ],
            }
        )
        _set_auth_cookie(response, token)
        _set_guest_visitor_cookie(response, visitor_id)
        return response

    @app.post("/api/auth/forgot-password")
    async def auth_forgot_password(payload: AuthForgotPasswordRequest) -> dict[str, str]:
        _ = payload.email.strip()
        return {"status": "ok", "message": "If this email exists, reset instructions have been sent."}

    @app.get("/api/admin/leads")
    async def admin_guest_demo_leads(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search: str = "",
        project_id: str = "",
        status: str = "",
    ) -> dict[str, object]:
        _require_admin_user(request)
        result = auth_store.list_guest_demo_leads(
            limit=limit,
            offset=offset,
            search=search or None,
            project_id=project_id or None,
            status=status or None,
        )
        return {
            "items": result["items"],
            "total": result["total"],
            "limit": result["limit"],
            "offset": result["offset"],
            "projects": result["projects"],
        }

    @app.get("/api/admin/leads/export")
    async def admin_guest_demo_leads_export(
        request: Request,
        search: str = "",
        project_id: str = "",
        status: str = "",
    ) -> Response:
        _require_admin_user(request)
        result = auth_store.list_guest_demo_leads(
            limit=5000,
            offset=0,
            search=search or None,
            project_id=project_id or None,
            status=status or None,
        )
        workbook_bytes = _build_guest_demo_leads_workbook(result["items"])
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        headers = {
            "Content-Disposition": f'attachment; filename="guest_demo_leads_{timestamp}.xlsx"'
        }
        return Response(
            content=workbook_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers=headers,
        )

    @app.get("/api/clients")
    async def clients(request: Request) -> dict[str, object]:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, created = _ensure_user_workspace_client(user)
        workspace_client_name = workspace_client_id
        with contextlib.suppress(Exception):
            workspace_bundle = load_client(workspace_client_id)
            workspace_client_name = (
                str(workspace_bundle.config.display_name or "").strip() or workspace_client_id
            )
        return {
            "clients": [workspace_client_id],
            "client_items": [
                {
                    "client_id": workspace_client_id,
                    "label": workspace_client_name,
                }
            ],
            "workspace_client_id": workspace_client_id,
            "workspace_session_key": _workspace_session_key_for_user(user),
            "requires_configuration": created,
        }

    @app.post("/api/client-builder/draft")
    async def client_builder_draft(request: ClientBuilderRequest, http_request: Request) -> dict[str, Any]:
        user = getattr(http_request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        return _build_client_builder_payload(request, workspace_client_id=workspace_client_id)

    @app.get("/api/settings")
    async def app_settings() -> dict[str, str | int]:
        return {
            "telephony_provider": settings.telephony_provider,
            "client_store_backend": settings.client_store_backend,
            "telephony_max_concurrent_sessions": await controller.max_concurrent_sessions(),
        }

    @app.put("/api/settings")
    async def update_app_settings(payload: AppSettingsUpdateRequest, request: Request) -> dict[str, str | int]:
        _require_admin_user(request)
        if payload.telephony_max_concurrent_sessions < 1:
            raise HTTPException(status_code=400, detail="Max concurrent sessions must be at least 1.")
        updated = await controller.update_max_concurrent_sessions(payload.telephony_max_concurrent_sessions)
        settings.telephony_max_concurrent_sessions = updated
        os.environ["TELEPHONY_MAX_CONCURRENT_SESSIONS"] = str(updated)
        _persist_env_setting("TELEPHONY_MAX_CONCURRENT_SESSIONS", str(updated))
        return {
            "telephony_provider": settings.telephony_provider,
            "client_store_backend": settings.client_store_backend,
            "telephony_max_concurrent_sessions": updated,
        }

    @app.get("/api/lead-sources")
    async def lead_sources() -> dict[str, object]:
        return {"sources": _lead_source_catalog()}

    @app.get("/api/analytics/overview")
    async def analytics_overview(request: Request) -> dict[str, object]:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        results_base_template = "default_v1"
        results_extra_fields: list[str] = []
        with contextlib.suppress(Exception):
            bundle = load_client(workspace_client_id)
            results_base_template = str(bundle.config.conversation.results_base_template or "default_v1").strip() or "default_v1"
            results_extra_fields = list(bundle.config.conversation.results_extra_fields or [])
        return build_dashboard_analytics(
            settings.session_output_dir,
            client_ids={workspace_client_id},
            results_base_template=results_base_template,
            results_extra_fields=results_extra_fields,
        )

    @app.get("/api/workspace/summary")
    async def workspace_summary(request: Request) -> dict[str, object]:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        return {
            "user": auth_manager.public_user(user),
            "workspace_client_id": workspace_client_id,
            "session_key": _workspace_session_key_for_user(user),
            "billing": build_workspace_billing_summary(
                settings.session_output_dir,
                client_ids={workspace_client_id},
            ),
        }

    @app.get("/api/recordings")
    async def workspace_recordings(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, object]:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        items = _list_workspace_recordings(workspace_client_id)
        return {
            "workspace_client_id": workspace_client_id,
            "total": len(items),
            "items": items[:limit],
        }

    @app.get("/api/recordings/{client_id}/{session_id}/{filename}")
    async def workspace_recording_file(
        client_id: str,
        session_id: str,
        filename: str,
        request: Request,
    ) -> Response:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        if client_id != workspace_client_id:
            raise HTTPException(status_code=403, detail="Access denied for this recording.")
        client_root = (settings.session_output_dir / workspace_client_id).resolve()
        target_path = (client_root / session_id / filename).resolve()
        try:
            target_path.relative_to(client_root)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Invalid recording path.") from exc
        if target_path.suffix.lower() not in ALLOWED_RECORDING_SUFFIXES:
            target_path = None
        if target_path is not None and target_path.exists() and target_path.is_file():
            with contextlib.suppress(Exception):
                if target_path.stat().st_size > 0:
                    with target_path.open("rb") as handle:
                        prefix = handle.read(32).lstrip()
                    if prefix.startswith((b"{", b"[")):
                        target_path = None
            if target_path is not None:
                return _serve_audio_file_with_range(target_path, request, filename)

        piopiy_meta_path = (client_root / session_id / "piopiy_recording.json").resolve()
        recording_url = ""
        recording_filename = ""
        if piopiy_meta_path.exists() and piopiy_meta_path.is_file():
            meta = _read_json_file(piopiy_meta_path)
            if isinstance(meta, dict):
                recording_url = str(meta.get("recording_url") or "").strip()
                recording_filename = str(meta.get("recording_filename") or "").strip()
                if not recording_filename and recording_url:
                    parsed = urllib.parse.urlparse(recording_url)
                    recording_filename = Path(parsed.path).name
        if not recording_url:
            artifacts = _read_json_file(client_root / session_id / "artifacts.json")
            telephony_context = {}
            if isinstance(artifacts, dict) and isinstance(artifacts.get("telephony_context"), dict):
                telephony_context = artifacts["telephony_context"]
            recording_url = str(telephony_context.get("piopiy_recording_url") or "").strip()
            if not recording_filename:
                recording_filename = str(telephony_context.get("piopiy_recording_filename") or "").strip()
                if not recording_filename and recording_url:
                    parsed = urllib.parse.urlparse(recording_url)
                    recording_filename = Path(parsed.path).name
        if recording_url and (filename == recording_filename or not recording_filename):
            request_headers = _piopiy_recording_request_headers(recording_url)
            async with httpx.AsyncClient(follow_redirects=True, timeout=45.0) as client:
                response = await client.get(recording_url, headers=request_headers)
                response.raise_for_status()
            content_type = str(response.headers.get("content-type") or "").strip() or "audio/mpeg"
            if not content_type.startswith("audio/"):
                raise HTTPException(status_code=404, detail="Recording audio not available yet.")
            if not recording_filename:
                parsed = urllib.parse.urlparse(recording_url)
                recording_filename = Path(parsed.path).name or filename
            recording_path = (client_root / session_id / recording_filename).resolve()
            recording_path.write_bytes(response.content)
            expected_content_length = None
            with contextlib.suppress(Exception):
                expected_content_length = int(str(response.headers.get("content-length") or "").strip())
            recording_duration_seconds = _probe_audio_duration_seconds(recording_path)
            playback_path, playback_meta = _ensure_browser_playback_recording(recording_path)
            meta_path = (client_root / session_id / "piopiy_recording.json").resolve()
            with contextlib.suppress(Exception):
                meta = _read_json_file(meta_path) or {}
                if not isinstance(meta, dict):
                    meta = {}
                meta.update(
                    {
                        "recording_url": recording_url,
                        "recording_filename": recording_filename,
                        "recording_path": str(recording_path),
                        "recording_content_type": content_type,
                        "recording_size_bytes": len(response.content),
                        "recording_expected_content_length": expected_content_length,
                        "recording_duration_seconds": round(recording_duration_seconds, 3) if recording_duration_seconds is not None else None,
                        "recording_status": "saved",
                        "recording_error": None,
                    }
                )
                meta.update({key: value for key, value in playback_meta.items() if value is not None})
                meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=True), encoding="utf-8")
            served_path = playback_path if playback_path is not None else recording_path
            return _serve_audio_file_with_range(served_path, request, served_path.name)

        if filename == "piopiy_recording.wav":
            artifacts = _read_json_file(client_root / session_id / "artifacts.json")
            telephony_context: dict[str, Any] = {}
            started_at = ended_at = None
            call_id_candidates: list[str] = []
            caller_id = to_number = direction = None
            app_id = None
            project_id = None
            if isinstance(artifacts, dict):
                telephony_context = artifacts.get("telephony_context") if isinstance(artifacts.get("telephony_context"), dict) else {}
                started_at = _parse_iso_datetime(artifacts.get("started_at"))
                ended_at = _parse_iso_datetime(artifacts.get("ended_at"))
                project_id = str(
                    artifacts.get("project_id")
                    or telephony_context.get("project_id")
                    or ""
                ).strip() or None
                call_id_candidates.extend(_piopiy_payload_call_ids(telephony_context))
                app_id = str(
                    telephony_context.get("app_id")
                    or telephony_context.get("appid")
                    or telephony_context.get("piopiy_app_id")
                    or ""
                ).strip() or None
                if not app_id:
                    app_id = _resolve_project_piopiy_app_id(workspace_client_id, project_id)
                caller_id = str(
                    telephony_context.get("caller_id")
                    or telephony_context.get("from_number")
                    or telephony_context.get("caller_number")
                    or ""
                ).strip() or None
                to_number = str(telephony_context.get("to_number") or telephony_context.get("to") or "").strip() or None
                direction = str(telephony_context.get("call_direction") or telephony_context.get("direction") or "").strip() or None
            if not call_id_candidates:
                call_id_candidates = [session_id]
            resolved = await _fetch_piopiy_recording_from_cdr(
                client_id=workspace_client_id,
                session_dir=(client_root / session_id).resolve(),
                app_id=app_id,
                call_id_candidates=call_id_candidates,
                caller_id=caller_id,
                to_number=to_number,
                started_at=started_at,
                ended_at=ended_at,
                direction=direction,
            )
            if isinstance(resolved, dict):
                recording_url = str(resolved.get("recording_url") or "").strip()
                recording_filename = str(resolved.get("recording_filename") or "").strip()
                if recording_url and recording_filename:
                    request_headers = _piopiy_recording_request_headers(recording_url)
                    async with httpx.AsyncClient(follow_redirects=True, timeout=45.0) as client:
                        response = await client.get(recording_url, headers=request_headers)
                        response.raise_for_status()
                    content_type = str(response.headers.get("content-type") or "").strip() or "audio/mpeg"
                    if not content_type.startswith("audio/"):
                        raise HTTPException(status_code=404, detail="Recording audio not available yet.")
                    recording_path = (client_root / session_id / recording_filename).resolve()
                    recording_path.write_bytes(response.content)
                    expected_content_length = None
                    with contextlib.suppress(Exception):
                        expected_content_length = int(str(response.headers.get("content-length") or "").strip())
                    recording_duration_seconds = _probe_audio_duration_seconds(recording_path)
                    playback_path, playback_meta = _ensure_browser_playback_recording(recording_path)
                    expected_duration_seconds = None
                    if started_at and ended_at and ended_at >= started_at:
                        expected_duration_seconds = (ended_at - started_at).total_seconds()
                    recording_status = "partial" if _is_partial_recording(
                        audio_duration_seconds=recording_duration_seconds,
                        expected_duration_seconds=expected_duration_seconds,
                        content_length=expected_content_length,
                        file_size=len(response.content),
                    ) else "saved"
                    meta_path = (client_root / session_id / "piopiy_recording.json").resolve()
                    meta = {
                        "recording_url": recording_url,
                        "recording_filename": recording_filename,
                        "recording_path": str(recording_path),
                        "recording_content_type": content_type,
                        "recording_downloaded_at": datetime.now(timezone.utc).isoformat(),
                        "recording_size_bytes": len(response.content),
                        "recording_expected_content_length": expected_content_length,
                        "recording_duration_seconds": round(recording_duration_seconds, 3) if recording_duration_seconds is not None else None,
                        "recording_expected_duration_seconds": round(expected_duration_seconds, 3) if expected_duration_seconds is not None else None,
                        "recording_status": recording_status,
                        "recording_error": "recording_duration_shorter_than_call" if recording_status == "partial" else None,
                        "recording_source": "cdr_lookup",
                    }
                    meta.update({key: value for key, value in playback_meta.items() if value is not None})
                    meta_path.write_text(
                        json.dumps(meta, indent=2, ensure_ascii=True),
                        encoding="utf-8",
                    )
                    with contextlib.suppress(Exception):
                        artifacts = _read_json_file(client_root / session_id / "artifacts.json")
                        if isinstance(artifacts, dict) and isinstance(artifacts.get("telephony_context"), dict):
                            telephony_context = artifacts["telephony_context"]
                            telephony_context.update(
                                {
                                    "piopiy_recording_url": recording_url,
                                    "piopiy_recording_filename": recording_filename,
                                    "piopiy_recording_path": str(recording_path),
                                    "recording_status": recording_status,
                                }
                            )
                            artifacts["telephony_context"] = telephony_context
                            (client_root / session_id / "artifacts.json").write_text(
                                json.dumps(artifacts, indent=2, ensure_ascii=True),
                                encoding="utf-8",
                            )
                    served_path = playback_path if playback_path is not None else recording_path
                    return _serve_audio_file_with_range(served_path, request, served_path.name)
        raise HTTPException(status_code=404, detail="Recording not found.")

    @app.post("/api/lead-sources/excel/parse")
    async def parse_excel_leads(request: Request, file: UploadFile = File(...)) -> dict[str, object]:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        filename = file.filename or "upload"
        suffix = Path(filename).suffix.lower()
        contents = await file.read()
        if not contents:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        if suffix == ".csv":
            rows = _extract_rows_from_csv(contents)
        elif suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
            rows = _extract_rows_from_excel(contents)
        else:
            raise HTTPException(status_code=400, detail="Upload a CSV or XLSX file.")

        leads = _extract_leads_from_tabular_rows(rows)
        if not leads:
            raise HTTPException(
                status_code=400,
                detail="Could not find name/phone columns. Use headers like Name and Phone or Mobile.",
            )
        ingestion = record_leads(
            client_id=workspace_client_id,
            source="excel",
            leads=leads,
            metadata={"file_name": filename},
        )
        return {
            "source": "excel",
            "file_name": filename,
            "count": len(leads),
            "leads": leads,
            "ingestion": ingestion,
        }

    @app.get("/api/clients/{client_id}")
    async def client_editor_payload(client_id: str, request: Request) -> dict:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        if client_id != workspace_client_id:
            raise HTTPException(status_code=403, detail="Access denied for this client.")
        try:
            return get_client_editor_payload(client_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/clients/{client_id}/projects")
    async def client_projects(client_id: str, request: Request) -> dict[str, object]:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        if client_id != workspace_client_id:
            raise HTTPException(status_code=403, detail="Access denied for this client.")
        try:
            projects = list_client_projects(client_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"client_id": client_id, "projects": projects}

    @app.post("/api/clients/{client_id}/active-project")
    async def set_active_project(
        client_id: str,
        request: ActiveProjectRequest,
        http_request: Request,
    ) -> dict[str, str]:
        user = getattr(http_request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        if client_id != workspace_client_id:
            raise HTTPException(status_code=403, detail="Access denied for this client.")

        target_project_id = str(request.project_id or "").strip()
        if not target_project_id:
            raise HTTPException(status_code=400, detail="project_id is required.")

        try:
            payload = get_client_editor_payload(client_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        projects = list(payload.get("projects") or [])
        if not projects:
            raise HTTPException(status_code=400, detail="No projects available on this client.")

        found = False
        for project in projects:
            project_id = str(project.get("project_id") or "").strip()
            if not project_id:
                continue
            if project_id == target_project_id:
                project["status"] = "active"
                found = True
            else:
                project["status"] = "inactive"

        if not found:
            raise HTTPException(status_code=404, detail=f"Project '{target_project_id}' not found.")

        payload["projects"] = projects
        payload["active_project_id"] = target_project_id
        try:
            save_client_editor_payload(client_id, payload)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {"status": "saved", "client_id": client_id, "active_project_id": target_project_id}

    @app.put("/api/clients/{client_id}")
    async def save_client_payload(client_id: str, request: ClientEditorRequest, http_request: Request) -> dict[str, str]:
        user = getattr(http_request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        if client_id != workspace_client_id:
            raise HTTPException(status_code=403, detail="Access denied for this client.")
        try:
            save_client_editor_payload(client_id, request.model_dump(mode="python"))
        except (FileNotFoundError, ValueError) as exc:
            logger.warning(
                "Failed saving workspace client payload user_id=%s client_id=%s error_type=%s detail=%s",
                str(user.get("id") or ""),
                client_id,
                exc.__class__.__name__,
                str(exc),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "saved", "client_id": client_id}

    @app.get("/api/session")
    async def session_state(request: Request, session_key: str = "dashboard") -> dict:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        scoped_session_key = _workspace_session_key_for_user(user)
        return await controller.snapshot(session_key=scoped_session_key)

    @app.post("/api/live-preview/extract")
    async def live_preview_extract(payload: LivePreviewExtractRequest, request: Request) -> dict[str, object]:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        scoped_session_key = _workspace_session_key_for_user(user)
        snapshot = await controller.snapshot(session_key=scoped_session_key)
        snapshot_turns = snapshot.get("turns") if isinstance(snapshot, dict) else []
        merged_turns: list[dict[str, str]] = []
        if isinstance(snapshot_turns, list):
            for turn in snapshot_turns:
                if isinstance(turn, dict):
                    speaker = str(turn.get("speaker") or "").strip().lower() or "unknown"
                    text = " ".join(str(turn.get("text") or "").split()).strip()
                    if text:
                        merged_turns.append({"speaker": speaker, "text": text})
        for turn in payload.turns or []:
            speaker = str(turn.speaker or "").strip().lower() or "unknown"
            text = " ".join(str(turn.text or "").split()).strip()
            if text:
                merged_turns.append({"speaker": speaker, "text": text})
        merged_turns = merged_turns[-24:]

        field_config = (
            [item.model_dump(mode="json") for item in payload.field_config]
            if payload.field_config
            else DEFAULT_LIVE_PREVIEW_FIELD_CONFIG
        )
        stage1_prompt = build_stage1_prompt(merged_turns)
        stage1_summary: dict[str, Any] = {}
        stage2_result: dict[str, Any] = {}
        stage1_source = "llm"
        stage2_source = "llm"

        try:
            stage1_summary = await live_preview_structured.generate_json(prompt=stage1_prompt, temperature=0.2)
            if not isinstance(stage1_summary, dict) or not stage1_summary:
                stage1_summary = {
                    "conversation_summary": "No reliable structured summary generated yet.",
                    "detected_facts": [],
                    "dates_times": [],
                    "money_values": [],
                    "customer_intent": [],
                    "stakeholders": [],
                    "action_items": [],
                    "commitments": [],
                    "open_questions": [],
                }
                stage1_source = "fallback"
        except Exception as exc:
            logger.warning("Live preview stage-1 extraction failed: %s", exc)
            stage1_summary = {
                "conversation_summary": "Live extraction fallback used due to model error.",
                "detected_facts": [],
                "dates_times": [],
                "money_values": [],
                "customer_intent": [],
                "stakeholders": [],
                "action_items": [],
                "commitments": [],
                "open_questions": [],
            }
            stage1_source = "fallback"

        try:
            stage2_prompt = build_stage2_prompt(stage1_summary, field_config)
            stage2_result = await live_preview_structured.generate_json(prompt=stage2_prompt, temperature=0.1)
            if not isinstance(stage2_result, dict) or "fields" not in stage2_result:
                raise ValueError("Stage-2 output missing fields object.")
        except Exception as exc:
            logger.warning("Live preview stage-2 mapping failed: %s", exc)
            stage2_result = fallback_stage2_mapping(stage1_summary, field_config)
            stage2_source = "fallback"

        if not isinstance(stage2_result.get("fields"), dict):
            stage2_result = fallback_stage2_mapping(stage1_summary, field_config)
            stage2_source = "fallback"
        fields_obj = stage2_result.get("fields")
        if not isinstance(fields_obj, dict):
            fields_obj = {}
            stage2_result["fields"] = fields_obj
        for entry in field_config:
            field_key = str(entry.get("key") or "").strip()
            if not field_key:
                continue
            if field_key not in fields_obj or not isinstance(fields_obj.get(field_key), dict):
                fields_obj[field_key] = {
                    "value": "Not mentioned yet",
                    "status": "not_mentioned",
                    "evidence": [],
                    "confidence": 0.0,
                }
            else:
                value = str((fields_obj[field_key] or {}).get("value") or "").strip()
                status = str((fields_obj[field_key] or {}).get("status") or "").strip().lower()
                if not value:
                    fields_obj[field_key]["value"] = "Not mentioned yet"
                if status not in {"confirmed", "inferred", "not_mentioned"}:
                    fields_obj[field_key]["status"] = "inferred" if value else "not_mentioned"
                evidence = fields_obj[field_key].get("evidence")
                if not isinstance(evidence, list):
                    fields_obj[field_key]["evidence"] = []
                confidence = fields_obj[field_key].get("confidence")
                try:
                    fields_obj[field_key]["confidence"] = float(confidence)
                except Exception:
                    fields_obj[field_key]["confidence"] = 0.0
        if not str(stage2_result.get("running_summary") or "").strip():
            summary_field = fields_obj.get("summary") if isinstance(fields_obj, dict) else None
            if isinstance(summary_field, dict):
                stage2_result["running_summary"] = str(summary_field.get("value") or "Not mentioned yet")
            else:
                stage2_result["running_summary"] = str(stage1_summary.get("conversation_summary") or "Not mentioned yet")
        if isinstance(fields_obj.get("summary"), dict):
            summary_field = fields_obj["summary"]
            running_summary = str(stage2_result.get("running_summary") or "").strip()
            if (
                running_summary
                and running_summary.lower() != "not mentioned yet"
                and str(summary_field.get("status") or "").strip().lower() == "not_mentioned"
            ):
                summary_field["value"] = running_summary
                summary_field["status"] = "inferred"
                if not isinstance(summary_field.get("evidence"), list):
                    summary_field["evidence"] = []
                try:
                    current_conf = float(summary_field.get("confidence") or 0.0)
                except Exception:
                    current_conf = 0.0
                summary_field["confidence"] = max(current_conf, 0.55)

        return {
            "status": "ok",
            "stage1_source": stage1_source,
            "stage2_source": stage2_source,
            "stage1_summary": stage1_summary,
            "mapping": stage2_result,
        }

    @app.post("/api/session/start")
    async def start_session(request: StartSessionRequest, http_request: Request) -> dict[str, str]:
        user = getattr(http_request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        if request.client_id != workspace_client_id:
            raise HTTPException(status_code=403, detail="Use your workspace client to start a session.")
        session_key = _workspace_session_key_for_user(user)
        transport = (request.transport or "local").strip().lower() or "local"
        client_bundle = load_client(request.client_id, project_id=request.project_id)
        active_project = client_bundle.active_project
        contact_details: dict[str, str] = {}
        if _is_guest_demo_workspace_client(request.client_id):
            try:
                validated_contact = _validate_guest_demo_contact(
                    customer_name=request.customer_name,
                    contact_phone=request.contact_phone,
                    contact_email=request.contact_email,
                )
                request.customer_name = validated_contact["name"]
                contact_details = {
                    "phone": validated_contact["phone"],
                    "email": validated_contact["email"],
                }
                guest_demo_lead = auth_manager.create_guest_demo_lead(
                    user=user,
                    workspace_client_id=request.client_id,
                    project_id=active_project.project_id if active_project else request.project_id,
                    project_name=active_project.name if active_project else None,
                    full_name=validated_contact["name"],
                    phone=contact_details["phone"],
                    email=contact_details["email"],
                    metadata={
                        "transport": transport,
                        "session_key": session_key,
                    },
                )
                notification_status, notification_error = await _notify_guest_demo_lead_via_formspree(guest_demo_lead)
                auth_manager.update_guest_demo_lead_notification(
                    lead_id=str(guest_demo_lead["id"]),
                    notification_status=notification_status,
                    notification_sent_at=(
                        datetime.now(timezone.utc).isoformat() if notification_status == "sent" else None
                    ),
                    notification_error=notification_error,
                )
                contact_details["guest_demo_lead_id"] = str(guest_demo_lead["id"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            contact_details = {
                "phone": str(request.contact_phone or "").strip(),
                "email": str(request.contact_email or "").strip(),
            }
            contact_details = {key: value for key, value in contact_details.items() if value}
        hostname = (http_request.url.hostname or "").strip().lower()
        if transport == "browser":
            bridge = browser_audio_bridges.get(session_key)
            if bridge is None:
                raise HTTPException(
                    status_code=409,
                    detail="Browser audio channel is not connected. Open audio WebSocket first.",
                )
            try:
                await controller.start_with_audio(
                    request.client_id,
                    request.customer_name,
                    project_id=request.project_id,
                    contact_details=contact_details,
                    audio=bridge,
                    telephony_context={
                        "provider": "browser",
                        "guest_demo_lead_id": contact_details.get("guest_demo_lead_id"),
                    },
                    defer_initial_prompt=False,
                    session_key=session_key,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {
                "status": "started",
                "client_id": request.client_id,
                "project_id": request.project_id or "",
                "customer_name": request.customer_name,
            }
        if hostname and hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Local microphone mode is disabled on remote hosts. "
                    "Use browser transport or the 'Call' flow."
                ),
            )
        if not _can_use_local_microphone():
            raise HTTPException(
                status_code=400,
                detail=(
                    "Local microphone mode is unavailable on this server. "
                    "Use the 'Call' flow to run sessions over telephony from remote devices."
                ),
            )
        try:
            await controller.start(
                request.client_id,
                request.customer_name,
                project_id=request.project_id,
                contact_details=contact_details,
                telephony_context={
                    "provider": "local",
                    "guest_demo_lead_id": contact_details.get("guest_demo_lead_id"),
                },
                session_key=session_key,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "status": "started",
            "client_id": request.client_id,
            "project_id": request.project_id or "",
            "customer_name": request.customer_name,
        }

    @app.post("/api/session/stop")
    async def stop_session(request: Request, session_key: str = "dashboard") -> dict[str, str]:
        user = getattr(request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        scoped_session_key = _workspace_session_key_for_user(user)
        await _safe_stop_session(session_key=scoped_session_key)
        return {"status": "stopped"}

    @app.websocket("/ws/browser-audio/{session_key}")
    async def browser_audio(session_key: str, websocket: WebSocket) -> None:
        user = auth_manager.current_user_from_cookie_header(websocket.headers.get("cookie", ""))
        if user is None:
            visitor_id = _sanitize_guest_visitor_id(
                websocket.headers.get("x-guest-visitor-id")
                or websocket.query_params.get("visitor_id")
            )
            if visitor_id:
                user = auth_manager.get_or_create_public_guest_user(visitor_id)
        if user is None:
            await websocket.close(code=4401)
            return
        if session_key != _workspace_session_key_for_user(user):
            await websocket.close(code=4403)
            return
        logger.info("Browser audio websocket connected session_key=%s", session_key)
        await websocket.accept()
        bridge = BrowserAudioBridge(websocket)
        browser_audio_bridges[session_key] = bridge
        try:
            while True:
                payload = await websocket.receive_text()
                try:
                    message = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                await bridge.handle_ws_message(message if isinstance(message, dict) else {})
        except WebSocketDisconnect:
            logger.info("Browser audio websocket disconnected session_key=%s", session_key)
        except RuntimeError as exc:
            logger.info("Browser audio websocket closed during receive session_key=%s error=%s", session_key, exc)
        finally:
            if browser_audio_bridges.get(session_key) is bridge:
                browser_audio_bridges.pop(session_key, None)
            await bridge.close()
            await _safe_stop_session(session_key=session_key)

    @app.post("/api/telephony/call")
    async def start_phone_call(request: StartPhoneCallRequest, http_request: Request) -> dict[str, str]:
        user = getattr(http_request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        if request.client_id != workspace_client_id:
            raise HTTPException(status_code=403, detail="Use your workspace client to place calls.")
        try:
            return await telephony.create_outbound_call(
                client_id=request.client_id,
                customer_name=request.customer_name,
                to_number=request.to_number,
                project_id=request.project_id,
                lead_source=request.lead_source or "manual_single",
                context_metadata={"workspace_session_key": _workspace_session_key_for_user(user)},
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telephony/call-batch")
    async def start_phone_call_batch(request: BatchCallRequest, http_request: Request) -> dict[str, object]:
        user = getattr(http_request.state, "auth_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        workspace_client_id, _ = _ensure_user_workspace_client(user)
        if request.client_id != workspace_client_id:
            raise HTTPException(status_code=403, detail="Use your workspace client to place calls.")
        if not request.leads:
            raise HTTPException(status_code=400, detail="Provide at least one lead.")
        if len(request.leads) > 300:
            raise HTTPException(status_code=400, detail="Batch size too large. Keep it within 300 leads per request.")

        bundle = load_client(request.client_id, project_id=request.project_id)
        active_project = bundle.active_project
        runtime_limit = active_project.runtime.max_concurrent_calls if active_project is not None else None
        requested_limit = request.max_concurrent_calls if request.max_concurrent_calls is not None else runtime_limit
        fallback_limit = settings.telephony_max_concurrent_sessions
        concurrency_limit = max(1, min(100, int(requested_limit or fallback_limit or 1)))

        semaphore = asyncio.Semaphore(concurrency_limit)
        results: list[dict[str, object]] = []

        async def _place_one(index: int, lead: BatchCallLead) -> None:
            async with semaphore:
                name = " ".join(str(lead.customer_name or "").split()).strip() or f"Lead {index + 1}"
                number = "".join(ch for ch in str(lead.to_number or "").strip() if ch in "+0123456789")
                if not number:
                    results.append(
                        {
                            "index": index,
                            "customer_name": name,
                            "to_number": str(lead.to_number or ""),
                            "status": "failed",
                            "error": "Missing phone number.",
                        }
                    )
                    return
                try:
                    outcome = await telephony.create_outbound_call(
                        client_id=request.client_id,
                        customer_name=name,
                        to_number=number,
                        project_id=request.project_id,
                        lead_source=request.lead_source or "excel_batch",
                        context_metadata={"workspace_session_key": _workspace_session_key_for_user(user)},
                    )
                    results.append(
                        {
                            "index": index,
                            "customer_name": name,
                            "to_number": number,
                            "status": "queued",
                            "provider": outcome.get("provider", ""),
                            "call_sid": outcome.get("call_sid", ""),
                            "pending_id": outcome.get("pending_id", ""),
                        }
                    )
                except Exception as exc:
                    results.append(
                        {
                            "index": index,
                            "customer_name": name,
                            "to_number": number,
                            "status": "failed",
                            "error": str(exc),
                        }
                    )

        await asyncio.gather(*[_place_one(i, lead) for i, lead in enumerate(request.leads)])
        results.sort(key=lambda item: int(item.get("index", 0)))
        success_count = sum(1 for item in results if item.get("status") == "queued")
        failed_count = len(results) - success_count
        return {
            "status": "completed",
            "requested_count": len(request.leads),
            "success_count": success_count,
            "failed_count": failed_count,
            "concurrency_used": concurrency_limit,
            "results": results,
        }

    @app.post("/internal/telephony/call")
    async def internal_start_phone_call(request: StartPhoneCallRequest, http_request: Request) -> dict[str, str]:
        internal_token = (os.getenv("INTERNAL_CALL_TEST_TOKEN") or "").strip()
        presented_token = (http_request.headers.get("x-internal-token") or "").strip()
        if not internal_token or presented_token != internal_token:
            raise HTTPException(status_code=403, detail="Forbidden.")
        provider_override = (http_request.headers.get("x-provider-override") or "").strip().lower()
        previous_provider = settings.telephony_provider
        if provider_override in {"twilio", "exotel", "airtel_iq", "meta_whatsapp", "piopiy"}:
            settings.telephony_provider = provider_override
        try:
            return await telephony.create_outbound_call(
                client_id=request.client_id,
                customer_name=request.customer_name,
                to_number=request.to_number,
                project_id=request.project_id,
                lead_source=request.lead_source or "manual_single",
                context_metadata={"workspace_session_key": "internal_call_test"},
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            settings.telephony_provider = previous_provider

    @app.post("/api/airtel-iq/action")
    async def airtel_iq_action(request: AirtelIQActionRequest) -> dict[str, object]:
        pending_call = await telephony.get_pending_call(request.pending_id)
        context = await telephony.get_call_context(request.pending_id) or {}
        vm_session_id = (request.vm_session_id or str(context.get("airtel_iq_vm_session_id") or "")).strip()
        if not vm_session_id:
            raise HTTPException(status_code=400, detail="Missing vmSessionId. Pass vm_session_id or wait for Airtel callbacks.")

        action = request.action.strip().lower()
        try:
            if action == "play_audio":
                if not request.audio_url:
                    raise HTTPException(status_code=400, detail="audio_url is required for play_audio.")
                response = await telephony.airtel_iq.play_audio(vm_session_id=vm_session_id, audio_url=request.audio_url)
            elif action == "collect_input":
                response = await telephony.airtel_iq.collect_input(
                    vm_session_id=vm_session_id,
                    timeout=request.timeout or 5,
                    max_digits=request.max_digits,
                )
            elif action == "hangup":
                response = await telephony.airtel_iq.hangup(vm_session_id=vm_session_id)
            else:
                raise HTTPException(status_code=400, detail="Unsupported action. Use play_audio, collect_input, or hangup.")
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        await telephony.update_call_context(
            request.pending_id,
            {
                "airtel_iq_vm_session_id": vm_session_id,
                "airtel_iq_last_action": action,
                "airtel_iq_last_action_response": json.dumps(response, ensure_ascii=True),
                "provider_call_sid": vm_session_id,
            },
        )
        if pending_call is not None:
            await telephony.update_pending_call_provider_sid(request.pending_id, vm_session_id)
        return {"status": "ok", "pending_id": request.pending_id, "action": action, "response": response}

    @app.post("/api/meta-whatsapp/action")
    async def meta_whatsapp_action(request: MetaWhatsAppActionRequest) -> dict[str, object]:
        pending_call = await telephony.get_pending_call(request.pending_id)
        context = await telephony.get_call_context(request.pending_id) or {}
        action = request.action.strip().lower()
        call_id = (request.call_id or str(context.get("provider_call_sid") or "")).strip()
        to_number = (request.to_number or str(context.get("to_number") or "")).strip()

        if action in {"connect"} and not to_number:
            raise HTTPException(status_code=400, detail="to_number is required for connect action.")
        if action in {"pre_accept", "accept", "reject", "terminate"} and not call_id:
            raise HTTPException(status_code=400, detail="call_id is required for this action.")

        body: dict[str, object] = {"messaging_product": "whatsapp", "action": action}
        if action == "connect":
            sdp_type = (request.sdp_type or "offer").strip() or "offer"
            sdp = (request.sdp or "").strip()
            if not sdp:
                raise HTTPException(status_code=400, detail="sdp is required for connect action.")
            body["to"] = to_number
            body["session"] = {"sdp_type": sdp_type, "sdp": sdp}
        elif action in {"pre_accept", "accept"}:
            sdp_type = (request.sdp_type or "answer").strip() or "answer"
            sdp = (request.sdp or "").strip()
            if not sdp:
                raise HTTPException(status_code=400, detail="sdp is required for pre_accept/accept actions.")
            body["call_id"] = call_id
            body["session"] = {"sdp_type": sdp_type, "sdp": sdp}
        else:
            body["call_id"] = call_id

        try:
            response = await telephony.meta_whatsapp.perform_call_action(body)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        normalized_response = response if isinstance(response, dict) else {}
        updated_call_id = str(
            normalized_response.get("sid")
            or normalized_response.get("call_id")
            or normalized_response.get("id")
            or call_id
            or ""
        ).strip()
        updates: dict[str, str | float | int | bool | None] = {
            "meta_whatsapp_last_action": action,
            "meta_whatsapp_last_action_response": json.dumps(normalized_response, ensure_ascii=True),
            "call_status": action,
        }
        if action in {"pre_accept", "accept"} and request.sdp:
            await telephony.apply_meta_answer(
                request.pending_id,
                sdp_type=(request.sdp_type or "answer").strip() or "answer",
                sdp=request.sdp,
            )
        if action in {"reject", "terminate"}:
            await telephony.close_meta_peer(request.pending_id)
        if updated_call_id:
            updates["provider_call_sid"] = updated_call_id
        await telephony.update_call_context(request.pending_id, updates)
        if pending_call is not None and updated_call_id:
            await telephony.update_pending_call_provider_sid(request.pending_id, updated_call_id)
        return {
            "status": "ok",
            "pending_id": request.pending_id,
            "action": action,
            "call_id": updated_call_id,
            "response": normalized_response,
        }

    @app.api_route("/twilio/voice/outbound/{pending_id}", methods=["GET", "POST"])
    async def twilio_outbound_voice(pending_id: str) -> Response:
        logger.info("Serving Twilio voice webhook for pending_id=%s", pending_id)
        pending_call = await telephony.get_pending_call(pending_id)
        if pending_call is None:
            xml = '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Call session expired.</Say><Hangup/></Response>'
            return Response(content=xml, media_type="application/xml")

        try:
            ws_url = build_ws_url(settings.public_base_url or "", f"/twilio/media/{pending_id}")
        except RuntimeError as exc:
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response>"
                f"<Say>{str(exc)}</Say>"
                "<Hangup/></Response>"
            )
            return Response(content=xml, media_type="application/xml", status_code=503)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f"<Connect><Stream url=\"{ws_url}\" /></Connect>"
            "<Hangup/></Response>"
        )
        return Response(content=xml, media_type="application/xml")

    @app.api_route("/twilio/voice/inbound", methods=["GET", "POST"])
    async def twilio_inbound_voice(request: Request) -> Response:
        form = await request.form()
        payload = _extract_twilio_payload(form)
        from_number = str(payload.get("From", "")).strip()
        to_number = str(payload.get("To", "")).strip()
        call_sid = str(payload.get("CallSid", "")).strip()
        caller_name = "Inbound Caller"
        try:
            target_client_id, target_project_id = _resolve_inbound_twilio_target()
            pending_id, _ = await telephony.create_inbound_call(
                provider="twilio",
                client_id=target_client_id,
                customer_name=caller_name,
                from_number=from_number,
                to_number=to_number,
                project_id=target_project_id,
                provider_call_sid=call_sid or None,
                lead_source="twilio_inbound",
                context_metadata={
                    "call_direction": str(payload.get("Direction", "")).strip() or "inbound",
                    "caller_city": str(payload.get("CallerCity", "")).strip() or None,
                    "caller_state": str(payload.get("CallerState", "")).strip() or None,
                    "caller_country": str(payload.get("CallerCountry", "")).strip() or None,
                    "called_via_number": to_number or None,
                },
            )
            ws_url = build_ws_url(settings.public_base_url or "", f"/twilio/media/{pending_id}")
        except Exception as exc:
            logger.exception("Failed to initialize inbound Twilio call")
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response>"
                f"<Say>{str(exc)}</Say>"
                "<Hangup/></Response>"
            )
            return Response(content=xml, media_type="application/xml", status_code=503)

        logger.info(
            "Serving inbound Twilio voice webhook call_sid=%s from=%s to=%s pending_id=%s client_id=%s project_id=%s",
            call_sid,
            from_number,
            to_number,
            pending_id,
            target_client_id,
            target_project_id or "",
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f"<Connect><Stream url=\"{ws_url}\" /></Connect>"
            "<Hangup/></Response>"
        )
        return Response(content=xml, media_type="application/xml")

    @app.post("/exotel/status/{pending_id}")
    async def exotel_status_callback(pending_id: str, request: Request) -> dict[str, str]:
        form = await request.form()
        payload = {str(key): str(value) for key, value in form.items()}
        if not await telephony.claim_webhook_event("exotel", pending_id, "status", payload):
            logger.info("Duplicate Exotel status callback ignored: pending_id=%s payload=%s", pending_id, payload)
            return {"status": "duplicate_ignored"}
        call_sid = str(payload.get("CallSid", "")).strip()
        status = str(payload.get("Status", "")).strip()
        if call_sid:
            await telephony.update_pending_call_provider_sid(pending_id, call_sid)
        await telephony.update_call_context(
            pending_id,
            {
                "provider_call_sid": call_sid or None,
                "call_status": status or None,
                "status_callback_at": str(payload.get("DateUpdated", "")).strip() or None,
                "recording_url": str(payload.get("RecordingUrl", "")).strip() or None,
                "status_callback_error": str(payload.get("Error", "")).strip() or None,
            },
        )
        logger.info("Exotel status callback pending_id=%s payload=%s", pending_id, payload)
        if _is_terminal_call_status(status):
            await telephony.consume_pending_call(pending_id)
            await _safe_stop_session(session_key=pending_id)
        return {"status": "ok"}

    @app.api_route("/airtel-iq/status/{pending_id}", methods=["GET", "POST"])
    async def airtel_iq_status_callback(pending_id: str, request: Request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "")
        payload: dict[str, str] = {}
        if "application/json" in content_type:
            raw_payload = await request.json()
            payload = _extract_airtel_iq_payload(raw_payload)
        else:
            form = await request.form()
            payload = {str(key): str(value) for key, value in form.items()}
        if not await telephony.claim_webhook_event("airtel_iq", pending_id, "status", payload):
            logger.info("Duplicate Airtel IQ status callback ignored: pending_id=%s payload=%s", pending_id, payload)
            return {"status": "duplicate_ignored"}
        call_sid = str(
            payload.get("callSid")
            or payload.get("call_id")
            or payload.get("sid")
            or payload.get("CallSid")
            or ""
        ).strip()
        status = str(payload.get("status") or payload.get("callStatus") or payload.get("Status") or "").strip()
        event_type = str(payload.get("eventType") or payload.get("event_type") or "").strip()
        vm_session_id = str(payload.get("vmSessionId") or payload.get("vm_session_id") or "").strip()
        if call_sid:
            await telephony.update_pending_call_provider_sid(pending_id, call_sid)
        await telephony.update_call_context(
            pending_id,
            {
                "provider_call_sid": call_sid or None,
                "call_status": status or None,
                "airtel_iq_event_type": event_type or None,
                "airtel_iq_vm_session_id": vm_session_id or None,
                "status_callback_payload": json.dumps(payload, ensure_ascii=True),
            },
        )
        logger.info("Airtel IQ status callback pending_id=%s payload=%s", pending_id, payload)
        if _is_terminal_call_status(status) or _is_terminal_event(event_type):
            await telephony.consume_pending_call(pending_id)
            await _safe_stop_session(session_key=pending_id)
        return {"status": "ok"}

    @app.api_route("/airtel-iq/events/{pending_id}", methods=["GET", "POST"])
    async def airtel_iq_events_callback(pending_id: str, request: Request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "")
        payload: dict[str, str] = {}
        if "application/json" in content_type:
            raw_payload = await request.json()
            payload = _extract_airtel_iq_payload(raw_payload)
        else:
            form = await request.form()
            payload = {str(key): str(value) for key, value in form.items()}
        if not await telephony.claim_webhook_event("airtel_iq", pending_id, "events", payload):
            logger.info("Duplicate Airtel IQ events callback ignored: pending_id=%s payload=%s", pending_id, payload)
            return {"status": "duplicate_ignored"}

        call_sid = str(
            payload.get("callSid")
            or payload.get("call_id")
            or payload.get("sid")
            or payload.get("CallSid")
            or ""
        ).strip()
        vm_session_id = str(payload.get("vmSessionId") or payload.get("vm_session_id") or "").strip()
        event_type = str(payload.get("eventType") or payload.get("event_type") or "").strip()
        transcript = str(payload.get("transcript") or payload.get("speechText") or payload.get("userInput") or "").strip()
        digit = str(payload.get("digit") or payload.get("dtmf") or "").strip()
        if call_sid:
            await telephony.update_pending_call_provider_sid(pending_id, call_sid)
        if vm_session_id:
            await telephony.update_pending_call_provider_sid(pending_id, vm_session_id)
        await telephony.update_call_context(
            pending_id,
            {
                "provider_call_sid": call_sid or vm_session_id or None,
                "airtel_iq_vm_session_id": vm_session_id or None,
                "airtel_iq_event_type": event_type or None,
                "airtel_iq_transcript": transcript or None,
                "airtel_iq_digit": digit or None,
                "events_callback_payload": json.dumps(payload, ensure_ascii=True),
            },
        )
        logger.info("Airtel IQ events callback pending_id=%s payload=%s", pending_id, payload)
        if _is_terminal_event(event_type):
            await telephony.consume_pending_call(pending_id)
            await _safe_stop_session(session_key=pending_id)
        if event_type.upper() in {"INCOMING_CALL", "CALL_CONNECTED"}:
            return {"action": "accept"}
        return {"status": "ok"}

    @app.api_route("/airtel-iq/cdr/{pending_id}", methods=["GET", "POST"])
    async def airtel_iq_cdr_callback(pending_id: str, request: Request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "")
        payload: dict[str, str] = {}
        if "application/json" in content_type:
            raw_payload = await request.json()
            payload = _extract_airtel_iq_payload(raw_payload)
        else:
            form = await request.form()
            payload = {str(key): str(value) for key, value in form.items()}
        if not await telephony.claim_webhook_event("airtel_iq", pending_id, "cdr", payload):
            logger.info("Duplicate Airtel IQ CDR callback ignored: pending_id=%s payload=%s", pending_id, payload)
            return {"status": "duplicate_ignored"}

        call_sid = str(
            payload.get("callSid")
            or payload.get("call_id")
            or payload.get("sid")
            or payload.get("CallSid")
            or ""
        ).strip()
        if call_sid:
            await telephony.update_pending_call_provider_sid(pending_id, call_sid)
        await telephony.update_call_context(
            pending_id,
            {
                "provider_call_sid": call_sid or None,
                "cdr_callback_payload": json.dumps(payload, ensure_ascii=True),
                "cdr_duration_seconds": str(payload.get("duration") or payload.get("callDuration") or "").strip() or None,
                "cdr_status": str(payload.get("status") or payload.get("callStatus") or "").strip() or None,
            },
        )
        logger.info("Airtel IQ CDR callback pending_id=%s payload=%s", pending_id, payload)
        await telephony.consume_pending_call(pending_id)
        await _safe_stop_session(session_key=pending_id)
        return {"status": "ok"}

    async def _process_meta_whatsapp_event(pending_id: str, payload: dict[str, str]) -> dict[str, str]:
        if not await telephony.claim_webhook_event("meta_whatsapp", pending_id, "status", payload):
            logger.info(
                "Duplicate Meta WhatsApp status callback ignored: pending_id=%s payload=%s",
                pending_id,
                payload,
            )
            return {"status": "duplicate_ignored"}

        call_sid = str(
            payload.get("call_id")
            or payload.get("callId")
            or payload.get("id")
            or payload.get("sid")
            or payload.get("CallSid")
            or ""
        ).strip()
        status = str(payload.get("status") or payload.get("callStatus") or payload.get("eventType") or "").strip()
        status_normalized = status.strip().lower()
        session_sdp = str(payload.get("session_sdp") or "").strip()
        session_sdp_type = str(payload.get("session_sdp_type") or "").strip() or "answer"
        if call_sid:
            await telephony.update_pending_call_provider_sid(pending_id, call_sid)
        if session_sdp:
            with contextlib.suppress(Exception):
                await telephony.apply_meta_answer(
                    pending_id=pending_id,
                    sdp_type=session_sdp_type,
                    sdp=session_sdp,
                )
        await telephony.update_call_context(
            pending_id,
            {
                "provider_call_sid": call_sid or None,
                "call_status": status or None,
                "meta_whatsapp_session_sdp_type": session_sdp_type if session_sdp else None,
                "meta_whatsapp_call_errors": str(payload.get("call_errors") or "").strip() or None,
                "meta_whatsapp_payload": json.dumps(payload, ensure_ascii=True),
            },
        )
        logger.info("Meta WhatsApp status callback pending_id=%s payload=%s", pending_id, payload)
        if status_normalized in {"accept", "accepted", "connect", "connected", "call_connected"}:
            pending_call = await telephony.consume_pending_call(pending_id)
            bridge = telephony.get_meta_media_bridge(pending_id)
            if pending_call is not None and bridge is not None and not await controller.is_busy(session_key=pending_id):
                await controller.start_with_audio(
                    client_id=pending_call.client_id,
                    customer_name=pending_call.customer_name,
                    project_id=(pending_call.metadata or {}).get("project_id"),
                    audio=bridge,
                    telephony_context=pending_call.metadata,
                    defer_initial_prompt=True,
                    session_key=pending_id,
                )
                await asyncio.sleep(0.7)
                await controller.release_initial_prompt(session_key=pending_id)
        if _is_terminal_call_status(status) or _is_terminal_event(status):
            await telephony.consume_pending_call(pending_id)
            await _safe_stop_session(session_key=pending_id)
            await telephony.close_meta_peer(pending_id)
        return {"status": "ok"}

    async def _process_meta_whatsapp_message_event(payload: dict[str, str]) -> dict[str, str]:
        from_number = str(payload.get("from") or "").strip()
        message_text = str(payload.get("message_text") or "").strip()
        message_id = str(payload.get("message_id") or "").strip()
        if not from_number or not message_text:
            return {"status": "ignored"}

        match = await telephony.find_pending_consent_by_number(from_number)
        if match is not None:
            pending_id, pending_call = match
            event_payload = dict(payload)
            event_payload["matched_pending_id"] = pending_id
            if not await telephony.claim_webhook_event(
                "meta_whatsapp",
                pending_id,
                f"message:{message_id or 'text'}",
                event_payload,
            ):
                return {"status": "duplicate_ignored"}

            if _is_affirmative_reply(message_text):
                call = await telephony.activate_consent_call(pending_id, pending_call)
                await telephony.update_call_context(
                    pending_id,
                    {
                        "consent_status": "approved",
                        "call_status": str(call.get("status", "queued")),
                    },
                )
                return {"status": "consent_approved_call_started"}

            if _is_negative_reply(message_text):
                await telephony.switch_consent_to_chat(pending_id, pending_call)
                return {"status": "consent_declined_switched_to_chat"}

            return {"status": "waiting_for_clear_consent"}

        chat_match = await telephony.find_chat_pending_by_number(from_number)
        if chat_match is None:
            latest_match = await telephony.find_latest_chat_by_number(from_number)
            if latest_match is None:
                target_client_id, target_project_id = _resolve_inbound_whatsapp_target()
                pending_id, _ = await telephony.start_inbound_whatsapp_consent_flow(
                    from_number=from_number,
                    initial_message=message_text,
                    client_id=target_client_id,
                    project_id=target_project_id,
                )
                logger.info(
                    "Started inbound WhatsApp consent flow pending_id=%s from=%s client_id=%s project_id=%s",
                    pending_id,
                    from_number,
                    target_client_id,
                    target_project_id or "",
                )
                return {"status": "consent_prompt_sent"}
            pending_id, pending_call, previous_status = latest_match
            if previous_status != "chat_closed":
                logger.info(
                    "Chat match found but not restartable pending_id=%s status=%s from=%s",
                    pending_id,
                    previous_status,
                    from_number,
                )
                return {"status": "no_active_chat"}
            event_payload = dict(payload)
            event_payload["matched_pending_id"] = pending_id
            if not await telephony.claim_webhook_event(
                "meta_whatsapp",
                pending_id,
                f"chat-reopen:{message_id or hashlib.sha1(message_text.encode('utf-8')).hexdigest()}",
                event_payload,
            ):
                return {"status": "duplicate_ignored"}
            await telephony.update_call_context(
                pending_id,
                {
                    "call_status": "chat_started",
                    "whatsapp_chat_history_json": "[]",
                    "chat_reopened_at_epoch": time.time(),
                },
            )
            await telephony.send_whatsapp_chat_reply(pending_id, pending_call, message_text)
            return {"status": "chat_reopened_replied"}

        pending_id, pending_call = chat_match
        event_payload = dict(payload)
        event_payload["matched_pending_id"] = pending_id
        if not await telephony.claim_webhook_event(
            "meta_whatsapp",
            pending_id,
            f"chat:{message_id or hashlib.sha1(message_text.encode('utf-8')).hexdigest()}",
            event_payload,
        ):
            return {"status": "duplicate_ignored"}
        await telephony.send_whatsapp_chat_reply(pending_id, pending_call, message_text)
        return {"status": "chat_replied"}

    @app.api_route("/meta-whatsapp/status/{pending_id}", methods=["POST"])
    async def meta_whatsapp_status_callback(pending_id: str, request: Request) -> dict[str, str]:
        raw_body = await request.body()
        signature = request.headers.get("x-hub-signature-256")
        if not _meta_whatsapp_signature_valid(settings, raw_body, signature):
            raise HTTPException(status_code=401, detail="Invalid Meta webhook signature.")
        content_type = request.headers.get("content-type", "")
        payload: dict[str, str] = {}
        if "application/json" in content_type:
            try:
                raw_payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except json.JSONDecodeError:
                raw_payload = {}
            payload = _extract_meta_whatsapp_payload(raw_payload)
        else:
            form = await request.form()
            payload = {str(key): str(value) for key, value in form.items()}
        return await _process_meta_whatsapp_event(pending_id, payload)

    @app.get("/meta-whatsapp/webhook")
    async def meta_whatsapp_webhook_verify(request: Request) -> Response:
        mode = request.query_params.get("hub.mode", "")
        token = request.query_params.get("hub.verify_token", "")
        challenge = request.query_params.get("hub.challenge", "")
        expected = settings.meta_whatsapp_webhook_verify_token or ""
        if mode == "subscribe" and expected and token == expected:
            return Response(content=challenge, media_type="text/plain")
        raise HTTPException(status_code=403, detail="Meta webhook verification failed.")

    @app.post("/meta-whatsapp/webhook")
    async def meta_whatsapp_webhook(request: Request) -> dict[str, str]:
        raw_body = await request.body()
        signature = request.headers.get("x-hub-signature-256")
        if not _meta_whatsapp_signature_valid(settings, raw_body, signature):
            raise HTTPException(status_code=401, detail="Invalid Meta webhook signature.")
        try:
            raw_payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except json.JSONDecodeError:
            raw_payload = {}
        events = _extract_meta_whatsapp_call_events(raw_payload)
        if not events:
            return {"status": "ignored"}

        for event in events:
            if str(event.get("eventType") or "").strip().lower() == "inbound_message":
                await _process_meta_whatsapp_message_event(event)
                continue
            pending_id = str(event.get("pending_id") or "").strip()
            if not pending_id:
                call_id = str(event.get("call_id") or "").strip()
                if call_id:
                    pending_id = await telephony.find_pending_id_by_provider_sid("meta_whatsapp", call_id) or ""
            if not pending_id:
                logger.info("Meta webhook event received without resolvable pending_id: %s", event)
                continue
            await _process_meta_whatsapp_event(pending_id, event)
        return {"status": "ok"}

    @app.get("/exotel/ws-url")
    @app.get("/exotel/ws-url/{pending_id}")
    async def exotel_ws_url(request: Request, pending_id: str | None = None) -> dict[str, str]:
        params = {key: value for key, value in request.query_params.items()}
        stream_metadata = _extract_exotel_stream_metadata(params)
        call_sid = request.query_params.get("CallSid", "").strip()
        request_time = time.time()
        explicit_pending_id = pending_id or request.query_params.get("pending_id")
        if explicit_pending_id:
            pending_call = await telephony.get_pending_call(explicit_pending_id)
            if pending_call is None or pending_call.provider != "exotel":
                raise HTTPException(status_code=404, detail=f"No pending Exotel call found for {explicit_pending_id}.")
            await telephony.update_call_context(
                explicit_pending_id,
                {
                    "provider_call_sid": call_sid or None,
                    "exotel_ws_url_requested_at_epoch": request_time,
                    **stream_metadata,
                },
            )
            path = f"/exotel/media/{explicit_pending_id}"
            return {"url": build_ws_url(settings.public_base_url or "", path), "pending_id": explicit_pending_id}

        pending_ids = await telephony.list_pending_call_ids("exotel")
        if len(pending_ids) == 1:
            resolved_pending_id = pending_ids[0]
            await telephony.update_call_context(
                resolved_pending_id,
                {
                    "provider_call_sid": call_sid or None,
                    "exotel_ws_url_requested_at_epoch": request_time,
                    **stream_metadata,
                },
            )
            path = f"/exotel/media/{resolved_pending_id}"
            return {"url": build_ws_url(settings.public_base_url or "", path), "pending_id": resolved_pending_id}
        if call_sid:
            await telephony.update_call_context_by_provider_sid(
                "exotel",
                call_sid,
                {
                    "exotel_ws_url_requested_at_epoch": request_time,
                    **stream_metadata,
                },
            )
        if len(pending_ids) > 1:
            raise HTTPException(
                status_code=409,
                detail="Multiple pending Exotel calls exist. Request a call-specific URL with /exotel/ws-url/{pending_id}.",
            )
        return {"url": build_ws_url(settings.public_base_url or "", "/exotel/media")}

    @app.get("/exotel/passthru/{pending_id}")
    async def exotel_passthru(pending_id: str, request: Request) -> dict[str, object]:
        params = {key: value for key, value in request.query_params.items()}
        stream_metadata = _extract_exotel_stream_metadata(params)
        await telephony.update_call_context(
            pending_id,
            {
                "passthru_status": params.get("CallStatus") or None,
                "passthru_stream_sid": stream_metadata.get("stream_sid"),
                **stream_metadata,
            },
        )
        context = await telephony.get_call_context(pending_id)
        return {
            "escalate": False,
            "pending_id": pending_id,
            "context": context or {},
            "query": params,
        }

    async def _resolve_static_exotel_pending_call(message: dict) -> PendingCall | None:
        start = message.get("start", {}) if isinstance(message.get("start"), dict) else {}
        call_sid = str(message.get("call_sid") or start.get("call_sid") or "").strip()
        if call_sid:
            pending_call = await telephony.consume_pending_call_by_provider_sid("exotel", call_sid)
            if pending_call is not None:
                logger.info(
                    "Matched static Exotel media websocket using call_sid=%s pending_id=%s",
                    call_sid,
                    pending_call.session_id,
                )
                return pending_call
            logger.warning("Static Exotel media websocket received unknown call_sid=%s", call_sid)

        pending_ids = await telephony.list_pending_call_ids("exotel")
        available_pending_ids: list[str] = []
        for candidate_pending_id in pending_ids:
            if not await controller.is_busy(session_key=candidate_pending_id):
                available_pending_ids.append(candidate_pending_id)
        if len(available_pending_ids) == 1:
            fallback_pending_id = available_pending_ids[0]
            logger.info(
                "Falling back to only pending Exotel call pending_id=%s for static media websocket",
                fallback_pending_id,
            )
            return await telephony.consume_pending_call(fallback_pending_id)
        if len(available_pending_ids) > 1:
            logger.warning(
                "Static Exotel media websocket could not be resolved safely because %s unclaimed Exotel pending calls exist",
                len(available_pending_ids),
            )
        elif pending_ids:
            logger.info(
                "Static Exotel media websocket skipped pending fallback because all %s candidate calls are already active",
                len(pending_ids),
            )
        return None

    async def _run_exotel_media_session(websocket: WebSocket, pending_id: str | None) -> None:
        logger.info("Exotel media websocket connection requested pending_id=%s", pending_id or "<fallback>")
        await websocket.accept()

        bridge = ExotelMediaBridge(
            websocket,
            echo_test=settings.exotel_echo_test,
            processing_ambience_gain=settings.processing_ambience_gain,
        )
        pending_call: PendingCall | None = None
        session_started = False
        message_count = 0
        try:
            while True:
                if (
                    session_started
                    and pending_call is not None
                    and not await controller.is_busy(session_key=pending_call.session_id)
                ):
                    logger.info("Voice session finished; closing Exotel media stream pending_id=%s", pending_call.session_id)
                    break
                try:
                    payload = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                except TimeoutError:
                    continue
                message = json.loads(payload)
                message_count += 1
                event = str(message.get("event", "")).strip().lower()
                if message_count <= 5:
                    logger.info(
                        "Exotel raw media message pending_id=%s #%s: %s",
                        pending_call.session_id if pending_call is not None else "<unresolved>",
                        message_count,
                        message,
                    )
                if event in {"connected", "start", "stop", "media", "clear"}:
                    logger.info(
                        "Exotel media event: pending_id=%s event=%s",
                        pending_call.session_id if pending_call is not None else "<unresolved>",
                        event or "<missing>",
                    )
                if event == "stop":
                    logger.info(
                        "Received Exotel stop event, closing media loop pending_id=%s",
                        pending_call.session_id if pending_call is not None else "<unresolved>",
                    )
                    break
                await bridge.handle_ws_message(message)
                if pending_call is None:
                    if pending_id:
                        pending_call = await telephony.consume_pending_call(pending_id)
                    else:
                        pending_call = await _resolve_static_exotel_pending_call(message)
                    if pending_call is None:
                        if event == "stop":
                            logger.warning("Exotel media stream ended before a pending call could be resolved.")
                            break
                        continue
                if settings.exotel_echo_test:
                    continue
                if not session_started and event == "connected":
                    await telephony.update_call_context(
                        pending_call.session_id,
                        {
                            "exotel_connected_at_epoch": time.time(),
                        },
                    )
                    logger.info(
                        "Pre-initializing voice session from Exotel connected event pending_id=%s",
                        pending_call.session_id,
                    )
                    await controller.start_with_audio(
                        client_id=pending_call.client_id,
                        customer_name=pending_call.customer_name,
                        project_id=(pending_call.metadata or {}).get("project_id"),
                        audio=bridge,
                        telephony_context=pending_call.metadata,
                        defer_initial_prompt=True,
                        session_key=pending_call.session_id,
                    )
                    session_started = True
                    continue
                if session_started and event == "start":
                    await telephony.update_call_context(
                        pending_call.session_id,
                        {
                            "stream_sid": bridge.stream_sid,
                            "provider_call_sid": bridge.call_sid or pending_call.provider_call_sid,
                            "stream_started": True,
                            "exotel_start_at_epoch": time.time(),
                        },
                    )
                    await controller.release_initial_prompt(session_key=pending_call.session_id)
                    continue
                if not session_started and event in {"start", "media"}:
                    logger.info(
                        "Starting voice session from Exotel media stream pending_id=%s on event=%s",
                        pending_call.session_id,
                        event,
                    )
                    await controller.start_with_audio(
                        client_id=pending_call.client_id,
                        customer_name=pending_call.customer_name,
                        project_id=(pending_call.metadata or {}).get("project_id"),
                        audio=bridge,
                        telephony_context=pending_call.metadata,
                        defer_initial_prompt=False,
                        session_key=pending_call.session_id,
                    )
                    session_started = True
        except WebSocketDisconnect:
            logger.info(
                "Exotel media websocket disconnected pending_id=%s",
                pending_call.session_id if pending_call is not None else "<unresolved>",
            )
        except RuntimeError:
            logger.exception(
                "Exotel media session failed pending_id=%s",
                pending_call.session_id if pending_call is not None else "<unresolved>",
            )
            await bridge.close()
        finally:
            if session_started:
                await _safe_stop_session(
                    session_key=pending_call.session_id if pending_call is not None else "dashboard"
                )
            await bridge.close()

    @app.websocket("/twilio/media/{pending_id}")
    async def twilio_media(pending_id: str, websocket: WebSocket) -> None:
        logger.info("Twilio media websocket connection requested for pending_id=%s", pending_id)
        await websocket.accept()
        pending_call = await telephony.consume_pending_call(pending_id)
        if pending_call is None:
            logger.warning("No pending call found for media websocket pending_id=%s", pending_id)
            await websocket.close()
            return

        bridge = TwilioMediaBridge(websocket)
        session_started = False
        workspace_session_key = str((pending_call.metadata or {}).get("workspace_session_key") or "").strip()
        active_session_key = workspace_session_key or pending_id
        alias_keys = [pending_id] if workspace_session_key and workspace_session_key != pending_id else []
        try:
            logger.info("Pre-initializing Twilio voice session pending_id=%s", pending_id)
            await controller.start_with_audio(
                client_id=pending_call.client_id,
                customer_name=pending_call.customer_name,
                project_id=(pending_call.metadata or {}).get("project_id"),
                audio=bridge,
                telephony_context=pending_call.metadata,
                session_key=active_session_key,
                session_aliases=alias_keys,
                defer_initial_prompt=True,
            )
            session_started = True
            while True:
                if session_started and not await controller.is_busy(session_key=active_session_key):
                    logger.info("Voice session finished; closing Twilio media stream pending_id=%s", pending_id)
                    break
                try:
                    payload = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                except TimeoutError:
                    continue
                message = json.loads(payload)
                if message.get("event") in {"start", "stop"}:
                    logger.info(
                        "Twilio media event: pending_id=%s event=%s",
                        pending_id,
                        message.get("event"),
                    )
                if message.get("event") == "stop":
                    logger.info("Received Twilio stop event, closing media loop pending_id=%s", pending_id)
                    break
                await bridge.handle_ws_message(message)
                if message.get("event") == "start":
                    await controller.release_initial_prompt(session_key=active_session_key)
                if message.get("event") == "media" and session_started:
                    # Some Twilio streams may emit media before start callback is observed.
                    await controller.release_initial_prompt(session_key=active_session_key)
        except WebSocketDisconnect:
            logger.info("Twilio media websocket disconnected for pending_id=%s", pending_id)
            pass
        except RuntimeError:
            logger.exception("Twilio media session failed for pending_id=%s", pending_id)
            await bridge.close()
        finally:
            if session_started:
                await _safe_stop_session(session_key=active_session_key)
            await bridge.close()

    @app.websocket("/smartflo/media/{pending_id}")
    async def smartflo_media(pending_id: str, websocket: WebSocket) -> None:
        logger.info("Smartflo media websocket connection requested for pending_id=%s", pending_id)
        await websocket.accept()
        pending_call = await telephony.consume_pending_call(pending_id)
        if pending_call is None:
            logger.warning("No pending Smartflo call found for media websocket pending_id=%s", pending_id)
            await websocket.close()
            return

        bridge = SmartfloMediaBridge(websocket, settings=settings)
        session_started = False
        workspace_session_key = str((pending_call.metadata or {}).get("workspace_session_key") or "").strip()
        active_session_key = workspace_session_key or pending_id
        alias_keys = [pending_id] if workspace_session_key and workspace_session_key != pending_id else []
        try:
            logger.info("Pre-initializing Smartflo voice session pending_id=%s", pending_id)
            await controller.start_with_audio(
                client_id=pending_call.client_id,
                customer_name=pending_call.customer_name,
                project_id=(pending_call.metadata or {}).get("project_id"),
                audio=bridge,
                telephony_context=pending_call.metadata,
                session_key=active_session_key,
                session_aliases=alias_keys,
                defer_initial_prompt=True,
            )
            session_started = True
            while True:
                if session_started and not await controller.is_busy(session_key=active_session_key):
                    logger.info("Voice session finished; closing Smartflo media stream pending_id=%s", pending_id)
                    break
                try:
                    payload = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                except TimeoutError:
                    continue
                message = json.loads(payload)
                if message.get("event") in {"start", "stop"}:
                    logger.info(
                        "Smartflo media event: pending_id=%s event=%s",
                        pending_id,
                        message.get("event"),
                    )
                if message.get("event") == "stop":
                    logger.info("Received Smartflo stop event, closing media loop pending_id=%s", pending_id)
                    break
                await bridge.handle_ws_message(message)
                if message.get("event") == "start":
                    await controller.release_initial_prompt(session_key=active_session_key)
                if message.get("event") == "media" and session_started:
                    await controller.release_initial_prompt(session_key=active_session_key)
        except WebSocketDisconnect:
            logger.info("Smartflo media websocket disconnected for pending_id=%s", pending_id)
        except RuntimeError:
            logger.exception("Smartflo media session failed for pending_id=%s", pending_id)
            await bridge.close()
        finally:
            if session_started:
                await _safe_stop_session(session_key=active_session_key)
            await bridge.close()

    @app.websocket("/exotel/media")
    async def exotel_media_fallback(websocket: WebSocket) -> None:
        await _run_exotel_media_session(websocket, pending_id=None)

    @app.websocket("/exotel/media/{pending_id}")
    async def exotel_media(websocket: WebSocket, pending_id: str) -> None:
        await _run_exotel_media_session(websocket, pending_id=pending_id)

    @app.websocket("/airtel-iq/media/{pending_id}")
    async def airtel_iq_media(websocket: WebSocket, pending_id: str) -> None:
        logger.info("Airtel IQ media websocket connection requested for pending_id=%s", pending_id)
        await websocket.accept()
        pending_call = await telephony.consume_pending_call(pending_id)
        if pending_call is None:
            logger.warning("No pending Airtel IQ call found for media websocket pending_id=%s", pending_id)
            await websocket.close()
            return

        bridge = AirtelIQMediaBridge(websocket)
        session_started = False
        try:
            while True:
                if session_started and not await controller.is_busy(session_key=pending_id):
                    logger.info("Voice session finished; closing Airtel IQ media stream pending_id=%s", pending_id)
                    break
                try:
                    payload = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                except TimeoutError:
                    continue
                message = json.loads(payload)
                event = str(message.get("event", "")).strip().lower()
                if event in {"connected", "start", "stop", "media", "clear"}:
                    logger.info("Airtel IQ media event: pending_id=%s event=%s", pending_id, event or "<missing>")
                if event == "stop":
                    logger.info("Received Airtel IQ stop event, closing media loop pending_id=%s", pending_id)
                    break
                await bridge.handle_ws_message(message)
                if not session_started and event in {"connected", "start", "media"}:
                    await controller.start_with_audio(
                        client_id=pending_call.client_id,
                        customer_name=pending_call.customer_name,
                        project_id=(pending_call.metadata or {}).get("project_id"),
                        audio=bridge,
                        telephony_context=pending_call.metadata,
                        defer_initial_prompt=event == "connected",
                        session_key=pending_id,
                    )
                    session_started = True
                    if event == "connected":
                        continue
                if session_started and event == "start":
                    await controller.release_initial_prompt(session_key=pending_id)
        except WebSocketDisconnect:
            logger.info("Airtel IQ media websocket disconnected for pending_id=%s", pending_id)
        except RuntimeError:
            logger.exception("Airtel IQ media session failed for pending_id=%s", pending_id)
            await bridge.close()
        finally:
            if session_started:
                await _safe_stop_session(session_key=pending_id)
            await bridge.close()

    async def _build_piopiy_answer_response(
        request: Request,
        payload: dict[str, Any],
    ) -> list[dict[str, object]]:
        from_number = str(payload.get("from") or payload.get("caller") or payload.get("customer_number") or "").strip()
        to_number = str(
            payload.get("to")
            or payload.get("did")
            or payload.get("piopiy_number")
            or settings.piopiy_caller_id
            or ""
        ).strip()
        provider_call_sid = str(payload.get("cmiuuid") or payload.get("callSid") or payload.get("call_id") or "").strip()
        direction = str(payload.get("direction") or "inbound").strip() or "inbound"
        agent_id = str(
            payload.get("agent_id")
            or settings.piopiy_agent_id
            or os.getenv("AGENT_ID")
            or ""
        ).strip() or None
        client_id, project_id = _resolve_inbound_piopiy_target(to_number)
        pending_id, pending_call = await telephony.create_inbound_call(
            provider="piopiy",
            client_id=client_id,
            customer_name="Inbound Caller",
            from_number=from_number or "Unknown",
            to_number=to_number or settings.piopiy_caller_id or "",
            provider_call_sid=provider_call_sid or None,
            lead_source="piopiy_inbound",
            context_metadata={
                "call_direction": direction,
                "app_id": payload.get("app_id") or payload.get("appid"),
                "appid": payload.get("appid") or payload.get("app_id"),
                "stream_on_answer": True,
                "client_id": client_id,
                "project_id": project_id,
                "agent_id": agent_id,
                "demo_voice": "Kore",
                "provider": "piopiy",
            },
        )
        _record_piopiy_stage(
            "inbound_call_created",
            pending_id=pending_id,
            provider_call_sid=provider_call_sid or None,
            client_id=client_id,
            project_id=project_id,
        )
        ws_url = _build_piopiy_ws_url(request, pending_id)
        _record_piopiy_stage(
            "answer_stream_url_built",
            pending_id=pending_id,
            ws_url=ws_url,
        )
        await telephony.update_call_context(
            pending_id,
            {
                "piopiy_answer_payload": json.dumps(payload, ensure_ascii=True),
                "piopiy_answer_at_epoch": time.time(),
                "piopiy_ws_url": ws_url,
                "piopiy_agent_id": agent_id,
                "piopiy_app_id": str(payload.get("app_id") or payload.get("appid") or "").strip() or None,
            },
        )
        return [
            {
                "action": "stream",
                "ws_url": ws_url,
                "listen_mode": "caller",
                "voice_quality": "8000",
                "stream_on_answer": True,
                "agent_id": agent_id,
            }
        ]

    async def _persist_piopiy_recording_artifact(
        pending_id: str,
        payload: dict[str, Any],
    ) -> None:
        async def _set_recording_status(
            status: str,
            error: str | None = None,
            extra: dict[str, Any] | None = None,
        ) -> None:
            updates: dict[str, Any] = {
                "recording_status": status,
                "recording_error": error,
                "recording_status_updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if extra:
                updates.update(extra)
            with contextlib.suppress(Exception):
                await telephony.update_call_context(pending_id, updates)
            with contextlib.suppress(Exception):
                current = _read_json_file(session_dir / "piopiy_recording.json") or {}
                if not isinstance(current, dict):
                    current = {}
                current.update({key: value for key, value in updates.items() if value is not None})
                current["pending_id"] = pending_id
                (session_dir / "piopiy_recording.json").write_text(
                    json.dumps(current, indent=2, ensure_ascii=True),
                    encoding="utf-8",
                )

        def _missing_recording_error() -> str:
            status = str(payload.get("status") or payload.get("event") or "").strip().lower()
            disconnect_info = payload.get("disconnect_info") if isinstance(payload.get("disconnect_info"), dict) else {}
            answered = disconnect_info.get("answered")
            if status in {"missed", "no_answer", "not_answered"} or answered is False:
                return "missed_call"
            if recording_url or file_name:
                return "recording_not_published"
            return "no_recording_url"

        recording_url, file_name = _extract_piopiy_recording_info(payload)
        recording_url = str(recording_url or "").strip()
        file_name = str(file_name or "").strip()
        lookup_recording_type: str | None = None
        lookup_cdr_row: dict[str, Any] | None = None
        context = await telephony.get_call_context(pending_id) or {}
        client_id = str(context.get("client_id") or "").strip()
        if not client_id:
            logger.info(
                "Skipping Piopiy recording download because client_id is missing pending_id=%s recording_url=%s",
                pending_id,
                recording_url,
            )
            return

        session_dir = (settings.session_output_dir / client_id / pending_id).resolve()
        session_dir.mkdir(parents=True, exist_ok=True)

        def _existing_recording_is_saved() -> bool:
            existing_meta = _read_json_file(session_dir / "piopiy_recording.json")
            if not isinstance(existing_meta, dict):
                return False
            if str(existing_meta.get("recording_status") or "").strip().lower() == "partial":
                return False
            existing_path = str(existing_meta.get("recording_path") or "").strip()
            existing_name = str(existing_meta.get("recording_filename") or "").strip()
            candidates = []
            if existing_path:
                candidates.append(Path(existing_path))
            if existing_name:
                candidates.append(session_dir / existing_name)
            for candidate in candidates:
                with contextlib.suppress(Exception):
                    if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                        return True
            return False

        if _existing_recording_is_saved():
            _append_piopiy_trace(
                "recording_processing_skipped_saved",
                pending_id=pending_id,
            )
            return
        await _set_recording_status("pending", None)

        if not recording_url or not file_name:
            artifacts = _read_json_file(session_dir / "artifacts.json")
            telephony_context = {}
            started_at = ended_at = None
            call_id_candidates = [pending_id]
            caller_id = to_number = direction = None
            app_id = None
            project_id = str(context.get("project_id") or "").strip() or None
            if isinstance(artifacts, dict):
                started_at = _parse_iso_datetime(artifacts.get("started_at"))
                ended_at = _parse_iso_datetime(artifacts.get("ended_at"))
                if isinstance(artifacts.get("telephony_context"), dict):
                    telephony_context = artifacts["telephony_context"]
                    project_id = str(
                        artifacts.get("project_id")
                        or telephony_context.get("project_id")
                        or project_id
                        or ""
                    ).strip() or None
                    call_id_candidates = _piopiy_payload_call_ids(telephony_context) or call_id_candidates
                    app_id = str(
                        telephony_context.get("app_id")
                        or telephony_context.get("appid")
                        or telephony_context.get("piopiy_app_id")
                        or ""
                    ).strip() or None
                    if not app_id:
                        app_id = _resolve_project_piopiy_app_id(client_id, project_id)
                    caller_id = str(
                        telephony_context.get("caller_id")
                        or telephony_context.get("from_number")
                        or telephony_context.get("caller_number")
                        or ""
                    ).strip() or None
                    to_number = str(telephony_context.get("to_number") or telephony_context.get("to") or "").strip() or None
                    direction = str(telephony_context.get("call_direction") or telephony_context.get("direction") or "").strip() or None
            resolved = await _fetch_piopiy_recording_from_cdr(
                client_id=client_id,
                session_dir=session_dir,
                app_id=app_id,
                call_id_candidates=call_id_candidates,
                caller_id=caller_id,
                to_number=to_number,
                started_at=started_at,
                ended_at=ended_at,
                direction=direction,
            )
            if isinstance(resolved, dict):
                recording_url = str(resolved.get("recording_url") or "").strip()
                file_name = str(resolved.get("recording_filename") or "").strip()
                lookup_recording_type = str(resolved.get("selected_recording_type") or "").strip() or None
                cdr_row = resolved.get("recording_cdr_row")
                if isinstance(cdr_row, dict):
                    lookup_cdr_row = cdr_row

        if not recording_url:
            if _existing_recording_is_saved():
                return
            await _set_recording_status("unavailable", _missing_recording_error())
            return
        if not file_name:
            parsed = urllib.parse.urlparse(recording_url)
            file_name = Path(parsed.path).name or f"{pending_id}.mp3"

        recording_path = session_dir / file_name
        downloaded_at = datetime.now(timezone.utc)
        request_headers = _piopiy_recording_request_headers(recording_url)
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=45.0) as client:
                response = await client.get(recording_url, headers=request_headers)
                response.raise_for_status()
            temp_recording_path = recording_path.with_name(f"{recording_path.name}.download")
            temp_recording_path.write_bytes(response.content)
            content_type = str(response.headers.get("content-type") or "").strip() or None
            size_bytes = len(response.content)
            expected_content_length = None
            with contextlib.suppress(Exception):
                expected_content_length = int(str(response.headers.get("content-length") or "").strip())
            recording_duration_seconds = _probe_audio_duration_seconds(temp_recording_path)
            expected_duration_seconds = None
            with contextlib.suppress(Exception):
                source_duration = (lookup_cdr_row or payload).get("duration") if isinstance((lookup_cdr_row or payload), dict) else None
                expected_duration_seconds = float(source_duration or payload.get("duration") or 0) or None
            cdr_leg = str((lookup_cdr_row or payload).get("leg") or payload.get("leg") or "").strip().lower() or "unknown"
            selected_recording_type = lookup_recording_type or (
                "ai_leg" if cdr_leg == "ai" else "caller_leg" if cdr_leg in {"a", "caller"} else "unknown"
            )
            recording_status = "partial" if _is_partial_recording(
                audio_duration_seconds=recording_duration_seconds,
                expected_duration_seconds=expected_duration_seconds,
                content_length=expected_content_length,
                file_size=size_bytes,
            ) else "saved"
            if recording_status == "partial":
                recording_error = (
                    "ai_leg_only"
                    if selected_recording_type == "ai_leg"
                    else "caller_leg_only"
                    if selected_recording_type == "caller_leg"
                    else "piopiy_partial_recording"
                )
            else:
                recording_error = None
            if size_bytes <= 0 or not temp_recording_path.exists():
                await _set_recording_status("failed", "file_missing_after_download")
                return
            existing_meta = _read_json_file(session_dir / "piopiy_recording.json") or {}
            existing_duration = None
            if isinstance(existing_meta, dict):
                with contextlib.suppress(Exception):
                    existing_duration = float(existing_meta.get("recording_duration_seconds") or 0) or None
            existing_status = str(existing_meta.get("recording_status") or "").strip().lower() if isinstance(existing_meta, dict) else ""
            if (
                recording_status == "partial"
                and existing_status == "saved"
                and existing_duration
                and recording_duration_seconds
                and existing_duration >= recording_duration_seconds
            ):
                with contextlib.suppress(Exception):
                    temp_recording_path.unlink()
                _append_piopiy_trace(
                    "recording_download_skipped_shorter_than_existing",
                    pending_id=pending_id,
                    recording_url=recording_url,
                    new_duration_seconds=recording_duration_seconds,
                    existing_duration_seconds=existing_duration,
                    selected_recording_type=selected_recording_type,
                )
                return
            if existing_duration and recording_duration_seconds and recording_duration_seconds + 1.0 < existing_duration:
                with contextlib.suppress(Exception):
                    temp_recording_path.unlink()
                _append_piopiy_trace(
                    "recording_download_skipped_shorter_than_existing",
                    pending_id=pending_id,
                    recording_url=recording_url,
                    new_duration_seconds=recording_duration_seconds,
                    existing_duration_seconds=existing_duration,
                    selected_recording_type=selected_recording_type,
                )
                return
            temp_recording_path.replace(recording_path)
            playback_path, playback_meta = _ensure_browser_playback_recording(recording_path)
            recording_meta = {
                "recording_url": recording_url,
                "recording_filename": file_name,
                "recording_path": str(recording_path),
                "recording_content_type": content_type,
                "recording_downloaded_at": downloaded_at.isoformat(),
                "recording_size_bytes": size_bytes,
                "recording_expected_content_length": expected_content_length,
                "recording_duration_seconds": round(recording_duration_seconds, 3) if recording_duration_seconds is not None else None,
                "recording_expected_duration_seconds": round(expected_duration_seconds, 3) if expected_duration_seconds is not None else None,
                "recording_status": recording_status,
                "recording_error": recording_error,
                "recording_partial_reason": recording_error if recording_status == "partial" else None,
                "selected_recording_type": selected_recording_type,
                "cdr_leg": cdr_leg,
            }
            recording_meta.update({key: value for key, value in playback_meta.items() if value is not None})
            (session_dir / "piopiy_recording.json").write_text(
                json.dumps(recording_meta, indent=2, ensure_ascii=True),
                encoding="utf-8",
            )
            await telephony.update_call_context(
                pending_id,
                {
                    "piopiy_recording_url": recording_url,
                    "piopiy_recording_filename": file_name,
                    "piopiy_recording_path": str(recording_path),
                    "piopiy_recording_content_type": content_type,
                    "piopiy_recording_downloaded_at": downloaded_at.isoformat(),
                    "piopiy_recording_size_bytes": size_bytes,
                    "recording_duration_seconds": round(recording_duration_seconds, 3) if recording_duration_seconds is not None else None,
                    "recording_status": recording_status,
                    "recording_error": recording_error,
                },
            )
            await controller.merge_piopiy_recording(
                pending_id,
                {
                    "piopiy_recording_url": recording_url,
                    "piopiy_recording_filename": file_name,
                    "piopiy_recording_path": str(recording_path),
                    "piopiy_recording_content_type": content_type,
                    "piopiy_recording_downloaded_at": downloaded_at,
                    "piopiy_recording_size_bytes": size_bytes,
                },
            )
            _append_piopiy_trace(
                "recording_downloaded",
                pending_id=pending_id,
                recording_url=recording_url,
                recording_filename=file_name,
                recording_path=str(recording_path),
                recording_size_bytes=size_bytes,
                recording_duration_seconds=recording_duration_seconds,
                expected_duration_seconds=expected_duration_seconds,
                recording_status=recording_status,
                session_id=pending_id,
                piopiy_call_id=str(payload.get("call_id") or "").strip() or None,
                cdr_leg=cdr_leg,
                remote_content_length=expected_content_length,
                local_file_size=size_bytes,
                call_duration_seconds=expected_duration_seconds,
                download_completed=True,
                selected_recording_type=selected_recording_type,
                playback_recording_path=str(playback_path) if playback_path is not None else None,
                playback_recording_status=playback_meta.get("recording_playback_status"),
                playback_recording_error=playback_meta.get("recording_playback_error"),
            )
            _write_latest_call_runtime(
                {
                    "latest_session_id": pending_id,
                    "session_id": pending_id,
                    "recording_status": recording_status,
                    "recording_duration_seconds": round(recording_duration_seconds, 3) if recording_duration_seconds is not None else None,
                    "recording_duration": round(recording_duration_seconds, 3) if recording_duration_seconds is not None else None,
                    "recording_error": recording_error,
                    "local_file_size": size_bytes,
                    "remote_content_length": expected_content_length,
                    "selected_recording_type": selected_recording_type,
                    "cdr_leg": cdr_leg,
                    "download_completed": True,
                }
            )
        except Exception as exc:
            await _set_recording_status("failed", "download_failed")
            logger.warning(
                "Failed to download Piopiy recording pending_id=%s url=%s error=%s",
                pending_id,
                recording_url,
                exc,
            )
            with contextlib.suppress(Exception):
                meta_path = session_dir / "piopiy_recording.json"
                meta_path.write_text(
                    json.dumps(
                        {
                            "recording_url": recording_url,
                            "recording_filename": file_name,
                            "recording_path": str(recording_path),
                            "download_error": str(exc),
                            "pending_id": pending_id,
                            "recording_status": "failed",
                            "recording_error": "download_failed",
                        },
                        ensure_ascii=True,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                await controller.merge_piopiy_recording(
                    pending_id,
                    {
                        "piopiy_recording_url": recording_url,
                        "piopiy_recording_filename": file_name,
                        "piopiy_recording_path": str(meta_path),
                    },
                )
            _append_piopiy_trace(
                "recording_download_failed",
                pending_id=pending_id,
                recording_url=recording_url,
                error=repr(exc),
            )

    def _enqueue_piopiy_recording_processing(
        *,
        pending_id: str,
        payload: dict[str, Any],
        source: str,
        attempt: int = 0,
        delay_seconds: float = 0.0,
    ) -> None:
        async def _runner() -> None:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            try:
                _append_piopiy_trace(
                    "recording_processing_started",
                    pending_id=pending_id,
                    source=source,
                    attempt=attempt,
                    delay_seconds=delay_seconds,
                )
                await _persist_piopiy_recording_artifact(pending_id, payload)
                context = await telephony.get_call_context(pending_id) or {}
                client_id = str(context.get("client_id") or "").strip()
                status = str(context.get("recording_status") or "").strip().lower()
                if client_id:
                    meta = _read_json_file(settings.session_output_dir / client_id / pending_id / "piopiy_recording.json")
                    if isinstance(meta, dict):
                        status = str(meta.get("recording_status") or status).strip().lower()
                retry_delays = [60.0, 180.0, 420.0]
                if status in {"partial", "unavailable", "pending"} and attempt < len(retry_delays):
                    next_delay = retry_delays[attempt]
                    retry_key = (pending_id, attempt + 1)
                    if retry_key not in piopiy_recording_retry_keys:
                        piopiy_recording_retry_keys.add(retry_key)
                        _append_piopiy_trace(
                            "recording_retry_scheduled",
                            pending_id=pending_id,
                            next_attempt=attempt + 1,
                            delay_seconds=next_delay,
                            status=status,
                        )
                        _enqueue_piopiy_recording_processing(
                            pending_id=pending_id,
                            payload=payload,
                            source=f"{source}_retry_{attempt + 1}",
                            attempt=attempt + 1,
                            delay_seconds=next_delay,
                        )
            except Exception as exc:
                logger.exception(
                    "Piopiy post-call recording processing failed pending_id=%s source=%s",
                    pending_id,
                    source,
                )
                _append_piopiy_trace(
                    "recording_processing_failed",
                    pending_id=pending_id,
                    source=source,
                    attempt=attempt,
                    error=repr(exc),
                )

        asyncio.create_task(_runner())

    def _find_piopiy_session_for_payload(payload: dict[str, Any]) -> tuple[str | None, str | None]:
        to_number = str(payload.get("to") or payload.get("to_number") or "").strip()
        client_id, _project_id = _resolve_inbound_piopiy_target(to_number)
        if not client_id:
            return None, None
        call_ids = set(_piopiy_payload_call_ids(payload))
        if not call_ids:
            return client_id, None
        client_root = (settings.session_output_dir / client_id).resolve()
        if not client_root.exists():
            return client_id, None
        candidates = sorted(
            (item for item in client_root.iterdir() if item.is_dir()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for session_dir in candidates[:80]:
            artifacts = _read_json_file(session_dir / "artifacts.json")
            if not isinstance(artifacts, dict):
                continue
            context = artifacts.get("telephony_context")
            if not isinstance(context, dict):
                continue
            existing_ids = set(_piopiy_payload_call_ids(context))
            raw_cdr = str(context.get("piopiy_last_cdr_json") or "").strip()
            if raw_cdr:
                with contextlib.suppress(Exception):
                    parsed = json.loads(raw_cdr)
                    if isinstance(parsed, dict):
                        existing_ids.update(_piopiy_payload_call_ids(parsed))
            if call_ids.intersection(existing_ids):
                return client_id, session_dir.name
        return client_id, None

    def _piopiy_payload_call_ids(payload: dict[str, Any]) -> list[str]:
        ids: list[str] = []
        for key in (
            "provider_call_sid",
            "cmiuuid",
            "callSid",
            "call_id",
            "conversation_id",
            "request_id",
        ):
            value = str(payload.get(key) or "").strip()
            if value and value not in ids:
                ids.append(value)
        raw_cdr = str(payload.get("piopiy_last_cdr_json") or "").strip()
        if raw_cdr:
            with contextlib.suppress(Exception):
                parsed = json.loads(raw_cdr)
                if isinstance(parsed, dict):
                    for nested_id in _piopiy_payload_call_ids(parsed):
                        if nested_id and nested_id not in ids:
                            ids.append(nested_id)
        return ids

    @app.api_route("/piopiy/answer", methods=["GET", "POST"])
    async def piopiy_answer(request: Request) -> list[dict[str, object]]:
        captured = await _capture_piopiy_request(request)
        _record_piopiy_stage(
            "answer_webhook_received",
            method=captured.get("method"),
            url=captured.get("url"),
            payload=captured.get("payload"),
        )
        _append_piopiy_trace(
            "answer_webhook_received",
            method=captured.get("method"),
            url=captured.get("url"),
            payload=captured.get("payload"),
        )
        if request.method != "POST":
            logger.info(
                "Piopiy answer received non-POST request; answering anyway to avoid dropping live calls. payload=%s",
                captured.get("payload"),
            )
        payload = captured["payload"] if isinstance(captured.get("payload"), dict) else {}
        response = await _build_piopiy_answer_response(request, payload)
        piopiy_capture["last_answer"] = {"request": captured, "response": response}
        return response

    @app.api_route("/piopiy/answer/", methods=["GET", "POST"])
    async def piopiy_answer_slash(request: Request) -> list[dict[str, object]]:
        return await piopiy_answer(request)

    @app.api_route("/piopiy/answer/pcmo", methods=["GET", "POST"])
    async def piopiy_answer_pcmo(request: Request) -> list[dict[str, object]]:
        return await piopiy_answer(request)

    @app.post("/piopiy/events/{pending_id}")
    async def piopiy_events(pending_id: str, request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        await telephony.update_call_context(
            pending_id,
            {
                "piopiy_last_event_json": json.dumps(payload, ensure_ascii=True),
                "piopiy_last_event_at_epoch": time.time(),
                "piopiy_event_status": str(payload.get("status") or payload.get("event") or "").strip() or None,
                "provider_call_sid": str(payload.get("cmiuuid") or payload.get("callSid") or "").strip() or None,
            },
        )
        for alias in _piopiy_payload_call_ids(payload)[1:]:
            await telephony.update_pending_call_provider_sid(pending_id, alias)
        return {"ok": True}

    @app.api_route("/piopiy/events", methods=["GET", "POST"])
    async def piopiy_events_generic(request: Request) -> list[dict[str, object]] | dict[str, object]:
        if request.method != "POST":
            logger.info("Ignoring non-Piopiy probe on /piopiy/events")
            return {"ok": True}
        captured = await _capture_piopiy_request(request)
        _record_piopiy_stage(
            "events_webhook_received",
            method=captured.get("method"),
            url=captured.get("url"),
            payload=captured.get("payload"),
        )
        _append_piopiy_trace(
            "events_webhook_received",
            method=captured.get("method"),
            url=captured.get("url"),
            payload=captured.get("payload"),
        )
        payload = captured["payload"] if isinstance(captured.get("payload"), dict) else {}
        if not _looks_like_piopiy_payload(payload):
            logger.info("Ignoring malformed Piopiy payload on /piopiy/events")
            return {"ok": True}
        call_ids = _piopiy_payload_call_ids(payload)
        provider_call_sid = call_ids[0] if call_ids else ""
        pending_id = await telephony.find_pending_id_by_provider_sid("piopiy", provider_call_sid) if provider_call_sid else None
        status = str(payload.get("status") or payload.get("event") or "").strip().lower()
        direction = str(payload.get("direction") or "").strip().lower()
        leg = str(payload.get("leg") or "").strip().lower()
        voice_ai = bool(payload.get("voice_ai"))
        if pending_id is None and direction == "inbound" and voice_ai and leg in {"", "ai"} and status in {"ringing", "incoming", "queued"}:
            logger.info(
                "Piopiy events webhook is acting as the call-notification entrypoint; answering inbound call via fallback. call_sid=%s status=%s",
                provider_call_sid or "<missing>",
                status,
            )
            _record_piopiy_stage(
                "events_fallback_answering",
                provider_call_sid=provider_call_sid or None,
                status=status,
                direction=direction,
            )
            response = await _build_piopiy_answer_response(request, payload)
            piopiy_capture["last_events"] = {"request": captured, "matched_pending_id": None, "fallback_answered": True}
            piopiy_capture["last_answer"] = {"request": captured, "response": response, "source": "events_fallback"}
            return response
        if pending_id is None:
            logger.info("Piopiy event webhook received without matching pending call sid=%s", provider_call_sid or "<missing>")
            piopiy_capture["last_events"] = {"request": captured, "matched_pending_id": None}
            return {"ok": True}
        await telephony.update_call_context(
            pending_id,
            {
                "piopiy_last_event_json": json.dumps(payload, ensure_ascii=True),
                "piopiy_last_event_at_epoch": time.time(),
                "piopiy_event_status": status or None,
                "provider_call_sid": provider_call_sid or None,
            },
        )
        for alias in call_ids[1:]:
            await telephony.update_pending_call_provider_sid(pending_id, alias)
        piopiy_capture["last_events"] = {"request": captured, "matched_pending_id": pending_id}
        return {"ok": True}

    @app.post("/piopiy/cdr/{pending_id}")
    async def piopiy_cdr(pending_id: str, request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        call_ids = _piopiy_payload_call_ids(payload)
        await telephony.update_call_context(
            pending_id,
            {
                "piopiy_last_cdr_json": json.dumps(payload, ensure_ascii=True),
                "piopiy_last_cdr_at_epoch": time.time(),
                "piopiy_cdr_status": str(payload.get("status") or payload.get("event") or "").strip() or None,
                "provider_call_sid": str(
                    payload.get("cmiuuid")
                    or payload.get("callSid")
                    or payload.get("call_id")
                    or payload.get("conversation_id")
                    or payload.get("request_id")
                    or ""
                ).strip() or None,
                "piopiy_recording_url": _extract_piopiy_recording_info(payload)[0],
            },
        )
        for alias in call_ids[1:]:
            await telephony.update_pending_call_provider_sid(pending_id, alias)
        await _bootstrap_piopiy_dashboard_from_cdr(pending_id=pending_id, payload=payload)
        _enqueue_piopiy_recording_processing(pending_id=pending_id, payload=payload, source="cdr_pending_route")
        return {"ok": True}

    @app.api_route("/piopiy/report/{pending_id}", methods=["GET", "POST"])
    async def piopiy_report(pending_id: str, request: Request) -> dict[str, object]:
        return await piopiy_cdr(pending_id, request)

    @app.api_route("/piopiy/cdr", methods=["GET", "POST"])
    async def piopiy_cdr_generic(request: Request) -> dict[str, object]:
        if request.method != "POST":
            logger.info("Ignoring non-Piopiy probe on /piopiy/cdr")
            return {"ok": True}
        captured = await _capture_piopiy_request(request)
        _record_piopiy_stage(
            "cdr_webhook_received",
            method=captured.get("method"),
            url=captured.get("url"),
            payload=captured.get("payload"),
        )
        _append_piopiy_trace(
            "cdr_webhook_received",
            method=captured.get("method"),
            url=captured.get("url"),
            payload=captured.get("payload"),
        )
        payload = captured["payload"] if isinstance(captured.get("payload"), dict) else {}
        if not _looks_like_piopiy_payload(payload):
            logger.info("Ignoring malformed Piopiy payload on /piopiy/cdr")
            return {"ok": True}
        call_ids = _piopiy_payload_call_ids(payload)
        provider_call_sid = call_ids[0] if call_ids else ""
        pending_id = None
        for candidate_sid in call_ids:
            pending_id = await telephony.find_pending_id_by_provider_sid("piopiy", candidate_sid)
            if pending_id is not None:
                break
        if pending_id is None:
            fallback_client_id, fallback_session_id = _find_piopiy_session_for_payload(payload)
            if fallback_session_id:
                pending_id = fallback_session_id
                logger.info(
                    "Matched Piopiy CDR to saved session pending_id=%s via payload ids=%s",
                    pending_id,
                    call_ids,
                )
            else:
                recording_url, _recording_name = _extract_piopiy_recording_info(payload)
                _append_piopiy_trace(
                    "cdr_unmatched",
                    client_id=fallback_client_id,
                    call_id_candidates=call_ids,
                    recording_url=recording_url,
                )
        if pending_id is None:
            logger.info("Piopiy CDR webhook received without matching pending call sid=%s", provider_call_sid or "<missing>")
            piopiy_capture["last_cdr"] = {"request": captured, "matched_pending_id": None}
            return {"ok": True}
        for alias in call_ids:
            await telephony.update_pending_call_provider_sid(pending_id, alias)
        await telephony.update_call_context(
            pending_id,
            {
                "piopiy_last_cdr_json": json.dumps(payload, ensure_ascii=True),
                "piopiy_last_cdr_at_epoch": time.time(),
                "piopiy_cdr_status": str(payload.get("status") or payload.get("event") or "").strip() or None,
                "provider_call_sid": provider_call_sid or None,
                "piopiy_recording_url": _extract_piopiy_recording_info(payload)[0],
            },
        )
        piopiy_capture["last_cdr"] = {"request": captured, "matched_pending_id": pending_id}
        await _bootstrap_piopiy_dashboard_from_cdr(pending_id=pending_id, payload=payload)
        _enqueue_piopiy_recording_processing(pending_id=pending_id, payload=payload, source="cdr_generic_route")
        return {"ok": True}

    @app.api_route("/piopiy/call-report", methods=["GET", "POST"])
    async def piopiy_call_report_generic(request: Request) -> dict[str, object]:
        return await piopiy_cdr_generic(request)

    @app.api_route("/piopiy/call-notification", methods=["GET", "POST"])
    async def piopiy_call_notification_generic(request: Request) -> dict[str, object]:
        return await piopiy_events_generic(request)

    @app.api_route("/piopiy/notification", methods=["GET", "POST"])
    async def piopiy_notification_generic(request: Request) -> dict[str, object]:
        return await piopiy_events_generic(request)

    @app.api_route("/piopiy/debug", methods=["GET", "POST"])
    async def piopiy_debug(request: Request) -> dict[str, object]:
        if request.method != "POST":
            logger.info("Ignoring non-Piopiy probe on /piopiy/debug")
            return {
                "ok": True,
                "pending_id": None,
                "answer_url": str(request.base_url).rstrip("/") + "/piopiy/answer",
                "events_url": str(request.base_url).rstrip("/") + "/piopiy/events",
                "cdr_url": str(request.base_url).rstrip("/") + "/piopiy/cdr",
                "inspect_url": str(request.base_url).rstrip("/") + "/piopiy/inspect",
                "trace_url": str(request.base_url).rstrip("/") + "/piopiy/trace",
            }
        captured = await _capture_piopiy_request(request)
        _record_piopiy_stage(
            "debug_webhook_received",
            method=captured.get("method"),
            url=captured.get("url"),
            payload=captured.get("payload"),
        )
        _append_piopiy_trace(
            "debug_webhook_received",
            method=captured.get("method"),
            url=captured.get("url"),
            payload=captured.get("payload"),
        )
        payload = captured["payload"] if isinstance(captured.get("payload"), dict) else {}
        if not _looks_like_piopiy_payload(payload):
            logger.info("Ignoring malformed Piopiy payload on /piopiy/debug")
            piopiy_capture["last_debug"] = {"request": captured, "matched_pending_id": None, "malformed": True}
            return {"ok": True, "pending_id": None, "source": "debug"}
        call_ids = _piopiy_payload_call_ids(payload)
        provider_call_sid = call_ids[0] if call_ids else ""
        pending_id = None
        for candidate_sid in call_ids:
            pending_id = await telephony.find_pending_id_by_provider_sid("piopiy", candidate_sid)
            if pending_id is not None:
                break
        status = str(payload.get("status") or payload.get("event") or "").strip().lower()
        direction = str(payload.get("direction") or "").strip().lower()
        leg = str(payload.get("leg") or "").strip().lower()
        voice_ai = bool(payload.get("voice_ai"))
        if pending_id is None and direction == "inbound" and voice_ai and leg in {"", "ai"} and status in {"ringing", "incoming", "queued"}:
            logger.info(
                "Piopiy debug webhook is acting as the call-notification entrypoint; answering inbound call via debug path. call_sid=%s status=%s",
                provider_call_sid or "<missing>",
                status,
            )
            _record_piopiy_stage(
                "debug_fallback_answering",
                provider_call_sid=provider_call_sid or None,
                status=status,
                direction=direction,
            )
            response = await _build_piopiy_answer_response(request, payload)
            piopiy_capture["last_debug"] = {
                "request": captured,
                "matched_pending_id": None,
                "fallback_answered": True,
            }
            piopiy_capture["last_answer"] = {"request": captured, "response": response, "source": "debug_fallback"}
            return {"ok": True, "pending_id": None, "source": "debug", "answered": True, "response": response}
        if pending_id is not None:
            await telephony.update_call_context(
                pending_id,
                {
                    "piopiy_last_debug_json": json.dumps(payload, ensure_ascii=True),
                    "piopiy_last_debug_at_epoch": time.time(),
                    "provider_call_sid": provider_call_sid or None,
                },
            )
            for alias in call_ids[1:]:
                await telephony.update_pending_call_provider_sid(pending_id, alias)
        piopiy_capture["last_debug"] = {"request": captured, "matched_pending_id": pending_id}
        logger.info("Piopiy debug webhook received pending_id=%s payload=%s", pending_id or "<unmatched>", payload)
        return {"ok": True, "pending_id": pending_id, "source": "debug"}

    @app.get("/piopiy/inspect")
    async def piopiy_inspect() -> dict[str, object]:
        return {
            "last_request": piopiy_capture["last_request"],
            "last_answer": piopiy_capture["last_answer"],
            "last_debug": piopiy_capture["last_debug"],
            "last_cdr": piopiy_capture["last_cdr"],
            "last_events": piopiy_capture["last_events"],
            "last_catcher": piopiy_capture["last_catcher"],
            "debug_state": piopiy_debug_state,
        }

    @app.get("/piopiy/debug-state")
    async def piopiy_debug_state_endpoint() -> dict[str, object]:
        return dict(piopiy_debug_state)

    @app.get("/debug/latest-call-runtime")
    async def latest_call_runtime_debug(request: Request) -> dict[str, object]:
        _require_admin_user(request)
        runtime = _read_latest_call_runtime()
        session_id = str(runtime.get("session_id") or runtime.get("latest_session_id") or "").strip()
        client_id = str(runtime.get("client_id") or "").strip()
        recording_entry = None
        if client_id and session_id:
            with contextlib.suppress(Exception):
                session_dir = (settings.session_output_dir / client_id / session_id).resolve()
                recording_entry = _build_recording_entry(client_id, session_dir)
        if isinstance(recording_entry, dict):
            runtime.setdefault("recording_status", recording_entry.get("recording_status"))
            runtime.setdefault("recording_duration", recording_entry.get("recording_duration_seconds"))
            runtime.setdefault("recording_duration_seconds", recording_entry.get("recording_duration_seconds"))
            runtime.setdefault("recording_error", recording_entry.get("recording_error"))
            files = recording_entry.get("files")
            if isinstance(files, list) and files:
                runtime.setdefault("recording_file", files[0].get("filename") if isinstance(files[0], dict) else None)
            runtime.setdefault("local_file_size", None)
            if client_id and session_id and runtime.get("recording_file"):
                with contextlib.suppress(Exception):
                    local_path = settings.session_output_dir / client_id / session_id / str(runtime["recording_file"])
                    runtime["local_file_size"] = local_path.stat().st_size
        return {
            "ok": True,
            "latest_session_id": session_id or None,
            "runtime": {
                key: value
                for key, value in runtime.items()
                if "key" not in key.lower() and "token" not in key.lower() and "secret" not in key.lower()
            },
            "recent_trace_entries": _read_piopiy_trace_entries(limit=30, pending_id=session_id or None),
        }

    @app.get("/piopiy/recording-debug/{client_id}/{session_id}")
    async def piopiy_recording_debug(client_id: str, session_id: str) -> dict[str, object]:
        session_dir = (settings.session_output_dir / client_id / session_id).resolve()
        artifacts_path = session_dir / "artifacts.json"
        recording_meta_path = session_dir / "piopiy_recording.json"
        artifacts = _read_json_file(artifacts_path)
        telephony_context = {}
        if isinstance(artifacts, dict) and isinstance(artifacts.get("telephony_context"), dict):
            telephony_context = dict(artifacts.get("telephony_context") or {})
        project_id = str(
            (artifacts.get("project_id") if isinstance(artifacts, dict) else None)
            or telephony_context.get("project_id")
            or ""
        ).strip() or None
        resolved_project_app_id = _resolve_project_piopiy_app_id(client_id, project_id)
        saved_files: list[dict[str, Any]] = []
        if session_dir.exists() and session_dir.is_dir():
            for item in sorted(session_dir.iterdir()):
                if not item.is_file():
                    continue
                saved_files.append(
                    {
                        "name": item.name,
                        "size_bytes": item.stat().st_size,
                        "suffix": item.suffix.lower(),
                    }
                )
        piopiy_payload = {}
        raw_cdr_json = str(telephony_context.get("piopiy_last_cdr_json") or "").strip()
        if raw_cdr_json:
            with contextlib.suppress(Exception):
                parsed_payload = json.loads(raw_cdr_json)
                if isinstance(parsed_payload, dict):
                    piopiy_payload = parsed_payload
        recording_url, recording_filename = _extract_piopiy_recording_info(piopiy_payload)
        started_at = _parse_iso_datetime(artifacts.get("started_at")) if isinstance(artifacts, dict) else None
        ended_at = _parse_iso_datetime(artifacts.get("ended_at")) if isinstance(artifacts, dict) else None
        lookup_result = await _fetch_piopiy_recording_from_cdr(
            client_id=client_id,
            session_dir=session_dir,
            app_id=resolved_project_app_id,
            call_id_candidates=_piopiy_payload_call_ids(telephony_context) or [session_id],
            caller_id=str(
                telephony_context.get("caller_id")
                or telephony_context.get("from_number")
                or telephony_context.get("caller_number")
                or ""
            ).strip()
            or None,
            to_number=str(telephony_context.get("to_number") or telephony_context.get("to") or "").strip() or None,
            started_at=started_at,
            ended_at=ended_at,
            direction=str(telephony_context.get("call_direction") or telephony_context.get("direction") or "").strip() or None,
        )
        pending_id = str(
            telephony_context.get("provider_call_sid")
            or telephony_context.get("piopiy_call_id")
            or telephony_context.get("piopiy_conversation_id")
            or session_id
        ).strip() or session_id
        return {
            "ok": True,
            "client_id": client_id,
            "session_id": session_id,
            "session_dir": str(session_dir),
            "artifacts_exists": artifacts_path.exists(),
            "piopiy_recording_meta_exists": recording_meta_path.exists(),
            "saved_files": saved_files,
            "project_id": project_id,
            "resolved_project_piopiy_app_id": resolved_project_app_id,
            "telephony_context": telephony_context,
            "cdr_payload_recording_url": recording_url,
            "cdr_payload_recording_filename": recording_filename,
            "live_lookup_result": lookup_result,
            "trace_entries": _read_piopiy_trace_entries(limit=40, pending_id=session_id)
            or _read_piopiy_trace_entries(limit=40, pending_id=pending_id),
        }

    @app.get("/piopiy/debug-url")
    async def piopiy_debug_url(request: Request) -> dict[str, object]:
        base_url = str(request.base_url).rstrip("/")
        return {
            "ok": True,
            "answer_url": f"{base_url}/piopiy/answer",
            "events_url": f"{base_url}/piopiy/events",
            "cdr_url": f"{base_url}/piopiy/cdr",
            "debug_url": f"{base_url}/piopiy/debug",
            "catcher_url": f"{base_url}/piopiy/catcher",
            "inspect_url": f"{base_url}/piopiy/inspect",
            "trace_url": f"{base_url}/piopiy/trace",
            "recording_debug_url_pattern": f"{base_url}/piopiy/recording-debug/{{client_id}}/{{session_id}}",
            "stream_url_pattern": f"{base_url}/piopiy/stream/{{pending_id}}",
            "debug_state": dict(piopiy_debug_state),
            "last_request": piopiy_capture["last_request"],
            "last_answer": piopiy_capture["last_answer"],
            "last_debug": piopiy_capture["last_debug"],
            "last_cdr": piopiy_capture["last_cdr"],
            "last_events": piopiy_capture["last_events"],
        }

    @app.get("/piopiy/trace")
    async def piopiy_trace(limit: int = 50) -> dict[str, object]:
        entries: list[dict[str, Any]] = []
        if piopiy_trace_file.exists():
            try:
                lines = piopiy_trace_file.read_text(encoding="utf-8").splitlines()
                for raw_line in lines[-max(limit, 1):]:
                    if not raw_line.strip():
                        continue
                    try:
                        entries.append(json.loads(raw_line))
                    except json.JSONDecodeError:
                        entries.append({"raw": raw_line})
            except Exception as exc:
                return {"ok": False, "error": str(exc), "path": str(piopiy_trace_file)}
        return {"ok": True, "path": str(piopiy_trace_file), "entries": entries}

    @app.api_route("/piopiy/catcher", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def piopiy_catcher(request: Request) -> dict[str, object]:
        captured = await _capture_piopiy_catcher_request(request)
        logger.info(
            "Piopiy catcher received request method=%s path=%s payload_type=%s",
            captured.get("method"),
            request.url.path,
            type(captured.get("payload")).__name__,
        )
        return {"ok": True}

    @app.websocket("/piopiy/stream/{pending_id}")
    async def piopiy_stream(websocket: WebSocket, pending_id: str) -> None:
        logger.info("Piopiy stream websocket connection requested for pending_id=%s", pending_id)
        _record_piopiy_stage("stream_websocket_requested", pending_id=pending_id)
        await websocket.accept()
        _record_piopiy_stage("stream_websocket_accepted", pending_id=pending_id)
        pending_call = await telephony.consume_pending_call(pending_id)
        if pending_call is None:
            logger.warning("No pending Piopiy call found for stream websocket pending_id=%s", pending_id)
            _record_piopiy_stage("stream_pending_call_missing", pending_id=pending_id)
            await websocket.close()
            return

        bridge = PiopiyMediaBridge(websocket)
        session_started = False
        bridge_task: asyncio.Task[None] | None = None
        try:
            configured_stream_runtime = str(os.getenv("PIOPIY_STREAM_RUNTIME") or "").strip().lower()
            stream_runtime = configured_stream_runtime or "session_controller"
            selected_native = stream_runtime in {"direct_gemini_bridge", "direct_gemini", "gemini_bridge"}
            selected_audio_path = "NATIVE_GEMINI_LIVE_AUDIO" if selected_native else "SPEECH_AGENT_WITH_TTS_FALLBACK"
            marker = f"AUDIO_PATH_SELECTED = {selected_audio_path}"
            _write_latest_call_runtime(
                {
                    "latest_session_id": pending_id,
                    "session_id": pending_id,
                    "client_id": pending_call.client_id,
                    "project_id": (pending_call.metadata or {}).get("project_id"),
                    "selected_agent_class": "PiopiyDirectGeminiBridgeSession" if selected_native else "VoiceSalesSession",
                    "selected_audio_path": selected_audio_path,
                    "native_voice_agent_enabled": selected_native,
                    "speech_agent_fallback_enabled": not selected_native,
                    "gemini_live_native_audio_enabled": selected_native,
                    "separate_tts_enabled": not selected_native,
                    "active_tts_provider": None if selected_native else "gemini",
                    "active_tts_model": None if selected_native else settings.tts_model,
                    "active_voice_name": "gemini_native" if selected_native else None,
                    "audio_path_locked": True,
                    "voice_locked": True,
                    "fallback_switch_count": 0,
                    "fallback_triggered": False,
                    "fallback_reason": None,
                    "duplicate_audio_path_detected": False,
                    "marker": marker,
                }
            )
            _record_piopiy_stage(
                "stream_runtime_selected",
                pending_id=pending_id,
                runtime=stream_runtime,
                client_id=pending_call.client_id,
                project_id=(pending_call.metadata or {}).get("project_id"),
                selected_audio_path=selected_audio_path,
                selected_agent_class="PiopiyDirectGeminiBridgeSession" if selected_native else "VoiceSalesSession",
                native_voice_agent_enabled=selected_native,
                speech_agent_fallback_enabled=not selected_native,
                gemini_live_native_audio_enabled=selected_native,
                separate_tts_enabled=not selected_native,
                audio_path_locked=True,
                voice_locked=True,
                fallback_switch_count=0,
                marker=marker,
            )
            logger.info(
                "%s session_id=%s selected_agent_class=%s native_voice_agent_enabled=%s speech_agent_fallback_enabled=%s gemini_live_native_audio_enabled=%s separate_tts_enabled=%s active_tts_provider=%s active_tts_model=%s active_voice_name=%s",
                marker,
                pending_id,
                "PiopiyDirectGeminiBridgeSession" if selected_native else "VoiceSalesSession",
                selected_native,
                not selected_native,
                selected_native,
                not selected_native,
                None if selected_native else "gemini",
                None if selected_native else settings.tts_model,
                "gemini_native" if selected_native else None,
            )
            if stream_runtime in {"direct_gemini_bridge", "direct_gemini", "gemini_bridge"}:
                logger.info("Starting direct Gemini bridge for Piopiy pending_id=%s", pending_id)
                def _direct_trace(message: str) -> None:
                    _append_piopiy_trace(
                        "direct_gemini_trace",
                        pending_id=pending_id,
                        message=message,
                    )
                    _capture_runtime_trace(pending_id, message)

                direct_session = PiopiyDirectGeminiBridgeSession(
                    settings=settings,
                    pending_call=pending_call,
                    audio=bridge,
                    trace_hook=_direct_trace,
                    stage_hook=lambda stage: (
                        _record_piopiy_stage(stage, pending_id=pending_id),
                        _write_latest_call_runtime({"latest_session_id": pending_id, "session_id": pending_id, "last_stage": stage}),
                    ),
                )
                session_started = True
                _record_piopiy_stage("stream_session_started", pending_id=pending_id, runtime=stream_runtime)
                bridge_task = asyncio.create_task(direct_session.run(), name=f"piopiy_direct_gemini_{pending_id}")
            else:
                logger.info("Pre-initializing Piopiy voice session pending_id=%s", pending_id)
                _record_piopiy_stage(
                    "stream_session_starting",
                    pending_id=pending_id,
                    client_id=pending_call.client_id,
                    project_id=(pending_call.metadata or {}).get("project_id"),
                )
                await controller.start_with_audio(
                    client_id=pending_call.client_id,
                    customer_name=pending_call.customer_name,
                    project_id=(pending_call.metadata or {}).get("project_id"),
                    audio=bridge,
                    telephony_context=pending_call.metadata,
                    session_key=pending_id,
                    defer_initial_prompt=False,
                    session_id=pending_id,
                )
                session_started = True
                _record_piopiy_stage("stream_session_started", pending_id=pending_id)
            while True:
                if bridge_task is not None and bridge_task.done():
                    exc = bridge_task.exception()
                    if exc is not None:
                        raise exc
                    _record_piopiy_stage("stream_session_finished", pending_id=pending_id)
                    logger.info("Direct Gemini bridge finished; closing Piopiy stream pending_id=%s", pending_id)
                    break
                if session_started and not await controller.is_busy(session_key=pending_id):
                    _record_piopiy_stage("stream_session_finished", pending_id=pending_id)
                    logger.info("Voice session finished; closing Piopiy stream pending_id=%s", pending_id)
                    break
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=1.0)
                except TimeoutError:
                    continue
                if "bytes" in message and message["bytes"] is not None:
                    await bridge.push_binary(message["bytes"])
                    _record_piopiy_stage(
                        "stream_binary_received",
                        pending_id=pending_id,
                        byte_length=len(message["bytes"]),
                    )
                    if session_started and bridge_task is None:
                        await controller.release_initial_prompt(session_key=pending_id)
                    continue
                text_payload = message.get("text")
                if not text_payload:
                    continue
                try:
                    parsed_message = json.loads(text_payload)
                except json.JSONDecodeError:
                    logger.debug("Piopiy stream websocket received non-JSON text for pending_id=%s", pending_id)
                    continue
                event = str(parsed_message.get("event") or parsed_message.get("status") or "").strip().lower()
                if event:
                    logger.info("Piopiy stream event: pending_id=%s event=%s", pending_id, event)
                    _record_piopiy_stage("stream_event_received", pending_id=pending_id, event=event)
                await bridge.handle_ws_message(parsed_message)
                if event in {"stop", "hangup", "stream_disconnected", "stream_error"}:
                    _record_piopiy_stage("stream_terminated", pending_id=pending_id, event=event)
                    break
        except WebSocketDisconnect:
            logger.info("Piopiy stream websocket disconnected for pending_id=%s", pending_id)
            _record_piopiy_stage("stream_websocket_disconnected", pending_id=pending_id)
        except RuntimeError:
            logger.exception("Piopiy stream session failed for pending_id=%s", pending_id)
            _record_piopiy_stage("stream_runtime_error", pending_id=pending_id)
            await bridge.close()
        finally:
            _record_piopiy_stage("stream_websocket_closed", pending_id=pending_id)
            if bridge_task is not None and not bridge_task.done():
                bridge_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bridge_task
            if session_started:
                await _safe_stop_session(session_key=pending_id)
            await bridge.close()

    return app


app = create_app()
