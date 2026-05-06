#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTANCE_NAME="${1:-voice-agent-prod}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/new_voice_agent}"
EXCLUDES_FILE="$ROOT_DIR/deploy/lightsail/rsync-excludes.txt"

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

echo "[1/6] Running local Python syntax checks..."
"$ROOT_DIR/.venv/bin/python" -m py_compile "$ROOT_DIR/voice_sales_agent/web_app.py" "$ROOT_DIR/voice_sales_agent/gemini_api.py"

echo "[2/6] Fetching Lightsail access details for instance: $INSTANCE_NAME"
ACCESS_JSON="$("$ROOT_DIR/scripts/aws_local.sh" lightsail get-instance-access-details --instance-name "$INSTANCE_NAME" --protocol ssh --output json)"
INSTANCE_IP="$(printf '%s' "$ACCESS_JSON" | jq -r '.accessDetails.ipAddress')"
if [[ -z "$INSTANCE_IP" || "$INSTANCE_IP" == "null" ]]; then
  echo "Could not resolve instance IP for $INSTANCE_NAME"
  exit 1
fi

KEY_FILE="$(mktemp /tmp/lightsail-key-XXXXXX)"
CERT_FILE="${KEY_FILE}-cert.pub"
cleanup() {
  rm -f "$KEY_FILE" "$CERT_FILE"
}
trap cleanup EXIT

printf '%s' "$ACCESS_JSON" | jq -r '.accessDetails.privateKey' > "$KEY_FILE"
printf '%s' "$ACCESS_JSON" | jq -r '.accessDetails.certKey' > "$CERT_FILE"
chmod 600 "$KEY_FILE" "$CERT_FILE"

SSH_CMD=(ssh -o StrictHostKeyChecking=accept-new -i "$KEY_FILE")
RSYNC_SSH="ssh -o StrictHostKeyChecking=accept-new -i $KEY_FILE"

echo "[3/6] Syncing app files to $INSTANCE_NAME ($INSTANCE_IP)..."
rsync -az --delete \
  --exclude-from="$EXCLUDES_FILE" \
  -e "$RSYNC_SSH" \
  "$ROOT_DIR/" "ubuntu@$INSTANCE_IP:/tmp/new_voice_agent_sync/"
scp -o StrictHostKeyChecking=accept-new -i "$KEY_FILE" "$EXCLUDES_FILE" "ubuntu@$INSTANCE_IP:/tmp/rsync-excludes.txt"

echo "[4/6] Applying files on server..."
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo rsync -az --delete --exclude-from=/tmp/rsync-excludes.txt /tmp/new_voice_agent_sync/ $REMOTE_APP_DIR/"

echo "[5/6] Installing requirements and restarting service..."
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "cd $REMOTE_APP_DIR && sudo $REMOTE_APP_DIR/.venv/bin/pip install -r requirements.txt >/tmp/pip_deploy.log 2>&1"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl restart voice-sales-agent.service"

echo "[6/6] Verifying service health..."
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl --no-pager --full status voice-sales-agent.service | sed -n '1,24p'"

echo
echo "Deployment complete for $INSTANCE_NAME ($INSTANCE_IP)."
