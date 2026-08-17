#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTANCE_NAME="${1:-voice-agent-prod}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/new_voice_agent}"
REMOTE_PIOPIY_DIR="${REMOTE_PIOPIY_DIR:-/opt/new_voice_agent-piopiy}"
VOICE_SERVICE_NAME="${VOICE_SERVICE_NAME:-voice-sales-agent.service}"
PIOPIY_SERVICE_NAME="${PIOPIY_SERVICE_NAME:-piopiy-agent.service}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"
EXCLUDES_FILE="$ROOT_DIR/deploy/lightsail/rsync-excludes.txt"

lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

read_env_value() {
  awk -F= -v key="$1" '$1==key{print substr($0, index($0, "=")+1); exit}' "$ENV_FILE"
}

if [[ ! -x "$ROOT_DIR/scripts/aws_local.sh" ]]; then
  echo "Missing $ROOT_DIR/scripts/aws_local.sh"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE"
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

echo "[4/6] Applying files on server..."
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo rsync -az --delete --exclude-from=/tmp/rsync-excludes.txt /tmp/new_voice_agent_sync/ $REMOTE_APP_DIR/"

echo "[5/6] Installing requirements and restarting services..."
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "cd $REMOTE_APP_DIR && sudo $REMOTE_APP_DIR/.venv/bin/pip install -r requirements.txt >/tmp/pip_deploy.log 2>&1"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ -f $REMOTE_APP_DIR/package.json ] && command -v npm >/dev/null 2>&1; then cd $REMOTE_APP_DIR && sudo npm install --omit=dev >/tmp/npm_deploy.log 2>&1; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo mkdir -p $REMOTE_PIOPIY_DIR && sudo chown -R ubuntu:ubuntu $REMOTE_PIOPIY_DIR"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "if [ ! -x $REMOTE_PIOPIY_DIR/.venv/bin/python ]; then python3 -m venv $REMOTE_PIOPIY_DIR/.venv; fi"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "$REMOTE_PIOPIY_DIR/.venv/bin/pip install -U pip >/tmp/piopiy_pip_bootstrap.log 2>&1"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "$REMOTE_PIOPIY_DIR/.venv/bin/pip install --upgrade --force-reinstall -r $REMOTE_APP_DIR/requirements-piopiy-agent.txt >/tmp/piopiy_pip_deploy.log 2>&1"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sed -e 's#/opt/new_voice_agent-piopiy#$REMOTE_PIOPIY_DIR#g' -e 's#/opt/new_voice_agent#$REMOTE_APP_DIR#g' -e 's#DASHBOARD_PORT=8000#DASHBOARD_PORT=$DASHBOARD_PORT#g' $REMOTE_APP_DIR/deploy/lightsail/voice-sales-agent.service | sudo tee /etc/systemd/system/$VOICE_SERVICE_NAME >/dev/null"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sed -e 's#/opt/new_voice_agent-piopiy#$REMOTE_PIOPIY_DIR#g' -e 's#/opt/new_voice_agent#$REMOTE_APP_DIR#g' $REMOTE_APP_DIR/deploy/lightsail/piopiy-agent.service | sudo tee /etc/systemd/system/$PIOPIY_SERVICE_NAME >/dev/null"
${SSH_CMD[@]} "ubuntu@$INSTANCE_IP" "sudo rm -f /etc/systemd/system/piopiy-agent-old-sdk.service >/dev/null 2>&1 || true"
${SSH_CMD[@]} "ubuntu@$INSTANCE_IP" "sudo systemctl disable --now piopiy-agent-old-sdk.service >/dev/null 2>&1 || true"
${SSH_CMD[@]} "ubuntu@$INSTANCE_IP" "sudo sed -i '/^PIOPIY_OLD_SDK_/d' $REMOTE_APP_DIR/.env"
${SSH_CMD[@]} "ubuntu@$INSTANCE_IP" "sudo rm -rf $REMOTE_APP_DIR/telecmi_agents_oldtest_remote $REMOTE_PIOPIY_DIR/telecmi_agents_oldtest_remote $REMOTE_APP_DIR/runtime/piopiy_old_sdk_trace.jsonl"

