#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICES=(apex-backend.service apex-frontend.service)
HTTPS_PID_FILE="$ROOT/.apex/frontend-https.pid"
HTTPS_LOG_FILE="$ROOT/.apex/frontend-https.log"
HTTPS_CERT_DIR="$ROOT/.apex/https"
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
  # If the command needs sudo, authenticate in the FOREGROUND first so the
  # password prompt appears cleanly. sudo caches the credential for a while,
  # so the backgrounded run below won't touch the tty (a backgrounded sudo
  # reading /dev/tty is stopped by SIGTTIN and the spinner would freeze).
  if [[ "$1" == "sudo" && $INTERACTIVE -eq 1 && -t 0 ]]; then
    # Global sudoers may allow specific commands without a password, in which
    # case `sudo -v` would still fail; tolerate that and let the real command run.
    sudo -v 2>/dev/null || true
  fi
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

# Replace dst with an exact copy of src. NOT a sync: the backup was made with
# `cp -a`, so it carries the live file's mtime, and git checks the committed
# file out during the same second — rsync's size+mtime quick check then sees
# "same size, same mtime" and skips the transfer, silently restoring nothing.
# Both SQLite files here are page-aligned, so a 1-row and a 3-row database are
# the same size. Wipe the destination first so the result is byte-exact.
restore_data() {   # <backup dir> <destination>
  local src="$1" dst="$2"
  [[ -d "$src" && -n "$(ls -A "$src" 2>/dev/null)" ]] || { warn "no backup to restore"; return 0; }
  case "$dst" in
    "$ROOT"/*) ;;
    *) die "refusing to restore outside the repo: $dst" ;;
  esac
  [[ "$dst" == */data || "$dst" == */data/ ]] || die "refusing to wipe $dst"
  rm -rf "$dst"
  mkdir -p "$dst"
  copy_with_bar "$src" "$dst"
}

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
  doctor [fix] [--whisper]      Health check the environment and repo; 'fix' installs
                                what is missing (--whisper adds the speech venv)
  update [ref]                  Copy local DBs, override with git (default origin/main), restore, rebuild, restart
                               e.g. ./apex update origin/v_2.0.27   (or set APEX_UPDATE_REF)
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

# Next's --experimental-https regenerates certificates/localhost.pem on every
# start when --hostname is an IP (Node's checkHost() does not match IP SANs) and
# only ever names localhost/127.0.0.1/::1 plus that one hostname. A browser on
# the LAN has no name it can match, refuses to register the service worker, and
# the PWA never offers to install. Issue one cert naming every address this host
# answers on, signed by the mkcert CA (so anything already trusting the CA
# trusts this too), and pass it to Next explicitly. Sets HTTPS_CERT/HTTPS_KEY.
ensure_https_cert() {
  local caroot="${MKCERT_CAROOT:-$HOME/.local/share/mkcert}"
  local key="$HTTPS_CERT_DIR/key.pem" cert="$HTTPS_CERT_DIR/cert.pem"
  local csr="$HTTPS_CERT_DIR/cert.csr" ext="$HTTPS_CERT_DIR/cert.ext"
  local san host ip have missing=0
  [[ -f "$caroot/rootCA.pem" && -f "$caroot/rootCA-key.pem" ]] || return 1
  command -v openssl >/dev/null 2>&1 || return 1
  host="$(hostname 2>/dev/null || true)"
  san="DNS:localhost,IP:127.0.0.1,IP:0.0.0.0"
  [[ -n "$host" ]] && san="DNS:localhost,DNS:$host,IP:127.0.0.1,IP:0.0.0.0"
  for ip in $(hostname -I 2>/dev/null || true); do san="$san,IP:$ip"; done
  mkdir -p "$HTTPS_CERT_DIR"
  # Reuse the current cert when it is unexpired and still covers every address.
  if [[ -f "$key" && -f "$cert" ]] && openssl x509 -in "$cert" -noout -checkend 86400 >/dev/null 2>&1; then
    have="$(openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null || true)"
    for ip in $(hostname -I 2>/dev/null || true) 127.0.0.1 0.0.0.0; do
      [[ "$have" == *"IP Address:$ip"* ]] || missing=1
    done
    [[ -n "$host" && "$have" != *"DNS:$host"* ]] && missing=1
    if [[ $missing -eq 0 ]]; then HTTPS_CERT="$cert"; HTTPS_KEY="$key"; return 0; fi
  fi
  printf 'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' "$san" >"$ext"
  openssl req -new -newkey rsa:2048 -nodes -keyout "$key" -out "$csr" -subj "/CN=${host:-localhost}" >/dev/null 2>&1 || return 1
  openssl x509 -req -in "$csr" -CA "$caroot/rootCA.pem" -CAkey "$caroot/rootCA-key.pem" \
    -set_serial "0x$(openssl rand -hex 8)" -days 825 -sha256 -extfile "$ext" -out "$cert" >/dev/null 2>&1 || return 1
  rm -f "$csr"
  HTTPS_CERT="$cert"; HTTPS_KEY="$key"
  return 0
}

