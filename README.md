# APEX — AI co-worker with real-time voice

A self-hosted AI assistant with a ChatGPT-style **OpenAI OAuth sign-in**, an
**always-on voice mode** (say **"Apex"** and talk to it — no push-to-talk), a
tool-using agent core with skills and long-term vector memory, and full support
for **local models** (Ollama / GPU) — all configurable at runtime from the UI.

UI: the [APEX-UI](https://github.com/RubenM1990/APEX-UI) orb. Backend: Python/Flask.

```
frontend/   Next.js 15 (React 19) — chat panel, settings, memory, voice, orb
backend/    Flask API — OAuth, agents, tools, skills, ChromaDB memory
```

## Features

- **OpenAI OAuth sign-in** (Sign in with ChatGPT) — PKCE flow, id-token
  verification, refresh tokens, optional "API on behalf of users" access.
- **Always-on voice assistant** — the browser mic keeps listening for the wake
  word (`apex` by default). After you speak a command it replies aloud (Web
  Speech TTS). Barge-in works: say the wake word mid-answer to cut it off.
  After each reply a follow-up window stays open (`FOLLOW_UP_SECONDS`, default
  30s) so you can keep talking without repeating the name. "Stop / sleep /
  goodbye" closes it.
- **Three agent engines, one interface** — raw Responses API, the OpenAI Agents
  SDK, and LangGraph. Streams tool calls, text and memory events over SSE:
  `meta · text_delta · tool_call · tool_result · memory · done · end`.
- **Tools** — web search, web fetch, weather, calculator, current time, and a
  sandboxed `run_python` (opt-in). **Skills** (markdown + YAML frontmatter)
  bundle tools and prompts into packs like `general`, `code`, `research`,
  `translator` — you can drop your own into `DATA_DIR/skills`.
- **Memory** — ChromaDB vector store per user with automatic embedding
  fallback: OpenAI → Ollama → torch → local `hash-384` (works with zero keys).
- **Providers** — OpenAI, **Ollama** (with tool calling + GPU offload), and a
  **torch/transformers** backend (`cuda:0` / `mps` / `auto`). Switch engine,
  provider and model live from the settings panel.
- **Dev mode** — no OpenAI account needed: `DEV_MODE=true` signs in a fixed
  local user so the whole UI runs against Ollama/torch.

## Quick start

### 1. Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # optional while bootstrapping
DEV_MODE=true python app.py          # http://localhost:5001
```

The heavy deps (`openai-agents`, `langgraph`) are optional — if one fails to
import, only its engine disappears from `/api/settings`.

### 2. Frontend (React UI)

```bash
cd frontend
npm install
npm run dev                  # http://localhost:3000
```

In dev the UI proxies API calls through `next.config.mjs`:
`/be/api/*` → `http://127.0.0.1:5001/api/*`. For a split deployment set
`NEXT_PUBLIC_API_URL` to the backend URL (including `/api`).

Open http://localhost:3000 — with dev mode enabled you land straight in the
assistant. Hit **MIC ON** and say *"Apex, what's the weather?"*

### 3. OpenAI OAuth login — use Codex (gpt-5-codex) as the model

Full procedure to let people **sign in with their OpenAI account** in the UI and
chat with **gpt-5-codex** through their ChatGPT subscription:

1. **Create the OAuth app**
   Go to https://platform.openai.com → **Apps → OAuth Apps → Create**.
   - Set the redirect/callback URI to
     `<BASE_URL>/api/auth/callback` (default `http://localhost:5001/api/auth/callback`).
   - Enable the **"API on behalf of users"-style / fine-grained token** setting
     (this is what lets the server call the API with the user's token instead
     of a server key).
2. **Fill in `backend/.env`** and restart the backend:

   ```bash
   OPENAI_CLIENT_ID=....            # from the OAuth app
   OPENAI_CLIENT_SECRET=....
   USE_OAUTH_ACCESS_KEY=true        # call the API with the signed-in user's token
   CHATGPT_MODEL=gpt-5-codex        # the subscription model (default already)
   # DEV_MODE should be OFF (or unset) - with OAuth configured it disables itself
   ```

   When `OPENAI_CLIENT_ID` is present the dev auto-login turns off
   automatically, so the UI shows the **SIGN IN WITH OPENAI** overlay.
3. **Sign in**
   Open http://localhost:3000 → *Sign in with OpenAI* → consent on
   auth.openai.com → you're dropped back into the chat panel, logged in as your
   ChatGPT account (`/api/me` shows it).
4. **The model is already Codex**
   With `USE_OAUTH_ACCESS_KEY=true` the assistant calls the Responses API with
   your token (`chatgpt: true`) and model `gpt-5-codex`. The Settings panel
   / model list shows `gpt-5-codex` at the top; the header chip reads
   `responses · openai · gpt-5-codex`. A ChatGPT Plus/Pro plan is required or
   the API returns 402.
5. **Verify**
   Send *"hello"* → an SSE stream appears (`responses · openai · gpt-5-codex`).
   Typing `what is 2**10? use calculate` exercises tool calling against Codex.

If instead you don't want per-user tokens, leave `USE_OAUTH_ACCESS_KEY`
unset, put a server `OPENAI_API_KEY` in `.env`, and set
`DEFAULT_MODEL=gpt-5-codex` — sign-in still works, the server key funds the calls.

## Local models

### Ollama

```bash
ollama pull llama3.2:latest      # or qwen2.5-coder, gemma3, …
curl -X POST localhost:5001/api/settings -H 'Content-Type: application/json' \
  -d '{"provider":"ollama","model":"llama3.2:latest","engine":"agents_sdk"}'
```

or just pick them in the UI settings panel. Ollama's `/v1/chat/completions`
gives you tool calling and `num_gpu` offload automatically.

> Tip: on this dev machine Ollama runs CPU-only with heavy swap — single turns
> can take a minute. A GPU (or `keep_alive` warm models) makes it snappy.

### torch / GPU

```bash
pip install torch transformers
# then from the UI (or API): provider=torch, torch_model=Qwen/Qwen2.5-7B-Instruct
```

Set `TORCH_DEVICE=cuda:0` (or `mps`/`auto`) in `backend/.env`. The torch
provider currently only runs through the raw `responses` engine.

## Voice mode

All voice happens in the browser (`SpeechRecognition` + `speechSynthesis`) —
nothing to install. The orb state reflects the assistant: idle → listening →
thinking → speaking.

Controls (also in the settings panel):

| Setting         | Env                | Default | Meaning                                   |
|-----------------|--------------------|---------|-------------------------------------------|
| Wake word       | `WAKE_WORD`        | `apex`  | drops the assistant out of standby        |
| Follow-up window| `FOLLOW_UP_SECONDS`| `30`    | seconds of hands-free follow-ups after a reply (0 = off) |
| Speak replies   | `tts_enabled`      | on      | reads answers aloud                       |
| TTS voice       | `VOICE`            | auto    | browser voice name, e.g. `Google UK English Female` |

If the browser's SpeechRecognition is unavailable (Firefox, older Safari) the
mic button shows a warning; typing still works.

## API surface

```
/api/oauth/start   /api/auth/callback   /api/me   /api/logout
/api/config        /api/settings (GET/POST) /api/models    /api/skills
/api/conversations /api/conversations/<id> (+/messages)
/api/chat          POST, SSE stream (voice_mode:true for voice turns)
/api/memory        GET/POST/DELETE (+/search)
/api/health
```

## Project layout

```
backend/
  app.py            Flask routes, sessions, SSE chat, dev auto-login
  config.py         env config (see .env.example)
  auth.py           OpenAI OAuth PKCE + token refresh + id-token verify
  db.py             SQLite (users, tokens, conversations, messages, settings)
  agent/            responses.py · agents_sdk.py · langgraph.py · factory.py
  models/           providers.py (openai/compat/ollama/torch) · embedders.py
  memory/store.py   ChromaDB per-user collections + embedding fallback
  tools/            core_tools.py · memory_tools.py · base.py
  skills/           manager.py + definitions/*.md
frontend/
  lib/api.ts        typed API client + SSE parser
  lib/voice.ts      wake-word listener + TTS (always-on)
  components/       ApexProvider (state) · ChatUI · AppShell · ApexWorld …
  app/page.tsx      shell mounting the world + assistant
```