# Agent notes for APEX

This file is for coding agents working on the APEX project. It complements
`README.md` with implementation details, conventions and extension points.

## Architecture at a glance

- `frontend/` — Next.js 15 (React 19) app. Component state is managed by
  `ApexProvider` (`frontend/components/ApexProvider.tsx`). The chat panel is
  `frontend/components/ChatUI.tsx`. The agent constellation is
  `frontend/components/ApexWorld.tsx` + `frontend/components/ReasoningWeb.jsx`.
- `backend/` — Flask API. Entry point is `backend/app.py`. Configuration lives in
  `backend/config.py` (env vars, defaults). Tools, skills and memory are loaded
  at startup and made available to the agent engines.
- Agent engines: `responses` (default), `agents_sdk`, `langgraph`. All consume the
  same `ToolRegistry` and skill system prompts.

## Adding a new tool

1. Create the tool implementation in `backend/tools/<name>_tools.py` (or extend
   an existing module). Follow the pattern in `memory_tools.py` / `obsidian_tools.py`:
   - define handler functions `t_<name>(args, ctx)`;
   - build `Tool(name, description, schema, handler)` objects;
   - expose `build_<name>_tools(config) -> list[Tool]`.
2. Register the tools in `backend/tools/base.py::load_default_tools`.
3. Add env vars to `backend/config.py` if the tool needs configuration, and
   document them in `backend/.env.example`.
4. Add unit tests in `backend/tests/test_<name>_tools.py`.
5. If a skill should use the tool, add it to the `tools:` list of the relevant
   skill definition in `backend/skills/definitions/*.md`.

Tool handlers receive a `ToolContext` with `user_id`, `conversation_id`,
`memory`, `emit`, `model`, `session`. Errors should be returned as strings so the
model can recover; do not raise out of the handler.

`ctx.emit(event_dict)` is the only way a tool talks to the browser. It is wired to
`AgentContext.push_event`, and each engine drains the queue right after that
tool's result (`AgentEngine._drained()` in `agent/base.py`), so mid-tool events
reach the SSE stream in causal order. An event without a string `type` is
dropped. Never use it for data the model should see — that is the return value.

`AgentContext.active_tools()` force-includes `notepad_control`, `terminal_command`
and `terminal_sessions` for **every** skill, whatever its `tools:` list says. The
agent must always be able to act on the machine and drive the live editor without
the user setting anything up, so those three are not optional per skill.

## Adding a new skill

Create `backend/skills/definitions/<skill>.md`:

```markdown
---
name: skill-name
description: One-line description.
tools: tool_a, tool_b, tool_c
---
System prompt instructions for the model.
```

- `tools:` can be a comma-separated list or YAML array. Empty means "all active
  tools".
- A `model:` override is optional.
- User skills placed in `DATA_DIR/skills` shadow built-in skills with the same
  name.
- The skill is picked up automatically on the next request; call
  `create_skill` if you need to generate one at runtime.

## Adding an agent dot to the orb

The reasoning-web constellation has two sources of truth:

1. `frontend/components/ReasoningWeb.jsx` — the visual SVG graph. Add a row to
   the `ROSTER` array:
   ```js
   ['key', 'Label', 'layer', x, y, live, bend, radius],
   ```
   `layer` is one of `consultant` (cyan), `doer` (orange), `tool` (grey-blue).
2. `frontend/components/ApexWorld.tsx` — the accessible roster and overview
   cards. Add an entry to `ROSTER` and to `INFO`.

Keep the two rosters in sync. Coordinates are in the SVG viewBox `0 0 680 480`.
Avoid overlapping labels; use `above = uy > 0.82` for bottom nodes.

## Extending chat message rendering

`frontend/components/ChatUI.tsx` renders messages in `MessageBubble`. The helper
`renderRichText` currently supports:

- plain URLs → clickable `<a>` links;
- image URLs (by extension) → `<img>`;
- markdown images `![alt](url)` → `<img>`;
- `InlineImage` hides broken images on `onError`.

Update the regex or add new token types there if you need richer rendering.

## Backend conventions

- All local paths are resolved with `Path.resolve()` and checked to stay inside
  their intended root directory (see `tools/obsidian_tools.py::_resolve`).
- Config defaults go in `backend/config.py`; never hard-code user-specific values
  in skill prompts.
- The global `config` object is created at import time. Tests that need a
  different config should pass a small config-like object to the builder
  function rather than mutating the global.

## Running tests

```bash
# backend (from repo root)
.venv/bin/pytest backend/tests -q

# frontend unit tests + typecheck build
node --test frontend/tests
cd frontend && npm run build
```

