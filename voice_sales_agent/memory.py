"""Lightweight session memory extraction for in-call context and logging."""

from __future__ import annotations

import re

from .models import SessionMemory

NAME_PATTERNS = [
    re.compile(r"\b(?:i am|i'm|this is)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", re.IGNORECASE),
]
COMPANY_PATTERNS = [
    re.compile(r"\bfrom\s+([A-Z][\w&.\- ]+)", re.IGNORECASE),
    re.compile(r"\bat\s+([A-Z][\w&.\- ]+)", re.IGNORECASE),
]
ROLE_PATTERNS = [
    re.compile(r"\b(?:i'm|i am)\s+(?:the\s+)?([a-z][a-z\s/-]{2,40})\s+at\b", re.IGNORECASE),
]
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(?:\+?\d[\d\s-]{7,}\d)")
TIME_PATTERN = re.compile(
    r"(?:at\s*)?(?:\d{1,2}(?::\d{2})?\s?(?:am|pm)?|(?<!\d)\d{1,2}(?::\d{2})?\s*(?:बजे|वाजता)|morning|afternoon|evening|सुबह|दोपहर|शाम|सकाळ|दुपार|सायंकाळ|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|सोमवार|मंगळवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार)",
    re.IGNORECASE,
)
DATE_HINT_PATTERN = re.compile(
    r"\b(?:tomorrow|day after tomorrow|today|कल|परसों|उद्या|परवा|सोमवार|मंगळवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार)\b",
    re.IGNORECASE,
)


def update_memory_from_user_text(memory: SessionMemory, text: str) -> SessionMemory:
    """Best-effort enrichment from user turns without blocking the live loop."""
    updated = memory.model_copy(deep=True)
    lowered = text.lower()

    if updated.lead_name is None:
        for pattern in NAME_PATTERNS:
            match = pattern.search(text)
            if match:
                updated.lead_name = match.group(1).strip()
                break

    if updated.company is None:
        for pattern in COMPANY_PATTERNS:
            match = pattern.search(text)
            if match:
                updated.company = match.group(1).strip(" .,")
                break

    if updated.role is None:
        for pattern in ROLE_PATTERNS:
            match = pattern.search(text)
            if match:
                updated.role = match.group(1).strip()
                break

    email_match = EMAIL_PATTERN.search(text)
    if email_match:
        updated.contact_details["email"] = email_match.group(0).strip("., ")

    phone_match = PHONE_PATTERN.search(text)
    if phone_match and "phone" not in updated.contact_details:
        updated.contact_details["phone"] = " ".join(phone_match.group(0).split())

    if any(keyword in lowered for keyword in ["budget", "quarter", "month", "timeline", "deadline"]):
        updated.budget_timeline = text.strip()

    mentions_scheduling_keyword = any(
        keyword in lowered
        for keyword in ["demo", "meeting", "call", "appointment", "calendar", "invite", "timing", "time"]
    )
    has_time_or_date_hint = bool(TIME_PATTERN.search(text) or DATE_HINT_PATTERN.search(text))
    if mentions_scheduling_keyword or has_time_or_date_hint:
        updated.appointment_details = text.strip()
        updated.next_step = text.strip()

    objection_markers = ["too expensive", "already have", "not interested", "send me", "busy", "budget"]
    if any(marker in lowered for marker in objection_markers):
        if text not in updated.objections:
            updated.objections.append(text.strip())

    pain_markers = ["manual", "slow", "backlog", "missed", "fatigue", "overloaded", "inefficient"]
    if any(marker in lowered for marker in pain_markers):
        if text not in updated.pain_points:
            updated.pain_points.append(text.strip())

    if any(marker in lowered for marker in ["interested", "sounds good", "worth exploring"]):
        updated.interest_level = "high"
    elif any(marker in lowered for marker in ["maybe", "later", "follow up"]):
        updated.interest_level = "medium"

    return updated
