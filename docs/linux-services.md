# APEX — Linux systemd services

Two services run the whole assistant on a Linux box:

```
┌────────────────────────────────────────────  :3000  ─┐
│  apex-frontend.service  Next.js (production)           │
│    UI, voice engine, orb                              │
│    proxies /be/api/* ─────────────────────────────┐   │
└────────────────────────────────────────────────────┘   │
                                        │                │
                                        ▼                │
┌────────────────────────────  :5001  ──┴─────────────────┐
│  apex-backend.service   gunicorn (Flask app.py)          │
│    car, settings, tools, skills, ChromaDB memory        │
│    ├─ OpenAI  :443   (api.openai.com)                │
│    ├─ Kimi    :443   (api.moonshot.ai)               │
│    ├─ Ollama  :11434 (local, optional)               │
│    └─ local   torch  (transformers, optional)        │
└─────────────────────────────────────────────────────────┘
```

| File | What it does |
|------|--------------|
| `deploy/systemd/apex-backend.service`  | Flask API on `127.0.0.1:5001` |
| `deploy/systemd/apex-frontend.service` | Next.js UI  on `127.0.0.1:3000` |
| `deploy/install-services.sh`           | build + install + enable + start |

Both units are self-restarting (`Restart=on-failure` / `Restart=always`) and log
to the journal. Nothing in the units contains secrets — configuration lives in
`backend/.env`, which the backend already loads at boot.

---

## 1. Prerequisites

| Thing | Check |
|-------|-------|
| Python venv | `.venv/bin/python` exists in the repo root |
| Backend deps | `gunicorn` inside the venv (`requirements.txt`) |
| Node.js ≥ 18 | `node -v`, `npm -v` |
| Frontend deps | `frontend/node_modules` (installed by the script) |
| Ollama (optional) | `systemctl status ollama` when using the `ollama` provider |
| OAuth app (optional) | `OPENAI_CLIENT_ID` / `SECRET` registered at platform.openai.com — see [README §3](../README.md) |

The script does every check for you and bails with a message if something is
missing.

## 2. Install (one command)

```bash
cd /home/ioannisb/Development/Apex
./deploy/install-services.sh
```

What it runs, in order:

1. sanity-checks the venv, gunicorn, npm and `node_modules`;
2. `npm run build` in `frontend/` (production bundle),
3. creates `backend/.env` from `backend/.env.example` the first time,
4. writes the two units into `/etc/systemd/system/` (asks for your sudo password),
   substituting the repo root, the current user/group, and the real binary paths,
5. `systemctl daemon-reload`,
6. `systemctl enable --now apex-backend apex-frontend`.

Skip the (slow) frontend rebuild on a re-run with:

```bash
./deploy/install-services.sh --no-build
```

## 2a. Development HTTPS (microphone testing)

For local development, run the backend in one terminal:

```bash
cd /home/ioannisb/Development/Apex
.venv/bin/python backend/app.py
```

Run the frontend in another terminal. Use HTTPS when testing the microphone
from another host or when the browser rejects an insecure-context page:

```bash
cd /home/ioannisb/Development/Apex/frontend
npm run dev -- --experimental-https
```

Open the `https://localhost:3000` URL printed by Next.js and allow microphone
access. Wake-word recognition uses the browser Web Speech API, so Chrome or
Edge is required; Firefox does not provide this API. A browser `network`
recognition error means the browser's remote speech service could not be
reached and is unrelated to the Apex backend.

## 3. Manual install (everything the script does, by hand)