A green backend test run, a green `node --test frontend/tests` run and a
successful `npm run build` are required before finishing any feature.

## Sign-in, lock screen and the session (security-critical)

APEX authenticates against the machine's own accounts. `backend/system_auth.py`
lists login-capable users from `/etc/passwd` and checks the password with PAM
(`libpam.so.0`) through `ctypes`. No password is stored, logged or sent to a
provider, and `/etc/shadow` is never read.

- `backend/auth.py` is the OpenAI OAuth module. It predates the system login
  and is a *different* thing: do not create a `backend/auth/` package, it
  shadows this file and the import fails.
- `SystemAuth` instances hold the login throttle, so `app.py::_system_auth()`
  caches one in `app.extensions["apex_system_auth"]`. Building one per request
  resets the counters and the lockout never fires.
- `AuthResult` is a dataclass: build it with keywords (`AuthResult(ok=True,
  user=...)`). Positionally the second field is `username`, not `user`.
- The throttle is keyed by username **and** source address, so failures from one
  host cannot lock a colleague out. `TRUST_PROXY` must only be true behind a
  proxy you control, or anyone can forge `X-Forwarded-For` and escape the limit.

Session rules, all of which have a test in `backend/tests/test_auth_routes.py`:

- Every session - system login, OAuth callback and `DEV_AUTO_LOGIN` - must set
  `session["sid"]`. The CSRF guard keys off it, so a session without one exempts
  every state-changing request from CSRF protection.
- Anything that rotates `sid` (lock, unlock, login) must return the new
  `csrf_token` in its response. The client cannot compute it, and the next
  request it makes is itself protected.
- `session["locked"]` is enforced in `before_request`: while locked, every
  `/api/` path except `LOCK_ALLOWED_PATHS` answers `423`. The overlay is client
  state; the server is the control. Unlocking must clear the flag server-side.
- `SYSTEM_LOGIN_ALLOW_OAUTH` is off by default. With system login on, an OAuth
  account is a way *around* `/etc/passwd`, so `/api/oauth/start` answers 403 and
  the sign-in screen shows no OpenAI button.
- `frontend/lib/api.ts` owns the CSRF token for the whole app. `auth.lock` and
  `auth.logout` must send the current token and only clear it afterwards;
  clearing first turns both into 403s. A `423` is surfaced through
  `onSessionLocked` so a tab locked from another window locks this one too.
- A new browser API that mutates state must go through `lib/api.ts` (or send
  `requestHeaders`); the server has no other way to tell it apart from a
  cross-site POST.

Object access: the session is the only source of identity. No route may accept a
client-supplied `user_id`, and a resource owned by someone else must answer `404`,
not `403` - a wrong owner has to be indistinguishable from an id that does not
exist.

## Panel and chat voice/text commands

`frontend/lib/commands.ts::parseLocalCommand` is the single dispatcher; it
tries the shell parsers first and returns `null` for anything that is a request
for the agent. `frontend/lib/panelBridge.ts` then hands the parsed command to
the React tree over a cancelable `CustomEvent`, retrying until a consumer calls
`preventDefault()`, and returns the consumer's spoken answer.

- Two traps, both already hit once. `normalize()` folds a final sigma
  (`ς` → `σ`), so Greek patterns must accept both spellings or "στις" silently
  fails. And a `write ... in the chat` command is matched against the *raw*
  text, never the punctuation-stripped copy, so a dictated "what is the time?"
  keeps its question mark.
- `write in chat` fills the draft and focuses the box; `send chat` sends. They
  are separate on purpose - dictating a message and sending it are two
  decisions.
- Ending a session is consequential, so `sign out` / `log out` (and `αποσύνδεση`,
  `βγες έξω`, `κάνε έξοδο`) is **anchored to the whole utterance**. "how do I
  log out" has words in front of the phrase and must reach the agent as a
  question; matching the phrase anywhere in the sentence would end the session
  because someone was talking about it. It speaks a confirmation (unlike `lock`,
  where the mic is already off) and it is a real revocation - see the session
  section, because a stateless cookie makes sign-out advisory unless the session
  id is refused server-side.
- The command phrase is stripped of trailing punctuation; the captured message
  is not.

## Window manager (desktop media layer)

Voice/text can open up to `MAX_WINDOWS` (10) floating windows holding images,
PDFs, rendered Word/Excel/text documents or generic download cards, each with
optional per-window notes.

- State lives only in the frontend session, in `ApexProvider` (`windows`,
  `focusedWindowId` + refs). Actions: `windowOpen/Close/CloseAll/Focus/
  ToggleMaximize/ToggleMinimize/Arrange/Next/Previous/SetNote/ToggleNotes/Update`.
- `frontend/lib/windows.ts` is the single model + helper module:
  `kindForName`/`kindForItems`, `titleFromUrl`, `layoutRects` (cascade/grid/
  tile-h/tile-v/center), `collectPreviewableItems` (token, obsidian and raw
  image/PDF URL extraction from assistant messages) and `windowContextBlock`.
  Tests: `frontend/tests/windows.test.cjs`.
- `frontend/components/WindowManager.tsx` renders the layer (drag title bar,
  bottom-right resize handle, gallery arrows in-image, notes panel, taskbar).
  It reuses `backendFileHref` from `FileDownloads.tsx` only as the
  "is this backend-signed?" gate; the render URL is always the original signed
  URL (never the `/be`-prefixed browser path).
- `sendMessage` appends a `window_context` string (from `windowContextBlock`)
  to the payload; `chat()` in `backend/app.py` appends it to the system prompt
  only as build-time context (never persisted to history).
- Local commands live in `commands.ts::parseWindowCommand` (EN + Greek): close
  all/list, arrange `<style>`, next/previous, targeted close/focus/maximize/
  minimize/restore by number/ordinal, and notes. Executed in
  `ApexProvider::executeLocalCommand` under `case "window"`.

## Voice mode (wake word)

`frontend/lib/voice.ts` is the whole voice engine and owns the phase state
machine `standby → awake → thinking → speaking`. `frontend/lib/voiceCommands.ts`
holds the browser-free matching logic (`wakePattern`, sleep phrases, wake-word
slicing, endpoint accumulation) and is the only place a new pure helper should
go — it is unit tested in `frontend/tests/voiceCommands.test.cjs` by
transpiling the `.ts` source on the fly.

- A wake (`wakeSlow`) beeps and then speaks a random acknowledgement from
  `pickWakeAck` (English or Greek, never twice in a row). It bypasses `speak()`
  on purpose: the phase must stay `awake` so the command that follows is still
  collected and the orb keeps showing LISTENING. Do not route it through
  `speak()`/`finishSpeaking()` — they drop to `standby` and re-arm the
  follow-up window, which would swallow the command.
- `stripAckEcho` / `applyAckEcho` remove the confirmation from text the speakers
  fed back into the recognizer. `applyAckEcho` returns `null` when the echo is
  all that was heard, so no command fires. It runs before `sliceAfterLastWake`,
  which matters for acks that contain the wake word ("APEX online.").
- The echo guard is cleared by `fireCommand`, `speak`, `sleepVoice`,
  `cancelSpeech` and `stopRecognition`, so its words can never be stripped from
  a later real command.

## Terminals (model-opened, never requested from the user)

`backend/tools/terminal_server.py::open_session` is the single way a PTY session
is created — the browser route and the model-facing tools share it, so a window
the agent opened is an ordinary session the user can type into and answer sudo
prompts in.

- `terminal_command` auto-opens a window when none exists and never answers
  "please open a terminal first". `new_terminal=true` adds one more and
  `count=N` opens N at once, running the command in the newest.
- `room_for(user_id, wanted)` in `terminal_server.py` caps bulk opening by the
  free room. `open_session` itself evicts the oldest session past the cap, which
  is right for a single manual window but wrong for bulk opening: it would close
  a window the user is looking at. Bulk callers must ask `room_for` first and
  report the cap to the model instead of evicting.
- `ctx.emit({"type": "terminal_opened", "terminal_id": ...})` per new session is
  what makes the window appear; the frontend `attachTerminalWindow` adopts it.
- **Terminals are numbered by their position among the visible terminal
  windows** (`frontend/lib/windows.ts::terminalNumber`), and that number is what
  the operator says ("open top on terminal 2"). Three things must agree or the
  command lands in the wrong window:
  1. the `T2` badge in `WindowManager` title/taskbar;
  2. `windowContextBlock`, which reports `terminal #N` (not `#N`, which is the
     position among *all* windows) so the model is handed the number spoken;
  3. `terminal_command`'s `terminal=N`, resolved by `_resolve_terminal` against
     `ctx.terminal_map` — the on-screen order sent as `terminal_map` in the chat
     payload — because the backend's own session list is ordered
     most-recently-active-first and does not match what is on screen.
  An explicit session id or id prefix still beats the number, and
  `_pick_session` resolves prefixes because the schema advertises them.
  `focused_terminal` is the single session id for the turn, and
  `parseTerminalTarget` in `frontend/lib/commands.ts` sets it from a spoken
  "… on terminal N" while leaving the rest of the sentence for the agent. That
  is not enough on its own: the model read "open top on terminal 4" as *open a
  terminal* and ran `top` in a brand new window while the operator watched
  another one. The turn therefore also sends `terminal_target: N`, and
  `app.terminal_target_note` appends a prompt note naming the window and
  forbidding an extra terminal — a pinned turn must run where it was pinned.
- A browser tab opened before a rebuild keeps running the old JavaScript until
  it is reloaded, so a fix that only exists in the frontend looks broken after a
  service restart. Check `journalctl -u apex-backend` for `[terminal-target]`:
  its absence means the browser never sent `terminal_target`, i.e. the tab is
  stale, not the backend.
- Desktop moves may address terminals by their own number too: `{action:
  "move", terminals: true, targets: [1, 2]}` is "move terminals 1 and 2 to
  desktop 2", and a move without `terminals` keeps addressing windows by their
  position in the full window list.
- The REST `drain` endpoint is **not consuming**: each poller owns its own
  `since` cursor and the session keeps a bounded scrollback
  (`TerminalSession.window_max`). A session can have more than one reader — the
  window remounts on focus/layout changes, StrictMode double-mounts effects, and
  a model-opened session gets a window attached while the previous one is still
  mounted. When drain consumed, the first poller swallowed the command and the
  window the operator was looking at stayed blank. Tests may poll it; reading
  `sess.data` under `sess.lock` (`peek_text`) is still fine for assertions.
- Tests must `close()` their sessions in `tearDown`. Clearing `_TERMINALS` only
  drops the references and leaks real login shells, which then starve the
  timing-sensitive pty assertions in later tests.
- `TerminalWindow.tsx` must only advance `cursorRef` for bytes it actually
  wrote to xterm, and must check `disposed` **before** that update. The window
  remounts constantly (focus/layout changes, StrictMode), and an effect that
  advances the cursor for a response it then discards loses those bytes
  permanently — the buffer looks fine to the next poller, but the screen stays
  blank. Because drain is not consuming, simply not advancing is enough: the
  remounted effect re-reads the same cursor and repaints for free. Never let
  two polls of one instance run concurrently (the `inFlightRef` guard), or the
  same bytes are written twice.

## Server-side media preview

`backend/tools/preview_tools.py::register_preview_routes` adds a single
`GET /api/preview/render?url=<signed-url>` route that renders one document into
HTML (`.docx` via xml-level block iteration, `.xlsx` via openpyxl, text into
`<pre>`) or streams images/PDFs inline. Only the four known signed URL shapes
(editor/file_search/image_browser download tokens + obsidian file links) are
accepted; every other URL returns 400, unsupported kinds 415. The same owner,
tamper and path-containment checks run as the source tools. Route is registered
in `app.py` and guarded by `require_user`. Tests:
`backend/tests/test_preview_tools.py`. Note: python-docx has no
`Document.blocks`; block iteration happens at the XML level, and doc images are
embedded as base64 data URIs.

## EDITOR skill (Word / Excel generation)

The `EDITOR` skill creates downloadable Office documents via two tools defined
in `backend/tools/editor_tools.py`:

- `editor_create_word` — builds `.docx` files with headings, paragraphs, tables,
  images, charts (matplotlib), lists and page breaks.
- `editor_create_excel` — builds `.xlsx` workbooks with multiple sheets,
  formatting, formulas and `openpyxl` charts.

Both tools receive a JSON `document` argument, save the file under
`$DATA_DIR/generated/editor/<user_id>/`, and return a signed download URL for
`/api/editor/download/<token>`. Links expire after `EDITOR_FILE_TTL_SECONDS`
(default 3600). Generated files are cleaned up on each new document creation.

If you add new element types or chart types, update the skill prompt in
`backend/skills/definitions/EDITOR.md` and add tests in
`backend/tests/test_editor_tools.py`.

## Scheduled tasks (TASKS tab)

Tasks are created either by hand in the TASKS tab or by the agent, and run by a
background thread whether or not anyone is looking at the page.

- `backend/tools/cron.py` is a dependency-free five-field cron parser plus a
  phrasing fallback. The phrasing layer is a safety net for a model that wrote
  "every morning" instead of converting, and it is where the sharp edges are:
  - **A schedule that is silently wrong is the one failure this must not have.**
    `_clock_parts` reads the meridiem from a captured group. It used to test
    `"am" in match.group(0)`, which reports *no* meridiem for `6:30 pm`, so the
    task was set for 06:30 in the morning with no error anywhere.
  - **`datetime.weekday()` counts Monday as 0; a cron day-of-week counts Sunday
    as 0.** "every week" has to be expressed as a weekday, and shifting it by
    one is the difference between a Monday run and a Tuesday run.
  - "every week" means once a week. It used to resolve to `0 0 * * *`, so a
    weekly summary ran every single day.
  - A phrase that opens with a word no cron field can be is phrasing, however
    many digits follow: the token-shape heuristic read "every weekday at 5 pm"
    as a mistyped expression and answered "'every' is not a valid minute value".
    `_LOOSE_START` decides the routing first.
- An ISO string carrying `Z` or an offset is an aware datetime and is converted.
  The browser's `toISOString()` always has a `Z`, and reading that as
  machine-local moved a one-off by the machine's offset. `oneOffLabel()` in
  `frontend/lib/tasks.ts` writes the local wall clock instead, which is both
  exact and readable in the editor.
- `backend/tools/tasks.py` holds the `TaskRunner`, the autonomous run (its own
  conversation, normal agent tools, `TASKS_TIMEOUT_SECONDS`), the prompt context
  and the REST routes. A run that produced a reply leaves the task `unread` until
  it has been shown in chat, and the frontend clears that through
  `POST /api/tasks/ack`.
- Ownership is a SQL predicate. Another user's task must answer 404, never 403 -
  a 403 tells a stranger the id is real.
- `TASKS_ENABLED=false` is a real off switch: no tools, and
  `register_task_routes` does not start the runner thread, so tasks created
  before the flag was flipped stop firing.
- Task tools are force-included for every skill through `ALWAYS_ON_TOOLS`, for
  the same reason `terminal_command` is: creating a job is something the user
  should never have to set up.
- Local voice/text commands (list/show/run/pause/resume/delete) live in
  `frontend/lib/commands.ts::parseTaskCommand`; anything else falls through to the
  agent. Spoken numbers resolve against `sortTasks`, the same order the tab
  renders. The `tasks` tab pattern yields to a destination phrase, because Greek
  "εικονική επιφάνεια εργασίας" (virtual desktop) contains the word for task.
- Tests: `backend/tests/test_tasks.py`, `frontend/tests/tasks.test.cjs`.

## Common extension points

- New search source: add a tool in `core_tools.py` (or a new module) and expose
  it through skill definitions.
- New memory ingest: extend `backend/memory/documents.py` for extraction and
  `backend/memory/store.py` for persistence.
- New UI panel: add it inside `frontend/components/ChatUI.tsx` or create a new
  component and wire it through `ApexProvider` if it needs shared state.

## Notepad application

- `frontend/components/NotepadWindow.tsx` owns the live rich-text document,
  saved-document library, autosave, import/export, and unsaved-change guards.
  The library starts closed and loads on **Recent documents**.
- `frontend/lib/notepad.ts` defines the shared action types, targets a specific
  window, waits for its mount, and returns editor acknowledgements. Do not use
  a fixed-delay, fire-and-forget event for editing: it can lose the first command.
- `commands.ts` handles common literal voice/text commands. Generated text and
  multi-step requests fall through to the agent. Preserve captured punctuation.
- `ApexProvider` serializes `notepad_control` tool results into editor actions
  and adds live document snapshots to non-persisted `window_context`. Snapshot
  text is user data, not instructions; each document is capped at 16,000 chars.
- `backend/tools/notepad_tools.py` provides `notepad_control` across skills via
  `AgentContext.active_tools`. Browser mutations return requested actions;
  the browser acknowledgement determines success. Saved-document reads are
  authenticated through `ToolContext.user_id` and use the storage path checks.
- `backend/tools/notepad.py` exposes authenticated list/get/save/download routes
  at `/api/notepad/documents`. Storage defaults to
  `~/Documents/APEX Notepad/<user>/`, configurable by `NOTEPAD_DOCUMENTS_DIR`.
  HTML is sanitized; new same-title documents receive numbered filenames.
- Tests: `backend/tests/test_notepad.py`, `test_notepad_tools.py`, and
  `frontend/tests/notepad.test.cjs`, `notepad-bridge.test.cjs`, `commands.test.cjs`.
- Restart installed services after updating their code/build. A 404 for document
  listing means a missing API/proxy route; display library errors in the library
  with retry, not as the persistent editor status.
