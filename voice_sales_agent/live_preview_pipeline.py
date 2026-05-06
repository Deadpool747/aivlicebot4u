"""Two-stage live preview extraction pipeline prompts and fallbacks."""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any


DEFAULT_LIVE_PREVIEW_FIELD_CONFIG: list[dict[str, Any]] = [
    {
        "key": "appointment",
        "label": "Appointment",
        "description": "Exact meeting date/time, site visit date, or scheduled booking time.",
        "required": False,
        "allow_inference": True,
        "fallback_behavior": "not_mentioned",
        "output_format": "short_text",
    },
    {
        "key": "budget",
        "label": "Budget",
        "description": "Any money amount, range, or stated spending limit.",
        "required": False,
        "allow_inference": True,
        "fallback_behavior": "not_mentioned",
        "output_format": "currency_or_range",
    },
    {
        "key": "callback",
        "label": "Callback",
        "description": "Requested follow-up date/time.",
        "required": False,
        "allow_inference": True,
        "fallback_behavior": "not_mentioned",
        "output_format": "short_text",
    },
    {
        "key": "deadline",
        "label": "Deadline",
        "description": "Target purchase window, urgency date, or completion timeline.",
        "required": False,
        "allow_inference": True,
        "fallback_behavior": "not_mentioned",
        "output_format": "short_text",
    },
    {
        "key": "decision",
        "label": "Decision / Commitment",
        "description": "Buying intent, approval conditions, decision stakeholders, or commitment signals.",
        "required": False,
        "allow_inference": True,
        "fallback_behavior": "not_mentioned",
        "output_format": "short_text",
    },
    {
        "key": "goal",
        "label": "Goal",
        "description": "What the customer wants to achieve or buy.",
        "required": False,
        "allow_inference": True,
        "fallback_behavior": "not_mentioned",
        "output_format": "short_text",
    },
    {
        "key": "next_steps",
        "label": "Next Steps",
        "description": "Follow-up actions for either side.",
        "required": False,
        "allow_inference": True,
        "fallback_behavior": "not_mentioned",
        "output_format": "action_text",
    },
    {
        "key": "summary",
        "label": "Running Summary",
        "description": "Short live summary of conversation progress.",
        "required": False,
        "allow_inference": True,
        "fallback_behavior": "best_effort",
        "output_format": "short_sentence",
    },
]


def _normalized_turns(turns: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for turn in turns[-24:]:
        speaker = str(turn.get("speaker") or "").strip().lower() or "unknown"
        text = " ".join(str(turn.get("text") or "").split()).strip()
        if not text:
            continue
        normalized.append({"speaker": speaker, "text": text})
    return normalized


def build_stage1_prompt(turns: list[dict[str, Any]]) -> str:
    transcript = _normalized_turns(turns)
    return textwrap.dedent(
        f"""
        Convert this live call transcript into a strict JSON fact sheet for real-time field mapping.

        Output valid JSON only. No markdown.
        Required JSON shape:
        {{
          "conversation_summary": "string",
          "detected_facts": [
            {{
              "category": "appointment|budget|callback|deadline|decision|goal|next_steps|stakeholder|intent|other",
              "value": "string",
              "evidence": "short snippet from transcript",
              "confidence": 0.0
            }}
          ],
          "dates_times": [{{"value":"string","evidence":"string","confidence":0.0}}],
          "money_values": [{{"value":"string","evidence":"string","confidence":0.0}}],
          "customer_intent": [{{"value":"string","evidence":"string","confidence":0.0}}],
          "stakeholders": [{{"value":"string","evidence":"string","confidence":0.0}}],
          "action_items": [{{"value":"string","owner":"customer|agent|unknown","evidence":"string","confidence":0.0}}],
          "commitments": [{{"value":"string","evidence":"string","confidence":0.0}}],
          "open_questions": [{{"value":"string","evidence":"string","confidence":0.0}}]
        }}

        Rules:
        - Use only transcript-grounded facts.
        - Include evidence snippet for each important fact.
        - If uncertain, still include best guess with lower confidence (>=0.2).
        - Keep conversation_summary concise and useful for UI.

        Transcript JSON:
        {json.dumps(transcript, ensure_ascii=False, indent=2)}
        """
    ).strip()


def build_stage2_prompt(stage1_summary: dict[str, Any], field_config: list[dict[str, Any]]) -> str:
    config = field_config or DEFAULT_LIVE_PREVIEW_FIELD_CONFIG
    return textwrap.dedent(
        f"""
        Map this structured call summary into configurable UI fields.

        Output valid JSON only. No markdown.
        Required output shape:
        {{
          "fields": {{
            "<field_key>": {{
              "value": "string",
              "status": "confirmed|inferred|not_mentioned",
              "evidence": ["string"],
              "confidence": 0.0
            }}
          }},
          "running_summary": "string"
        }}

        Mapping configuration:
        {json.dumps(config, ensure_ascii=False, indent=2)}

        Stage-1 structured summary:
        {json.dumps(stage1_summary, ensure_ascii=False, indent=2)}

        Rules:
        - Populate every configured field key.
        - Never omit keys.
        - If missing, set value="Not mentioned yet" and status="not_mentioned".
        - Prefer confirmed when directly supported by evidence.
        - Use inferred when context strongly implies value.
        - Keep values concise and UI-friendly.
        """
    ).strip()


def parse_json_text(raw_text: str) -> dict[str, Any]:
    payload = (raw_text or "").strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        payload = payload.replace("json", "", 1).strip()
    try:
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    except Exception:
        start = payload.find("{")
        end = payload.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(payload[start : end + 1])
                return parsed if isinstance(parsed, dict) else {"raw": parsed}
            except Exception:
                return {"raw_text": raw_text}
        return {"raw_text": raw_text}


def _pick_fact(summary: dict[str, Any], categories: tuple[str, ...]) -> tuple[str | None, list[str], float]:
    evidence: list[str] = []
    best_value: str | None = None
    best_conf = 0.0
    for fact in summary.get("detected_facts") or []:
        if not isinstance(fact, dict):
            continue
        category = str(fact.get("category") or "").strip().lower()
        if category not in categories:
            continue
        value = str(fact.get("value") or "").strip()
        if not value:
            continue
        conf = float(fact.get("confidence") or 0.0)
        snippet = str(fact.get("evidence") or "").strip()
        if snippet:
            evidence.append(snippet)
        if conf >= best_conf or best_value is None:
            best_conf = conf
            best_value = value
    return best_value, evidence[:2], best_conf


def fallback_stage2_mapping(
    stage1_summary: dict[str, Any],
    field_config: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    config = field_config or DEFAULT_LIVE_PREVIEW_FIELD_CONFIG
    fields: dict[str, Any] = {}

    def make_field(value: str | None, evidence: list[str], confidence: float, allow_infer: bool) -> dict[str, Any]:
        if value:
            status = "confirmed" if confidence >= 0.72 else "inferred"
            if not allow_infer and status == "inferred":
                return {
                    "value": "Not mentioned yet",
                    "status": "not_mentioned",
                    "evidence": [],
                    "confidence": 0.0,
                }
            return {
                "value": value,
                "status": status,
                "evidence": evidence,
                "confidence": round(max(0.01, min(0.99, confidence)), 2),
            }
        return {
            "value": "Not mentioned yet",
            "status": "not_mentioned",
            "evidence": [],
            "confidence": 0.0,
        }

    facts = stage1_summary.get("detected_facts") or []
    summary_text = str(stage1_summary.get("conversation_summary") or "").strip()
    money_values = [str(item.get("value") or "").strip() for item in (stage1_summary.get("money_values") or []) if isinstance(item, dict)]
    dates_values = [str(item.get("value") or "").strip() for item in (stage1_summary.get("dates_times") or []) if isinstance(item, dict)]
    action_values = [str(item.get("value") or "").strip() for item in (stage1_summary.get("action_items") or []) if isinstance(item, dict)]
    commit_values = [str(item.get("value") or "").strip() for item in (stage1_summary.get("commitments") or []) if isinstance(item, dict)]

    by_key: dict[str, tuple[str | None, list[str], float]] = {
        "appointment": _pick_fact(stage1_summary, ("appointment",)),
        "budget": _pick_fact(stage1_summary, ("budget",)),
        "callback": _pick_fact(stage1_summary, ("callback",)),
        "deadline": _pick_fact(stage1_summary, ("deadline",)),
        "decision": _pick_fact(stage1_summary, ("decision",)),
        "goal": _pick_fact(stage1_summary, ("goal", "intent")),
        "next_steps": _pick_fact(stage1_summary, ("next_steps",)),
    }

    for entry in config:
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        allow_inference = bool(entry.get("allow_inference", True))
        value, evidence, confidence = by_key.get(key, (None, [], 0.0))
        if key == "budget" and not value and money_values:
            value, confidence = money_values[0], 0.63
        if key in {"appointment", "callback", "deadline"} and not value and dates_values:
            value, confidence = dates_values[0], 0.52
        if key == "next_steps" and not value and action_values:
            value, confidence = action_values[0], 0.65
        if key == "decision" and not value and commit_values:
            value, confidence = commit_values[0], 0.61
        if key == "goal" and not value:
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                raw = str(fact.get("value") or "").strip()
                if re.search(r"\b(improve|reduce|increase|buy|purchase|goal|want)\b", raw.lower()):
                    value, confidence = raw, 0.58
                    break
        if key == "summary":
            summary_bits = [
                value
                for value in [
                    by_key.get("goal", (None, [], 0.0))[0],
                    by_key.get("next_steps", (None, [], 0.0))[0],
                ]
                if value
            ]
            summary_value = summary_text or (" | ".join(summary_bits) if summary_bits else "Not mentioned yet")
            fields[key] = make_field(summary_value, evidence, max(confidence, 0.55 if summary_text else 0.0), True)
            continue
        fields[key] = make_field(value, evidence, confidence, allow_inference)

    running_summary = str(fields.get("summary", {}).get("value") or "Not mentioned yet")
    return {"fields": fields, "running_summary": running_summary}