```bash
cd /home/ioannisb/Development/Apex

# 1. backend deps
../.venv/bin/pip --version >/dev/null || python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

# 2. frontend build
cd frontend && npm install && npm run build && cd ..

# 3. config
[ -f backend/.env ] || cp backend/.env.example backend/.env

# 4. unit files (templating replaces the four __PLACEHOLDERS__)
ROOT="$(pwd)"; USER="$(id -un)"; GROUP="$(id -gn)"
GUN="$ROOT/.venv/bin/gunicorn"; NPM="$(command -v npm)"
sed -e "s|__ROOT__|$ROOT|g" -e "s|__USER__|$USER|g" -e "s|__GROUP__|$GROUP|g" \
    -e "s|__GUNBIN__|$GUN|g" -e "s|__NPM__|$NPM|g" \
    deploy/systemd/apex-backend.service  | sudo tee /etc/systemd/system/apex-backend.service
sed -e "s|__ROOT__|$ROOT|g" -e "s|__USER__|$USER|g" -e "s|__GROUP__|$GROUP|g" \
    -e "s|__GUNBIN__|$GUN|g" -e "s|__NPM__|$NPM|g" \
    deploy/systemd/apex-frontend.service | sudo tee /etc/systemd/system/apex-frontend.service

# 5. enable + start
sudo systemctl daemon-reload
sudo systemctl enable --now apex-backend apex-frontend
```

## 4. Configuration

### Provider setup wizard

Use the interactive wizard from the repository root to configure Codex or
another provider:

```bash
cd /home/ioannisb/Development/Apex
.venv/bin/python backend/setup_provider.py
```

For Codex, choose **ChatGPT subscription (Codex browser login)** and complete
the browser login. The wizard saves credentials under `backend/data/codex`
and writes provider defaults to `backend/.env`. Restart the backend after a
successful setup:

```bash
sudo systemctl restart apex-backend
```

When the wizard writes the main `backend/.env`, it clears stale UI overrides
for provider, engine, and model so the new setup controls the next startup.
The optional `--env /path/to/.env` mode writes another file and does not alter
the main runtime configuration automatically.

### `backend/.env` (main)
Loaded automatically at boot. The important ones:

```bash
SECRET_KEY=…                  # sign your own when going public
OPENAI_CLIENT_ID=…            # enables the SIGN IN WITH OPENAI overlay
OPENAI_CLIENT_SECRET=…
USE_OAUTH_ACCESS_KEY=true     # + CHATGPT_MODEL=gpt-5-codex for Codex
OPENAI_API_KEY=…              # or a server key when not using per-user tokens
KIMI_API_KEY=…                # for the Kimi (Moonshot) provider
PROVIDER_DEFAULT=openai       # openai | ollama | kimi | torch
AGENT_ENGINE=responses        # responses | agents_sdk | langgraph
WAKE_WORD=apex
FOLLOW_UP_SECONDS=30
MEMORY_ENABLED=true
DEV_MODE=true                 # local-only: auto sign-in, no OAuth
```

### `/etc/apex/backend.env` (optional override)
The backend unit reads this file if you create it (`EnvironmentFile=-…`). It is
handy for machine-level settings you do not want inside the repo:

```bash
# /etc/apex/backend.env - overrides backend/.env on boot
SECRET_KEY=super-secret-from-the-root-secret-manager
DEV_MODE=true
```

Both are plain `KEY=VALUE` files; there is no required key format difference
between them.

### Firewall / ports
- `apex-backend` binds `127.0.0.1:5001` (loopback only).
- `apex-frontend` binds `127.0.0.1:3000` (loopback only).

For access from other machines, either change the service to `0.0.0.0` and put
`FRONTEND_URL`/CORS accordingly, or (recommended) put a reverse proxy in front:

```nginx
# nginx example: HTTPS terminates here, proxies the UI and backend
server {
  listen 443 ssl;
  server_name apex.example.com;
  location / { proxy_pass http://127.0.0.1:3000; }
}
```

## 5. Everyday operations

The repository includes a shortcut for these operations:

```bash
./apex install-service --no-build
./apex start
./apex stop
./apex restart
./apex status
```

For remote microphone testing, `start` and `restart` launch the HTTPS
development frontend alongside the systemd services:

```bash
./apex start
./apex restart
```

It listens on port `3001`; open `https://192.168.1.218:3001` and accept the
local certificate. Stop it with `./apex stop`. Its output is stored in
`.apex/frontend-https.log`; it uses a separate `.next-https` build directory
so it does not interfere with the production frontend. Use
`./apex start --no-https` to disable it.

