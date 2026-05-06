# Lightsail Local Storage -> RDS + S3 Migration Plan

## Goal
- Keep compute on Lightsail.
- Move persistent structured data to RDS (MySQL).
- Move session artifacts/recordings/exports to S3.
- Keep rollback simple and low-risk.

## Current state (from codebase)
- Local session artifacts are written under `SESSION_OUTPUT_DIR` via `SessionLogger`:
  - `voice_sales_agent/transcripts.py`
  - `voice_sales_agent/session.py`
- Call outcomes are stored in local SQLite:
  - `voice_sales_agent/call_outcomes_store.py` (`SqliteCallOutcomeStore`)
- Auth/user DB is SQLite by default:
  - `voice_sales_agent/auth_backend.py`
- Client/project data already supports MySQL backend:
  - `voice_sales_agent/clients.py` (file + mysql repositories)
  - configured by `CLIENT_STORE_BACKEND` and `MYSQL_*` env vars.

## Target architecture
- Lightsail instance: app runtime only.
- RDS MySQL:
  - clients/projects/config data
  - call outcomes
  - auth/users (phase 2)
- S3 bucket:
  - session JSON/MD artifacts
  - recordings (`*_caller.wav`, `*_agent.wav`, `*_conversation.wav`)
  - optional analytics exports

## Phase 0: Pre-check and backup
1. Snapshot Lightsail disk.
2. Backup local data:
   - `clients/`
   - `sessions/`
   - auth DB file
   - call outcomes DB (`call_outcomes.db`)
3. Rotate exposed secrets and move secrets to AWS Secrets Manager or SSM.

## Phase 1: Move client/project store to RDS (lowest risk, already supported)
1. Create RDS MySQL (private subnet preferred), enable automated backups (7-30 days).
2. Security groups:
   - Allow inbound MySQL from Lightsail static IP only.
3. Create DB/schema/user with least privilege.
4. Set env on app:
   - `CLIENT_STORE_BACKEND=mysql`
   - `MYSQL_HOST=...`
   - `MYSQL_PORT=3306`
   - `MYSQL_USER=...`
   - `MYSQL_PASSWORD=...`
   - `MYSQL_DATABASE=voice_agent`
   - `MYSQL_CLIENTS_TABLE=clients`
5. Data migration:
   - Read each folder in `clients/` and save to MySQL repository via existing app save flow (or one-time script).
6. Restart app and verify:
   - login -> dashboard -> project editor loads/saves
   - active project selection persists
   - outbound call uses selected project runtime.

## Phase 2: Move artifacts from local FS to S3
1. Create S3 bucket (e.g., `aivoicebot4u-prod-artifacts`), enable:
   - Block public access
   - Versioning
   - Default SSE-S3 (or SSE-KMS)
   - Lifecycle rules (e.g., transition/expire old audio after retention)
2. Add IAM policy to app role/user:
   - `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` scoped to that bucket/prefix.
3. App changes required:
   - Add storage abstraction (local vs s3).
   - Upload session artifacts after generation from `SessionLogger` outputs.
   - Persist S3 object keys/URLs (private keys, not public URLs) in DB/log metadata.
4. Migration of historical files:
   - `aws s3 sync ./sessions s3://<bucket>/sessions/`
5. Verification:
   - run new session
   - confirm objects appear under expected prefix
   - confirm download/view path works for admin/reporting.

## Phase 3: Move call outcomes from SQLite to RDS
1. Create `call_outcomes` table in MySQL mirroring SQLite schema.
2. Add MySQL-backed store implementation (`MySqlCallOutcomeStore`) and toggle via env.
3. One-time backfill:
   - export from SQLite
   - import into MySQL with idempotent upsert on `session_id`.
4. Switch read/write to MySQL store.
5. Verify dashboards and analytics endpoints.

## Phase 4: Move auth DB from SQLite to RDS (optional but recommended)
1. Add MySQL auth backend implementing methods used by `auth_backend.py`.
2. Backfill users + guest demo leads.
3. Switch auth backend with feature flag/env.
4. Verify signup/login/password reset flows.

## Recommended env flags after migration
- `CLIENT_STORE_BACKEND=mysql`
- `MYSQL_HOST=...`
- `MYSQL_PORT=3306`
- `MYSQL_USER=...`
- `MYSQL_PASSWORD=...`
- `MYSQL_DATABASE=voice_agent`
- `MYSQL_CLIENTS_TABLE=clients`
- New (to add in code):
  - `ARTIFACT_STORE_BACKEND=s3`
  - `S3_BUCKET=...`
  - `S3_PREFIX=sessions/`
  - `AWS_REGION=us-east-1`
  - `CALL_OUTCOMES_BACKEND=mysql`
  - `AUTH_STORE_BACKEND=mysql`

## Rollout strategy (safe)
1. Deploy code with dual-readiness (feature flags, default old behavior).
2. Enable MySQL only for clients/projects first.
3. Enable S3 artifact writes (optionally keep local copy for 1 week).
4. Enable call outcomes MySQL.
5. Enable auth MySQL last.

## Rollback plan
- Keep local DB/files untouched for first rollout window.
- If issues:
  - Set `CLIENT_STORE_BACKEND=file`
  - Set artifact backend back to local
  - Set call outcomes/auth backend back to SQLite
  - restart app.

## Cost notes
- RDS adds fixed monthly baseline (instance + storage + backup).
- S3 is cheap for storage but request/egress costs apply.
- Net result: higher than local-only Lightsail, but much safer/recoverable.

## What changes in user experience?
- No major UI change expected.
- Better reliability and recoverability.
- Slight latency increase possible when loading large historical artifacts.

## One-day execution checklist
1. Create RDS + SG + DB user.
2. Create S3 bucket + IAM policy.
3. Configure env for MySQL client store and restart.
4. Validate project editor/call flow.
5. Deploy artifact-store code (S3 writes), validate new sessions.
6. Backfill historical sessions to S3.
7. Deploy MySQL call-outcomes store + backfill + validate reports.
8. Optional: migrate auth to MySQL.

