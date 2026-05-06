#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AWS_SHARED_CREDENTIALS_FILE="$ROOT_DIR/.aws/credentials"
export AWS_CONFIG_FILE="$ROOT_DIR/.aws/config"

exec "$ROOT_DIR/.venv/bin/aws" "$@"