remote_set_env_var() {
  local key="$1"
  local value="${2:-}"
  local encoded
  encoded="$(printf '%s' "$value" | base64 | tr -d '\n')"
  "${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "python3 - \"$REMOTE_APP_DIR/.env\" \"$key\" \"$encoded\"" <<'PY'
import base64
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = base64.b64decode(sys.argv[3]).decode("utf-8")
prefix = f"{key}="
lines = path.read_text(encoding="utf-8").splitlines()
updated = []
replaced = False
for line in lines:
    if line.startswith(prefix):
        updated.append(prefix + value)
        replaced = True
    else:
        updated.append(line)
if not replaced:
    updated.append(prefix + value)
path.write_text("\n".join(updated) + "\n", encoding="utf-8")
PY
}

remote_set_env_var_if_nonempty() {
  local key="$1"
  local value="${2:-}"
  if [ -n "$value" ]; then
    remote_set_env_var "$key" "$value"
  fi
}

LOCAL_AGENT_ID="$(read_env_value AGENT_ID)"
LOCAL_AGENT_TOKEN="$(read_env_value AGENT_TOKEN)"
LOCAL_PIOPIY_API_TOKEN="$(read_env_value PIOPIY_API_TOKEN)"
LOCAL_PIOPIY_AGENT_ID="$(read_env_value PIOPIY_AGENT_ID)"
LOCAL_PIOPIY_CALLER_ID="$(read_env_value PIOPIY_CALLER_ID)"
LOCAL_PIOPIY_APP_ID="$(read_env_value PIOPIY_APP_ID)"
LOCAL_TELEPHONY_PROVIDER="$(read_env_value TELEPHONY_PROVIDER)"
LOCAL_PIOPIY_CLIENT_ID="$(read_env_value PIOPIY_CLIENT_ID)"
LOCAL_PIOPIY_PROJECT_ID="$(read_env_value PIOPIY_PROJECT_ID)"
LOCAL_PIOPIY_DEFAULT_CLIENT_ID="$(read_env_value PIOPIY_DEFAULT_CLIENT_ID)"
LOCAL_PIOPIY_DEFAULT_PROJECT_ID="$(read_env_value PIOPIY_DEFAULT_PROJECT_ID)"
LOCAL_PIOPIY_PIPELINE_MODE="$(read_env_value PIOPIY_PIPELINE_MODE)"
LOCAL_PIOPIY_LLM_FACTORY="$(read_env_value PIOPIY_LLM_FACTORY)"
LOCAL_PIOPIY_LLM_API_KEY="$(read_env_value PIOPIY_LLM_API_KEY)"
LOCAL_PIOPIY_LLM_MODEL="$(read_env_value PIOPIY_LLM_MODEL)"
LOCAL_PIOPIY_LLM_BASE_URL="$(read_env_value PIOPIY_LLM_BASE_URL)"
LOCAL_PIOPIY_GEMINI_LIVE_MODEL="$(read_env_value PIOPIY_GEMINI_LIVE_MODEL)"
LOCAL_PIOPIY_GEMINI_TTS_FACTORY="$(read_env_value PIOPIY_GEMINI_TTS_FACTORY)"
LOCAL_PIOPIY_GEMINI_TTS_MODEL="$(read_env_value PIOPIY_GEMINI_TTS_MODEL)"
LOCAL_PIOPIY_GEMINI_TTS_VOICE_ID="$(read_env_value PIOPIY_GEMINI_TTS_VOICE_ID)"
LOCAL_PIOPIY_STT_FACTORY="$(read_env_value PIOPIY_STT_FACTORY)"
LOCAL_PIOPIY_TTS_FACTORY="$(read_env_value PIOPIY_TTS_FACTORY)"
LOCAL_PIOPIY_TTS_VOICE_NAME="$(read_env_value PIOPIY_TTS_VOICE_NAME)"
LOCAL_SPEECH_PROVIDER="$(read_env_value SPEECH_PROVIDER)"
LOCAL_LIVE_PROVIDER="$(read_env_value LIVE_PROVIDER)"
LOCAL_STRUCTURED_PROVIDER="$(read_env_value STRUCTURED_PROVIDER)"
LOCAL_TTS_PROVIDER="$(read_env_value TTS_PROVIDER)"
LOCAL_RECORDING_STT_PROVIDER="$(read_env_value RECORDING_STT_PROVIDER)"
LOCAL_FASTER_STT_ENABLED="$(read_env_value FASTER_STT_ENABLED)"
LOCAL_FASTER_STT_MODEL="$(read_env_value FASTER_STT_MODEL)"
LOCAL_FASTER_STT_DEVICE="$(read_env_value FASTER_STT_DEVICE)"
LOCAL_FASTER_STT_COMPUTE_TYPE="$(read_env_value FASTER_STT_COMPUTE_TYPE)"
LOCAL_FASTER_STT_BEAM_SIZE="$(read_env_value FASTER_STT_BEAM_SIZE)"
LOCAL_AGENT_RESPONSE_MODE="$(read_env_value AGENT_RESPONSE_MODE)"
LOCAL_GEMINI_STRUCTURED_MODEL="$(read_env_value GEMINI_STRUCTURED_MODEL)"
LOCAL_GEMINI_STRUCTURED_FALLBACK_MODEL="$(read_env_value GEMINI_STRUCTURED_FALLBACK_MODEL)"
LOCAL_GEMINI_API_KEY="$(read_env_value GEMINI_API_KEY)"
LOCAL_PIOPIY_GEMINI_API_KEY="$(read_env_value PIOPIY_GEMINI_API_KEY)"
LOCAL_PIOPIY_TTS_PITCH="$(read_env_value PIOPIY_TTS_PITCH)"
LOCAL_PIOPIY_GOOGLE_CREDENTIALS_PATH="$(read_env_value PIOPIY_GOOGLE_CREDENTIALS_PATH)"
LOCAL_PIOPIY_GOOGLE_CREDENTIALS_JSON="$(read_env_value PIOPIY_GOOGLE_CREDENTIALS_JSON)"
LOCAL_PIOPIY_WS_PUBLIC_BASE_URL="$(read_env_value PIOPIY_WS_PUBLIC_BASE_URL)"
LOCAL_PIOPIY_WS_SCHEME="$(read_env_value PIOPIY_WS_SCHEME)"
LOCAL_AIRTEL_IQ_TEST_ALLOWED_IPS="$(read_env_value AIRTEL_IQ_TEST_ALLOWED_IPS)"
LOCAL_AIRTEL_IQ_WS_PUBLIC_BASE_URL="$(read_env_value AIRTEL_IQ_WS_PUBLIC_BASE_URL)"
LOCAL_TRUSTED_HOSTS="$(read_env_value TRUSTED_HOSTS)"
LOCAL_PIOPIY_USE_WEB_BRIDGE="$(read_env_value PIOPIY_USE_WEB_BRIDGE)"
LOCAL_JANJAL_OUTBOUND_AUTO_ROUTE_ENABLED="$(read_env_value JANJAL_OUTBOUND_AUTO_ROUTE_ENABLED)"
LOCAL_JANJAL_OUTBOUND_PROJECT_ID="$(read_env_value JANJAL_OUTBOUND_PROJECT_ID)"
LOCAL_META_WHATSAPP_BASE_URL="$(read_env_value META_WHATSAPP_BASE_URL)"
LOCAL_META_WHATSAPP_API_VERSION="$(read_env_value META_WHATSAPP_API_VERSION)"
LOCAL_META_WHATSAPP_ACCESS_TOKEN="$(read_env_value META_WHATSAPP_ACCESS_TOKEN)"
LOCAL_META_WHATSAPP_PHONE_NUMBER_ID="$(read_env_value META_WHATSAPP_PHONE_NUMBER_ID)"
LOCAL_META_WHATSAPP_FROM_NUMBER="$(read_env_value META_WHATSAPP_FROM_NUMBER)"
LOCAL_META_WHATSAPP_WEBHOOK_VERIFY_TOKEN="$(read_env_value META_WHATSAPP_WEBHOOK_VERIFY_TOKEN)"
LOCAL_META_WHATSAPP_APP_SECRET="$(read_env_value META_WHATSAPP_APP_SECRET)"
LOCAL_CALL_TRANSCRIPTION_WHATSAPP_NUMBER="$(read_env_value CALL_TRANSCRIPTION_WHATSAPP_NUMBER)"
remote_set_env_var AGENT_ID "$LOCAL_AGENT_ID"
remote_set_env_var AGENT_TOKEN "$LOCAL_AGENT_TOKEN"
remote_set_env_var_if_nonempty PIOPIY_API_TOKEN "$LOCAL_PIOPIY_API_TOKEN"
remote_set_env_var_if_nonempty PIOPIY_AGENT_ID "$LOCAL_PIOPIY_AGENT_ID"
remote_set_env_var_if_nonempty PIOPIY_CALLER_ID "$LOCAL_PIOPIY_CALLER_ID"
remote_set_env_var_if_nonempty PIOPIY_APP_ID "$LOCAL_PIOPIY_APP_ID"
remote_set_env_var TELEPHONY_PROVIDER "${LOCAL_TELEPHONY_PROVIDER:-piopiy}"
remote_set_env_var PIOPIY_CLIENT_ID "${LOCAL_PIOPIY_CLIENT_ID:-aivoicebot4u_guest_demo}"
remote_set_env_var PIOPIY_PROJECT_ID "${LOCAL_PIOPIY_PROJECT_ID:-real_estate_english_demo}"
remote_set_env_var PIOPIY_DEFAULT_CLIENT_ID "${LOCAL_PIOPIY_DEFAULT_CLIENT_ID:-aivoicebot4u_guest_demo}"
remote_set_env_var PIOPIY_DEFAULT_PROJECT_ID "${LOCAL_PIOPIY_DEFAULT_PROJECT_ID:-real_estate_english_demo}"
remote_set_env_var PIOPIY_PIPELINE_MODE "${LOCAL_PIOPIY_PIPELINE_MODE:-gemini_native_simple}"
remote_set_env_var PIOPIY_LLM_FACTORY "${LOCAL_PIOPIY_LLM_FACTORY:-piopiy.services.openai.llm:OpenAILLMService}"
remote_set_env_var_if_nonempty PIOPIY_LLM_API_KEY "$LOCAL_PIOPIY_LLM_API_KEY"
remote_set_env_var PIOPIY_LLM_MODEL "${LOCAL_PIOPIY_LLM_MODEL:-gemini-2.5-flash}"
remote_set_env_var PIOPIY_LLM_BASE_URL "${LOCAL_PIOPIY_LLM_BASE_URL:-}"
remote_set_env_var_if_nonempty PIOPIY_GEMINI_LIVE_MODEL "$LOCAL_PIOPIY_GEMINI_LIVE_MODEL"
remote_set_env_var_if_nonempty PIOPIY_GEMINI_TTS_FACTORY "$LOCAL_PIOPIY_GEMINI_TTS_FACTORY"
remote_set_env_var_if_nonempty PIOPIY_GEMINI_TTS_MODEL "$LOCAL_PIOPIY_GEMINI_TTS_MODEL"
remote_set_env_var_if_nonempty PIOPIY_GEMINI_TTS_VOICE_ID "$LOCAL_PIOPIY_GEMINI_TTS_VOICE_ID"
remote_set_env_var PIOPIY_STT_FACTORY "${LOCAL_PIOPIY_STT_FACTORY:-}"
remote_set_env_var PIOPIY_TTS_FACTORY "${LOCAL_PIOPIY_TTS_FACTORY:-}"
remote_set_env_var PIOPIY_TTS_VOICE_NAME "${LOCAL_PIOPIY_TTS_VOICE_NAME:-Kore}"
remote_set_env_var_if_nonempty GEMINI_API_KEY "$LOCAL_GEMINI_API_KEY"
remote_set_env_var_if_nonempty PIOPIY_GEMINI_API_KEY "$LOCAL_PIOPIY_GEMINI_API_KEY"
remote_set_env_var PIOPIY_TTS_PITCH "${LOCAL_PIOPIY_TTS_PITCH:-0}"
remote_set_env_var FASTER_STT_ENABLED "${LOCAL_FASTER_STT_ENABLED:-1}"
remote_set_env_var FASTER_STT_MODEL "${LOCAL_FASTER_STT_MODEL:-tiny}"
remote_set_env_var FASTER_STT_DEVICE "${LOCAL_FASTER_STT_DEVICE:-cpu}"
remote_set_env_var FASTER_STT_COMPUTE_TYPE "${LOCAL_FASTER_STT_COMPUTE_TYPE:-int8_float32}"
remote_set_env_var FASTER_STT_BEAM_SIZE "${LOCAL_FASTER_STT_BEAM_SIZE:-1}"
remote_set_env_var_if_nonempty PIOPIY_USE_WEB_BRIDGE "$LOCAL_PIOPIY_USE_WEB_BRIDGE"
remote_set_env_var PIOPIY_WS_PUBLIC_BASE_URL "${LOCAL_PIOPIY_WS_PUBLIC_BASE_URL:-}"
remote_set_env_var PIOPIY_WS_SCHEME "${LOCAL_PIOPIY_WS_SCHEME:-}"
remote_set_env_var AIRTEL_IQ_TEST_ALLOWED_IPS "${LOCAL_AIRTEL_IQ_TEST_ALLOWED_IPS:-}"
remote_set_env_var AIRTEL_IQ_WS_PUBLIC_BASE_URL "${LOCAL_AIRTEL_IQ_WS_PUBLIC_BASE_URL:-}"
remote_set_env_var TRUSTED_HOSTS "${LOCAL_TRUSTED_HOSTS:-}"
remote_set_env_var JANJAL_OUTBOUND_AUTO_ROUTE_ENABLED "${LOCAL_JANJAL_OUTBOUND_AUTO_ROUTE_ENABLED:-}"
remote_set_env_var JANJAL_OUTBOUND_PROJECT_ID "${LOCAL_JANJAL_OUTBOUND_PROJECT_ID:-}"
remote_set_env_var META_WHATSAPP_BASE_URL "${LOCAL_META_WHATSAPP_BASE_URL:-}"
remote_set_env_var META_WHATSAPP_API_VERSION "${LOCAL_META_WHATSAPP_API_VERSION:-}"
remote_set_env_var_if_nonempty META_WHATSAPP_ACCESS_TOKEN "$LOCAL_META_WHATSAPP_ACCESS_TOKEN"
remote_set_env_var_if_nonempty META_WHATSAPP_PHONE_NUMBER_ID "$LOCAL_META_WHATSAPP_PHONE_NUMBER_ID"
remote_set_env_var_if_nonempty META_WHATSAPP_FROM_NUMBER "$LOCAL_META_WHATSAPP_FROM_NUMBER"
remote_set_env_var_if_nonempty META_WHATSAPP_WEBHOOK_VERIFY_TOKEN "$LOCAL_META_WHATSAPP_WEBHOOK_VERIFY_TOKEN"
remote_set_env_var_if_nonempty META_WHATSAPP_APP_SECRET "$LOCAL_META_WHATSAPP_APP_SECRET"
remote_set_env_var_if_nonempty CALL_TRANSCRIPTION_WHATSAPP_NUMBER "$LOCAL_CALL_TRANSCRIPTION_WHATSAPP_NUMBER"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl daemon-reload"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl restart $VOICE_SERVICE_NAME"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl daemon-reload"
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
echo "Deployment complete for $INSTANCE_NAME ($INSTANCE_IP)."
