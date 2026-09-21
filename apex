#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICES=(apex-backend.service apex-frontend.service)
HTTPS_PID_FILE="$ROOT/.apex/frontend-https.pid"
HTTPS_LOG_FILE="$ROOT/.apex/frontend-https.log"

usage() {
  cat <<'EOF'
Usage: ./apex COMMAND [OPTIONS]

Commands:
  install-service [--no-build]  Install, enable, and start systemd services
  start [--no-https]            Start services and HTTPS frontend on port 3001
  stop                          Stop backend and frontend
  restart [--no-https]           Restart services and HTTPS frontend on port 3001
  status                        Show backend and frontend status
EOF
}

units_installed() {
  systemctl cat "${SERVICES[0]}" >/dev/null 2>&1 &&
    systemctl cat "${SERVICES[1]}" >/dev/null 2>&1
}

require_units() {
  if ! units_installed; then
    printf '%s\n' "APEX systemd services are not installed." >&2
    printf '%s\n' "Run: ./apex install-service" >&2
    exit 1
  fi
}

start_https() {
  mkdir -p "$ROOT/.apex"
  if [[ -f "$HTTPS_PID_FILE" ]] && kill -0 "$(<"$HTTPS_PID_FILE")" 2>/dev/null; then
    printf '%s\n' "HTTPS frontend already running on port 3001 (PID $(<"$HTTPS_PID_FILE"))."
    return
  fi
  rm -f "$HTTPS_PID_FILE"
  (
    cd "$ROOT/frontend"
    NEXT_DIST_DIR=.next-https nohup npm run dev -- --experimental-https --hostname 0.0.0.0 --port 3001 \
      >"$HTTPS_LOG_FILE" 2>&1 &
    printf '%s\n' "$!" >"$HTTPS_PID_FILE"
  )
  printf '%s\n' "HTTPS frontend starting on https://<this-host>:3001"
  printf '%s\n' "Log: $HTTPS_LOG_FILE"
}

stop_https() {
  if [[ -f "$HTTPS_PID_FILE" ]]; then
    pid="$(<"$HTTPS_PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
      printf '%s\n' "Stopped HTTPS frontend (PID $pid)."
    fi
    rm -f "$HTTPS_PID_FILE"
  fi
}

https_status() {
  if [[ -f "$HTTPS_PID_FILE" ]] && kill -0 "$(<"$HTTPS_PID_FILE")" 2>/dev/null; then
    printf '%s\n' "HTTPS frontend: running on port 3001 (PID $(<"$HTTPS_PID_FILE"))."
  else
    printf '%s\n' "HTTPS frontend: stopped"
  fi
}

command="${1:-}"
shift || true

case "$command" in
  install-service)
    exec "$ROOT/deploy/install-services.sh" "$@"
    ;;
  start|stop|restart)
    require_units
    use_https=true
    if [[ "${1:-}" == "--no-https" ]]; then
      use_https=false
      shift
    elif [[ "${1:-}" == "--https" ]]; then
      shift
    fi
    [[ "$#" -eq 0 ]] || { usage >&2; exit 2; }
    if [[ "$command" == "stop" ]]; then
      stop_https
    fi
    sudo systemctl "$command" "${SERVICES[@]}"
    if [[ "$use_https" == true && "$command" != "stop" ]]; then
      start_https
    fi
    ;;
  status)
    require_units
    [[ "$#" -eq 0 ]] || { usage >&2; exit 2; }
    systemctl --no-pager --full status "${SERVICES[@]}" || true
    https_status
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac