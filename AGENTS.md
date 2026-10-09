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
- A `model:` override is optional, as are `require_tool: true` (first call of
  every turn must call a tool) and `exclude_tools:` (never offer these, even
  with `tools: ALL`).
- User skills placed in `DATA_DIR/skills` shadow built-in skills with the same
  name.
- The skill is picked up automatically on the next request; call
  `create_skill` if you need to generate one at runtime.

### Runtime skill packs (the skill_creator skill)

`skill_creator` builds a whole pack, not just the `.md`:

```
DATA_DIR/skills/<name>.md       definition        (create_skill)
DATA_DIR/skills/<name>/         pack directory
DATA_DIR/skills/<name>/.env     script secrets    (set_env scope=skill, 0600)
DATA_DIR/skills/<name>/<script> helper scripts    (create_script, 0700 for .py/.sh/.bash/.js)
backend/.env                    backend config    (set_env scope=backend)
```

- `list_tools` is how the model sees real tool names. `create_skill` validates
  every name against the live registry: unknown names are rejected (the engine
  would silently drop them), registered-but-disabled ones warn.
- `set_env scope=backend` writes `config.ENV_FILE` and applies the value live
  (`os.environ` + `setattr(config, ...)`); variables read at import time still
  need a backend restart, and the reply says so. It never stores an empty
  value - an empty line would pin `""` over the default forever (the SIP
  mirror's lesson); removal is `remove=true`.
- Env line editing is shared: `tools/envfile.py::upsert_env` is the one
  implementation, and `sip_tools.write_env_file` is a thin wrapper over it.
- Two frontmatter traps are handled in `create_skill`: a `---` line in the
  system prompt would close the frontmatter early (rewritten to `***`), and a
  description containing `": "` must not be hand-concatenated into YAML
  `safe_load` rejects - the skill would be written but never load. Frontmatter
  is emitted with `yaml.safe_dump`.
- The skill panel refreshes through a `skills_changed` SSE event emitted in
  `app.py` when a `create_skill` tool_result arrives; all three side-effect
  events (`skills_changed`, `sudo_password`, `github_token`) share the single
  generic `tool_result` branch - they used to sit in `elif` branches below it
  and were unreachable.
- Tests: `backend/tests/test_core_tools.py`, `backend/tests/test_skill_creator.py`
  (pack tools + the SSE events).

### Skill validation and testing (`skills/tooling.py`)

The skill_creator does not stop when the files are written: it validates and
tests the pack and repairs it until it works. The engine is
`backend/skills/tooling.py` (pure functions, unit-tested); the agent-facing
wrapper is three tools in `tools/core_tools.py`; the same engine is exposed as
CLIs for humans. Never copy the rules into the prompt or a script - call the
engine.

- `validate_skill(target, registry=...)` is **static** (never executes the
  skill): it checks frontmatter, name (matches the filename), description,
  prompt (no `[TODO:`), tool names against the live registry, and every file in
  the pack (Python compiles via `compile()`, `bash -n`, JSON parses, YAML
  `safe_load`), plus warnings (missing `tests/` for executable code, world-
  readable `.env`, tool names that are disabled). Errors fail it; warnings do
  not.
- `run_skill_tests` validates first, then runs the pack's `tests/` with pytest
  (unit), each script's `--check`/`--help` entry point (functional, with
  `--json` output required to carry a `status` field), and an integration check
  that `SkillManager` parses the file and its tools resolve. Each stage is
  PASS/FAIL/SKIP/BLOCKED; a validation failure short-circuits the rest as SKIP.
  Once `--check` runs, budget for the 2-4s pty/test startup - the functional
  stage is a real subprocess, not an import.
- `skill_report` adds a completeness score (0-100) and a READY/BLOCKED/FAILED
  verdict, plus a security scan (`shell=True`, `os.system`, string-command
  subprocess, `eval`/`exec`, `pickle.loads`, `verify=False`, `chmod 777`).
  READY needs score >= 90, a valid skill and tests PASS.
