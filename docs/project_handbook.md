# AI Voice Bot 4 U - Project Handbook (Living Document)

Last updated: 2026-04-12

## 1. Purpose

This document is the single memory source for the project:

- what the system is
- how it is designed
- how production is deployed
- strengths and weaknesses
- what should be improved next

Use this as the default onboarding and decision reference.

## 2. Product Summary

AI Voice Bot 4 U is a voice-agent platform for:

- live browser-mic demos
- outbound telephony calls
- multi-client configurable conversation behavior
- session analytics and lead capture

Core stack:

- Backend: FastAPI (`voice_sales_agent/web_app.py`)
- Realtime voice: Gemini Live API (`voice_sales_agent/gemini_api.py`)
- Session orchestration: `voice_sales_agent/session.py`
- Frontend: static HTML/JS pages in `voice_sales_agent/web_static/`
- Storage:
  - Auth + guest leads: SQLite (`auth_backend.py`)
  - Client configs: file-based by default, optional MySQL/Mongo
  - Call state: in-memory by default, optional Redis for scale

## 3. Architecture

### 3.1 High-level components

1. Landing/demo UI (`landing.html`)
2. Authenticated app dashboard (`index.html`)
3. FastAPI backend (`create_app()` in `web_app.py`)
4. Session engine (`VoiceSalesSession`)
5. Telephony providers (Twilio / Exotel / Airtel IQ / Meta WhatsApp)
6. Client/project config repository (`clients/<client_id>/...`)
7. Analytics and transcript artifact system (`sessions/`, `analytics.py`)

### 3.2 Runtime flows

#### A) Landing demo flow (browser mic)

1. Browser loads `/`
2. Frontend requests `/api/demo/options` (guest workspace/session allocation)
3. User fills details (Name, Phone, Email)
4. Browser opens websocket `/ws/browser-audio/{workspace_session_key}`
5. Browser calls `/api/session/start` with `transport=browser`
6. Backend starts `VoiceSalesSession` bound to that workspace session key
7. Audio chunks stream both ways over websocket
8. `/api/session/stop` ends the run

#### B) Authenticated dashboard flow

1. User logs in (`/api/auth/login`) and gets signed cookie
2. Workspace client is scoped per user
3. Start/Stop and Call operations are restricted to that workspace
4. Dashboard polls `/api/session`, `/api/analytics/overview`, `/api/workspace/summary`

#### C) Telephony call flow

1. UI triggers `/api/telephony/call`
2. `TelephonyController` creates pending call and provider callback metadata
3. Provider webhooks/media websockets call backend routes
4. Session is started and mapped per call session key
5. Call context, artifacts, and analytics are updated

## 4. Data & Configuration Model

### 4.1 Client configuration

Each client is folder-based:

- `config.json`
- `projects.json`
- `system_prompt.txt`
- `knowledge.md`
- `objections.json`
- `qualification.json`
- `cta.json`

### 4.2 Session artifacts

Saved under `sessions/<client_id>/<session_id>/`:

- transcript
- extraction summary
- latency/cost metrics
- session metadata

### 4.3 Environment configuration

Defined in `.env.example` and loaded by `voice_sales_agent/config.py`.

Key groups:

- Gemini models/API
- telephony credentials
- Redis/MySQL settings
- cost tracking settings
- public URL / callback URLs
- Formspree lead notification

## 5. Security & Access Behavior

### 5.1 Authentication

- Signed HTTP-only cookie session (`SESSION_COOKIE_NAME`)
- users stored in SQLite
- admin and non-admin role behavior in middleware

### 5.2 Guest demo constraints (current)

- Guest demo workspace is isolated per visitor/user
- `Start` requires valid Name + Phone + Email for guest demo workspaces
- Backend enforces this; frontend alone cannot bypass it
- Lead is recorded before session starts for guest demos

### 5.3 Known security gaps

- Twilio signature validation is still noted as not implemented in README
- Cookies are secure only when HTTPS/public base conditions are correct
- Secrets are environment-based; no centralized secret manager yet

## 6. Deployment System (Current Production)

Current production target:

- Domain: `https://aivoicebot4u.com`
- Host: AWS Lightsail instance `voice-agent-prod`
- Service: `voice-sales-agent.service`
- Reverse proxy: Nginx

One-command deployment:

