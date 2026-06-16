#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
INSTANCE_NAME = "voice-agent-prod"
CLIENT_ID = "user_janjal_voicebot_12c92bbc"
PROJECT_ID = "janjal_ward22_inbound_918065254654"
PROJECT_NAME = "Janjal Ward 22 Inbound"

SESSIONS = [
    {
        "session_id": "bfd62ef36e864221bfe850bd4fe9d8c4",
        "to_number": "919999999999",
        "provider_call_sid": "test-recording-123",
    },
    {
        "session_id": "b8b6051969734a5886631c75221c3f77",
        "to_number": "919370677316",
        "provider_call_sid": "473a9ad5-42b5-4c18-9f2c-fba3ea2237e7",
    },
]


def get_instance_access_details() -> dict:
    cmd = [
        str(ROOT_DIR / "scripts" / "aws_local.sh"),
        "lightsail",
        "get-instance-access-details",
        "--instance-name",
        INSTANCE_NAME,
        "--protocol",
        "ssh",
        "--output",
        "json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def write_access_files(access: dict) -> tuple[Path, Path, str]:
    ip_address = access["ipAddress"]
    private_key = access["privateKey"]
    cert_key = access["certKey"]

    key_dir = Path("/tmp") / "codex-lightsail-key"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_file = key_dir / "id_ecdsa"
    cert_file = key_dir / "id_ecdsa-cert.pub"
    key_file.write_text(private_key, encoding="utf-8")
    cert_file.write_text(cert_key, encoding="utf-8")
    key_file.chmod(0o600)
    cert_file.chmod(0o600)
    return key_file, cert_file, ip_address


def main() -> None:
    access = get_instance_access_details()["accessDetails"]
    key_file, cert_file, ip_address = write_access_files(access)

    ssh_cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ConnectTimeout=20",
        "-o",
        f"CertificateFile={cert_file}",
        "-i",
        str(key_file),
        f"ubuntu@{ip_address}",
        "python3",
        "-",
    ]

    remote_script = f"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

root = Path("/opt/new_voice_agent")
sess_root = root / "sessions"
lead_store = json.loads((root / "logs" / "lead_pipeline.json").read_text())
lead_index = {{
    (str(item.get("client_id") or ""), str(item.get("source") or ""), str(item.get("to_number") or "")): item
    for item in lead_store.get("leads") or []
}}

for spec in {SESSIONS!r}:
    session_id = spec["session_id"]
    to_number = spec["to_number"]
    session_dir = sess_root / {CLIENT_ID!r} / session_id
    source_dir = sess_root / "acme_health" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    if source_dir.exists():
        for item in source_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, session_dir / item.name)

    recording_meta = session_dir / "piopiy_recording.json"
    if not recording_meta.exists():
        raise SystemExit(f"recording metadata missing for {{session_id}}")

    lead = lead_index.get(({CLIENT_ID!r}, "piopiy_inbound", to_number))
    if lead is None:
        raise SystemExit(f"matching lead record not found for {{to_number}}")

    metadata = lead.get("metadata") or {{}}
    recording = json.loads(recording_meta.read_text())
    started_at = lead.get("first_seen_at") or datetime.now(timezone.utc).isoformat()
    ended_at = recording.get("downloaded_at") or datetime.now(timezone.utc).isoformat()
    caller_number = str(lead.get("to_number") or "").strip()
    called_number = str(metadata.get("called_number") or "").strip() or None
    provider_call_sid = str(metadata.get("provider_call_sid") or spec["provider_call_sid"]).strip()

    artifact = {{
        "client_id": {CLIENT_ID!r},
        "project_id": {PROJECT_ID!r},
        "project_name": {PROJECT_NAME!r},
        "session_id": session_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "transcript": [],
        "parallel_stt_transcript": None,
        "parallel_stt_segments": [],
        "recording_stt_corpus": None,
        "recording_stt_full_corpus": None,
        "recording_llm_details": None,
        "piopiy_recording_url": recording.get("recording_url"),
        "piopiy_recording_path": str(session_dir / (recording.get("recording_filename") or "recording.mp3")),
        "piopiy_recording_filename": recording.get("recording_filename"),
        "piopiy_recording_content_type": recording.get("recording_content_type"),
        "piopiy_recording_downloaded_at": recording.get("downloaded_at"),
        "piopiy_recording_size_bytes": recording.get("recording_size_bytes"),
        "telephony_context": {{
            "provider": "piopiy",
            "call_direction": "inbound",
            "direction": "inbound",
            "call_status": "completed",
            "stream_status": "completed",
            "lead_source": "piopiy_inbound_stream",
            "from_number": caller_number,
            "to_number": to_number,
            "called_via_number": called_number,
            "provider_call_sid": provider_call_sid,
            "client_id": {CLIENT_ID!r},
            "project_id": {PROJECT_ID!r},
            "project_name": {PROJECT_NAME!r},
        }},
        "memory": {{
            "lead_name": "Inbound Caller",
            "company": None,
            "role": None,
            "contact_details": {{}},
            "appointment_details": None,
            "pain_points": [],
            "use_case": None,
            "budget_timeline": None,
            "objections": [],
            "interest_level": "unknown",
            "next_step": "Review the recorded inbound Piopiy call.",
        }},
        "summary": {{
            "lead_name": "Inbound Caller",
            "company": None,
            "role": None,
            "contact_details": {{}},
            "use_case": None,
            "budget_timeline_hints": None,
            "objections": [],
            "interest_level": "unknown",
            "summary": "Inbound Piopiy call captured and recording stored for Janjal's workspace.",
            "suggested_next_action": "Review the recorded inbound Piopiy call.",
            "qualification": {{
                "status": "unknown",
                "checklist": {{}},
            }},
        }},
        "errors": [],
        "metrics": {{}},
        "actual_cost": {{
            "status": "finalized",
            "currency": "INR",
            "telephony": [],
            "gemini": [],
            "total_estimated_cost": 0.0,
            "notes": [],
            "provider_call_sid": provider_call_sid,
            "telephony_provider": "piopiy",
            "raw_provider_usage": {{
                "provider": "piopiy",
                "lead_source": "piopiy_inbound_stream",
                "call_status": "completed",
                "stream_status": "completed",
                "from_number": caller_number,
                "to_number": to_number,
                "provider_call_sid": provider_call_sid,
                "stream_duration_seconds": 0,
            }},
            "raw_model_usage": {{}},
        }},
    }}

    (session_dir / "artifacts.json").write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    (session_dir / "lead_info.json").write_text(json.dumps(artifact["memory"], indent=2, ensure_ascii=False), encoding="utf-8")
    (session_dir / "summary.json").write_text(json.dumps(artifact["summary"], indent=2, ensure_ascii=False), encoding="utf-8")
    (session_dir / "actual_cost.json").write_text(json.dumps(artifact["actual_cost"], indent=2, ensure_ascii=False), encoding="utf-8")
    (session_dir / "transcript.md").write_text("# Transcript\\n\\n- No transcript captured yet for this backfilled Piopiy call.\\n", encoding="utf-8")
    (session_dir / "summary.md").write_text("# Post-Call Summary\\n\\n- Backfilled Piopiy session for Janjal workspace.\\n", encoding="utf-8")
    print(session_dir / "artifacts.json")
"""


if __name__ == "__main__":
    main()
