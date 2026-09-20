#!/usr/bin/env bash
set -euo pipefail
ENV_FILE="${HOME}/.config/r2t2/client.env"
[ -f "$ENV_FILE" ] || { echo "missing: $ENV_FILE"; exit 1; }
cd "$(dirname "$0")/../client"
set -a
source "$ENV_FILE"
set +a
exec python client.py