start_https() {
  mkdir -p "$ROOT/.apex"
  if [[ -f "$HTTPS_PID_FILE" ]] && kill -0 "$(<"$HTTPS_PID_FILE")" 2>/dev/null; then
    ok "HTTPS frontend already running on port 3001 (PID $(<"$HTTPS_PID_FILE"))."
    return
  fi
  rm -f "$HTTPS_PID_FILE"
  local https_args=(--experimental-https --hostname 0.0.0.0 --port 3001)
  if ensure_https_cert; then
    https_args+=(--experimental-https-key "$HTTPS_KEY" --experimental-https-cert "$HTTPS_CERT")
  else
    warn "mkcert CA not found under ${MKCERT_CAROOT:-$HOME/.local/share/mkcert}; falling back to Next's localhost-only certificate (LAN access will warn)"
  fi
  (
    cd "$ROOT/frontend"
    NEXT_DIST_DIR=.next-https nohup npm run dev -- "${https_args[@]}" \
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
PYTHON_BIN=""
pick_python() {
  local c
  for c in "$ROOT/.venv/bin/python" "$ROOT/backend/venv/bin/python" "$(command -v python3 2>/dev/null || true)"; do
    if [[ -n "$c" && -x "$c" ]]; then PYTHON_BIN="$c"; return 0; fi
  done
  return 0
}

# Consistent copy of a SQLite database. apex.db and chroma.sqlite3 are tracked in
# git AND are the live databases, and they run in WAL mode: a plain file copy
# taken while a writer is active can be torn, so the databases are re-copied
# through SQLite's own backup API once the services are stopped.
sqlite_backup() {   # <live db> <destination file>
  local src="$1" dst="$2"
  [[ -f "$src" ]] || return 0
  [[ -n "$PYTHON_BIN" ]] || return 0
  "$PYTHON_BIN" - "$src" "$dst" <<'PY' 2>/dev/null || return 1
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close(); s.close()
PY
}

# update_apex [ref] — back up the local databases, override the checkout with
# git, then restore the databases. Default target is origin/main; pass a ref
# (or set APEX_UPDATE_REF) to update to something else, e.g. `./apex update
# origin/v_2.0.27`.
update_apex() {
  local target="${1:-${APEX_UPDATE_REF:-origin/main}}"
  local head_before
  head_before="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"

  say "${C_BOLD}Pre-flight checks${C_RESET}"
  [[ -d "$ROOT/.git" ]] || die "not a git repository at $ROOT"
  git -C "$ROOT" remote get-url origin >/dev/null 2>&1 || die "no 'origin' remote configured"
  local branch
  branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  ok "repo $ROOT on branch '${branch:-detached}' @ ${head_before:0:7}"

  if [[ -n "$branch" && "$branch" != "HEAD" && "$branch" != "main" ]]; then
    say "  ${C_DIM}target: ${target} · your '${branch}' branch will not be moved${C_RESET}"
  fi

  say "${C_BOLD}Fetching ${C_RESET}"
  spinner "fetching origin" git -C "$ROOT" fetch --prune origin || die "fetch failed"
  if ! git -C "$ROOT" rev-parse --verify --quiet "$target" >/dev/null 2>&1; then
    die "'$target' does not exist — run './apex update origin/main' or pick a ref from 'git branch -r'"
  fi
  local head_target
  head_target="$(git -C "$ROOT" rev-parse --short "$target" 2>/dev/null || true)"
  if [[ -n "$head_before" && "$head_before" == "$(git -C "$ROOT" rev-parse "$target" 2>/dev/null || true)" ]]; then
    ok "already up to date with $target (${head_target})"
  else
    ok "$target is now at ${head_target}"
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
  if [[ "$had_https" == true ]]; then stop_https; fi

  # ── back up everything local that must survive the override ──
  say "${C_BOLD}Copying local databases${C_RESET}"
  pick_python
  local backup_root backup_dir manifest env_backup
  backup_root="$(mktemp -d "${TMPDIR:-/tmp}/apex-update.XXXXXX")"
  backup_dir="$backup_root/data"
  manifest="$backup_root/MANIFEST.txt"
  env_backup="$backup_root/backend.env"
  mkdir -p "$backup_dir"

  if [[ -d "$DATA_DIR" && -n "$(ls -A "$DATA_DIR" 2>/dev/null)" ]]; then
    copy_with_bar "$DATA_DIR" "$backup_dir"
    ok "copied $(count_files "$DATA_DIR") files (${C_BOLD}$(dir_size "$DATA_DIR")${C_RESET}) → $backup_dir"
  else
    warn "no data found under $DATA_DIR — nothing to copy"
  fi

  # Re-copy each SQLite database through the backup API so the copy is a
  # consistent snapshot rather than a byte-for-byte copy of a live WAL file.
  local db
  while IFS= read -r db; do
    local rel="${db#"$DATA_DIR"/}"
    if sqlite_backup "$db" "$backup_dir/$rel"; then
      ok "database ${C_BOLD}$rel${C_RESET} snapshotted consistently"
    else
      warn "could not snapshot $rel via SQLite API — using the file copy"
    fi
  done < <(find "$DATA_DIR" -maxdepth 3 -type f \( -name '*.db' -o -name '*.sqlite3' \) 2>/dev/null)

  if [[ -f "$ROOT/backend/.env" ]]; then
    cp -a "$ROOT/backend/.env" "$env_backup" 2>/dev/null || true
    ok "secrets ${C_BOLD}backend/.env${C_RESET} copied"
  fi

  # Safety net: the override discards uncommitted code edits, so save them as a
  # patch the user can re-apply with `git apply` instead of losing them.
  local patch="$backup_root/local-changes.patch"
  if ! git -C "$ROOT" diff --quiet HEAD -- . 2>/dev/null; then
    git -C "$ROOT" diff HEAD -- . ":(exclude)backend/data" >"$patch" 2>/dev/null || true
    if [[ -s "$patch" ]]; then
      warn "uncommitted code edits saved to ${C_BOLD}$patch${C_RESET} — they will be overridden"
    fi
  fi

  {
    printf 'APEX update backup · %s\n' "$(date -Is)"
    printf 'source:     %s\n' "$DATA_DIR"
    printf 'branch:     %s\n' "${branch:-detached}"
    printf 'before:     HEAD %s\n' "${head_before:0:7}"
    printf 'target:     %s (%s)\n' "$target" "${head_target:-?}"
    printf 'databases:  %s\n' "$(find "$backup_dir" -maxdepth 3 -type f \( -name '*.db' -o -name '*.sqlite3' \) 2>/dev/null | sed "s#^$backup_dir/##" | tr '\n' ' ')"
    printf 'files:      %s\n' "$(count_files "$backup_dir" 2>/dev/null || echo 0)"
    printf 'size:       %s\n' "$(dir_size "$backup_dir" 2>/dev/null || echo 0)"
    printf 'env copy:   %s\n' "$([[ -f "$env_backup" ]] && echo yes || echo no)"
    printf 'code patch: %s\n' "$([[ -s "$patch" ]] && echo "$patch" || echo none)"
  } >"$manifest"
  ok "manifest written to $manifest"

  # ── override the checkout with git ──
  say "${C_BOLD}Updating from git (override)${C_RESET}"
  type_cmd "git reset --hard HEAD   # discard local edits (saved above)"
  git -C "$ROOT" reset --hard HEAD >/dev/null 2>&1 || true
  type_cmd "git checkout --detach $target"
  if ! git -C "$ROOT" checkout --detach "$target"; then
    warn "git could not switch to $target"
    restore_data "$backup_dir" "$DATA_DIR"
    ok "restored local databases from the backup"
    restart_services "$had_units" "$had_https"
    die "update aborted; your databases are safe in $backup_root"
  fi
  local head_after
  head_after="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
  ok "working tree is now ${head_after:0:7} (${target}); branch '${branch:-HEAD}' left untouched"

  # ── restore the local databases over the git snapshot ──
  say "${C_BOLD}Restoring local databases (yours wins)${C_RESET}"
  restore_data "$backup_dir" "$DATA_DIR"
  ok "restored (apex.db, chroma/, skills/, generated/, …)"
  if [[ -f "$env_backup" && ! -f "$ROOT/backend/.env" ]]; then
    cp -a "$env_backup" "$ROOT/backend/.env" 2>/dev/null || true
    ok "restored backend/.env"
  fi

  # ── dependencies ──
  say "${C_BOLD}Checking dependencies${C_RESET}"
  if [[ -f "$ROOT/backend/requirements.txt" && -x "$ROOT/.venv/bin/python" ]]; then
    spinner "installing backend deps (pip)" "$ROOT/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt" \
      && ok "backend dependencies up to date" \
      || warn "pip install failed — check the backend service"
  fi
  # The SQL Server ODBC library is a system package pip cannot install. Provision
  # it into the user-owned INTEGRATIONS_ODBC_DIR (no root) so the MCP tab's SQL
  # Server kind works without asking the operator to run apt.
  if [[ -x "$ROOT/.venv/bin/python" && -f "$ROOT/scripts/install_sql_drivers.py" ]]; then
    spinner "provisioning the SQL Server ODBC driver" \
      "$ROOT/.venv/bin/python" "$ROOT/scripts/install_sql_drivers.py" \
      && ok "SQL Server ODBC driver ready" \
      || warn "SQL Server ODBC driver not provisioned (MCP servers still work)"
  fi
  if [[ -d "$ROOT/frontend/node_modules" ]]; then
    spinner "installing frontend deps (npm)" npm --prefix "$ROOT/frontend" install --silent \
      && ok "frontend dependencies up to date" \
      || warn "npm install failed — check the frontend service"
  else
    warn "no frontend/node_modules — run npm install in frontend/"
  fi

  # The frontend service runs `npm start`, which serves .next. Without a build
  # the new code would never reach the browser, so rebuild whenever the
  # frontend changed (or .next is missing).
  local fe_changed=true
  if [[ -n "$head_before" && -n "$head_after" ]] && git -C "$ROOT" diff --quiet "$head_before" "$head_after" -- frontend/ 2>/dev/null; then
    fe_changed=false
  fi
  [[ -d "$ROOT/frontend/.next" ]] || fe_changed=true
  if [[ "$fe_changed" == true && -d "$ROOT/frontend/node_modules" ]]; then
    spinner "building the frontend (npm run build)" npm --prefix "$ROOT/frontend" run build \
      && ok "frontend production build refreshed" \
      || warn "npm run build failed — the frontend will serve a stale build"
  else
    ok "frontend source unchanged — keeping the existing build"
  fi

  say "${C_BOLD}Restarting${C_RESET}"
  restart_services "$had_units" "$had_https"

  say "${C_BOLD}Update complete${C_RESET}"
  ok "HEAD is now ${head_after:0:7} ($(git -C "$ROOT" log -1 --format=%s "$head_after" 2>/dev/null || echo '?'))"
  if [[ -s "$patch" ]]; then
    warn "your uncommitted edits are in: $patch"
    warn "  it is a diff against the pre-update commit ${head_before:0:7} — after a jump"
    warn "  across many commits use 'git apply -3 $patch' and resolve any conflicts"
  fi
  warn "backup kept at: $backup_root  (delete it once you trust the update)"
  ok "your databases are intact (git shows backend/data as modified — that is expected)"
}

# ─────────────────────────────── doctor ───────────────────────────────────
# `./apex doctor`     — one-shot health check of the environment and this repo.
# `./apex doctor fix` — check, then install/provision whatever is missing.
# `./apex doctor fix --whisper` — also build the optional speech-recognition venv.
#
# The check is deliberately split in two: a small "core" set whose absence stops
# APEX from running (and drives the exit code), and a "feature" set whose absence
# only disables a capability (ffmpeg → camera, baresip → phone calls, …). A
# health check that failed on a missing nmap would be useless.
DOCTOR_FIX=0
DOCTOR_WHISPER=0
DOCTOR_CORE_FAIL=0
DOCTOR_OPT_WARN=0
DOCTOR_APT=()

have() { command -v "$1" >/dev/null 2>&1; }

doc_core() {  # <label> <okflag 0=ok> [hint]
  local label="$1" okflag="$2" hint="${3:-}"
  if [[ "$okflag" -eq 0 ]]; then
    ok "$label"
  else
    printf '%s\n' "${C_RED}  ✗${C_RESET} $label"
    [[ -n "$hint" ]] && printf '%s\n' "${C_DIM}      $hint${C_RESET}"
    DOCTOR_CORE_FAIL=1
  fi
}

doc_opt() {  # <label> <okflag 0=ok> [apt_pkg] [hint]
  local label="$1" okflag="$2" pkg="${3:-}" hint="${4:-}"
  if [[ "$okflag" -eq 0 ]]; then
    ok "$label"
  else
    printf '%s\n' "${C_YELLOW}  ⚠${C_RESET} $label"
    [[ -n "$hint" ]] && printf '%s\n' "${C_DIM}      $hint${C_RESET}"
    DOCTOR_OPT_WARN=1
    [[ -n "$pkg" ]] && DOCTOR_APT+=("$pkg")
  fi
}

# The checks, separated from the driver so `doctor fix` can re-run them cleanly.
doctor_checks() {
  local pyver py_major py_minor node_major missing_mods mkcert_caroot exploitdb_dir
  DOCTOR_CORE_FAIL=0
  DOCTOR_OPT_WARN=0
  DOCTOR_APT=()

  say "${C_BOLD}Core${C_RESET}"
  if have git; then
    doc_core "git" 0
  else
    doc_core "git" 1 "sudo apt-get install -y git"
  fi

  if have python3; then
    pyver="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
    py_major="${pyver%%.*}"
    py_minor="${pyver#*.}"
    if [[ "${py_major:-0}" -gt 3 || ( "${py_major:-0}" -eq 3 && "${py_minor:-0}" -ge 10 ) ]]; then
      doc_core "python3 (${pyver})" 0
    else
      doc_core "python3 >= 3.10 (found ${pyver})" 1 "install Python 3.10+ and recreate .venv"
    fi
  else
    doc_core "python3" 1 "sudo apt-get install -y python3 python3-venv"
  fi

  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    doc_core "Python virtualenv (.venv)" 0
    missing_mods="$("$ROOT/.venv/bin/python" - <<'PY' 2>/dev/null || echo __ERR__
import importlib.util as u
mods = ["flask","openai","chromadb","gunicorn","docx","openpyxl","pptx",
        "matplotlib","edge_tts","mcp","jwt","dotenv","PyPDF2","ddgs",
        "agents","langgraph","langchain_openai","psycopg2","pymysql",
        "pyodbc","oracledb"]
print(" ".join(m for m in mods if u.find_spec(m) is None))
PY
)"
    if [[ "$missing_mods" == "__ERR__" ]]; then
      doc_core "backend Python packages" 1 "run: .venv/bin/pip install -r backend/requirements.txt"
    elif [[ -z "$missing_mods" ]]; then
      doc_core "backend Python packages" 0
    else
      doc_core "backend Python packages (missing: ${missing_mods})" 1 "run: .venv/bin/pip install -r backend/requirements.txt"
    fi
  else
    doc_core "Python virtualenv (.venv)" 1 "create: python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt"
  fi

  if have node; then
    node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    if [[ "${node_major:-0}" -ge 18 ]]; then
      doc_core "node (v${node_major})" 0
    else
      doc_core "node >= 18 (found v${node_major})" 1 "install Node 18.18+ (nvm, or NodeSource)"
    fi
  else
    doc_core "node" 1 "install Node 18.18+ (nvm, or NodeSource)"
  fi
  if have npm; then doc_core "npm" 0; else doc_core "npm" 1 "npm ships with Node"; fi

  if [[ -d "$ROOT/frontend/node_modules" ]]; then
    doc_core "frontend node_modules" 0
  else
    doc_core "frontend node_modules" 1 "run: npm --prefix frontend install"
  fi

  if [[ -f "$ROOT/backend/.env" ]]; then
    doc_core "backend/.env" 0
  else
    doc_core "backend/.env (provider keys)" 1 "cp backend/.env.example backend/.env (or ./apex install-service)"
  fi

  say "${C_BOLD}Feature dependencies${C_RESET}"
  if have ffmpeg; then doc_opt "ffmpeg — camera (VISIO) + voice notes" 0; else doc_opt "ffmpeg — camera (VISIO) + voice notes" 1 ffmpeg "sudo apt-get install -y ffmpeg"; fi
  if have espeak-ng || have espeak; then doc_opt "espeak — SIP text-to-speech fallback" 0; else doc_opt "espeak — SIP text-to-speech fallback" 1 espeak-ng "sudo apt-get install -y espeak-ng"; fi
  if have pactl && have paplay && have parec; then doc_opt "PulseAudio tools — SIP call audio" 0; else doc_opt "PulseAudio tools — SIP call audio (pactl/paplay/parec)" 1 pulseaudio-utils "sudo apt-get install -y pulseaudio-utils"; fi
  if have baresip; then doc_opt "baresip — phone calls" 0; else doc_opt "baresip — phone calls" 1 baresip "sudo apt-get install -y baresip"; fi
  mkcert_caroot="${MKCERT_CAROOT:-$HOME/.local/share/mkcert}"
  if have openssl && [[ -f "$mkcert_caroot/rootCA.pem" ]]; then
    doc_opt "HTTPS certs — openssl + mkcert CA (PWA install on LAN)" 0
  else
    doc_opt "HTTPS certs — openssl + mkcert CA (PWA install on LAN)" 1 openssl "install mkcert then: mkcert -install  (or set MKCERT_CAROOT)"
  fi
  # `ldconfig -p | grep -q` is a trap under `set -o pipefail`: grep closes the
  # pipe on the first match, ldconfig dies of SIGPIPE (141), and the pipeline
  # reports failure even though the library was found. Capture, then match.
  local ld_cache
  ld_cache="$(ldconfig -p 2>/dev/null || true)"
  if [[ "$ld_cache" == *"libpam.so"* ]]; then
    doc_opt "PAM — machine sign-in" 0
  else
    doc_opt "PAM — machine sign-in (libpam)" 1 libpam0g "sudo apt-get install -y libpam0g"
  fi
  if have rsync; then doc_opt "rsync — faster ./apex update" 0; else doc_opt "rsync — faster ./apex update (falls back to cp)" 1 rsync; fi
  if have nmap; then doc_opt "nmap — VAPT scanning" 0; else doc_opt "nmap — VAPT scanning" 1 nmap; fi

  exploitdb_dir="${EXPLOITDB_DIR:-$ROOT/backend/data/exploitdb}"
  if [[ -f "$exploitdb_dir/searchsploit" && -f "$exploitdb_dir/files_exploits.csv" ]]; then
    doc_opt "Exploit-DB mirror — VAPT exploit intelligence" 0
  else
    doc_opt "Exploit-DB mirror — VAPT exploit intelligence" 1 "" "clones automatically on first use, or: ./apex doctor fix"
  fi
  if [[ -x "$ROOT/.venv/sip-whisper/bin/python" ]]; then
    doc_opt "speech-recognition venv — voice transcription" 0
  else
    doc_opt "speech-recognition venv — voice transcription (optional)" 1 "" "provision with: ./apex doctor fix --whisper"
  fi

  printf '\n'
  if [[ "$DOCTOR_CORE_FAIL" -eq 0 ]]; then
    ok "${C_BOLD}Core is healthy.${C_RESET}"
  else
    printf '%s\n' "${C_RED}  ✗${C_RESET} ${C_BOLD}Core problems found.${C_RESET} Run: ./apex doctor fix"
  fi
  [[ "$DOCTOR_OPT_WARN" -ne 0 ]] && warn "Some optional features are unavailable (marked ⚠ above)."
  return "$DOCTOR_CORE_FAIL"
}

doctor() {
  local fix="$DOCTOR_FIX" exploitdb_dir exploitdb_url
  say "${C_BOLD}APEX health check${C_RESET}  ${C_DIM}$ROOT${C_RESET}"
  doctor_checks || true

  if [[ "$fix" -eq 1 ]]; then
    printf '\n'
    say "${C_BOLD}Repairing${C_RESET}"

    if [[ ! -x "$ROOT/.venv/bin/python" ]] && have python3; then
      spinner "creating the Python virtualenv" python3 -m venv "$ROOT/.venv" \
        && ok "created .venv" || warn "could not create .venv"
    fi
    if [[ -x "$ROOT/.venv/bin/python" ]]; then
      spinner "installing backend Python packages" \
        "$ROOT/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt" \
        && ok "backend Python packages ready" || warn "pip install failed — check the output above"
      if [[ -f "$ROOT/scripts/install_sql_drivers.py" ]]; then
        spinner "provisioning the SQL Server ODBC driver" \
          "$ROOT/.venv/bin/python" "$ROOT/scripts/install_sql_drivers.py" \
          && ok "SQL Server ODBC driver ready" \
          || warn "SQL Server ODBC driver not provisioned (MCP servers still work)"
      fi
    fi

    if have npm && [[ ! -d "$ROOT/frontend/node_modules" ]]; then
      spinner "installing frontend packages (npm)" npm --prefix "$ROOT/frontend" install --silent \
        && ok "frontend packages ready" || warn "npm install failed"
    fi

    if [[ ! -f "$ROOT/backend/.env" && -f "$ROOT/backend/.env.example" ]]; then
      cp "$ROOT/backend/.env.example" "$ROOT/backend/.env" \
        && ok "created backend/.env from example — edit your provider keys"
    fi

    exploitdb_dir="${EXPLOITDB_DIR:-$ROOT/backend/data/exploitdb}"
    if [[ ! -f "$exploitdb_dir/searchsploit" ]]; then
      exploitdb_url="${EXPLOITDB_REPO_URL:-https://gitlab.com/exploit-database/exploitdb.git}"
      if have git; then
        mkdir -p "$(dirname "$exploitdb_dir")"
        spinner "cloning the Exploit-DB mirror" git clone --depth 1 "$exploitdb_url" "$exploitdb_dir" \
          && ok "Exploit-DB mirror ready" || warn "could not clone the Exploit-DB mirror"
      else
        warn "git is required to clone the Exploit-DB mirror"
      fi
    fi

    if [[ "$DOCTOR_WHISPER" -eq 1 && ! -x "$ROOT/.venv/sip-whisper/bin/python" ]]; then
      if have python3; then
        spinner "creating the speech-recognition venv" python3 -m venv "$ROOT/.venv/sip-whisper" \
          && spinner "installing faster-whisper" \
               "$ROOT/.venv/sip-whisper/bin/pip" install -q -r "$ROOT/backend/tools/sip-whisper-requirements.txt" \
          && ok "speech-recognition venv ready" \
          || warn "speech recognition not provisioned (SIP/Telegram voice transcription stays off)"
      fi
    fi

    if [[ "${#DOCTOR_APT[@]}" -gt 0 ]]; then
      local pkg
      mapfile -t DOCTOR_APT < <(printf '%s\n' "${DOCTOR_APT[@]}" | sort -u)
      if have apt-get; then
        spinner "installing system packages (${DOCTOR_APT[*]})" \
          sudo apt-get install -y "${DOCTOR_APT[@]}" \
          && ok "system packages installed" \
          || warn "apt-get install failed — install manually: ${DOCTOR_APT[*]}"
      else
        warn "no apt-get on this system — install manually: ${DOCTOR_APT[*]}"
      fi
    fi

    printf '\n'
    say "${C_BOLD}Re-checking${C_RESET}"
    doctor_checks || true
  fi

  return "$DOCTOR_CORE_FAIL"
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
  doctor)
    for arg in "$@"; do
      case "$arg" in
        fix) DOCTOR_FIX=1 ;;
        --whisper) DOCTOR_WHISPER=1 ;;
        *) usage >&2; exit 2 ;;
      esac
    done
    doctor
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac