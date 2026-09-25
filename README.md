# APEX — AI co-worker with real-time voice

For everyday use, voice commands, desktop apps, and troubleshooting, see the [User Manual](USER-MANUAL.md).

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
  - [Local command keywords](#local-command-keywords)
  - [Agent orb](#agent-orb)
  - [Chat rendering](#chat-rendering)
  - [Voice mode](#voice-mode)
  - [Autonomous mode](#autonomous-mode)
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
an allowed tool list. Built-in skills live in `backend/skills/definitions/`; you
can drop custom `.md` skill files into `DATA_DIR/skills` to override or extend
them. User skills shadow built-in skills with the same name and are picked up
automatically on the next request. In the chat UI, custom skills show a small
**×** next to their button; clicking it deletes the skill file. Built-in skills
cannot be deleted.

#### Auto-routing from general mode

When the active skill is **general**, APEX can automatically route each turn to
the most appropriate specialist skill. The model reads the skill descriptions
and replies with the best match; the turn then runs with that skill's system
prompt and tools, and the selected skill is highlighted in the skill bar. The
conversation itself stays in **general** mode, so the next turn is routed again
from scratch. Disable this with `AUTO_ROUTE_FROM_GENERAL=false` in
`backend/.env`.

| Skill | Description | Tools |
|-------|-------------|-------|
| **general** | Default assistant for everyday questions and tasks. | `file_search`, `current_time`, `get_weather`, `web_search`, `web_image_search`, `web_news_search`, `web_fetch`, `calculate`, `remember`, `recall` |
| **code** | Coding assistant that writes, runs and debugs code. | All active tools (uses `CODE_MODEL` when configured) |
| **research** | Web research with live sources, citations and source checking. | `web_search`, `web_image_search`, `web_news_search`, `web_fetch`, `current_time`, `calculate`, `remember`, `recall` |
| **translator** | Professional translation and multilingual editing. | None required (empty tool list) |
| **obsidian** | Full read/write/search access to a local Obsidian vault. | All Obsidian vault tools plus `remember` / `recall` |
| **shell** | Local systems administrator; runs shell commands directly on the host. | `run_shell` (requires `ENABLE_RUN_SHELL=true`) |
| **skill_creator** | Designs and creates new Apex skills on demand. | `create_skill` |
| **FILE_SEARCH** | Finds files on the APEX server by name, extension or directory and returns download links. | `file_search` |
| **EDITOR** | Researches a topic online and generates downloadable Word or Excel documents. | `editor_create_word`, `editor_create_excel`, `web_search`, `web_fetch`, `web_image_search`, `calculate` |

#### Skill details

**general**
- Handles weather, current time, web search, image search, news, memory,
  calculations and local file search.
- Use for questions like *“What’s the weather?”*, *“Search my Documents for
  invoices”*, *“Remember my bike lock code is 4821”*.

**code**
- Plans, implements and tests code. Uses `run_python` when `ENABLE_RUN_PYTHON`
  is enabled, and searches the web for up-to-date APIs or package versions.
- Example: *“Write a Python script that fetches NOAA weather data and plots the
  temperature trend.”*

**research**
- Performs broad web searches, fetches authoritative pages, cross-checks claims
  and presents structured summaries with numbered citations.
- Example: *“Research the latest solid-state battery breakthroughs and cite
  your sources.”*

**translator**
- Translates faithfully while preserving tone, register and idiomatic meaning.
- Example: *“Translate this email into formal Japanese.”*

**obsidian**
- Creates, reads, updates and deletes notes and folders; follows wiki-links;
  reads YAML frontmatter and tags; opens daily notes; lists attachments.
- Configure `OBSIDIAN_VAULT_PATH` and the daily-note settings in `backend/.env`.

**shell**
- Executes shell commands on the backend host. Requires explicit confirmation
  for destructive or invasive operations unless already authorized.
- Enable only on trusted machines with `ENABLE_RUN_SHELL=true`.

**skill_creator**
- Gathers a name, description, system prompt and tool list, then writes a new
  skill definition file that appears in the skill bar immediately.
- Example: *“Create a skill that helps me draft project risk assessments.”*

**FILE_SEARCH**
- Searches filename fragments or glob patterns on the backend filesystem and
  returns clickable download links, full paths and sizes.
- Searches do not read file contents. By default the search root is `/`; restrict
  it with `FILE_SEARCH_ROOTS` in `backend/.env`.
- Named folders resolve from the backend OS user's home and XDG directories, or
  you can provide an absolute path.
- Downloads require the same signed-in APEX user, expire after one hour, and are
  rejected if the file changed since the search.
- Set `BASE_URL` in `backend/.env` to the public backend address so download and
  image links work from remote clients.

**EDITOR**
- Researches a topic and produces `.docx` or `.xlsx` files with headings,
  paragraphs, tables, charts, images and styles.
- Returns a signed download link that expires after
  `EDITOR_FILE_TTL_SECONDS` (default 1 hour).
- Example: *“Create a professional Word report on renewable energy trends with
  charts and citations.”*

### Local command keywords

APEX parses a small set of local commands from typed or spoken input. These are
handled directly in the browser (for example, timers and reminders fire locally
without contacting the model). English commands work in every language; Greek
commands are enabled only when the **Default response language** is set to
Greek (`el` / `el-GR`). Accents, case and final sigma are normalized.

#### Skill switching

Switch skill before sending the rest of the message. This works for both built-in and custom skills by their exact name:

| English | Greek | Example result |
|---------|-------|----------------|
| `use <skill>`, `switch to <skill>`, `activate <skill>`, `enable <skill>` | `χρησιμοποίησε <skill>`, `ενεργοποίησε <skill>`, `επίλεξε <skill>`, `άλλαξε σε <skill>`, `μετάβαση σε <skill>` | `use research, summarize quantum computing` → switches to **research** and sends “summarize quantum computing”. `use google-places, find cafes near me` → switches to the custom **google-places** skill. |

Greek aliases for built-in skills:

| Skill | Greek aliases |
|-------|---------------|
| general | γενικά, γενική, γενική βοήθεια, βοήθεια, γενικός βοηθός |
| code | κώδικας, προγραμματισμός, προγραμματιστής |
| research | έρευνα, ερευνητής, μελέτη |
| translator | μεταφραστής, μετάφραση |
| obsidian | σημειώσεις, οψιδιανός, σημειωματάριο |
| shell | τερματικό, κέλυφος, κονσόλα |
| skill_creator | δημιουργός δεξιοτήτων, δημιουργία δεξιοτήτων |
| FILE_SEARCH | αναζήτηση αρχείων, αρχεία, ψάξε αρχεία |
| EDITOR | συντάκτης, επεξεργαστής εγγράφων, επεξεργαστής, έγγραφα, word, excel |

#### Timers and reminders

Timers and reminders are handled locally in the browser. They support both
relative durations and absolute clock times.

**Reminders**

| English forms | Greek forms | Examples |
|---------------|-------------|----------|
| `remind me ...`, `add a reminder ...`, `set a reminder ...`, `create a reminder ...`, `reminder ...` | `θύμισέ μου ...`, `θύμησέ μου ...`, `υπενθύμισέ μου ...`, `βάλε (μου) (μια/ένα) υπενθύμιση ...`, `όρισε (μου) (μια/ένα) υπενθύμιση ...`, `πρόσθεσε (μου) (μια/ένα) υπενθύμιση ...`, `δημιούργησε (μου) (μια/ένα) υπενθύμιση ...`, `κάνε (μου) (μια/ένα) υπενθύμιση ...`, `υπενθύμιση ...` | `add a reminder to call John in 10 minutes`, `βάλε μου υπενθύμιση να καλέσω τον Γιάννη σε δέκα λεπτά` |

Time markers: `in` / `at` (English) and `σε` / `στις` / `στη` / `στην` (Greek)
for relative and absolute times. Task separator: `to` (English) / `να` (Greek).

**Timers**

| English forms | Greek forms | Examples |
|---------------|-------------|----------|
| `set a timer ...`, `start timer ...`, `create a countdown ...`, `timer ...`, `countdown ...` | `βάλε (μου) (ένα/μια) χρονόμετρο ...`, `όρισε (μου) (ένα/μια) χρονόμετρο ...`, `ξεκίνα (μου) (ένα/μια) αντίστροφη μέτρηση ...`, `δημιούργησε (μου) (ένα/μια) χρονόμετρο ...`, `κάνε (μου) (ένα/μια) χρονόμετρο ...`, `χρονόμετρο ...`, `αντίστροφη μέτρηση ...` | `set a timer for 5 seconds`, `κάνε μου αντίστροφη μέτρηση για πέντε λεπτά` |

**Cancellation**

| English | Greek |
|---------|-------|
| `cancel all timers`, `stop timer`, `clear timers` | `ακύρωσε όλα τα χρονόμετρα`, `σταμάτα το χρονόμετρο`, `διάγραψε τα χρονόμετρα` |
| `cancel all reminders`, `stop reminders`, `clear reminders` | `ακύρωσε όλες τις υπενθυμίσεις`, `σταμάτα τις υπενθυμίσεις`, `διάγραψε τις υπενθυμίσεις` |

#### Image browser

Open the image browser panel for local or web images:

| English | Greek |
|---------|-------|
| `show images`, `browse gallery`, `find photos`, `open image browser` | `δείξε εικόνες`, `άνοιξε τις φωτογραφίες`, `βρες φωτογραφίες` |
| `show my pictures`, `show local images` | `δείξε τις φωτογραφίες μου`, `δείξε τις τοπικές εικόνες` |
| `show images of <query>` | `δείξε μου εικόνες με <query>` |

#### Preview controls

When a preview panel is open:

| Action | English | Greek |
|--------|---------|-------|
| Close | `close`, `hide`, `dismiss` | `κλείσε`, `κρύψε`, `απόκρυψε` |
| Maximize | `maximize`, `full screen`, `enlarge` | `μεγιστοποίησε`, `πλήρης οθόνη` |
| Restore | `restore`, `minimize`, `normalize` | `επανάφερε`, `ελαχιστοποίησε`, `μίκρυνε` |
| Next | `next`, `forward` | `επόμενο`, `μπροστά` |
| Previous | `previous`, `back` | `προηγούμενο`, `πίσω` |

#### Autonomy and operator

| Command | English | Greek |
|---------|---------|-------|
| Enable autonomous mode | `enable autonomous mode`, `start autonomy` | `ενεργοποίησε την αυτόνομη λειτουργία`, `άνοιξε αυτονομία` |
| Disable autonomous mode | `disable autonomous mode`, `turn off autonomy` | `απενεργοποίησε την αυτονομία`, `κλείσε την αυτόνομη λειτουργία` |
| Silence autonomy for 10 min | `be quiet`, `silence`, `stop talking`, `pause autonomy` | `σιωπή`, `ησυχία`, `κάνε ησυχία`, `μη μιλάς`, `σταμάτα να μιλάς`, `παύση αυτονομίας` |
| Declare operator | `I am your operator`, `call me operator <name>` | `είμαι ο χειριστής σου`, `αποκάλεσέ με χειριστή <name>` |

#### Wake word and sleep

The default wake word is `apex`. When Greek is selected, `Άπεξ` / `Απέξ` is
also recognized as a wake alias.

Sleep phrases end the voice session:

| English | Greek |
|---------|-------|
| `stop`, `sleep`, `goodbye`, `good night`, `never mind`, `that’s all`, `dismiss`, `quiet`, `go to sleep`, `stand down` | `σταμάτα`, `σταμάτησε`, `κοιμήσου`, `πήγαινε για ύπνο`, `καληνύχτα`, `αντίο`, `άστο`, `αυτό ήταν`, `αυτά ήταν`, `τέλος`, `άκυρο`, `μπες σε αναμονή`, `πήγαινε σε αναμονή` |

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

## Autonomous mode

Enable **Autonomous mode** in Settings, or say/type **"enable autonomous mode"**.
Keep APEX open in a visible tab and leave the mouse and keyboard idle. At the
default humor level, the first social prompt starts after about 55 seconds of
inactivity, plus model response time. Later social prompts are at least ten
minutes apart. Listening, speaking, active chat requests and hidden tabs pause
autonomous activity.

APEX posts its suggestions as assistant messages and can also read them aloud
when voice and spoken replies are enabled and the daily voice budget allows.
Self-checks start after about a minute of idle operation and repeat hourly.
They can take several minutes to run. They report diagnostics and can propose
fixes; they do not edit code or rebuild the running frontend.
**"Be quiet"** pauses autonomy for ten minutes; **"disable autonomous mode"**
turns it off. Autonomous activity runs in the browser, so closing the tab stops it.

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
| `AUTO_ROUTE_FROM_GENERAL` | Auto-select specialist skill from general mode (`true`) |
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
| `FILE_SEARCH_ROOTS` | Colon-separated directories to restrict file-search to |
| `BASE_URL` | Public backend address for download / image links |
| `EDITOR_FILE_TTL_SECONDS` | Generated document link lifetime (default `3600`) |

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

### Notepad

Open **Notepad** from the apps menu or say “open notepad”. Say or type
“open new notepad” to create a separate empty window while keeping existing notes open. It includes rich-text
formatting, a searchable document library, text/Markdown import as plain text,
HTML and TXT downloads, and live word and character counts. Paste inserts plain
text. Use Ctrl/Cmd+S to save; saved documents autosave after edits. New documents
need an initial save. Opening another document, starting a new one, or closing
the window prompts before discarding unsaved changes.

Documents are stored as standalone HTML in `~/Documents/APEX Notepad/<user>/`.
Set `NOTEPAD_DOCUMENTS_DIR` to change the base directory. New documents with the
same title receive numbered filenames; editing an existing document keeps its
filename. HTML downloads save the current document first. Chat also supports
“save notepad”, “download notepad”, and “write Meeting notes in notepad”.

The document sidebar starts closed; **Recent documents** opens it. Voice and text
share the same controls. Examples:

- “Write to notepad Hello, world!” or “Open notepad and write Meeting notes.”
- “Open notepad and add command output” copies the latest available command
  result from this conversation; it does not run a command again.
- “Run uname -a and put the output in notepad” uses the agent's terminal tool,
  then writes the actual result (requires an open terminal).
- “Replace notepad contents with …”, “rename notepad to Meeting Notes”,
  “open Meeting Notes in notepad”, “show recent documents in notepad”.
- “Format notepad bold”, “undo in notepad”, “read notepad”,
  “clear notepad”, “export notepad as text”, “save notepad”.

The `notepad_control` tool is available across skills for flexible, multi-step
requests and generated text. It receives live document context, including unsaved
text (up to 16,000 characters per open document). Actions target the focused
Notepad, otherwise the latest on the current desktop. Formatting commands apply
to the whole document; the toolbar can format a selection. Editing commands open
Notepad when needed. The browser reports action results in chat; unsaved-change
prompts still apply when switching or closing documents.

#### Notepad troubleshooting

“0 words · 0 characters” is normal for an empty document. A `404 Not Found`
means a server route was unavailable; it is not a word-count error. Document
history loads when **Recent documents** is opened. Library failures appear in
that panel with **Retry**, separate from the document status.

After updating an installed systemd deployment, build the frontend and restart
both services so the UI and API use the same version:

```bash
cd frontend && npm run build
sudo systemctl restart apex-backend.service apex-frontend.service
```

Restarting the backend ends open terminal sessions. Reload the browser after
restarting. Check `/api/notepad/documents` on the backend and
`/be/api/notepad/documents` through the frontend; a signed-out request should
return 401, and a signed-in request should return a document list, never 404.
See [Linux services](docs/linux-services.md) for deployment details.