- `./scripts/deploy_lightsail.sh`

Deployment behavior:

1. local syntax check
2. fetch temporary Lightsail SSH access
3. rsync to server temp path
4. rsync apply to `/opt/new_voice_agent`
5. pip install requirements
6. restart and health-check systemd service

Deploy excludes are in:

- `deploy/lightsail/rsync-excludes.txt`

## 7. UX/Design System Snapshot

### 7.1 Landing page

- marketing + demo intake on same page
- guided Start/Stop for demo
- field focus order on Start validation:
  - Name -> Phone -> Email
- visual branding and feature explanation

### 7.2 App dashboard

- control center for sessions, calling, analytics, project edits
- workspace indicators and billing summary
- browser-mic support on hosted environments via websocket audio bridge

## 8. Strengths (Pros)

1. End-to-end working product: demo + telephony + analytics + deployment
2. Flexible multi-provider telephony architecture
3. Strong modularity:
   - session engine
   - telephony layer
   - client repository abstraction
4. Fast iteration with file-driven client/project configuration
5. Practical deployment automation script already in place
6. Guest demo lead capture integrated into runtime

## 9. Weaknesses (Cons)

1. `web_app.py` is very large (many responsibilities in one file)
2. Static HTML/JS files are large and hard to maintain
3. Some behavior complexity (guest/auth/demo/call modes) is difficult to reason about
4. Security hardening is incomplete for some webhook providers
5. No formal test suite coverage visible for critical flows
6. Ops maturity is medium:
   - no CI/CD pipeline
   - manual environment management
   - minimal automated rollback

## 10. Risks To Track

1. Regression risk in start/call/auth flows due to tight coupling
2. Provider behavior changes (API schema/rate/headers) can break call flows
3. Cost drift risk if production instance remains running without controls
4. Growth risk if in-memory state is used under multi-worker load
5. Compliance/privacy risk if lead/session data retention policy is undefined

## 11. Recommended Improvements (Priority Roadmap)

### P0 - Reliability and security

1. Add webhook signature validation and replay protection for all providers
2. Add smoke tests for:
   - guest demo start gate
   - browser audio websocket
   - outbound call initiation
3. Add deployment rollback command/script

### P1 - Maintainability

1. Split `web_app.py` into modules:
   - auth routes
   - session routes
   - telephony routes
   - admin routes
2. Extract shared frontend utilities and reduce monolithic JS
3. Add typed API contracts for frontend/backend payloads

### P2 - Product and ops

1. Add CI checks (lint + compile + smoke tests)
2. Add structured observability dashboard (errors, latency, provider failure rates)
3. Add persistent job/event audit log for lead lifecycle
4. Add backup/restore plan for auth + lead databases

## 12. Operating Playbook

### 12.1 Pre-deploy

1. confirm `.env` and provider creds
2. run python compile checks
3. run dry-run rsync if needed

### 12.2 Deploy

1. `./scripts/deploy_lightsail.sh`
2. verify service active
3. verify domain responses
4. run demo Start and one call smoke test

### 12.3 Pause cost

1. stop instance to pause runtime activity
2. delete instance/static IP for near-zero recurring cost

## 13. Important Project Paths

- App entry: `voice_sales_agent/web_app.py`
- Session orchestration: `voice_sales_agent/session.py`
- Telephony providers: `voice_sales_agent/telephony.py`
- Gemini integration: `voice_sales_agent/gemini_api.py`
- Config loader: `voice_sales_agent/config.py`
- Auth storage: `voice_sales_agent/auth_backend.py`
- Landing UI: `voice_sales_agent/web_static/landing.html`
- App UI: `voice_sales_agent/web_static/index.html`
- Deploy script: `scripts/deploy_lightsail.sh`
- Deploy docs: `docs/lightsail_deployment.md`

## 14. Decision Log (Current State)

1. Hosted Start uses browser-mic websocket transport (not server-local PyAudio)
2. Guest demo Start requires contact details before session start
3. Deployment excludes runtime and guest-generated local-only files
4. Production deploys through Lightsail + systemd + nginx

## 15. How to keep this document alive

After every significant change, update:

1. What changed
2. Why it changed
3. Risk introduced
4. Rollback plan
5. Next follow-up action

If this rule is followed, the team will not lose context between releases.
