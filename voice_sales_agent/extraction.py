"""Structured post-call extraction using Gemini text generation."""

from __future__ import annotations

import json
import logging
import re
import textwrap
from datetime import datetime, timezone
from typing import Any

from .models import ClientBundle, PostCallSummary, RecordingKeyDetails, SessionArtifacts, SessionMemory

logger = logging.getLogger(__name__)


def _dedupe_summary_text(text: str) -> str:
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        return ""
    parts = [chunk.strip() for chunk in re.split(r"(?<=[.!?।])\s+|\s*\|\s*|\n+", normalized) if chunk.strip()]
    seen: set[str] = set()
    kept: list[str] = []
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(part)
    return " ".join(kept) if kept else normalized


def build_extraction_prompt(client: ClientBundle, artifacts: SessionArtifacts) -> str:
    """Compose a strict JSON extraction prompt from transcript and client context."""
    transcript_text = "\n".join(f"{turn.speaker}: {turn.text}" for turn in artifacts.transcript)
    required_shape = {
        "lead_name": "string or null",
        "company": "string or null",
        "role": "string or null",
        "contact_details": {"email": "string", "phone": "string"},
        "use_case": "string or null",
        "budget_timeline_hints": "string or null",
        "objections": ["string"],
        "interest_level": "low | medium | high | unknown",
        "summary": "string",
        "suggested_next_action": "string",
        "qualification": {
            "status": "qualified | partially_qualified | not_qualified | unknown",
            "checklist": {"field_name": "short status or note"},
        },
    }
    return textwrap.dedent(
        f"""
        You are extracting CRM-ready information from a sales call.

        Return valid JSON only. Do not wrap it in markdown.
        Use exactly these top-level keys:
        {json.dumps(required_shape, indent=2, ensure_ascii=False)}

        Rules:
        - Only include facts grounded in the transcript.
        - If a field is unknown, use null, [] or "unknown" as appropriate.
        - Keep the summary concise and useful for a sales rep.
        - Keep `summary` to 1-3 short sentences and avoid repeating the same point.
        - Keep `suggested_next_action` as one concrete, non-repetitive action line.
        - `contact_details` may only contain keys actually mentioned in the transcript.
        - `qualification.checklist` should map checklist field names to short statuses or notes.

        Client context:
        {json.dumps(client.config.model_dump(mode="json"), indent=2, ensure_ascii=False)}

        Qualification config:
        {json.dumps(client.qualification, indent=2, ensure_ascii=False)}

        Transcript:
        {transcript_text}
        """
    ).strip()


def build_recording_details_prompt(
    corpus_text: str,
    call_started_at: str | None = None,
    booking_intent_detected: bool = False,
) -> str:
    """Build strict JSON prompt for extracting critical appointment fields."""
    required_shape = {
        "name": "string or null",
        "age": "integer or null",
        "appointment_date": "YYYY-MM-DD or null",
        "appointment_time": "HH:MM (24h) or null",
        "important_questions_asked": ["string"],
    }
    call_start_raw = str(call_started_at or "").strip()
    call_date_context = ""
    if call_start_raw:
        call_date_context = f"Current date context (UTC): {call_start_raw[:10]}"
    else:
        call_date_context = f"Current date context (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    booking_rule_block = ""
    if booking_intent_detected:
        booking_rule_block = textwrap.dedent(
            """
            Booking-intent guardrails:
            - Booking intent is present in this call corpus. Prioritize extraction of `age`, `appointment_date`, and `appointment_time`.
            - If age/date/time are explicitly present in the end user speech, capture them.
            - Do not hallucinate; if not explicitly present or not confidently parseable, return null.
            """
        ).strip()
    return textwrap.dedent(
        f"""
        You are extracting critical healthcare call details from STT corpus text.

        Return valid JSON only. Do not use markdown.

        Use exactly this shape:
        {json.dumps(required_shape, indent=2, ensure_ascii=False)}

        Rules:
        - The conversation is between a bot and an end user. The bot asks questions, and the patient’s details must be extracted only from the end user’s responses.
        - Extract the patient’s full name (lead name) only if explicitly mentioned by the end user. If only partial name is given, capture it as-is. Do not infer or expand.
        - Accept `name` only when the user explicitly self-identifies (examples: "my name is ...", "मेरा नाम ...", "माझं नाव ...", "I am ..."). If this pattern is missing, set `name` to null.
        - Reject `name` values that look like organization/script text (hospital/helpdesk/agent lines) or contain more than 4 words.
        - Extract age only if the end user clearly states it in response to the bot’s question.
        - Use only information explicitly present in the corpus text.
        - If unclear or missing, use null (or [] for list).
        - Normalize appointment_date to YYYY-MM-DD when possible.
        - If the end user mentions a weekday (e.g., Sunday, Monday, Raviwar, Somvar, etc.) instead of an exact date, convert it to the nearest upcoming date matching that weekday (based on current date context). If conversion is not possible, return null.
        - Normalize appointment_time to HH:MM 24-hour format when possible.
        - Accept `appointment_time` only when an explicit time is present (examples: HH:MM, "10 AM", "10:30 PM", "10 बजे", "10 वाजता"). Do not infer from vague words alone (morning/evening/etc).
        - `age` must be an integer only if confidently stated as the patient's age.
        - `important_questions_asked` should include only questions asked by the end user (not the bot).
        - Keep values concise and avoid hallucinations.

        {call_date_context}
        {booking_rule_block}

        STT Corpus:
        {corpus_text}
        """
    ).strip()


