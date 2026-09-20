#!/usr/bin/env bash
set -euo pipefail
ENV_FILE="${HOME}/.config/r2t2/server.env"
DATA_DIR="${R2T2_DATA_DIR:-/workspace/r2t2-data}"
[ -f "$ENV_FILE" ] || { echo "missing: $ENV_FILE"; exit 1; }
mkdir -p "$DATA_DIR"
docker rm -f r2t2 2>/dev/null || true
docker run -d --name r2t2 --gpus all --restart unless-stopped \
  -p 8000:8000 \
  -v "${DATA_DIR}:/app/data" \
  --env-file "$ENV_FILE" \
  r2t2-server
docker logs -f r2t2
