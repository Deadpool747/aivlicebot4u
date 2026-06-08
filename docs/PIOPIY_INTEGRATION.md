# Piopiy Integration

Piopiy is now wired into the telephony provider stack as an optional outbound provider.

## What changed

- Added `piopiy` as a valid telephony provider in app config and project runtime config.
- Added Piopiy-specific project overrides:
  - `piopiy_agent_id`
  - `piopiy_caller_id`
  - `piopiy_app_id`
- Added `PiopiyCallClient` to place calls through Piopiy's AI call API.
- Added Piopiy fields to `.env.example`.
- Added `piopiy` to the frontend project editor.
- Added a separate Piopiy agent worker launcher at `scripts/run_piopiy_agent.py`.

## Environment variables

Set these in `.env` or in your hosting environment:

- `PIOPIY_API_TOKEN`
- `PIOPIY_AGENT_ID`
- `PIOPIY_CALLER_ID`
- `PIOPIY_APP_ID` optional

For the agent worker process:

- `AGENT_ID` defaults to `ai_agent`
- `AGENT_TOKEN` is the Piopiy agent token from the dashboard
- `PIOPIY_CLIENT_ID` is the workspace client used to build the live prompt
- `PIOPIY_PROJECT_ID` is optional
- `PIOPIY_LLM_FACTORY`, `PIOPIY_STT_FACTORY`, `PIOPIY_TTS_FACTORY` can override the provider classes if your installed Piopiy AI version uses different class names

## How it works

When a project selects `piopiy` as its outbound provider, the backend uses Piopiy's AI call API with:

- the project override values if present
- otherwise the global environment values

## Notes

- Keep the existing providers enabled. Piopiy is additive, not a replacement.
- The Piopiy worker SDK is installed from PyPI as `piopiy-ai`.
- Run the Piopiy worker in a separate virtualenv from the legacy `piopiy` SDK if you still need the older REST client for outbound calling, because both distributions use the `piopiy` import namespace.