- `create_skill_pack` is the scaffold used by `scripts/create_skill.py`; it
  refuses to overwrite without `overwrite=true` and renders
  `skills/creator_templates/*.tmpl`.
- CLIs (thin wrappers, re-exec into `.venv` if PyYAML is missing):
  `scripts/validate_skill.py`, `scripts/test_skill.py`, `scripts/test_all_skills.py`,
  `scripts/skill_report.py`, `scripts/create_skill.py`; or
  `python -m skills.tooling <cmd>` from `backend/`.
- The prompt contract in `skills/definitions/skill_creator.md` is the repair
  loop: create -> validate -> create tests -> test -> fix -> re-run until
  `RESULT: PASS` and `Completeness >= 90`. A missing `tests/` directory for a
  pack with executable code is a warning in validation and a SKIP in testing, so
  it cannot reach READY - not a skipped test turned green.
- Tests: `backend/tests/test_skill_tooling.py`.


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
- **The sigma trap is now closed by construction, not by memory.** Every Greek
  vocabulary handed to a regex in `commands.ts` goes through `fold()`, which
  runs each word through `normalize()`. Write "τις" and "κονσόλες" the way a
  person says them; `ALL_WORDS`, `DETERMINERS` and `bulkWindowAction`'s
  noun/verb arguments fold them once at build time. Several of those words
  (`όλους`, `τους`) had been spelled with a final sigma for months in a form
  that could never fire, which is exactly the failure mode to avoid repeating by
  hand. The test asserts a phrase ending in final sigma still parses.
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

## The command reasoning layer (what the parsers miss)

A regex only matches the phrases its author wrote down, and the agent cannot fix
the miss: no tool can un-minimize a window. So an unrecognized *window* phrase
used to produce a reply and no change. Three pieces, in order:

1. `frontend/lib/commands.ts` still runs first and still wins. The bulk
   vocabulary (`bulkWindowAction`, one function for windows, terminals, file
   managers and notepads) is the fix for the cheap half of the problem - a
   missing regex slot. A request is bulk when a quantifier is present **or** the
   noun is plural on its own; a singular noun with no quantifier is deliberately
   not bulk, so "restore terminal" keeps meaning the focused window.
2. `frontend/lib/commandSpec.ts` describes what the browser can do to itself:
   `LOCAL_ACTIONS` (the catalogue), `buildUiState` (windows with the numbers
   shown on screen, their kind, minimized/maximized/desktop, plus tasks) and
   `sanitizeActions`, the trust boundary.
3. `backend/tools/command_router.py` + `POST /api/resolve-command` ask a model
   to read one utterance against the catalogue and the state, and answer with
   `{"actions": [...]}`. `COMMAND_ROUTER_ENABLED` (default on) and
   `COMMAND_ROUTER_MODEL` (optional; ignored if the provider cannot serve it) are
   in `backend/config.py`.

Four rules make this safe, and each has a test:

- **The reply is a proposal, never a decision.** The route forwards what the
  browser sent and the browser validates it against the *same* catalogue, so the
  two cannot drift. An invented action is dropped. The server is not the
  authority on what may be done to the desktop; the session is.
- **A number that is not on screen drops its action.** Stripping the field would
  turn "close window 4" into "close the focused window" - a different window
  from the one named, which for a close is not a guess to make. An action nobody
  numbered is unaffected, because there the focused window is the intent.
- **`signout`, `lock` and `chatinput` need the parser to agree**
  (`CONFIRMATORY_ACTIONS`). The model's opinion is not enough for ending a
  session or writing into the box; the parser that anchors those phrases to the
  whole utterance stays the only way they fire.
- **Every failure is silent and looks like success.** A provider that is down, a
  model that will not answer in JSON, a router that is off and "this was a
  question for the agent" all return `[]`, because the caller's next step for
  all four is the same one: send it to the agent, as it always did. The resolver
  never raises, and a chain runs sequentially (its steps read each other's
  result) and is capped at `MAX_ACTION_CHAIN`.
