#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTANCE_NAME="${1:-voice-agent-prod}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/new_voice_agent}"
REMOTE_PIOPIY_DIR="${REMOTE_PIOPIY_DIR:-/opt/new_voice_agent-piopiy}"
VOICE_SERVICE_NAME="${VOICE_SERVICE_NAME:-voice-sales-agent.service}"
PIOPIY_SERVICE_NAME="${PIOPIY_SERVICE_NAME:-piopiy-agent.service}"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"
EXCLUDES_FILE="$ROOT_DIR/deploy/lightsail/rsync-excludes.txt"

lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

if [[ ! -x "$ROOT_DIR/scripts/aws_local.sh" ]]; then
  echo "Missing $ROOT_DIR/scripts/aws_local.sh"
  exit 1
fi

if [[ ! -f "$EXCLUDES_FILE" ]]; then
  echo "Missing rsync excludes file: $EXCLUDES_FILE"
  exit 1
fi

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "Project virtualenv not found at $ROOT_DIR/.venv"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Environment file not found: $ENV_FILE"
  exit 1
fi

echo "[1/6] Running local Python syntax checks..."
"$ROOT_DIR/.venv/bin/python" -m py_compile \
  "$ROOT_DIR/voice_sales_agent/web_app.py" \
  "$ROOT_DIR/voice_sales_agent/gemini_api.py" \
  "$ROOT_DIR/voice_sales_agent/piopiy_agent.py" \
  "$ROOT_DIR/scripts/run_piopiy_agent.py"

echo "[2/6] Fetching Lightsail access details for instance: $INSTANCE_NAME"
ACCESS_JSON="$("$ROOT_DIR/scripts/aws_local.sh" lightsail get-instance-access-details --instance-name "$INSTANCE_NAME" --protocol ssh --output json)"
INSTANCE_IP="$(printf '%s' "$ACCESS_JSON" | jq -r '.accessDetails.ipAddress')"
if [[ -z "$INSTANCE_IP" || "$INSTANCE_IP" == "null" ]]; then
  echo "Could not resolve instance IP for $INSTANCE_NAME"
  exit 1
fi

KEY_DIR="$(mktemp -d /tmp/lightsail-key-XXXXXX)"
KEY_FILE="$KEY_DIR/id_ecdsa"
CERT_FILE="$KEY_FILE-cert.pub"
cleanup() {
  rm -rf "$KEY_DIR"
}
trap cleanup EXIT

printf '%s\n' "$(printf '%s' "$ACCESS_JSON" | jq -r '.accessDetails.privateKey')" > "$KEY_FILE"
printf '%s\n' "$(printf '%s' "$ACCESS_JSON" | jq -r '.accessDetails.certKey')" > "$CERT_FILE"
chmod 600 "$KEY_FILE" "$CERT_FILE"

SSH_CMD=(
  ssh
  -o StrictHostKeyChecking=accept-new
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=4
  -o ConnectTimeout=30
  -o CertificateFile="$CERT_FILE"
  -i "$KEY_FILE"
)
RSYNC_SSH="ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o ConnectTimeout=30 -o CertificateFile=$CERT_FILE -i $KEY_FILE"

echo "[3/6] Syncing app files to $INSTANCE_NAME ($INSTANCE_IP)..."
rsync -az --delete \
  --exclude-from="$EXCLUDES_FILE" \
  -e "$RSYNC_SSH" \
  "$ROOT_DIR/" "ubuntu@$INSTANCE_IP:/tmp/new_voice_agent_sync/"
scp -o StrictHostKeyChecking=accept-new -o CertificateFile="$CERT_FILE" -i "$KEY_FILE" "$EXCLUDES_FILE" "ubuntu@$INSTANCE_IP:/tmp/rsync-excludes.txt"
scp -o StrictHostKeyChecking=accept-new -o CertificateFile="$CERT_FILE" -i "$KEY_FILE" "$ENV_FILE" "ubuntu@$INSTANCE_IP:/tmp/deploy.env"

echo "[4/6] Applying files on server..."
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo rsync -az --delete --exclude-from=/tmp/rsync-excludes.txt /tmp/new_voice_agent_sync/ $REMOTE_APP_DIR/"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo install -m 0600 /tmp/deploy.env $REMOTE_APP_DIR/.env"

