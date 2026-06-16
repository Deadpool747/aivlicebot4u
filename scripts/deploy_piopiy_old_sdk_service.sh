#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTANCE_NAME="${1:-voice-agent-prod}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/new_voice_agent_staging}"
REMOTE_PIOPIY_DIR="${REMOTE_PIOPIY_DIR:-/opt/new_voice_agent_staging-piopiy}"
SERVICE_NAME="${SERVICE_NAME:-piopiy-agent-old-sdk-staging.service}"
UNIT_TEMPLATE="$ROOT_DIR/deploy/lightsail/piopiy-agent-old-sdk.service"

if [[ ! -f "$UNIT_TEMPLATE" ]]; then
  echo "Missing unit template: $UNIT_TEMPLATE"
  exit 1
fi

ACCESS_JSON="$("$ROOT_DIR/scripts/aws_local.sh" lightsail get-instance-access-details --instance-name "$INSTANCE_NAME" --protocol ssh --output json)"
INSTANCE_IP="$(printf '%s' "$ACCESS_JSON" | jq -r '.accessDetails.ipAddress')"
if [[ -z "$INSTANCE_IP" || "$INSTANCE_IP" == "null" ]]; then
  echo "Could not resolve instance IP for $INSTANCE_NAME"
  exit 1
fi

KEY_DIR="$(mktemp -d /tmp/lightsail-old-sdk-service-XXXXXX)"
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

scp -o StrictHostKeyChecking=accept-new -o CertificateFile="$CERT_FILE" -i "$KEY_FILE" \
  "$UNIT_TEMPLATE" "ubuntu@$INSTANCE_IP:/tmp/piopiy-agent-old-sdk.service"

"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" \
  "sudo sh -c \"sed -e 's#/opt/new_voice_agent-piopiy#__REMOTE_PIOPIY_DIR__#g' -e 's#/opt/new_voice_agent#__REMOTE_APP_DIR__#g' -e 's#__REMOTE_PIOPIY_DIR__#$REMOTE_PIOPIY_DIR#g' -e 's#__REMOTE_APP_DIR__#$REMOTE_APP_DIR#g' /tmp/piopiy-agent-old-sdk.service > /etc/systemd/system/$SERVICE_NAME\""
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl daemon-reload"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl enable $SERVICE_NAME"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl restart $SERVICE_NAME"
"${SSH_CMD[@]}" "ubuntu@$INSTANCE_IP" "sudo systemctl --no-pager --full status $SERVICE_NAME | sed -n '1,60p'"

echo "Deployed $SERVICE_NAME on $INSTANCE_NAME ($INSTANCE_IP)"
