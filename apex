#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICES=(apex-backend.service apex-frontend.service)
HTTPS_PID_FILE="$ROOT/.apex/frontend-https.pid"
HTTPS_LOG_FILE="$ROOT/.apex/frontend-https.log"
DATA_DIR="$ROOT/backend/data"

# ─────────────────────────── terminal polish helpers ──────────────────────────
INTERACTIVE=1
[[ -t 1 ]] || INTERACTIVE=0
[[ -n "${NO_COLOR:-}" ]] && INTERACTIVE=0

C_RESET=$'\e[0m'
C_BOLD=$'\e[1m'
C_DIM=$'\e[2m'
C_CYAN=$'\e[36m'
C_GREEN=$'\e[32m'
C_YELLOW=$'\e[33m'
C_RED=$'\e[31m'

FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
PULSE=(· · · · ● ● ● ●)

say()  { printf '%s\n' "${C_CYAN}◆${C_RESET} $*"; }
ok()   { printf '%s\n' "${C_GREEN}  ✓${C_RESET} $*"; }
warn() { printf '%s\n' "${C_YELLOW}  ⚠${C_RESET} $*"; }
die()  { printf '%s\n' "${C_RED}  ✗${C_RESET} $*" >&2; exit 1; }

clear_line() { printf '\r\033[K'; }

## spinner <label> <cmd...> — run a command with an animated ring light.
spinner() {
  local label="$1"
  shift
  local log rc=0 i=0 pid
  log="$(mktemp "${TMPDIR:-/tmp}/apex-spin.XXXXXX")"
  if [[ $INTERACTIVE -eq 1 ]]; then
    "$@" >"$log" 2>&1 & pid=$!
    while kill -0 "$pid" 2>/dev/null; do
      clear_line
      printf "${C_CYAN}%s${C_RESET} %s" "${FRAMES[i]}" "$label"
      i=$(( (i + 1) % ${#FRAMES[@]} ))
      sleep 0.12
    done
    if wait "$pid"; then rc=0; else rc=$?; fi
    clear_line
    if [[ $rc -eq 0 ]]; then printf "${C_GREEN}  ✓${C_RESET} %s\n" "$label"; else printf "${C_RED}  ✗${C_RESET} %s\n" "$label"; fi
  else
    printf '%s ... ' "$label"
    if "$@" >"$log" 2>&1; then rc=0; else rc=$?; fi
    if [[ $rc -eq 0 ]]; then printf 'done\n'; else printf 'FAILED\n'; fi
  fi
  if [[ $rc -ne 0 ]]; then
    printf '%s\n' "${C_DIM}───── last output ─────────────────────────${C_RESET}"
    tail -n 12 "$log" 2>/dev/null || true
    printf '%s\n' "${C_DIM}───────────────────────────────────────────${C_RESET}"
  fi
  rm -f "$log"
  return $rc
}

## pulse <label> — a heartbeat animation for short (~1.5s) probes.
pulse() {
  local label="$1"
  local i
  if [[ $INTERACTIVE -ne 1 ]]; then
    printf '%s\n' "$label"
    return 0
  fi
  for i in "${!PULSE[@]}"; do
    clear_line
    printf "${C_CYAN}%s${C_RESET} %s" "${PULSE[i]}" "$label"
    sleep 0.12
  done
  clear_line
}

## type_cmd <cmdline> — "type out" the command being run (dramatic pause).
type_cmd() {
  if [[ $INTERACTIVE -ne 1 || ${#1} -gt 120 ]]; then
    printf '%s\n' "${C_DIM}\$ $1${C_RESET}"
    return 0
  fi
  local i ch
  printf '%s' "${C_DIM}\$ ${C_RESET}"
  for ((i = 0; i < ${#1}; i++)); do
    ch="${1:i:1}"
    printf '%s' "$ch"
    sleep 0.006
  done
  printf '\n'
}

## progress bar over a long file copy (backup / restore).
copy_with_bar() {
  local src="$1" dst="$2"
  local total dn pct
  total=$(find "$src" -type f 2>/dev/null | wc -l)
  [[ ${total:-0} -eq 0 ]] && total=1
  if [[ $INTERACTIVE -eq 1 ]]; then
    (
      while :; do
        dn=$(find "$dst" -type f 2>/dev/null | wc -l)
        (( pct = dn * 100 / total ))
        (( pct > 100 )) && pct=100
        bar=''
        for ((j = 0; j < 20; j++)); do
          (( j < pct * 20 / 100 )) && bar+='█' || bar+='░'
        done
        printf "\r${C_CYAN}◆${C_RESET} syncing [%s] %3s%%" "$bar" "$pct"
        sleep 0.12
        (( pct >= 100 )) && break
      done
    ) &
    local anim=$!
  fi
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "$src/" "$dst/"
  else
    cp -a "$src/." "$dst/"
  fi
  if [[ $INTERACTIVE -eq 1 ]]; then kill "$anim" 2>/dev/null || true; wait "$anim" 2>/dev/null || true; fi
  clear_line
}

dir_size()  { du -sh "$1" 2>/dev/null | cut -f1; }
count_files() { find "$1" -type f 2>/dev/null | wc -l; }

banner() {
  local cmd="${1:-}"
  if [[ $INTERACTIVE -ne 1 ]]; then
    printf '%s\n' "APEX · administrative tool"
    return 0
  fi
  clear
  printf '%s\n' "${C_DIM}┌─────────────────────────────────────────────────────┐${C_RESET}"
  printf '%s'  "${C_CYAN}${C_BOLD}   ▟▟ A P E X ▚▚${C_RESET} ${C_DIM}— admin tool${C_RESET}"
  printf '%s\n' "                     "
  printf '%s\n' "${C_CYAN}${C_BOLD}   ▄▀█ ▄▀█ █ ▄▀█ ▄▀█${C_RESET}"
  printf '%s\n' "${C_CYAN}${C_BOLD}   █▀█ █▀█ █ █▀█ █▀▄${C_RESET}"
  printf '%s\n' "${C_DIM}└─────────────────────────────────────────────────────┘${C_RESET}"
  printf '%s' "${C_DIM} >> ${C_RESET}${C_CYAN}${C_BOLD}./apex ${cmd:-help}${C_RESET}"
  for _ in 1 2 3; do printf '%s' "${C_YELLOW}.${C_RESET}"; sleep 0.08; done
  printf '\n\n'
}

usage() {
  cat <<'EOF'
Usage: ./apex COMMAND [OPTIONS]

Commands:
  install-service [--no-build]  Install, enable, and start systemd services
  start [--no-https]            Start services and HTTPS frontend on port 3001
  stop                          Stop backend and frontend
  restart [--no-https]          Restart services and HTTPS frontend on port 3001
  status                        Show backend and frontend status
  update                        Back up data, git pull (keeps local DB), then restart
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

service_active() { systemctl is-active --quiet "$1" 2>/dev/null; }

start_https() {
  mkdir -p "$ROOT/.apex"
  if [[ -f "$HTTPS_PID_FILE" ]] && kill -0 "$(<"$HTTPS_PID_FILE")" 2>/dev/null; then
    ok "HTTPS frontend already running on port 3001 (PID $(<"$HTTPS_PID_FILE"))."
    return
  fi
  rm -f "$HTTPS_PID_FILE"
  (
    cd "$ROOT/frontend"
    NEXT_DIST_DIR=.next-https nohup npm run dev -- --experimental-https --hostname 0.0.0.0 --port 3001 \
      >"$HTTPS_LOG_FILE" 2>&1 &
    printf '%s\n' "$!" >"$HTTPS_PID_FILE"
  )
  pulse "binding https://<this-host>:3001"
  ok "HTTPS frontend on https://<this-host>:3001  ${C_DIM}(log: $HTTPS_LOG_FILE)${C_RESET}"
}

stop_https() {
  if [[ -f "$HTTPS_PID_FILE" ]]; then
    pid="$(<"$HTTPS_PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      pulse "shutting down HTTPS frontend (PID $pid)"
      kill -TERM "$pid" 2>/dev/null || true
      ok "Stopped HTTPS frontend (PID $pid)."
    fi
    rm -f "$HTTPS_PID_FILE"
  fi
}

https_running() { [[ -f "$HTTPS_PID_FILE" ]] && kill -0 "$(<"$HTTPS_PID_FILE")" 2>/dev/null; }
https_status() {
  if https_running; then
    ok "HTTPS frontend: running on port 3001 (PID $(<"$HTTPS_PID_FILE"))."
  else
    warn "HTTPS frontend: stopped"
  fi
}

restart_services() {
  local had_units="$1" had_https="$2"
  if [[ "$had_units" == true ]]; then
    spinner "starting ${SERVICES[0]}" sudo systemctl start "${SERVICES[0]}" || true
    spinner "starting ${SERVICES[1]}" sudo systemctl start "${SERVICES[1]}" || true
    if service_active "${SERVICES[0]}" && service_active "${SERVICES[1]}"; then
      ok "services running: ${SERVICES[0]} + ${SERVICES[1]}"
    else
      warn "could not start a service — run 'sudo journalctl -u apex-backend -u apex-frontend' to inspect"
    fi
  else
    warn "systemd units not installed — services were not restarted"
  fi
  if [[ "$had_https" == true ]]; then
    start_https
  fi
}

# ─────────────────────────────────── update ───────────────────────────────────
update_apex() {
  say "${C_BOLD}Pre-flight checks${C_RESET}"
  [[ -d "$ROOT/.git" ]] || die "not a git repository at $ROOT"
  if ! git -C "$ROOT" remote -v | grep -q '(fetch)'; then
    die "no git remote configured — nothing to pull from"
  fi
  local branch
  branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  ok "repo $ROOT on branch '${branch:-?}'"

  # Anything else locally modified (outside tracked data) can block the merge.
  local other_dirty
  other_dirty="$(git -C "$ROOT" status --porcelain -- . 2>/dev/null | grep -v '^ M backend/data/' | grep -v '^. backend/data/' || true)"
  if [[ -n "$other_dirty" ]]; then
    warn "you have uncommitted changes outside backend/data; git may refuse to merge"
  fi

  local had_units=false had_https=false
  units_installed && had_units=true
  https_running && had_https=true

  say "${C_BOLD}Stopping services${C_RESET}"
  if [[ "$had_units" == true ]]; then
    local unit
    for unit in "${SERVICES[@]}"; do
      if service_active "$unit"; then
        spinner "stopping $unit" sudo systemctl stop "$unit" || true
      fi
    done
  else
    warn "systemd units not installed — assuming a dev run; skipping service stop"
  fi
  if [[ "$had_https" == true ]]; then
    stop_https
  fi

  say "${C_BOLD}Backing up live data${C_RESET}"
  local backup_root backup_dir manifest req_before pkg_before req_after pkg_after
  backup_root="$(mktemp -d "${TMPDIR:-/tmp}/apex-update.XXXXXX")"
  backup_dir="$backup_root/data"
  manifest="$backup_root/MANIFEST.txt"
  mkdir -p "$backup_dir"
  if [[ -d "$DATA_DIR" && -n "$(ls -A "$DATA_DIR" 2>/dev/null)" ]]; then
    copy_with_bar "$DATA_DIR" "$backup_dir"
    ok "backed up $(count_files "$DATA_DIR") files (${C_BOLD}$(dir_size "$DATA_DIR")${C_RESET}) → $backup_dir"
  else
    warn "no data found under $DATA_DIR — nothing to back up"
  fi
  {
    printf 'APEX update backup · %s\n' "$(date -Is)"
    printf 'source: %s\n' "$DATA_DIR"
    printf 'branch: %s\n' "${branch:-?}"
    printf 'before: HEAD %s\n' "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || true)"
    printf 'files:  %s\n' "$(count_files "$backup_dir" 2>/dev/null || echo 0)"
    printf 'size:   %s\n' "$(dir_size "$backup_dir" 2>/dev/null || echo 0)"
  } >"$manifest"
  ok "backup manifest written to $manifest"

  # Snapshot dependency files *before* the pull so we can detect changes.
  req_before=$(sha256sum "$ROOT/backend/requirements.txt" 2>/dev/null | cut -d' ' -f1 || true)
  pkg_before=$(sha256sum "$ROOT/frontend/package.json" 2>/dev/null | cut -d' ' -f1 || true)

  say "${C_BOLD}Git pull (keeping local data)${C_RESET}"
  # Local SQLite/Chroma files are tracked in git AND newer than the repo copy.
  # Reset them to HEAD so git can merge cleanly; the real content is safe in
  # the backup and is restored right after the pull.
  type_cmd "git checkout -- backend/data"
  git -C "$ROOT" checkout -- backend/data 2>/dev/null || true
  ok "tracked data files reset to the committed snapshot"

  type_cmd "git pull"
  if ! spinner "pulling latest code via git" git -C "$ROOT" pull; then
    warn "git pull failed — restoring your data and restarting"
    if [[ -d "$backup_dir" && -n "$(ls -A "$backup_dir" 2>/dev/null)" ]]; then
      copy_with_bar "$backup_dir" "$DATA_DIR"
      ok "restored live data from backup"
    fi
    restart_services "$had_units" "$had_https"
    die "update aborted; your data is safe in $backup_root"
  fi
  ok "pulled → HEAD $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)"

  say "${C_BOLD}Restoring local data (yours wins)${C_RESET}"
  if [[ -d "$backup_dir" && -n "$(ls -A "$backup_dir" 2>/dev/null)" ]]; then
    copy_with_bar "$backup_dir" "$DATA_DIR"
    ok "live data restored (apex.db, chroma/, skills/, …)"
  else
    warn "no backup to restore"
  fi

  say "${C_BOLD}Checking dependencies${C_RESET}"
  req_after=$(sha256sum "$ROOT/backend/requirements.txt" 2>/dev/null | cut -d' ' -f1 || true)
  pkg_after=$(sha256sum "$ROOT/frontend/package.json" 2>/dev/null | cut -d' ' -f1 || true)
  if [[ -n "$req_before" && "$req_before" != "$req_after" ]]; then
    if [[ -x "$ROOT/.venv/bin/python" ]]; then
      spinner "installing backend deps (pip)" "$ROOT/.venv/bin/pip" install -r "$ROOT/backend/requirements.txt"
      ok "backend dependencies updated"
    else
      warn "requirements changed but no venv at $ROOT/.venv — install manually"
    fi
  else
    ok "backend dependencies unchanged"
  fi
  if [[ -n "$pkg_before" && "$pkg_before" != "$pkg_after" ]]; then
    if [[ -d "$ROOT/frontend/node_modules" ]]; then
      spinner "installing frontend deps (npm)" npm --prefix "$ROOT/frontend" install
      ok "frontend dependencies updated"
    else
      warn "package.json changed but node_modules absent — run npm install"
    fi
  else
    ok "frontend dependencies unchanged"
  fi

  say "${C_BOLD}Restarting${C_RESET}"
  restart_services "$had_units" "$had_https"

  say "${C_BOLD}Update complete${C_RESET}"
  ok "HEAD is now $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)"
  warn "backup kept at: $backup_root  (delete it once you trust the update)"
  ok "your live data is intact (git shows backend/data as modified — that is expected)"
}

command="${1:-}"
shift || true

banner "$command"

case "$command" in
  install-service)
    type_cmd "./deploy/install-services.sh $*"
    spinner "installing systemd services" "$ROOT/deploy/install-services.sh" "$@"
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
    if [[ "$command" == "start" || "$command" == "restart" ]]; then
      spinner "systemctl ${command} ${SERVICES[*]}" sudo systemctl "$command" "${SERVICES[@]}"
      ok "${command}ed: ${SERVICES[0]} + ${SERVICES[1]}"
    else
      spinner "systemctl stop ${SERVICES[*]}" sudo systemctl stop "${SERVICES[@]}"
      ok "stopped: ${SERVICES[0]} + ${SERVICES[1]}"
    fi
    if [[ "$use_https" == true && "$command" != "stop" ]]; then
      start_https
    fi
    ;;
  status)
    require_units
    [[ "$#" -eq 0 ]] || { usage >&2; exit 2; }
    pulse "probing service status"
    systemctl --no-pager --full status "${SERVICES[@]}" || true
    https_status
    ;;
  update)
    update_apex
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac