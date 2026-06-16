"""Session transcript and artifact persistence."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import SessionArtifacts, TranscriptTurn


class SessionLogger:
    """Persist transcript, summary, and markdown notes for a call session."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def create_session_dir(self, client_id: str, session_id: str) -> Path:
        session_dir = self.output_dir / client_id / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    def save(self, artifacts: SessionArtifacts, session_dir: Path) -> None:
        """Write machine-readable and human-readable outputs."""
        json_path = session_dir / "artifacts.json"
        lead_json_path = session_dir / "lead_info.json"
        summary_json_path = session_dir / "summary.json"
        actual_cost_json_path = session_dir / "actual_cost.json"
        transcript_md_path = session_dir / "transcript.md"
        summary_md_path = session_dir / "summary.md"

        json_path.write_text(
            json.dumps(artifacts.model_dump(mode="json"), indent=2, default=str),
            encoding="utf-8",
        )
        lead_json_path.write_text(
            json.dumps(artifacts.memory.model_dump(mode="json"), indent=2, default=str),
            encoding="utf-8",
        )
        if artifacts.summary:
            summary_json_path.write_text(
                json.dumps(artifacts.summary.model_dump(mode="json"), indent=2, default=str),
                encoding="utf-8",
            )
        if artifacts.actual_cost:
            actual_cost_json_path.write_text(
                json.dumps(artifacts.actual_cost.model_dump(mode="json"), indent=2, default=str),
                encoding="utf-8",
            )

        transcript_lines = [
            f"# Transcript\n",
            f"- Client: {artifacts.client_id}",
            f"- Session: {artifacts.session_id}",
            f"- Started: {artifacts.started_at.isoformat()}",
            f"- Ended: {artifacts.ended_at.isoformat() if artifacts.ended_at else 'in_progress'}",
            "",
        ]
        for turn in artifacts.transcript:
            timestamp = turn.timestamp.strftime("%H:%M:%S")
            latency = f" ({turn.latency_ms:.0f} ms)" if turn.latency_ms is not None else ""
            transcript_lines.append(f"**[{timestamp}] {turn.speaker.title()}**{latency}: {turn.text}")

        transcript_md_path.write_text("\n".join(transcript_lines), encoding="utf-8")

        summary_lines = [
            "# Post-Call Summary",
            "",
            f"- Generated: {datetime.utcnow().isoformat()}Z",
            f"- Client: {artifacts.client_id}",
            f"- Session: {artifacts.session_id}",
            "",
            "## Memory Snapshot",
            artifacts.memory.compact_context(),
            "",
        ]
        if artifacts.summary:
            summary_lines.extend(
                [
                    "## Summary",
                    artifacts.summary.summary,
                    "",
                    f"## Suggested Next Action\n{artifacts.summary.suggested_next_action}",
                    "",
                    "## Qualification",
                    json.dumps(artifacts.summary.qualification.model_dump(mode="json"), indent=2),
                ]
            )
        if artifacts.errors:
            summary_lines.extend(["", "## Errors", *[f"- {error}" for error in artifacts.errors]])
        if artifacts.actual_cost:
            summary_lines.extend(
                [
                    "",
                    "## Internal Actual Cost Tracking",
                    f"- Status: {artifacts.actual_cost.status}",
                    f"- Currency: {artifacts.actual_cost.currency}",
                    f"- Estimated total: {artifacts.actual_cost.total_estimated_cost}",
                ]
            )
        if artifacts.metrics:
            summary_lines.extend(
                [
                    "",
                    "## Session Metrics",
                    *[f"- {key}: {value}" for key, value in sorted(artifacts.metrics.items())],
                ]
            )
        recording_lines = [
            f"- URL: {artifacts.piopiy_recording_url}" if artifacts.piopiy_recording_url else "",
            f"- Path: {artifacts.piopiy_recording_path}" if artifacts.piopiy_recording_path else "",
            f"- File: {artifacts.piopiy_recording_filename}" if artifacts.piopiy_recording_filename else "",
            (
                f"- Content Type: {artifacts.piopiy_recording_content_type}"
                if artifacts.piopiy_recording_content_type
                else ""
            ),
            (
                f"- Downloaded At: {artifacts.piopiy_recording_downloaded_at.isoformat()}"
                if artifacts.piopiy_recording_downloaded_at
                else ""
            ),
            (
                f"- Size Bytes: {artifacts.piopiy_recording_size_bytes}"
                if artifacts.piopiy_recording_size_bytes is not None
                else ""
            ),
        ]
        recording_lines = [line for line in recording_lines if line]
        if recording_lines:
            summary_lines.extend(["", "## Piopiy Recording", *recording_lines])
        if artifacts.actual_cost and artifacts.actual_cost.raw_provider_usage:
            summary_lines.extend(
                [
                    "",
                    "## Telephony Metadata",
                    *[
                        f"- {key}: {value}"
                        for key, value in sorted(artifacts.actual_cost.raw_provider_usage.items())
                        if value not in {None, ""}
                    ],
                ]
            )

        summary_md_path.write_text("\n".join(summary_lines), encoding="utf-8")

    def append_turn(self, artifacts: SessionArtifacts, turn: TranscriptTurn) -> None:
        """Append a turn to in-memory artifacts."""
        artifacts.transcript.append(turn)