def build_key_details_prompt(transcript_text: str) -> str:
    """Build strict JSON prompt for extracting a name, problem, and location."""
    required_shape = {
        "name": "string or null",
        "problem": "string or null",
        "location": "string or null",
    }
    return textwrap.dedent(
        f"""
        You are extracting key call details from a transcript for a WhatsApp follow-up.

        Return valid JSON only. Do not wrap it in markdown.
        Use exactly this shape:
        {json.dumps(required_shape, indent=2, ensure_ascii=False)}

        Rules:
        - Only use facts that are explicitly present in caller/user speech.
        - Ignore any agent/AI speech, even if it repeats, confirms, or infers a detail.
        - `name` should come from the caller only. If the caller does not state a name, return null.
        - `problem` should be a short plain-language description of the caller's issue or complaint.
        - `location` should be the location, ward, area, village, city, or other place name if the caller mentions it. If not mentioned by the caller, return null.
        - If a location is mentioned only by the agent/AI while asking the caller to send details on WhatsApp, treat it as not mentioned and return null.
        - Keep `problem` and `location` concise.
        - Do not infer or expand missing information.

        Transcript:
        {transcript_text}
        """
    ).strip()


def parse_key_details_payload(raw_text: str) -> RecordingKeyDetails:
    """Parse key-detail extraction output into a validated model."""
    payload = raw_text.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        payload = payload.replace("json", "", 1).strip()
    try:
        details = RecordingKeyDetails.model_validate_json(payload)
    except Exception:
        start = payload.find("{")
        end = payload.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                details = RecordingKeyDetails.model_validate_json(payload[start : end + 1])
            except Exception:
                details = RecordingKeyDetails()
        else:
            details = RecordingKeyDetails()
    details.name = " ".join(str(details.name or "").split()).strip() or None
    details.problem = " ".join(str(details.problem or "").split()).strip() or None
    details.location = " ".join(str(details.location or "").split()).strip() or None
    return details


def clear_non_caller_location(details: RecordingKeyDetails, caller_transcript_text: str) -> RecordingKeyDetails:
    """Drop locations that do not appear in the caller's own transcript text."""
    location = str(details.location or "").strip()
    if not location:
        return details

    caller_text = " ".join(str(caller_transcript_text or "").split()).lower()
    normalized_location = " ".join(location.split()).lower()
    if normalized_location and normalized_location not in caller_text:
        details.location = None
    return details


def parse_summary_payload(raw_text: str) -> PostCallSummary:
    """Parse model output into a validated summary object."""
    payload = raw_text.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        payload = payload.replace("json", "", 1).strip()
    try:
        summary = PostCallSummary.model_validate_json(payload)
    except Exception:
        start = payload.find("{")
        end = payload.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                summary = PostCallSummary.model_validate_json(payload[start : end + 1])
            except Exception:
                summary = PostCallSummary(
                    summary="Call completed, but structured summary generation returned incomplete JSON.",
                    suggested_next_action="Review transcript manually and follow up based on the latest confirmed call outcome.",
                )
        else:
            summary = PostCallSummary(
                summary="Call completed, but structured summary generation returned incomplete JSON.",
                suggested_next_action="Review transcript manually and follow up based on the latest confirmed call outcome.",
            )
    if not summary.suggested_next_action.strip():
        if summary.interest_level in {"high", "medium"}:
            summary.suggested_next_action = "Follow up with the prospect and propose a short demo or callback."
        else:
            summary.suggested_next_action = "Record the interaction and have a specialist review the next outreach step."
    summary.summary = _dedupe_summary_text(summary.summary)
    summary.suggested_next_action = _dedupe_summary_text(summary.suggested_next_action)
    if summary.suggested_next_action.lower() == summary.summary.lower():
        if summary.interest_level in {"high", "medium"}:
            summary.suggested_next_action = "Follow up with the prospect and confirm an exact date/time for the next call."
        else:
            summary.suggested_next_action = "Retry outreach and capture the caller's requirement with a clear callback window."
    return summary


def merge_memory(existing: SessionMemory, summary: PostCallSummary) -> SessionMemory:
    """Merge post-call summary fields into the current memory object."""
    updated = existing.model_copy(deep=True)
    updated.lead_name = summary.lead_name or updated.lead_name
    updated.company = summary.company or updated.company
    updated.role = summary.role or updated.role
    updated.contact_details.update(summary.contact_details)
    updated.use_case = summary.use_case or updated.use_case
    updated.budget_timeline = summary.budget_timeline_hints or updated.budget_timeline
    updated.objections = list(dict.fromkeys([*updated.objections, *summary.objections]))
    updated.interest_level = summary.interest_level
    updated.next_step = summary.suggested_next_action or updated.next_step
    return updated


def best_effort_json(raw_text: str) -> dict[str, Any]:
    """Try to parse arbitrary text as JSON for debugging."""
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        logger.debug("Could not parse model output as JSON: %s", raw_text)
        return {"raw_text": raw_text}