- **The answer's *shape* is not the catalogue's shape, and the difference is
  silent.** `renderActionCatalogue` prints its entries as `terminal.open`, and
  the live model copies that printed key straight into `"type"` and never sets
  `"action"` (`{"type": "terminal.open", "count": 2}`; it also produced
  `{"terminal.open": {"count": 2}}`). `specKey` builds `type + "." + action`, so
  that entry looks up `terminal.open.` — in no map. **Every action in the chain is
  dropped, the resolver returns `[]`, and the turn reaches the agent**, which has
  `terminal_command` and answers half the sentence: "show me two terminals and a
  notepad" opened two terminals and no notepad. `normalizeEntry` in
  `commandSpec.ts` reshapes all three forms before the lookup. Reshaping is safe
  where adding an action is not — the result still has to pass `ALLOWED`, and
  splitting on the first dot cannot invent a pair — and there is a test per shape
  plus one asserting a bogus dotted name still drops. **When adding an action,
  check the real model's answer to a phrase that uses it**; the router prompt now
  says `type` and `action` are separate fields, but a prompt instruction is a
  request, not a guarantee, so the normalizer is what makes it hold.
- **A count the operator gave implies creation.** "two terminals" is a request
  for two *new* ones. The deterministic parser spells out `create` + `count`, but
  a model that answers `count: 2` and no `create` used to be read as "focus one",
  because the executor keyed off `create` alone. `count > 1` now implies it.
- **A parser must consume the whole sentence, or decline.** "open 3 terminals one
  notepad and a file manager" is *two thirds* the request. The count branch of
  `parseTerminalCommand` matched that prefix and returned, so the parser won over
  the router and the operator got 3 terminals and a silent loss of the rest -
  the worst kind of failure, because it looks like the system worked. The branch
  now checks that nothing follows, the same way `finish()` does for the
  single-target branches; declining sends the whole sentence to the reasoning
  layer, which answers all three actions. **This is the general trap: a regex
  with `^` and no end anchor is a prefix matcher.** When adding a branch, anchor
  it or pass the leftover text to the helper that declines. Test the compound
  form, not just the single one.

Window titles reach the model, so the state block is fenced and labelled as
data in the prompt, and `sanitizeActions` treats the returned text as a value to
store, not an instruction. Tests: `backend/tests/test_command_router.py`,
`frontend/tests/commandSpec.test.cjs`.

Adding an action means adding it to `LOCAL_ACTIONS` **and** to the executor in
`ApexProvider::executeLocalCommand`; the catalogue is the model's menu, and an
action it does not list is one it will not be offered. Content-extracting
commands (`timer`, `reminder`, `images`, `operator`, notepad editing) stay out of
it deliberately - they slice a payload out of the operator's sentence, which a
catalogue entry cannot express.

## Window manager (desktop media layer)

Voice/text can open up to `MAX_WINDOWS` (10) floating windows holding images,
PDFs, rendered Word/Excel/text documents or generic download cards, each with
optional per-window notes.

- State lives only in the frontend session, in `ApexProvider` (`windows`,
  `focusedWindowId` + refs). Actions: `windowOpen/Close/CloseAll/Focus/
  ToggleMaximize/ToggleMinimize/Arrange/Next/Previous/SetNote/ToggleNotes/Update`.
- `frontend/lib/windows.ts` is the single model + helper module:
  `kindForName`/`kindForItems`, `titleFromUrl`, `layoutRects` (cascade/grid/
  tile-h/tile-v/center), `collectPreviewableItems` (token, obsidian, camera
  snapshot and raw image/PDF URL extraction from assistant messages — a
  `![alt](url)` counts as an image even without an extension) and
  `groupItemsByKind` (one window per kind: photos share a gallery, a document
  or PDF gets its own) and `windowContextBlock`.
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

## PWA / installable app

APEX installs as a standalone app (own window, own icon, offline shell). The
pieces are: `frontend/public/manifest.webmanifest`, `frontend/public/sw.js`,
`frontend/lib/pwa.ts` and `frontend/components/PwaManager.tsx` (mounted from
`app/page.tsx`). Icons are generated, not hand-drawn: `node
frontend/scripts/generate-icons.mjs` (zero deps, PNG encoder included).