Use `./apex install-service` to rebuild the frontend before installing the
units. The lifecycle commands require the services to have been installed;
they use `sudo` for start, stop, and restart.

```bash
systemctl status  apex-backend apex-frontend   # are they running?
systemctl restart apex-backend                 # apply backend/.env changes
systemctl restart apex-frontend                # apply UI changes (after rebuild)
systemctl stop/start apex-backend apex-frontend
systemctl disable apex-backend apex-frontend   # stop autostart on boot
```

### Logs (journald)

```bash
journalctl -u apex-backend  -f                 # follow backend logs + gunicorn access log
journalctl -u apex-backend  -n 200             # last 200 lines
journalctl -u apex-frontend -f                 # follow UI logs
journalctl -u apex-backend  --since "10 min ago"
```

A healthy boot looks like:

```
apex-backend[1234]: Booting worker with pid: 1234
apex-backend[1234]: 127.0.0.1 - - "GET /api/health HTTP/1.1" 200
apex-frontend[1234]: ✓ Ready in 2.3s
```

> The backend runs **one** gunicorn worker (16 threads) on purpose: cross-request
> in-memory state like the live PTY terminal sessions must live in a single
> process, so every request (terminal create/drain/input and the model's
> `terminal_command`) can always find the session it opened. Do not raise
> `--workers` above 1 or terminal windows will open and immediately die (or
> Apex will report that no terminal is open).

### Health checks

```bash
curl -s http://127.0.0.1:5001/api/health            # {"ok":true,"engines":[...]}
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000/   # 200
curl -s http://localhost:3000/be/api/health         # via the UI proxy
```

## 6. Updating to a new revision

```bash
cd /home/ioannisb/Development/Apex
git pull
.venv/bin/pip install -r backend/requirements.txt   # if backend deps changed
./deploy/install-services.sh --no-build             # reinstall units, restart
# (omit --no-build to also rebuild the frontend)
```

Or just:

```bash
cd frontend && npm run build
sudo systemctl restart apex-frontend
cd ../backend && sudo systemctl restart apex-backend
```

## 7. Backups

Everything persistent lives under `backend/data/`:

```
backend/data/
  apex.db       users, conversations, messages, settings
  chroma/       memory vectors (one collection per user)
```

```bash
# stop for a clean snapshot, or use sqlite .backup for the db only
sudo systemctl stop apex-backend
tar -czf apex-backup-$(date +%F).tgz backend/data
sudo systemctl start apex-backend
```

## 8. Troubleshooting

| Symptom | Check |
|---------|-------|
| `Failed to start … Address already in use` | something already runs on :5001/:3000 — `fuser -k 5001/tcp` |
| UI shows login overlay although dev mode is on | `OPENAI_CLIENT_ID` is set → OAuth mode wins; remove it to re-enable dev auto-login |
| Chat errors `OpenAI provider … no API key` | the settings panel still points at `openai`; add a key or pick `ollama`/`kimi` |
| Chat streams `meta` then nothing (Ollama) | slow CPU/IP turn — check `journalctl -u apex-backend`, try `ollama pull` a smaller model |
| 401 `Incorrect API key provided: not-needed` | `openai` selected with no key — see the row above (fixed backend rejects it cleanly now) |
| 402 on sign-in | ChatGPT Plus/Pro required for `gpt-5-codex` via the user token |
| `Cannot find module './548.js'` | stale `.next` — `cd frontend && rm -rf .next && npm run build`, then restart the UI |
| Services up but page slow | machine under memory pressure/swap (this box) — close apps or add swap-free RAM |
| Want a different port | edit the `--bind` / `--port` in the unit, `sudo systemctl daemon-reload` + restart |

## 9. Uninstall

```bash
sudo systemctl disable --now apex-backend apex-frontend
sudo rm /etc/systemd/system/apex-backend.service /etc/systemd/system/apex-frontend.service
sudo systemctl daemon-reload
```