echo "[5/6] Installing requirements and restarting services..."
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo mkdir -p $REMOTE_APP_DIR && sudo chown -R ubuntu:ubuntu $REMOTE_APP_DIR"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ ! -x $REMOTE_APP_DIR/.venv/bin/python ]; then python3 -m venv $REMOTE_APP_DIR/.venv; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "$REMOTE_APP_DIR/.venv/bin/pip install -U pip >/tmp/app_pip_bootstrap.log 2>&1"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "cd $REMOTE_APP_DIR && $REMOTE_APP_DIR/.venv/bin/pip install -r requirements.txt >/tmp/pip_deploy.log 2>&1"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo mkdir -p $REMOTE_PIOPIY_DIR && sudo chown -R ubuntu:ubuntu $REMOTE_PIOPIY_DIR"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ ! -x $REMOTE_PIOPIY_DIR/.venv/bin/python ]; then python3 -m venv $REMOTE_PIOPIY_DIR/.venv; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "$REMOTE_PIOPIY_DIR/.venv/bin/pip install -U pip >/tmp/piopiy_pip_bootstrap.log 2>&1"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "$REMOTE_PIOPIY_DIR/.venv/bin/pip install -r $REMOTE_APP_DIR/requirements-piopiy-agent.txt >/tmp/piopiy_pip_deploy.log 2>&1"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo sh -c \"sed -e 's#/opt/new_voice_agent-piopiy#__REMOTE_PIOPIY_DIR__#g' -e 's#/opt/new_voice_agent#__REMOTE_APP_DIR__#g' -e 's#__REMOTE_PIOPIY_DIR__#$REMOTE_PIOPIY_DIR#g' -e 's#__REMOTE_APP_DIR__#$REMOTE_APP_DIR#g' -e 's#^Environment=DASHBOARD_HOST=.*#Environment=DASHBOARD_HOST=$DASHBOARD_HOST#' -e 's#^Environment=DASHBOARD_PORT=.*#Environment=DASHBOARD_PORT=$DASHBOARD_PORT#' $REMOTE_APP_DIR/deploy/lightsail/voice-sales-agent.service > /etc/systemd/system/$VOICE_SERVICE_NAME\""
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo sh -c \"sed -e 's#/opt/new_voice_agent-piopiy#__REMOTE_PIOPIY_DIR__#g' -e 's#/opt/new_voice_agent#__REMOTE_APP_DIR__#g' -e 's#__REMOTE_PIOPIY_DIR__#$REMOTE_PIOPIY_DIR#g' -e 's#__REMOTE_APP_DIR__#$REMOTE_APP_DIR#g' $REMOTE_APP_DIR/deploy/lightsail/piopiy-agent.service > /etc/systemd/system/$PIOPIY_SERVICE_NAME\""
LOCAL_AGENT_ID="$(awk -F= '$1=="AGENT_ID"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_AGENT_TOKEN="$(awk -F= '$1=="AGENT_TOKEN"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_API_TOKEN="$(awk -F= '$1=="PIOPIY_API_TOKEN"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_AGENT_ID="$(awk -F= '$1=="PIOPIY_AGENT_ID"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_CALLER_ID="$(awk -F= '$1=="PIOPIY_CALLER_ID"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_APP_ID="$(awk -F= '$1=="PIOPIY_APP_ID"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_TELEPHONY_PROVIDER="$(awk -F= '$1=="TELEPHONY_PROVIDER"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_CLIENT_ID="$(awk -F= '$1=="PIOPIY_CLIENT_ID"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_PROJECT_ID="$(awk -F= '$1=="PIOPIY_PROJECT_ID"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_DEFAULT_CLIENT_ID="$(awk -F= '$1=="PIOPIY_DEFAULT_CLIENT_ID"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_DEFAULT_PROJECT_ID="$(awk -F= '$1=="PIOPIY_DEFAULT_PROJECT_ID"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_PIPELINE_MODE="$(awk -F= '$1=="PIOPIY_PIPELINE_MODE"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_LLM_FACTORY="$(awk -F= '$1=="PIOPIY_LLM_FACTORY"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_LLM_API_KEY="$(awk -F= '$1=="PIOPIY_LLM_API_KEY"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_LLM_MODEL="$(awk -F= '$1=="PIOPIY_LLM_MODEL"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_LLM_BASE_URL="$(awk -F= '$1=="PIOPIY_LLM_BASE_URL"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_GEMINI_LIVE_MODEL="$(awk -F= '$1=="PIOPIY_GEMINI_LIVE_MODEL"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_GEMINI_TTS_FACTORY="$(awk -F= '$1=="PIOPIY_GEMINI_TTS_FACTORY"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_GEMINI_TTS_MODEL="$(awk -F= '$1=="PIOPIY_GEMINI_TTS_MODEL"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_GEMINI_TTS_VOICE_ID="$(awk -F= '$1=="PIOPIY_GEMINI_TTS_VOICE_ID"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_STT_FACTORY="$(awk -F= '$1=="PIOPIY_STT_FACTORY"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_TTS_FACTORY="$(awk -F= '$1=="PIOPIY_TTS_FACTORY"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_TTS_VOICE_NAME="$(awk -F= '$1=="PIOPIY_TTS_VOICE_NAME"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_SPEECH_PROVIDER="$(awk -F= '$1=="SPEECH_PROVIDER"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_LIVE_PROVIDER="$(awk -F= '$1=="LIVE_PROVIDER"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_STRUCTURED_PROVIDER="$(awk -F= '$1=="STRUCTURED_PROVIDER"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_TTS_PROVIDER="$(awk -F= '$1=="TTS_PROVIDER"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_RECORDING_STT_PROVIDER="$(awk -F= '$1=="RECORDING_STT_PROVIDER"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_AGENT_RESPONSE_MODE="$(awk -F= '$1=="AGENT_RESPONSE_MODE"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_GEMINI_API_KEY="$(awk -F= '$1=="GEMINI_API_KEY"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_GEMINI_API_KEY="$(awk -F= '$1=="PIOPIY_GEMINI_API_KEY"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_SARVAM_API_KEY="$(awk -F= '$1=="SARVAM_API_KEY"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_SARVAM_API_KEY="$(awk -F= '$1=="PIOPIY_SARVAM_API_KEY"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_TTS_PITCH="$(awk -F= '$1=="PIOPIY_TTS_PITCH"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_GOOGLE_CREDENTIALS_PATH="$(awk -F= '$1=="PIOPIY_GOOGLE_CREDENTIALS_PATH"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_GOOGLE_CREDENTIALS_JSON="$(awk -F= '$1=="PIOPIY_GOOGLE_CREDENTIALS_JSON"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_WS_PUBLIC_BASE_URL="$(awk -F= '$1=="PIOPIY_WS_PUBLIC_BASE_URL"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_WS_SCHEME="$(awk -F= '$1=="PIOPIY_WS_SCHEME"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_TRUSTED_HOSTS="$(awk -F= '$1=="TRUSTED_HOSTS"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
LOCAL_PIOPIY_USE_WEB_BRIDGE="$(awk -F= '$1=="PIOPIY_USE_WEB_BRIDGE"{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE")"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_AGENT_ID\" ]; then grep -q '^AGENT_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^AGENT_ID=.*#AGENT_ID=$LOCAL_AGENT_ID#' $REMOTE_APP_DIR/.env || printf '%s\n' 'AGENT_ID=$LOCAL_AGENT_ID' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_AGENT_TOKEN\" ]; then grep -q '^AGENT_TOKEN=' $REMOTE_APP_DIR/.env && sed -i 's#^AGENT_TOKEN=.*#AGENT_TOKEN=$LOCAL_AGENT_TOKEN#' $REMOTE_APP_DIR/.env || printf '%s\n' 'AGENT_TOKEN=$LOCAL_AGENT_TOKEN' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_API_TOKEN\" ]; then grep -q '^PIOPIY_API_TOKEN=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_API_TOKEN=.*#PIOPIY_API_TOKEN=$LOCAL_PIOPIY_API_TOKEN#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_API_TOKEN=$LOCAL_PIOPIY_API_TOKEN' >> $REMOTE_APP_DIR/.env; fi"
if [ -n "$LOCAL_PIOPIY_AGENT_ID" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_AGENT_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_AGENT_ID=.*#PIOPIY_AGENT_ID=$LOCAL_PIOPIY_AGENT_ID#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_AGENT_ID=$LOCAL_PIOPIY_AGENT_ID' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_CALLER_ID" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_CALLER_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_CALLER_ID=.*#PIOPIY_CALLER_ID=$LOCAL_PIOPIY_CALLER_ID#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_CALLER_ID=$LOCAL_PIOPIY_CALLER_ID' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_APP_ID" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_APP_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_APP_ID=.*#PIOPIY_APP_ID=$LOCAL_PIOPIY_APP_ID#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_APP_ID=$LOCAL_PIOPIY_APP_ID' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_TELEPHONY_PROVIDER" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^TELEPHONY_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^TELEPHONY_PROVIDER=.*#TELEPHONY_PROVIDER=$LOCAL_TELEPHONY_PROVIDER#' $REMOTE_APP_DIR/.env || printf '%s\n' 'TELEPHONY_PROVIDER=$LOCAL_TELEPHONY_PROVIDER' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^TELEPHONY_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^TELEPHONY_PROVIDER=.*#TELEPHONY_PROVIDER=piopiy#' $REMOTE_APP_DIR/.env || printf '%s\n' 'TELEPHONY_PROVIDER=piopiy' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_CLIENT_ID" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_CLIENT_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_CLIENT_ID=.*#PIOPIY_CLIENT_ID=$LOCAL_PIOPIY_CLIENT_ID#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_CLIENT_ID=$LOCAL_PIOPIY_CLIENT_ID' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_CLIENT_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_CLIENT_ID=.*#PIOPIY_CLIENT_ID=aivoicebot4u_guest_demo#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_CLIENT_ID=aivoicebot4u_guest_demo' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_PROJECT_ID" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_PROJECT_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_PROJECT_ID=.*#PIOPIY_PROJECT_ID=$LOCAL_PIOPIY_PROJECT_ID#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_PROJECT_ID=$LOCAL_PIOPIY_PROJECT_ID' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_PROJECT_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_PROJECT_ID=.*#PIOPIY_PROJECT_ID=real_estate_english_demo#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_PROJECT_ID=real_estate_english_demo' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_DEFAULT_CLIENT_ID" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_DEFAULT_CLIENT_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_DEFAULT_CLIENT_ID=.*#PIOPIY_DEFAULT_CLIENT_ID=$LOCAL_PIOPIY_DEFAULT_CLIENT_ID#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_DEFAULT_CLIENT_ID=$LOCAL_PIOPIY_DEFAULT_CLIENT_ID' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_DEFAULT_CLIENT_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_DEFAULT_CLIENT_ID=.*#PIOPIY_DEFAULT_CLIENT_ID=aivoicebot4u_guest_demo#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_DEFAULT_CLIENT_ID=aivoicebot4u_guest_demo' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_DEFAULT_PROJECT_ID" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_DEFAULT_PROJECT_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_DEFAULT_PROJECT_ID=.*#PIOPIY_DEFAULT_PROJECT_ID=$LOCAL_PIOPIY_DEFAULT_PROJECT_ID#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_DEFAULT_PROJECT_ID=$LOCAL_PIOPIY_DEFAULT_PROJECT_ID' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_DEFAULT_PROJECT_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_DEFAULT_PROJECT_ID=.*#PIOPIY_DEFAULT_PROJECT_ID=real_estate_english_demo#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_DEFAULT_PROJECT_ID=real_estate_english_demo' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_PIPELINE_MODE" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_PIPELINE_MODE=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_PIPELINE_MODE=.*#PIOPIY_PIPELINE_MODE=$LOCAL_PIOPIY_PIPELINE_MODE#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_PIPELINE_MODE=$LOCAL_PIOPIY_PIPELINE_MODE' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_PIPELINE_MODE=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_PIPELINE_MODE=.*#PIOPIY_PIPELINE_MODE=cascaded#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_PIPELINE_MODE=cascaded' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_LLM_FACTORY" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_LLM_FACTORY=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_LLM_FACTORY=.*#PIOPIY_LLM_FACTORY=$LOCAL_PIOPIY_LLM_FACTORY#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_LLM_FACTORY=$LOCAL_PIOPIY_LLM_FACTORY' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_LLM_FACTORY=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_LLM_FACTORY=.*#PIOPIY_LLM_FACTORY=piopiy.services.openai.llm:OpenAILLMService#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_LLM_FACTORY=piopiy.services.openai.llm:OpenAILLMService' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_LLM_API_KEY" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_LLM_API_KEY=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_LLM_API_KEY=.*#PIOPIY_LLM_API_KEY=$LOCAL_PIOPIY_LLM_API_KEY#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_LLM_API_KEY=$LOCAL_PIOPIY_LLM_API_KEY' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_LLM_MODEL" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_LLM_MODEL=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_LLM_MODEL=.*#PIOPIY_LLM_MODEL=$LOCAL_PIOPIY_LLM_MODEL#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_LLM_MODEL=$LOCAL_PIOPIY_LLM_MODEL' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_LLM_MODEL=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_LLM_MODEL=.*#PIOPIY_LLM_MODEL=sarvam-m#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_LLM_MODEL=sarvam-m' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_LLM_BASE_URL" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_LLM_BASE_URL=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_LLM_BASE_URL=.*#PIOPIY_LLM_BASE_URL=$LOCAL_PIOPIY_LLM_BASE_URL#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_LLM_BASE_URL=$LOCAL_PIOPIY_LLM_BASE_URL' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_LLM_BASE_URL=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_LLM_BASE_URL=.*#PIOPIY_LLM_BASE_URL=https://api.sarvam.ai/v1#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_LLM_BASE_URL=https://api.sarvam.ai/v1' >> $REMOTE_APP_DIR/.env"
fi
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_GEMINI_LIVE_MODEL\" ]; then grep -q '^PIOPIY_GEMINI_LIVE_MODEL=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_GEMINI_LIVE_MODEL=.*#PIOPIY_GEMINI_LIVE_MODEL=$LOCAL_PIOPIY_GEMINI_LIVE_MODEL#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_GEMINI_LIVE_MODEL=$LOCAL_PIOPIY_GEMINI_LIVE_MODEL' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_GEMINI_TTS_FACTORY\" ]; then grep -q '^PIOPIY_GEMINI_TTS_FACTORY=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_GEMINI_TTS_FACTORY=.*#PIOPIY_GEMINI_TTS_FACTORY=$LOCAL_PIOPIY_GEMINI_TTS_FACTORY#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_GEMINI_TTS_FACTORY=$LOCAL_PIOPIY_GEMINI_TTS_FACTORY' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_GEMINI_TTS_MODEL\" ]; then grep -q '^PIOPIY_GEMINI_TTS_MODEL=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_GEMINI_TTS_MODEL=.*#PIOPIY_GEMINI_TTS_MODEL=$LOCAL_PIOPIY_GEMINI_TTS_MODEL#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_GEMINI_TTS_MODEL=$LOCAL_PIOPIY_GEMINI_TTS_MODEL' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_GEMINI_TTS_VOICE_ID\" ]; then grep -q '^PIOPIY_GEMINI_TTS_VOICE_ID=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_GEMINI_TTS_VOICE_ID=.*#PIOPIY_GEMINI_TTS_VOICE_ID=$LOCAL_PIOPIY_GEMINI_TTS_VOICE_ID#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_GEMINI_TTS_VOICE_ID=$LOCAL_PIOPIY_GEMINI_TTS_VOICE_ID' >> $REMOTE_APP_DIR/.env; fi"
if [ -n "$LOCAL_PIOPIY_STT_FACTORY" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_STT_FACTORY=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_STT_FACTORY=.*#PIOPIY_STT_FACTORY=$LOCAL_PIOPIY_STT_FACTORY#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_STT_FACTORY=$LOCAL_PIOPIY_STT_FACTORY' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_STT_FACTORY=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_STT_FACTORY=.*#PIOPIY_STT_FACTORY=piopiy.services.sarvam.stt:SarvamSTTService#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_STT_FACTORY=piopiy.services.sarvam.stt:SarvamSTTService' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_TTS_FACTORY" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_TTS_FACTORY=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_TTS_FACTORY=.*#PIOPIY_TTS_FACTORY=$LOCAL_PIOPIY_TTS_FACTORY#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_TTS_FACTORY=$LOCAL_PIOPIY_TTS_FACTORY' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_TTS_FACTORY=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_TTS_FACTORY=.*#PIOPIY_TTS_FACTORY=piopiy.services.sarvam.tts:SarvamHttpTTSService#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_TTS_FACTORY=piopiy.services.sarvam.tts:SarvamHttpTTSService' >> $REMOTE_APP_DIR/.env"
fi
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_GEMINI_API_KEY\" ]; then grep -q '^GEMINI_API_KEY=' $REMOTE_APP_DIR/.env && sed -i 's#^GEMINI_API_KEY=.*#GEMINI_API_KEY=$LOCAL_GEMINI_API_KEY#' $REMOTE_APP_DIR/.env || printf '%s\n' 'GEMINI_API_KEY=$LOCAL_GEMINI_API_KEY' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_GEMINI_API_KEY\" ]; then grep -q '^PIOPIY_GEMINI_API_KEY=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_GEMINI_API_KEY=.*#PIOPIY_GEMINI_API_KEY=$LOCAL_PIOPIY_GEMINI_API_KEY#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_GEMINI_API_KEY=$LOCAL_PIOPIY_GEMINI_API_KEY' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_SARVAM_API_KEY\" ]; then grep -q '^SARVAM_API_KEY=' $REMOTE_APP_DIR/.env && sed -i 's#^SARVAM_API_KEY=.*#SARVAM_API_KEY=$LOCAL_SARVAM_API_KEY#' $REMOTE_APP_DIR/.env || printf '%s\n' 'SARVAM_API_KEY=$LOCAL_SARVAM_API_KEY' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_SARVAM_API_KEY\" ]; then grep -q '^PIOPIY_SARVAM_API_KEY=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_SARVAM_API_KEY=.*#PIOPIY_SARVAM_API_KEY=$LOCAL_PIOPIY_SARVAM_API_KEY' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_SARVAM_API_KEY=$LOCAL_PIOPIY_SARVAM_API_KEY' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_TTS_PITCH\" ]; then grep -q '^PIOPIY_TTS_PITCH=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_TTS_PITCH=.*#PIOPIY_TTS_PITCH=$LOCAL_PIOPIY_TTS_PITCH#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_TTS_PITCH=$LOCAL_PIOPIY_TTS_PITCH' >> $REMOTE_APP_DIR/.env; else grep -q '^PIOPIY_TTS_PITCH=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_TTS_PITCH=.*#PIOPIY_TTS_PITCH=0#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_TTS_PITCH=0' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_TTS_VOICE_NAME\" ]; then grep -q '^PIOPIY_TTS_VOICE_NAME=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_TTS_VOICE_NAME=.*#PIOPIY_TTS_VOICE_NAME=$LOCAL_PIOPIY_TTS_VOICE_NAME#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_TTS_VOICE_NAME=$LOCAL_PIOPIY_TTS_VOICE_NAME' >> $REMOTE_APP_DIR/.env; else grep -q '^PIOPIY_TTS_VOICE_NAME=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_TTS_VOICE_NAME=.*#PIOPIY_TTS_VOICE_NAME=Kore#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_TTS_VOICE_NAME=Kore' >> $REMOTE_APP_DIR/.env; fi"
if [ -n "$LOCAL_PIOPIY_USE_WEB_BRIDGE" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_USE_WEB_BRIDGE=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_USE_WEB_BRIDGE=.*#PIOPIY_USE_WEB_BRIDGE=$LOCAL_PIOPIY_USE_WEB_BRIDGE#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_USE_WEB_BRIDGE=$LOCAL_PIOPIY_USE_WEB_BRIDGE' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_PIOPIY_WS_PUBLIC_BASE_URL" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_WS_PUBLIC_BASE_URL=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_WS_PUBLIC_BASE_URL=.*#PIOPIY_WS_PUBLIC_BASE_URL=$LOCAL_PIOPIY_WS_PUBLIC_BASE_URL#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_WS_PUBLIC_BASE_URL=$LOCAL_PIOPIY_WS_PUBLIC_BASE_URL' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^PIOPIY_WS_PUBLIC_BASE_URL=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_WS_PUBLIC_BASE_URL=.*#PIOPIY_WS_PUBLIC_BASE_URL=#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_WS_PUBLIC_BASE_URL=' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_SPEECH_PROVIDER" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^SPEECH_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^SPEECH_PROVIDER=.*#SPEECH_PROVIDER=$LOCAL_SPEECH_PROVIDER#' $REMOTE_APP_DIR/.env || printf '%s\n' 'SPEECH_PROVIDER=$LOCAL_SPEECH_PROVIDER' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^SPEECH_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^SPEECH_PROVIDER=.*#SPEECH_PROVIDER=sarvam#' $REMOTE_APP_DIR/.env || printf '%s\n' 'SPEECH_PROVIDER=sarvam' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_LIVE_PROVIDER" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^LIVE_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^LIVE_PROVIDER=.*#LIVE_PROVIDER=$LOCAL_LIVE_PROVIDER#' $REMOTE_APP_DIR/.env || printf '%s\n' 'LIVE_PROVIDER=$LOCAL_LIVE_PROVIDER' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^LIVE_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^LIVE_PROVIDER=.*#LIVE_PROVIDER=sarvam#' $REMOTE_APP_DIR/.env || printf '%s\n' 'LIVE_PROVIDER=sarvam' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_STRUCTURED_PROVIDER" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^STRUCTURED_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^STRUCTURED_PROVIDER=.*#STRUCTURED_PROVIDER=$LOCAL_STRUCTURED_PROVIDER#' $REMOTE_APP_DIR/.env || printf '%s\n' 'STRUCTURED_PROVIDER=$LOCAL_STRUCTURED_PROVIDER' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^STRUCTURED_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^STRUCTURED_PROVIDER=.*#STRUCTURED_PROVIDER=sarvam#' $REMOTE_APP_DIR/.env || printf '%s\n' 'STRUCTURED_PROVIDER=sarvam' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_TTS_PROVIDER" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^TTS_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^TTS_PROVIDER=.*#TTS_PROVIDER=$LOCAL_TTS_PROVIDER#' $REMOTE_APP_DIR/.env || printf '%s\n' 'TTS_PROVIDER=$LOCAL_TTS_PROVIDER' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^TTS_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^TTS_PROVIDER=.*#TTS_PROVIDER=sarvam#' $REMOTE_APP_DIR/.env || printf '%s\n' 'TTS_PROVIDER=sarvam' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_RECORDING_STT_PROVIDER" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^RECORDING_STT_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^RECORDING_STT_PROVIDER=.*#RECORDING_STT_PROVIDER=$LOCAL_RECORDING_STT_PROVIDER#' $REMOTE_APP_DIR/.env || printf '%s\n' 'RECORDING_STT_PROVIDER=$LOCAL_RECORDING_STT_PROVIDER' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^RECORDING_STT_PROVIDER=' $REMOTE_APP_DIR/.env && sed -i 's#^RECORDING_STT_PROVIDER=.*#RECORDING_STT_PROVIDER=sarvam#' $REMOTE_APP_DIR/.env || printf '%s\n' 'RECORDING_STT_PROVIDER=sarvam' >> $REMOTE_APP_DIR/.env"
fi
if [ -n "$LOCAL_AGENT_RESPONSE_MODE" ]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^AGENT_RESPONSE_MODE=' $REMOTE_APP_DIR/.env && sed -i 's#^AGENT_RESPONSE_MODE=.*#AGENT_RESPONSE_MODE=$LOCAL_AGENT_RESPONSE_MODE#' $REMOTE_APP_DIR/.env || printf '%s\n' 'AGENT_RESPONSE_MODE=$LOCAL_AGENT_RESPONSE_MODE' >> $REMOTE_APP_DIR/.env"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "grep -q '^AGENT_RESPONSE_MODE=' $REMOTE_APP_DIR/.env && sed -i 's#^AGENT_RESPONSE_MODE=.*#AGENT_RESPONSE_MODE=live_audio#' $REMOTE_APP_DIR/.env || printf '%s\n' 'AGENT_RESPONSE_MODE=live_audio' >> $REMOTE_APP_DIR/.env"
fi
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_GOOGLE_CREDENTIALS_PATH\" ]; then grep -q '^PIOPIY_GOOGLE_CREDENTIALS_PATH=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_GOOGLE_CREDENTIALS_PATH=.*#PIOPIY_GOOGLE_CREDENTIALS_PATH=$LOCAL_PIOPIY_GOOGLE_CREDENTIALS_PATH#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_GOOGLE_CREDENTIALS_PATH=$LOCAL_PIOPIY_GOOGLE_CREDENTIALS_PATH' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_GOOGLE_CREDENTIALS_JSON\" ]; then grep -q '^PIOPIY_GOOGLE_CREDENTIALS_JSON=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_GOOGLE_CREDENTIALS_JSON=.*#PIOPIY_GOOGLE_CREDENTIALS_JSON=$LOCAL_PIOPIY_GOOGLE_CREDENTIALS_JSON#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_GOOGLE_CREDENTIALS_JSON=$LOCAL_PIOPIY_GOOGLE_CREDENTIALS_JSON' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_WS_PUBLIC_BASE_URL\" ]; then grep -q '^PIOPIY_WS_PUBLIC_BASE_URL=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_WS_PUBLIC_BASE_URL=.*#PIOPIY_WS_PUBLIC_BASE_URL=$LOCAL_PIOPIY_WS_PUBLIC_BASE_URL#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_WS_PUBLIC_BASE_URL=$LOCAL_PIOPIY_WS_PUBLIC_BASE_URL' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_PIOPIY_WS_SCHEME\" ]; then grep -q '^PIOPIY_WS_SCHEME=' $REMOTE_APP_DIR/.env && sed -i 's#^PIOPIY_WS_SCHEME=.*#PIOPIY_WS_SCHEME=$LOCAL_PIOPIY_WS_SCHEME#' $REMOTE_APP_DIR/.env || printf '%s\n' 'PIOPIY_WS_SCHEME=$LOCAL_PIOPIY_WS_SCHEME' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -n \"$LOCAL_TRUSTED_HOSTS\" ]; then grep -q '^TRUSTED_HOSTS=' $REMOTE_APP_DIR/.env && sed -i 's#^TRUSTED_HOSTS=.*#TRUSTED_HOSTS=$LOCAL_TRUSTED_HOSTS#' $REMOTE_APP_DIR/.env || printf '%s\n' 'TRUSTED_HOSTS=$LOCAL_TRUSTED_HOSTS' >> $REMOTE_APP_DIR/.env; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl daemon-reload"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl enable $VOICE_SERVICE_NAME >/tmp/voice_service_enable.log 2>&1 || true"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl restart $VOICE_SERVICE_NAME"
if [[ "$(lower "${LOCAL_PIOPIY_USE_WEB_BRIDGE}")" == "true" || "${LOCAL_PIOPIY_USE_WEB_BRIDGE}" == "1" || "$(lower "${LOCAL_PIOPIY_USE_WEB_BRIDGE}")" == "yes" || "$(lower "${LOCAL_PIOPIY_USE_WEB_BRIDGE}")" == "on" ]]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl stop $PIOPIY_SERVICE_NAME >/tmp/piopiy_stop.log 2>&1 || true"
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl disable $PIOPIY_SERVICE_NAME >/tmp/piopiy_disable.log 2>&1 || true"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl enable $PIOPIY_SERVICE_NAME >/tmp/piopiy_enable.log 2>&1 || true"
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl restart $PIOPIY_SERVICE_NAME"
fi

echo "[6/6] Verifying service health..."
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl --no-pager --full status $VOICE_SERVICE_NAME | sed -n '1,24p'"
echo
if [[ "$(lower "${LOCAL_PIOPIY_USE_WEB_BRIDGE}")" == "true" || "${LOCAL_PIOPIY_USE_WEB_BRIDGE}" == "1" || "$(lower "${LOCAL_PIOPIY_USE_WEB_BRIDGE}")" == "yes" || "$(lower "${LOCAL_PIOPIY_USE_WEB_BRIDGE}")" == "on" ]]; then
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl --no-pager --full status $PIOPIY_SERVICE_NAME | sed -n '1,24p' || true"
else
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl --no-pager --full status $PIOPIY_SERVICE_NAME | sed -n '1,24p'"
fi

echo
echo "Deployment complete for $INSTANCE_NAME ($INSTANCE_IP) using $ENV_FILE."
