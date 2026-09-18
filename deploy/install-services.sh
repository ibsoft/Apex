#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Install/update the APEX systemd services.
#
#   ./deploy/install-services.sh            install + build + enable + start
#   ./deploy/install-services.sh --no-build skip the frontend production build
#
# What it does:
#   1. sanity-checks the venv / node_modules / npm binary
#   2. builds the Next.js frontend for production (`npm run build`)
#   3. creates backend/.env from .env.example if it does not exist
#   4. templates deploy/systemd/*.service and installs them into
#      /etc/systemd/system (via sudo)
#   5. systemctl daemon-reload, enable --now apex-backend apex-frontend
#
# Servers:
#   backend  http://127.0.0.1:5001   (gunicorn,   talks to nobody else)
#   frontend http://127.0.0.1:3000   (next start, proxies /be/api -> :5001)
#
# Secrets stay in backend/.env (or /etc/apex/backend.env); they are never
# written here.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSD="/etc/systemd/system"
USER="$(id -un)"
GROUP="$(id -gn)"
PY_BIN="$ROOT/.venv/bin/python"
GUN_BIN="$ROOT/.venv/bin/gunicorn"
NPM_BIN="$(command -v npm || true)"

DO_BUILD=1
[ "${1:-}" = "--no-build" ] && DO_BUILD=0

say()  { printf '\033[1;36m[apex]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[apex] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- sanity checks ----------------------------------------------------------
[ -d "$ROOT/backend" ] || die "no backend dir at $ROOT/backend"
[ -x "$PY_BIN" ] || die "python venv not found at $PY_BIN (create it: python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt)"
[ -x "$GUN_BIN" ] || die "gunicorn missing in venv ($GUN_BIN) - run: .venv/bin/pip install -r backend/requirements.txt"
[ -n "$NPM_BIN" ] || die "npm not found on PATH"
[ -d "$ROOT/frontend" ] || die "no frontend dir at $ROOT/frontend"
[ -d "$ROOT/frontend/node_modules" ] || { say "installing frontend deps..."; (cd "$ROOT/frontend" && npm install) || die "npm install failed"; }

# ---- frontend production build ----------------------------------------------
if [ "$DO_BUILD" = "1" ]; then
  say "building frontend (npm run build) ..."
  (cd "$ROOT/frontend" && npm run build) || die "frontend build failed"
fi

# ---- backend .env -----------------------------------------------------------
if [ ! -f "$ROOT/backend/.env" ]; then
  cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
  say "created backend/.env from example - EDIT IT (keys, OAuth, DEV_MODE)"
fi

# ---- install unit files ------------------------------------------------------
for s in apex-backend apex-frontend; do
  src="$ROOT/deploy/systemd/$s.service"
  sed -e "s|__ROOT__|$ROOT|g" \
      -e "s|__USER__|$USER|g" \
      -e "s|__GROUP__|$GROUP|g" \
      -e "s|__GUNBIN__|$GUN_BIN|g" \
      -e "s|__NPM__|$NPM_BIN|g" \
      "$src" | sudo tee "$SYSD/$s.service" >/dev/null
  say "installed $SYSD/$s.service"
done

# ---- enable + start ---------------------------------------------------------
sudo systemctl daemon-reload
sudo systemctl enable --now apex-backend.service apex-frontend.service
sleep 2

say "status:"
systemctl --no-pager --lines 0 status apex-backend.service apex-frontend.service || true
say "done. Open http://localhost:3000"
say "useful: journalctl -u apex-backend -f    |    journalctl -u apex-frontend -f"
say "docs:   $ROOT/docs/linux-services.md"