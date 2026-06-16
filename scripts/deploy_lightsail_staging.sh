#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTANCE_NAME="${1:-voice-agent-prod}"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.staging}" \
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/new_voice_agent_staging}" \
REMOTE_PIOPIY_DIR="${REMOTE_PIOPIY_DIR:-/opt/new_voice_agent_staging-piopiy}" \
VOICE_SERVICE_NAME="${VOICE_SERVICE_NAME:-voice-sales-agent-staging.service}" \
PIOPIY_SERVICE_NAME="${PIOPIY_SERVICE_NAME:-piopiy-agent-staging.service}" \
DASHBOARD_PORT="${DASHBOARD_PORT:-8001}" \
"$ROOT_DIR/scripts/deploy_lightsail.sh" "$INSTANCE_NAME"
