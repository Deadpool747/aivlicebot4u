# Piopiy Old-SDK Gemini Sandbox

This sandbox is for testing the older TeleCMI/Piopiy `development` branch
Gemini Live sample without touching the main runtime.

## What it tests

- older `VoiceAgent.configure(...)` API
- Gemini Live speech-to-speech shape
- separate trace output

## Runner

Use:

```bash
python scripts/run_piopiy_old_sdk_gemini_agent.py
```

Important env vars:

```env
PIOPIY_OLD_SDK_SRC=/opt/telecmi_agents_oldtest_remote/src
PIOPIY_OLD_SDK_TRACE_FILE=/opt/new_voice_agent/runtime/piopiy_old_sdk_trace.jsonl
PIOPIY_OLD_SDK_MODEL=models/gemini-2.0-flash-exp
```

The runner imports the older source tree directly from `PIOPIY_OLD_SDK_SRC`
before importing `piopiy`, so it does not rely on the currently installed
Piopiy package API shape.

## Website integration

The main website can now store the intended Piopiy runtime per project.

For a project that should use this isolated worker:

- set `Outbound Call Provider` to `piopiy`
- set `Piopiy Agent ID` to the conversational Piopiy agent connected by this worker
- set `Piopiy App ID` to the inbound Piopiy app mapped to that agent
- set `Piopiy Stream Runtime` to `old_sdk_gemini_live_s2s`

Important:

- the website records that runtime choice and uses it for project routing metadata
- the actual audio session still runs in this separate old-SDK worker service, not in the normal FastAPI web bridge
