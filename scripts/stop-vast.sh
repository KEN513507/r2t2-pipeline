#!/usr/bin/env bash
# Stop the remote instance while retaining its Vast.ai storage volume.
set -Eeuo pipefail

readonly PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly VASTAI="$PROJECT_DIR/.venv/bin/vastai"
readonly INSTANCE_ID="${R2T2_VAST_INSTANCE_ID:-51715606}"

[[ -x "$VASTAI" ]] || { echo "missing Vast.ai CLI: $VASTAI" >&2; exit 1; }
"$VASTAI" stop instance "$INSTANCE_ID"
echo "Stop requested for Vast.ai instance $INSTANCE_ID. Its storage volume is retained."
