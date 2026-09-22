# APEX — AI co-worker with real-time voice

A self-hosted AI assistant with **ChatGPT subscription login through Codex**, an
**always-on voice mode** (say **"Apex"** and talk to it — no push-to-talk), a
tool-using agent core with skills and long-term vector memory, and full support
for **local models** (Ollama / GPU) — all configurable at runtime from the UI.

The assistant can also search the public web for text, images and news, upload
and recall documents, and read or edit a local **Obsidian** vault.

```
frontend/   Next.js 15 (React 19) — chat panel, settings, memory, voice, orb
backend/    Flask API — OAuth, agents, tools, skills, ChromaDB memory
```

## Table of contents

- [Features](#features)
- [Quick start](#quick-start)
  - [Backend](#backend)
  - [Frontend](#frontend)
  - [ChatGPT subscription via Codex](#chatgpt-subscription--browser-login-through-codex)
- [Using the assistant](#using-the-assistant)
  - [Skills](#skills)
  - [Agent orb](#agent-orb)
  - [Chat rendering](#chat-rendering)
  - [Voice mode](#voice-mode)
- [Memory](#memory)
  - [Vector store](#vector-store)
  - [Document upload](#document-upload)
- [Web search, images and news](#web-search-images-and-news)
- [Obsidian vault integration](#obsidian-vault-integration)
- [Local models](#local-models)
  - [Ollama](#ollama)
  - [torch / GPU](#torch--gpu)
- [API surface](#api-surface)
- [Project layout](#project-layout)
- [Configuration reference](#configuration-reference)
- [Development](#development)

## Features

- **ChatGPT subscription login** — browser OAuth handled by Codex, with a
  pasted callback fallback in the terminal setup wizard.
- **Always-on voice assistant** — the browser mic keeps listening for the wake
  word (`apex` by default). After you speak a command it replies aloud (Web
  Speech TTS). Barge-in works: say the wake word mid-answer to cut it off.
  After each reply a follow-up window stays open (`FOLLOW_UP_SECONDS`, default
  30s) so you can keep talking without repeating the name. "Stop / sleep /
  goodbye" closes it.
- **Three agent engines, one interface** — raw Responses API, the OpenAI Agents
  SDK, and LangGraph. Streams tool calls, text and memory events over SSE:
  `meta · text_delta · tool_call · tool_result · memory · done · end`.
- **Skills** — markdown + YAML frontmatter packs that bundle tools and prompts.
  Built-ins include `general`, `code`, `research`, `translator`, `obsidian`, and
  you can drop your own into `DATA_DIR/skills`.
- **Tools** — web search, web image search, web news search, web fetch, weather,
  calculator, current time, sandboxed `run_python` (opt-in), a full Obsidian
  vault toolset, and memory `remember` / `recall` / `forget`.
- **Memory** — ChromaDB vector store per user with automatic embedding fallback:
  OpenAI → Ollama → torch → local `hash-384` (works with zero keys). Upload
  documents from the UI to make them searchable.
- **Providers** — OpenAI, **Ollama** (with tool calling + GPU offload), and a
  **torch/transformers** backend (`cuda:0` / `mps` / `auto`). Switch engine,
  provider and model live from the settings panel.
- **Dev mode** — no OpenAI account needed: `DEV_MODE=true` signs in a fixed
  local user so the whole UI runs against Ollama/torch.

## Quick start

### Backend

After the first setup, the backend and frontend services can be managed with
the project command:

```bash
./apex install-service --no-build
./apex start
./apex stop
./apex restart
./apex status
```

`start` and `restart` also launch the HTTPS development frontend on port
`3001`, for remote microphone testing. Use `--no-https` when it is not needed:

```bash
./apex start --no-https
./apex restart --no-https
```

Then open `https://<your-host>:3001` and accept the local certificate.
The HTTPS development server uses a separate `.next-https` build directory so
it can run alongside the production frontend. Its log is written to
`.apex/frontend-https.log`.

Use `./apex install-service` without `--no-build` when the frontend production
bundle must be rebuilt.

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python setup_provider.py            # interactive provider + .env setup
DEV_MODE=true python app.py          # http://localhost:5001
```

The terminal wizard supports ChatGPT subscriptions through Codex, OpenAI
(including compatible API endpoints), Kimi, Ollama, and torch. Use arrow keys
and Enter to select options; API keys are masked. Enter keeps an existing
field, Ctrl+U clears typed input, and Esc cancels without writing. Review the
settings and select **Save configuration** to update `backend/.env`. Unrelated
settings and comments are preserved; the saved file is readable and writable
only by its owner. A session secret is generated if absent.

You can also run it from the project root:

```bash
.venv/bin/python backend/setup_provider.py
# Optional alternate output file:
.venv/bin/python backend/setup_provider.py --env /path/to/.env
```

Run the setup wizard from an interactive terminal. It writes provider settings
to `backend/.env`, including the Codex account directory and model defaults:

```bash
cd /path/to/apex
.venv/bin/python backend/setup_provider.py
```

Choose **ChatGPT subscription (Codex browser login)**, complete the browser
login, and wait for the wizard to confirm that credentials were saved. Then
restart the backend so it reloads `.env`:

```bash
.venv/bin/python backend/app.py
```

The wizard resets stale provider, engine, and model overrides saved by the UI
when it writes the main `backend/.env`, so the selected setup becomes the next
startup default. Existing Codex credentials are reused when the wizard offers
that option.

Requires an interactive Linux/macOS terminal and `python-dotenv` (included in
backend requirements). Choose local auto-login only on a trusted local machine;
it bypasses sign-in. Select **Configure OAuth sign-in** to enter your client ID,
optional client secret, backend/frontend URLs, registered callback URL, and scopes.
This disables local auto-login. For OpenAI, choose whether model requests use a
server API key or an OAuth access token; token mode skips the API-key prompt and
requires API authorization for your OAuth app. This configures the backend's
existing OAuth integration; it does not register an OAuth app or perform sign-in.
**Keep existing authentication settings** leaves the login configuration intact.
It selects the built-in `responses` engine. Install/pull local models separately
(`ollama pull <model>` or `pip install torch transformers`). Restart the backend
after saving; existing per-user UI settings override these startup defaults.
An alternate output file must be copied to `backend/.env` or loaded by your launcher.

The heavy deps (`openai-agents`, `langgraph`) are optional — if one fails to
import, only its engine disappears from `/api/settings`.

### Frontend (React UI)

```bash
cd frontend
npm install
npm run dev                  # http://localhost:3000
```

For microphone and wake-word testing from another host, run the frontend over
HTTPS:

```bash
cd frontend
npm run dev -- --experimental-https
# open the https://localhost:3000 URL printed by Next.js
```

Allow microphone access when prompted. Browser speech recognition requires the
browser's speech service to be reachable; `network` recognition errors come
from that browser service and are separate from the backend or Codex. Firefox
does not support the Web Speech Recognition API used by Apex, so use Chrome or
Edge for wake-word listening.

In dev the UI proxies API calls through `next.config.mjs`:
`/be/api/*` → `http://127.0.0.1:5001/api/*`. For a split deployment set
`NEXT_PUBLIC_API_URL` to the backend URL (including `/api`).

Open http://localhost:3000 — with dev mode enabled you land straight in the
assistant. Hit **MIC ON** and say *"Apex, what's the weather?"*

### ChatGPT subscription — browser login through Codex

Install a current [Codex CLI](https://learn.chatgpt.com/docs/cli) and run:

```bash
.venv/bin/python backend/setup_provider.py
```

1. Choose **ChatGPT subscription (Codex browser login)**.
2. Keep the model blank to use Codex's account default, or enter an available model.
3. Review the local setup and choose **Continue to browser login**.
4. Sign in to ChatGPT in the browser that opens. The terminal waits for completion.
5. If the browser cannot reach the local callback (for example over SSH), copy its
   complete `http://localhost:.../auth/callback?code=...&state=...` URL. Press **P**
   in the wizard and paste it there. Do not paste it into chat or an issue.
   The wizard validates the callback address and login state before passing it
   to the local Codex listener. A success-page URL is not a callback URL.
6. On successful login the wizard saves `backend/.env`. Restart the backend.
   If you previously selected a different provider in the UI, select **codex**
   in Settings; saved per-user choices override environment defaults.

No client ID, client secret, or API key is needed. The integration uses the
[Codex app-server protocol](https://learn.chatgpt.com/docs/app-server); Codex
stores and refreshes the subscription credentials in `backend/data/codex`
(or `APEX_CODEX_HOME`). This is separate from your normal Codex login. Cancelling
before login completes leaves `.env` unchanged; a completed login may already
have saved credentials in that Codex directory.

This setup enables local APEX auto-login and is intended for a trusted personal
machine. All APEX requests using this provider use that one ChatGPT account.
Run the backend under the same OS user as setup. Account plan limits and model
availability apply. The provider uses the `responses` agent engine and bridges
APEX tools through Codex's experimental dynamic-tool protocol. Codex's shell,
apps, plugins, and multi-agent features are disabled for this integration;
threads run with a read-only sandbox in a temporary working directory.

The separate **Configure OAuth sign-in** option is for a pre-existing OAuth
application registration. It is not the ChatGPT subscription login path, and
this project does not provide a public OpenAI OAuth client-registration process.

## Using the assistant

### Skills

Skills are selected from the chat UI header. Each skill is a system prompt plus
an allowed tool list:

- **general** — default assistant; handles weather, time, web search, images,
  news, memory and calculations.
- **research** — web research with citations and source checking.
- **code** — coding help with optional `run_python` execution.
- **translator** — translation tasks.
- **obsidian** — full read/write/search access to a local Obsidian vault.

Drop custom skill files into `DATA_DIR/skills` to override or extend built-ins.

### Agent orb

The central orb is surrounded by a reasoning graph of specialist nodes. Clicking
a node opens an overview card. Active nodes pulse during a turn so you can see
which specialists contributed.

### Chat rendering

The chat panel renders:

- Plain `https://` URLs as clickable links.
- Image URLs (`.jpg`, `.png`, `.gif`, `.webp`, `.svg`, `.bmp`) as inline images.
- Markdown images `![alt](url)` as inline images.
- Broken external images are hidden automatically.

## Voice mode

All voice happens in the browser (`SpeechRecognition` + `speechSynthesis`) —
nothing to install. The orb state reflects the assistant: idle → listening →
thinking → speaking.

Controls (also in the settings panel):

| Setting          | Env                 | Default | Meaning                                   |
|------------------|---------------------|---------|-------------------------------------------|
| Wake word        | `WAKE_WORD`         | `apex`  | drops the assistant out of standby        |
| Follow-up window | `FOLLOW_UP_SECONDS` | `30`    | seconds of hands-free follow-ups after a reply (0 = off) |
| Speak replies    | `tts_enabled`       | on      | reads answers aloud                       |
| TTS voice        | `VOICE`             | auto    | browser voice name, e.g. `Google UK English Female` |

If the browser's SpeechRecognition is unavailable (Firefox, older Safari) the
mic button shows a warning; typing still works.

## Memory

### Vector store

Long-term memory uses ChromaDB. Each user gets their own collection. Embeddings
fall back automatically through OpenAI → Ollama → torch → a local
`hash-384` implementation, so memory works even without API keys.

Use the **Memory** tab in the UI or the `remember` / `recall` / `forget` tools.

### Document upload

The **Memory** tab lets you upload documents. Supported formats:
`.txt`, `.md`, `.pdf` (requires `PyPDF2`), `.json`, `.csv`, code files,
`.html`, `.yaml`/`.yml`.

Uploaded files are:

1. Extracted to plain text (`backend/memory/documents.py`).
2. Chunked (default 800 characters, 100-character overlap; configurable in the
   upload form).
3. Embedded and stored with metadata `category: document`,
   `source: <filename>`, `chunk_index: <n>`.

Stored chunks are automatically included in memory recall and can be retrieved
with the `recall` tool.

## Web search, images and news

The assistant searches the public web through DuckDuckGo (`ddgs`):

- `web_search` — general web search returning titles, URLs and snippets.
- `web_image_search` — image search returning direct image URLs.
- `web_news_search` — news search returning titles, snippets, source URLs and
  article image URLs.

The model is instructed to respect requested counts (e.g. "one image"), add
`site:` filters when a source is named (e.g. "from LinkedIn"), and only return
results whose title/source matches the subject.

## Obsidian vault integration

A dedicated **Obsidian** agent node and skill give the assistant full read/write
access to a local Obsidian vault configured with `OBSIDIAN_VAULT_PATH`.

Supported operations:

- **CRUD** — `obsidian_create_note`, `obsidian_read_note`,
  `obsidian_update_note`, `obsidian_delete_note`, `obsidian_create_folder`,
  `obsidian_delete_folder`.
- **Navigation** — `obsidian_get_outgoing_links`, `obsidian_get_backlinks`,
  `obsidian_follow_link` for `[[wiki-links]]`.
- **Metadata** — `obsidian_get_note_metadata` reads YAML frontmatter and inline
  `#tags`; `obsidian_search_by_tag` filters the vault by tag.
- **Daily notes** — `obsidian_daily_note` opens or creates today's note using
  `OBSIDIAN_DAILY_NOTES_FOLDER` and `OBSIDIAN_DAILY_NOTES_FORMAT`.
- **Attachments** — `obsidian_list_attachments` lists images/PDFs/etc.;
  `obsidian_attachment_url` returns a URL served by `GET /api/obsidian/file`,
  so images can be rendered inline in chat.

All paths are resolved inside the vault and path-traversal attempts are rejected.

## Local models

### Ollama

```bash
ollama pull llama3.2:latest      # or qwen2.5-coder, gemma3, …
curl -X POST localhost:5001/api/settings -H 'Content-Type: application/json' \
  -d '{"provider":"ollama","model":"llama3.2:latest","engine":"agents_sdk"}'
```

or just pick them in the UI settings panel. Ollama's `/v1/chat/completions`
gives you tool calling and `num_gpu` offload automatically.

> Tip: on a CPU-only machine with heavy swap, single turns can take a minute.
> A GPU (or `keep_alive` warm models) makes it snappy.

### torch / GPU

```bash
pip install torch transformers
# then from the UI (or API): provider=torch, torch_model=Qwen/Qwen2.5-7B-Instruct
```

Set `TORCH_DEVICE=cuda:0` (or `mps`/`auto`) in `backend/.env`. The torch
provider currently only runs through the raw `responses` engine.

## API surface

```
Auth & user
  /api/oauth/start   /api/auth/callback   /api/me   /api/logout

Config & skills
  /api/config        /api/settings (GET/POST) /api/models    /api/skills

Conversations
  /api/conversations      /api/conversations/<id>
  /api/conversations/<id>/messages

Chat & memory
  /api/chat               POST, SSE stream (voice_mode:true for voice turns)
  /api/memory             GET/POST/DELETE (+/search, +/upload)

Obsidian
  /api/obsidian/file      GET — serve a vault attachment (path query param)

Health
  /api/health
```

## Project layout

```
backend/
  app.py               Flask routes, sessions, SSE chat, dev auto-login
  config.py            env config (see .env.example)
  auth.py              OpenAI OAuth PKCE + token refresh + id-token verify
  db.py                SQLite (users, tokens, conversations, messages, settings)
  agent/               responses.py · agents_sdk.py · langgraph.py · factory.py
  models/              providers.py (openai/compat/ollama/torch) · embedders.py
  memory/
    store.py           ChromaDB per-user collections + embedding fallback
    documents.py       text extraction + chunking for uploads
  tools/
    base.py            Tool / ToolRegistry / load_default_tools
    core_tools.py      web search/fetch, weather, time, calc, run_python, …
    memory_tools.py    remember / recall / forget / memory_stats
    obsidian_tools.py  full Obsidian vault integration
  skills/
    manager.py         skill discovery and prompt construction
    definitions/*.md   built-in skills (general, code, research, obsidian, …)

frontend/
  lib/api.ts           typed API client + SSE parser
  lib/voice.ts         wake-word listener + TTS (always-on)
  components/
    ApexProvider.tsx   shared state & chat actions
    ChatUI.tsx         chat panel, memory tab, message rendering
    ApexWorld.tsx      agent overview cards + accessible roster
    ReasoningWeb.jsx   SVG reasoning-graph constellation
  app/page.tsx         shell mounting the world + assistant
```

## Configuration reference

Key environment variables (see `backend/.env.example` for the full list):

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Server-side OpenAI / compatible API key |
| `OPENAI_BASE_URL` | OpenAI-compatible base URL |
| `DEFAULT_MODEL` | Default chat model |
| `AGENT_ENGINE` | `responses` (default), `agents_sdk`, `langgraph` |
| `MEMORY_ENABLED` | Enable vector memory (`true`) |
| `DATA_DIR` | SQLite, ChromaDB and user skill overrides |
| `WAKE_WORD` | Voice wake word (`apex`) |
| `FOLLOW_UP_SECONDS` | Hands-free follow-up window (`30`) |
| `DEV_MODE` / `DEV_AUTO_LOGIN` | Skip OAuth for local development |
| `ENABLE_RUN_PYTHON` | Enable the sandboxed Python tool (`false`) |
| `ENABLE_RUN_SHELL` | Enable the local shell tool (`false`) |
| `OBSIDIAN_VAULT_PATH` | Absolute path to your Obsidian vault |
| `OBSIDIAN_DAILY_NOTES_FOLDER` | Daily notes folder inside the vault |
| `OBSIDIAN_DAILY_NOTES_FORMAT` | strftime format for daily note filenames (`%Y-%m-%d`) |

## Development

Run the backend test suite from the repo root:

```bash
.venv/bin/pytest backend/tests -q
```

Build the frontend:

```bash
cd frontend && npm run build
```

A green backend test run and a successful `npm run build` are required before
finishing any feature.

For agent-focused implementation guidance (adding tools, skills, UI nodes, etc.)
see `AGENTS.md`.


## FILE_SEARCH skill

In normal **general** chat, ask “Search Documents for files named logo and list
them.” APEX calls file search and lists matches with download links; selecting
**FILE_SEARCH** explicitly also works. The filename and directory come from each request. Named folders resolve from
the backend OS user's actual home folders and Linux XDG user directory settings. You can also specify an absolute directory.
The skill searches filename fragments or glob patterns and returns clickable
download links, full paths, and sizes. Files stay on the backend machine until
you download them; searching does not read their contents.

Search runs as the backend OS user, without elevation, and skips inaccessible
paths, symlinks, special files, and `/proc`, `/sys`, `/dev`, and `/run`. Each search
is bounded to 10 seconds, 100,000 entries, and at most 100 matches; partial results
are explicitly marked. Narrow the directory when a system-wide search is truncated.
Downloads require the same signed-in APEX user, expire after one hour, and are
rejected if the file changed since the search.

By default, the search root is `/`. To restrict it, set `FILE_SEARCH_ROOTS` in
`backend/.env` to colon-separated directories and restart the backend. APEX users
share the backend OS account's file permissions, so configure these roots for
any shared deployment. Select the skill after restarting the backend to load it.


### Public URLs for downloads and images

Set `BASE_URL` in `backend/.env` to the public address of the **backend**:

```dotenv
BASE_URL=https://apex.example.com
# Or a LAN address, for example: http://192.168.1.50:5001
```

All APEX-hosted file download and Obsidian attachment/image links use this base.
It must serve `/api` routes; do not append `/api` to the setting. A reverse-proxy
prefix such as `https://example.com/apex` is supported. Restart the backend and
repeat the search to generate new links. Previously saved links retain their old
address. External web-search image URLs still point to their original sources.

File-search results also appear as **Download filename** links directly beneath
chat replies. These come from the tool results and remain available when reopening
the conversation, independently of how the model formats its answer.
