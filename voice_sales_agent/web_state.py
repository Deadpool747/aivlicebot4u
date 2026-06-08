"""Shared runtime state for the local dashboard."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .events import SessionEvent


@dataclass(slots=True)
class DashboardState:
    """Track the currently running local session for the browser UI."""

    active_client_id: str | None = None
    active_project_id: str | None = None
    customer_name: str | None = None
    opening_language: str | None = None
    running: bool = False
    status: str = "idle"
    detail: str = "No active session."
    session_id: str | None = None
    session_dir: str | None = None
    last_error: str | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    summary: dict[str, Any] | None = None
    latest_user_text: str | None = None
    latest_user_intents: list[str] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def reset_for_start(self, client_id: str, customer_name: str, project_id: str | None = None) -> None:
        async with self._lock:
            self.active_client_id = client_id
            self.active_project_id = project_id
            self.customer_name = customer_name
            self.opening_language = None
            self.running = True
            self.status = "starting"
            self.detail = "Opening browser audio and live session..."
            self.session_id = None
            self.session_dir = None
            self.last_error = None
            self.turns = []
            self.metrics = {}
            self.summary = None
            self.latest_user_text = None
            self.latest_user_intents = []

    async def apply(self, event: SessionEvent) -> None:
        async with self._lock:
            if event.kind == "status":
                self.status = str(event.payload.get("status", self.status))
                self.detail = str(event.payload.get("detail", self.detail))
            elif event.kind == "turn":
                self.turns.append(event.payload)
            elif event.kind == "metric":
                for key, value in event.payload.items():
                    if isinstance(value, (int, float)):
                        self.metrics[key] = float(value)
            elif event.kind == "summary":
                self.summary = event.payload
            elif event.kind == "user_intents":
                intents = event.payload.get("intents")
                self.latest_user_intents = [str(item) for item in intents] if isinstance(intents, list) else []
                text = event.payload.get("text")
                self.latest_user_text = str(text) if text is not None else None
            elif event.kind == "error":
                self.last_error = str(event.payload.get("message", "Unknown error"))
                self.status = "error"
                self.detail = self.last_error
            elif event.kind == "lifecycle":
                stage = str(event.payload.get("stage", ""))
                if stage == "starting":
                    self.status = "starting"
                    self.detail = "Starting session..."
                    self.active_project_id = event.payload.get("project_id") or self.active_project_id
                    self.customer_name = event.payload.get("customer_name") or self.customer_name
                    self.opening_language = event.payload.get("opening_language") or self.opening_language
                if stage == "finished":
                    self.running = False
                    self.status = "finished"
                    self.detail = "Session finished."
                    self.session_dir = event.payload.get("session_dir") or self.session_dir
                    self.session_id = event.payload.get("session_id") or self.session_id

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "active_client_id": self.active_client_id,
                "customer_name": self.customer_name,
                "active_project_id": self.active_project_id,
                "opening_language": self.opening_language,
                "running": self.running,
                "status": self.status,
                "detail": self.detail,
                "session_id": self.session_id,
                "session_dir": self.session_dir,
                "last_error": self.last_error,
                "turns": list(self.turns),
                "metrics": dict(self.metrics),
                "summary": self.summary,
                "latest_user_text": self.latest_user_text,
                "latest_user_intents": list(self.latest_user_intents),
            }
