#!/usr/bin/env bash
# Start the Vast.ai instance, local tunnel, VOICEVOX, and client.
set -Eeuo pipefail

readonly PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly VASTAI="$PROJECT_DIR/.venv/bin/vastai"
readonly CLIENT_ENV="${HOME}/.config/r2t2/client.env"
readonly INSTANCE_ID="${R2T2_VAST_INSTANCE_ID:-51715606}"
readonly VOICEVOX_IMAGE="voicevox/voicevox_engine:cpu-latest"
readonly VOICEVOX_CONTAINER="r2t2-voicevox"

log() { printf '%s\n' "$*"; }
die() { log "error: $*" >&2; exit 1; }

instance_state() {
  "$VASTAI" --raw show instance "$INSTANCE_ID" | python3 -c \
    'import json, sys; print(json.load(sys.stdin).get("actual_status", "unknown"))'
}

wait_for_instance() {
  local elapsed=0 state
  while :; do
    state="$(instance_state)" || die "could not obtain Vast.ai instance status"
    if [[ "$state" == "running" ]]; then
      return
    fi
    if (( elapsed >= 600 )); then
      die "instance did not become running within 10 minutes (last state: $state)"
    fi
    log "Vast.ai instance state: $state; waiting..."
    sleep 10
    ((elapsed += 10))
  done
}

start_tunnel() {
  local ssh_url ssh_target ssh_port
  ssh_url="$($VASTAI ssh-url "$INSTANCE_ID")" || die "could not obtain SSH URL"
  [[ "$ssh_url" =~ ^ssh://[^@]+@[^:]+:[0-9]+$ ]] || die "unexpected SSH URL format"
  ssh_target="${ssh_url#ssh://}"
  ssh_port="${ssh_target##*:}"
  ssh_target="${ssh_target%:*}"

  if curl --max-time 1 -fsS http://127.0.0.1:8000/docs >/dev/null; then
    log "local tunnel is already serving the proxy"
    return
  fi
  log "creating SSH tunnel to $ssh_target:$ssh_port"
  ssh -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -L 127.0.0.1:8000:127.0.0.1:8000 -p "$ssh_port" "$ssh_target"

  local elapsed=0
  until curl --max-time 2 -fsS http://127.0.0.1:8000/docs >/dev/null; do
    if (( elapsed >= 300 )); then
      die "proxy did not become reachable through the SSH tunnel"
    fi
    sleep 5
    ((elapsed += 5))
  done
  log "proxy is reachable through the SSH tunnel"
}

start_voicevox() {
  if curl --max-time 2 -fsS http://127.0.0.1:50021/version >/dev/null; then
    log "VOICEVOX is already available"
    return
  fi
  if docker ps -a --format '{{.Names}}' | grep -Fxq "$VOICEVOX_CONTAINER"; then
    docker start "$VOICEVOX_CONTAINER" >/dev/null
  else
    docker run -d --name "$VOICEVOX_CONTAINER" --restart unless-stopped \
      -p 127.0.0.1:50021:50021 "$VOICEVOX_IMAGE" >/dev/null
  fi

  local elapsed=0
  until curl --max-time 2 -fsS http://127.0.0.1:50021/version >/dev/null; do
    if (( elapsed >= 180 )); then
      die "VOICEVOX did not become ready"
    fi
    sleep 3
    ((elapsed += 3))
  done
  log "VOICEVOX is ready"
}

main() {
  [[ -x "$VASTAI" ]] || die "missing Vast.ai CLI: $VASTAI"
  [[ -f "$CLIENT_ENV" ]] || die "missing client environment file: $CLIENT_ENV"

  local state
  state="$(instance_state)" || die "could not obtain Vast.ai instance status"
  if [[ "$state" != "running" ]]; then
    log "starting Vast.ai instance $INSTANCE_ID"
    "$VASTAI" start instance "$INSTANCE_ID"
  else
    log "Vast.ai instance $INSTANCE_ID is already running"
  fi
  wait_for_instance
  start_tunnel
  start_voicevox

  cd "$PROJECT_DIR"
  set -a
  # shellcheck disable=SC1090
  source "$CLIENT_ENV"
  set +a
  export R2T2_WS_URL=ws://127.0.0.1:8000/stream
  exec "$PROJECT_DIR/.venv/bin/python" client/client.py
}

main "$@"
