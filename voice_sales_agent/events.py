"""Event models for session state updates across CLI and web UI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class SessionEvent:
    """Serializable event emitted by a running voice session."""

    kind: Literal["status", "turn", "metric", "summary", "error", "lifecycle", "user_intents"]
    payload: dict[str, Any] = field(default_factory=dict)


SessionEventHandler = Callable[[SessionEvent], Awaitable[None] | None]