- **The service worker caches exactly two things**: `/` (network-first, used
  only when the network is gone) and `/_next/static/**` (cache-first;
  content-hashed and immutable). Every other request returns from the fetch
  handler *without* calling `respondWith`, so the browser handles it on the
  original code path. That is not a shortcut — it is the requirement. A worker
  that mediates `POST /be/api/chat` can buffer the SSE turn stream, and one that
  touches `GET /be/api/terminal/session/<id>/drain` (polled every 160 ms,
  non-consuming, cursor in the response) makes the terminal look frozen. The
  `NEVER_CACHE` guard in `sw.js` lists `/be` and `/api` explicitly so a future
  edit has to delete a line to break the app rather than making a subtle change.
- **Do not widen the fetch handler** to "just cache the API responses". The API
  is session-bound, CSRF-protected and full of short-lived signed URLs.
  `UNCACHEABLE_PATHS` in `lib/pwa.ts` is the data form of that rule.
- **Bump the version in two places together**: `VERSION` in `public/sw.js`
  (the cache names derive from it) and `SW_VERSION` in `lib/pwa.ts` (it is the
  `?v=` on the registration URL that forces the browser to re-read the worker).
  A mismatch between the two otherwise pins an installed app to an old build.
- **The worker never calls `self.skipWaiting()`.** An update that takes over
  mid-session swaps the cache set out from under a page that may be streaming a
  reply. `PwaManager` shows "update ready", posts `SKIP_WAITING` on the
  operator's click, and reloads on `controllerchange` — but only if a controller
  existed *before* registration. `clients.claim()` fires `controllerchange` on
  the first install too, and reloading there is an infinite loop.
- **`beforeinstallprompt` is captured before hydration.** Chrome fires it at
  most once per load and does not re-emit if nothing is listening; React attaches
  its listener only after hydration, so a heavy first paint can lose the offer
  and the install button never appears. `INSTALL_CAPTURE_SCRIPT` in `lib/pwa.ts`
  (installed by `app/layout.tsx` via `next/script strategy="beforeInteractive"`)
  stores the event on `window.__apexInstallPrompt` and forwards
  `INSTALL_AVAILABLE_EVENT`; `PwaManager` reads the store on mount. Both are
  tested by executing the real script, so do not re-inline a copy in the layout.
