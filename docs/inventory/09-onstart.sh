#!/usr/bin/env bash
# Vast.ai onstart hook. Its output is written to /var/log/onstart.log.
set -Eeuo pipefail

readonly APP_DIR=/root/Confucius4-R2T2
readonly VENV_PYTHON=/workspace/r2t2-venv/bin/python
readonly ENV_FILE=/root/.config/r2t2/server.env
readonly ASR_LOG="$APP_DIR/nohup_service_ws_localhost_8272.log"
readonly PROXY_LOG="$APP_DIR/nohup_proxy_server_8000.log"

log() { printf '%s onstart: %s\n' "$(date -Is)" "$*"; }

port_open() {
  "$VENV_PYTHON" - "$1" <<'PY'
import socket
import sys

with socket.socket() as sock:
    sock.settimeout(1)
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

wait_for_port() {
  local port=$1 timeout=$2 elapsed=0
  until port_open "$port"; do
    if (( elapsed >= timeout )); then
      log "timed out waiting for port $port"
      return 1
    fi
    sleep 2
    ((elapsed += 2))
  done
}

main() {
  [[ -x "$VENV_PYTHON" ]] || { log "missing Python venv: $VENV_PYTHON"; exit 1; }
  [[ -f "$ENV_FILE" ]] || { log "missing environment file: $ENV_FILE"; exit 1; }
  [[ -f "$APP_DIR/proxy_server.py" ]] || { log "missing: $APP_DIR/proxy_server.py"; exit 1; }

  # Export values only to child processes; never print this file or its values.
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  mkdir -p /root/r2t2-data
  cd "$APP_DIR"

  if port_open 8272; then
    log "R2T2 already listening on 8272"
  else
    log "starting R2T2 on 8272"
    ./run_start_server.sh start \
      --model_path netease-youdao/Confucius4-R2T2 \
      --vad_model_path checkpoints/vad/Stream-VAD \
      --port 8272 --gpu 0 >>"$ASR_LOG" 2>&1
    wait_for_port 8272 300
    log "R2T2 is ready on 8272"
  fi

  if port_open 8000; then
    log "proxy already listening on 8000"
  else
    log "starting proxy on 8000"
    nohup "$VENV_PYTHON" proxy_server.py >>"$PROXY_LOG" 2>&1 &
    wait_for_port 8000 60
    log "proxy is ready on 8000"
  fi
}

main "$@"