- **Installability needs a secure origin on the address the operator uses.**
  `ensure_https_cert` in `apex` (run by `start`/`restart`) issues
  `.apex/https/cert.pem` at the repo root — gitignored, signed by the mkcert CA, covering
  `localhost`, the host name, `127.0.0.1`, `0.0.0.0` and every address from
  `hostname -I` — and hands it to Next via `--experimental-https-key` /
  `--experimental-https-cert`. Without that, Next's `--experimental-https`
  regenerates `frontend/certificates/localhost.pem` on every start when
  `--hostname` is an IP (Node's `checkHost()` does not match IP SANs) and names
  only loopback, so a LAN browser errors, refuses the service worker, and reports
  `Page.getInstallabilityErrors` → `not-from-secure-origin` — no install option,
  however correct the code is. The browser must also *trust* the mkcert CA: on
  this box it is only in `/etc/ssl/certs` (OpenSSL), while Chromium/Edge use the
  NSS/Chrome root store and `~/.pki/nssdb` has no entry (`certutil` /
  `libnss3-tools` absent). Import `~/.local/share/mkcert/rootCA.pem` through the
  browser's certificate authorities, or install `libnss3-tools` and re-run
  `mkcert -install`. `http://localhost:3000` needs none of this.
- **`public/` is scanned once at `next start` startup.** Adding a file there
  needs `systemctl restart apex-frontend` before it is served; in dev it does
  not. `next.config.mjs` sets `Cache-Control: no-cache` on `/sw.js` and the
  manifest content type, so neither needs a `headers()` change.
- A manifest shortcut deep-links with `?panel=<tab>`. The valid names come from
  `PANEL_TAB_NAMES` in `lib/panelBridge.ts` (not a copy), and `ApexProvider`
  strips the query and waits for a signed-in `user` before sending the panel
  command — on a signed-out launch nothing mounts, and the bridge would
  otherwise retry every 50 ms for 30 s against a login screen.
- Safe-area insets live in `app/globals.css` as `--safe-*` custom properties and
  are read by the fixed chrome; `.apex-stage` uses `100dvh` with a `100vh`
  fallback. `viewportFit: "cover"` in `app/layout.tsx` is what makes the insets
  non-zero. Keep them in step with anything new that is pinned to an edge.
- Tests: `frontend/tests/pwa.test.cjs` executes the real `public/sw.js` in a
  `vm` with a stubbed CacheStorage, so the "never answers these" cases fail if
  the worker starts answering them. It also parses the manifest and checks every
  referenced icon is a real PNG at the declared size.

## SIP calls (phone)

`backend/tools/sip_tools.py` is the whole feature: the `sip_call` tool, the
settings merge, the env-file mirror and the per-call baresip process.
`backend/skills/definitions/SIP.md` is the prompt; `force_sip_skill` in
`skills/manager.py` routes "call me" to it ahead of the host-state skill.

- **The account lives in the settings table and is mirrored into the env file**
  (`backend/.env`, not `/etc/APEX`). `mirror_to_env` writes the *stored* rows,
  never the merged values: merging would write every documented default into the
  file on first save and pin them there forever, and after a password is cleared
  it would read the secret straight back out of the file it had just removed it
  from. `write_env_file` distinguishes *absent* (leave the line alone), *non-empty*
  (replace) and *present and empty* (remove the line). That last case is what
  makes "forget the stored password" real; writing `SIP_PASSWORD=` would pin `""`.
- **Two null sinks, not one.** A call needs APEX's voice to go out and the far
  end's voice to come back at the same time: `<id>_rx` is baresip's
  `audio_player`, `<id>_tx.monitor` its `audio_source`, `<id>_tx` is where APEX
  plays into and `<id>_rx.monitor` is where APEX records from. Reusing one sink
  for both directions feeds the call back into itself.
- **baresip must receive `env=pulse_env()` too.** The systemd backend lacks
  `XDG_RUNTIME_DIR`. Without it, `pulse.so` fails to connect while SIP still
  rings and establishes a silent call. Giving only pactl/paplay/parec the
  desktop environment is insufficient; the SIP process needs the same one.
- **baresip runs on a pty, not a pipe.** `/dial` and `/hangup` are registered by
  the *menu* module, and the menu is only instantiated when the app starts, which
  needs a terminal. On a pipe baresip prints "baresip is ready" and then answers
  `/dial` with `command not found (dial)` - no call, no error, nothing to
  notice. The slave is put in raw mode so the line discipline does not echo the
  command back, which would otherwise be indistinguishable from baresip talking.
  The menu is loaded as `module_app menu.so` and *not* also as `module menu.so`:
  both lines load it, the second reports "module already loaded", the app never
  starts, and the commands go missing again.
- **Codecs must be loaded before `account.so`.** account.so parses the `accounts`
  file the moment it loads, so with it first the account binds no codec and
  every call dies with "no common audio codecs" - or worse, an established call
  carrying silence. The order in `write_baresip_config` is load-bearing.
- **An account naming a transport no module carries never registers**, with
  "Destination address required" - a message that points at the network rather
  than at a missing `tls.so`. `available_transports()` reports what this host can
  carry, the settings tab offers only that, and `start_call` refuses up front.
- **The answer is recognised by baresip's own wording** (`Call established: <peer>`)
  and a rejection is detected instead of waited out, so a number that does not
  exist says so in a second rather than ringing for the full `RING_TIMEOUT_SECONDS`.
  The reader starts at a mark taken when `/dial` went out, not at "now": a phone
  answering inside the 2s registration settle has already written the line by the
  time the reader starts.
- **`poll_method select` is not optional here.** This box's container refuses
  `epoll_ctl` on stdin, and with epoll baresip does not start at all.
- **G.711 only.** A codec the far end lacks gives an established call carrying
  silence, which is worse than a call that plainly fails to connect.
- **The conversation budget starts at answer, not at `/dial`.** Ring time is
  outside it, otherwise a phone that rings for twenty seconds is hung up the
  instant it says hello and the symptom looks like a server fault. An unanswered
  call is bounded separately by `RING_TIMEOUT_SECONDS`.
- **`confirm=true` is required for `action="call"`**, and the default action is
  `status`. The plan returns the resolved destination and asks for agreement
  first. `plan_call` never dials even with `confirm=true`.
- **`Path("")` is `.`, which exists.** An unset `SIP_WHISPER_PYTHON` would
  otherwise pass the interpreter check and be executed as a directory, and a
  44-byte header is treated as "nothing was recorded" - checked before the
  setup guard so silence is never reported to the model as a failed hearing.
- **The destination is allowlisted** (`[+0-9*#,A-Za-z._@-]`), and a complete
  `sip:`/`sips:` URI is matched by a separate pattern that admits no whitespace.
  It reaches baresip on stdin, which splits on spaces, so anything looser lets a
  destination append a second command to the same line.
- **Secrets never travel to the browser.** `public_settings()` *deletes*
  `sip_password` and adds `sip_password_set`, so a client cannot round-trip a
  placeholder back over a real password; an empty posted password keeps the
  stored one, and clearing is the separate `POST /api/sip/clear-password`.
  `redact()` masks by parameter name as well as by value, because baresip echoes
  the account line itself.
- **Speech recognition lives in its own virtualenv.** `faster-whisper` pulls
  torch-scale binaries, so `backend/tools/sip-whisper-requirements.txt` pins
  `huggingface_hub<0.26` (0.26 dropped the `open(**kwargs)` download shape) and
  `av<14` (PyAV 14 dropped `metadata_errors`) - both break at transcribe time,
  not install time, so an unpinned install looks fine until the first call.
  `sip_whisper.py` imports nothing from APEX, because it runs under an
  interpreter that cannot see the backend on `sys.path`.
- Sessions are owner-scoped in memory, one at a time (`MAX_CALLS = 1`), and
  `sip_call` refuses to run without a `ToolContext.user_id`.
- Tests: `backend/tests/test_sip_tools.py`.

## Camera, vision and showing the picture (VISIO)

`backend/tools/visio_tools.py` owns the `visio` tool and `GET /api/visio/frame/<token>`;
`backend/skills/definitions/VISIO.md` is the prompt and `force_visio_skill` in
`skills/manager.py` routes camera requests to it ahead of the classifier.

- **"Show me what you see" is two requests in one sentence**, and one snapshot
  answers both: the tool emits `{"type": "visio_frame", "url": ...}`, and
  `ApexProvider` opens that URL in a desktop window, while the tool result's
  `description` is what the model reports. Opening from the **event** rather than
  by scanning the reply is deliberate — a model paraphrasing the sentence must not
  be able to drop the window, and the signed URL has no image extension so
  `collectPreviewableItems` would not find it anyway. `visioFrameMsgRef` keeps
  that scanner from opening it a second time if the model does write it out.
- **Whether the picture is wanted at all is the model's call, via `show`.**
  `show=true` (the default) stores the frame and opens the window; `show=false`
  stores nothing and the reply is words only ("describe what you see"). The
  default is deliberate: a forgotten flag costs one extra window, never the
  picture the operator asked for. Validate it before the camera is touched.
- **A frame shown is stored on disk, not in RAM.** `_store_frame` writes the
  JPEG to `DATA_DIR/visio/<user>/<key>.jpg` (0600, `O_EXCL`) behind a signed
  token naming the owner, swept after `_FRAME_TTL_SECONDS` (7 days) and capped
  at `_FRAMES_USER_MAX` per user. RAM was the wrong store: the window died on
  the first restart or sweep, which is exactly when the operator is looking at
  it. `action="save"` remains the only thing that writes into the user's
  Pictures folder; a preview never lands in a user folder. `Cache-Control:
  private, no-store` keeps a proxy serving past the TTL.
- **A frame belongs to the person who captured it.** The ticket's `u` must
  match the session user, `_frame_path` only resolves inside that user's
  directory, and the route answers 404 rather than 403, so another user's
  frame is indistinguishable from one that never existed. `preview_tools.
  _resolve_source` has a matching branch so `windowSource` can render the
  snapshot through `/api/preview/render` like every other signed link.
- **The window layer decides what shows and how many windows.** The reply
  scanner (`collectPreviewableItems` → `groupItemsByKind`) opens one window
  per kind — photos share a gallery window, a PDF or document gets its own —
  and treats `![alt](url)` as an image even without an extension, because a
  web image search result often has none. `backendFileHref`,
  `isSignedPreviewToken`, `isPreviewableUrl` and `kindForName` all know the
  `/api/visio/frame/<token>` shape; a new signed URL shape has to be added to
  all of them or its window silently shows the "IMAGE UNAVAILABLE" card.
- **`force_visio_skill` needed "show me what you see".** The regex knew "what do
  you see" but not an imperative in front of it, so the phrasing an operator
  actually uses was answered from training data instead of turning the camera on.
  That form is anchored to the start of the utterance — unlike the "what do you
  see" form — because "how do I write a program to show me what you see" is a
  question *about* the feature and must reach the agent.
- **`_strip_accents` does not fold final sigma.** It removes diacritics and
  nothing else, so Greek reaches these patterns with ς intact. The Greek
  alternatives list both spellings (`τι βλεπει[σς]`); the pre-existing
  `τι βλεπεις` had been dead for as long as it had been there. This is the same
  trap `normalize()` documents on the frontend, in the one place that does not
  call it.
- Tests: `backend/tests/test_visio_tools.py` (`PreviewTests`, routing), `test_visio_routes.py`
  (`VisioFrameRouteTests`), `test_preview_tools.py` (snapshot branch),
  `frontend/tests/windows.test.cjs` (visio URL shapes, `groupItemsByKind`).

## Voice prompt, secrets and mail routing (`skills/manager.py`)

`SkillManager.build_system_prompt` appends two cross-skill rules every turn, so
do not re-state them in a skill prompt:

- a **secrets** rule, placed after the skill body so it outranks the skill:
  never read, `cat`, quote, summarise or return the contents of any `.env` file
  (the backend's or a pack's); name the variable and leave editing to the
  operator. SIP's `redact()` and the `set_env`/`create_script` guards already
  keep secrets out of tool output; this is the model-facing half.
- a **voice** rule (only when `voice_mode`), appended **last** so it is the final
  word and outranks the `shell` skill's "return output verbatim": the whole reply
  is spoken by TTS, word for word, so the reply *itself* must be 1-3 plain
  sentences of meaning — no code blocks, tables, `Filesystem ...` lines, paths,
  logs or exit codes (a `df -h` becomes "the disk is 71% full"). A prompt is a
  request, not a guarantee, so the browser enforces it too:
  `frontend/lib/voiceCommands.ts::speechSummary` strips fenced blocks and
  markdown table rows from the reply before `voice.ts::speak` hands it to TTS.
  The rule must never be the only defence — write the prompt *and* keep the
  sanitizer in sync. Tests: `frontend/tests/voiceCommands.test.cjs`.

`force_email_skill` is the fourth deterministic router (after VISIO and SIP,
before the host guard, in `route_skill`). It matters because the email skill is
user-made and acts through `run_shell`: "run df -h and email me the result"
matches the host-state patterns and would otherwise be forced to `shell`, which
owns the terminal but cannot send. The email skill is discovered, not
hard-coded, from a skill's **name or description** only (never its prompt body —
VAPT lists `smtp-user-enum`). Fires on an address or a mail word (EN + Greek),
returns `None` when no email skill is loaded, and `_CODEISH_RE` still wins so
"write a python function to send email" reaches the code skill. Tests:
`test_skill_routing.py` (`ForceEmailSkillTests`, the route-bypass case) and
`test_skills.py` (`VoicePromptTests`, `SecretPromptTests`).

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
