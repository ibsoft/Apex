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
- `SkillManager.delete` removes a user skill from disk **completely**: the
  `<name>.md` definition and the whole `<name>/` pack directory - scripts,
  tests, README, the `.env` secrets and the `.env-example` - matching what the
  UI confirmation promises ("definition, scripts and saved environment
  variables are removed permanently"). It refuses built-ins and anything whose
  source is not a direct child of the user skills directory, so a crafted name
  can never reach outside it. `DELETE /api/skills/<name>` is that method behind
  the browser's confirm modal.

### Runtime skill packs (the skill_creator skill)

`skill_creator` builds a whole pack, not just the `.md`:

```
DATA_DIR/skills/<name>.md       definition        (create_skill)
DATA_DIR/skills/<name>/         pack directory
DATA_DIR/skills/<name>/.env     script secrets    (set_env scope=skill, 0600)
DATA_DIR/skills/<name>/.env-example   key-only mirror of .env (write_env_example)
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
- **A pack `.env` always has a key-only `.env-example` sibling.** Nothing is
  private in an env file except its values, so the example - comments, names,
  ordering, every value masked - is the sharable/shippable form. `set_env`
  (scope=skill) refreshes it on every write or removal, and
  `create_skill_pack` ships one from the start, so the convention holds with
  no extra step. The implementation is `write_env_example` in
  `tools/envfile.py`, the file that owns env line editing - never copy its
  masking into a prompt or a script. The backend's own `backend/.env.example`
  is separate: hand-authored and tracked, and deliberately *not* touched by
  the mirror (values there default to documented examples, not to zero).
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

`frontend/components/ChatUI.tsx` renders messages in `MessageBubble`. The body
goes through three pure parsers (all node-tested, because the component is not):

1. `lib/markdown.ts::splitFencedCode` pulls fenced blocks out first. A
   ```geometry block paints as SVG via `parseGeometry` (`lib/geometry.ts`) and
   `GeometryBlock`; anything else is a `CodeBlock` (language label, COPY,
   scrollable body). An unclosed fence runs to the end on purpose - that is what
   a streaming reply looks like before its terminator arrives.
2. `lib/math.ts::splitMath` splits the remaining prose into text and math
   ($...$ / $$...$$, plus \( \) and \[ \]; a `\` escapes the next char, and
   "costs $5 and $7" stays money). `MathSpan` calls KaTeX
   (`renderToString`, `throwOnError:false`); KaTeX CSS is imported in
   `app/layout.tsx`.
3. `renderRichText` scans each text run for URLs/images into `<a>`/`<img>`;
   `InlineImage` hides broken images on `onError`.

The geometry DSL is a fixed 0..100 box (`viewBox "-5 -5 110 110"`); one
directive per line, `#` comments, and a bad line is reported in the block rather
than silently dropped. `SkillManager.build_system_prompt` appends a cross-skill
note telling the model this syntax exists (skipped in voice mode, where a
formula would be read out as raw TeX). Add new syntax in the pure parser and its
test, then teach the model in that note.

## Streaming replies: the queue and mid-turn steering

The composer no longer freezes while a reply streams. `ApexProvider` keeps a
single ordered queue (`queueRef` is the list, `queued` state is only its render
mirror, `busyRef` is the synchronous "a turn is running" gate).

- `sendMessage` handles `/steer <instruction>` **before** its busy check
  (`parseSteerCommand`, a pure helper tested in `commands.test.js`): with a turn
  running it queues the instruction and POSTs `/api/steer`; with none it just
  sends the text.
- A plain message typed while busy is **enqueued, not dropped**, and rendered by
  `QueuedBubble` with a QUEUED tag; the operator can cancel it. A queued `/steer`
  is tagged STEER. When the turn ends an effect drains the queue one item at a
  time (`sendRef`/`busyRef` are read, not the closure's `busy`, which is a
  render stale by then).
- Switching or creating a conversation is blocked while busy: the queue is not
  per-conversation, so a drain must land in the thread it was typed in.
- **A steer is only removed from the queue when `steer_applied` arrives.** That
  event is the model's confirmation it was folded in; until then the instruction
  is safe, so an engine that cannot consume steers (or a turn that ended first)
  simply leaves it to be sent as an ordinary message. Never drop a steer on the
  POST result alone.
- The server half is `backend/agent/steer.py` (`SteerInbox`, process-wide
  `INBOX`) and `POST /api/steer` (ownership checked like `/api/chat`; another
  owner answers 404). Only `agent/responses.py` calls `INBOX.begin`/`end` - it is
  the engine that knows how to consume a steer, and `begin` is what makes
  `push` report `active`.
- **A text-only answer is a single model call, so a steer has no step to wait
  for.** When one appears mid-stream the engine abandons the current call
  (`chunks.close()`), keeps the partial text, appends the instruction as a user
  message and starts a fresh call - and drains again after every tool round,
  where no model call is in flight to catch it. Each steer costs a step, so the
  `MAX_TOOL_STEPS` ceiling still bounds the turn.
- `app.py` records an applied steer as its own user message (`{"steer": true}`)
  as the event streams through, so it is in history after a reload - the local
  `messages` list the engine edits is never persisted.
- Tests: `backend/tests/test_steer.py`, `frontend/tests/commands.test.cjs`.

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

## The `apex` admin script (`./apex`)

The single operator entry point. It runs on the machine, so it is the one place
that touches systemd, `sudo`, apt, and the network — and the only place a new
dependency should be added.

- `./apex doctor [fix] [--whisper]` checks the environment and this repo and
  (with `fix`) installs what is missing. The check has **two tiers**: a "core"
  set whose absence stops APEX from running and drives the exit code (`git`,
  Python 3.10+ and the `.venv` with `backend/requirements.txt` installed, Node
  18+ and `frontend/node_modules`, `backend/.env`), and a "feature" set whose
  absence only disables a capability (`ffmpeg` → camera/voice notes, `baresip`
  + PulseAudio + `espeak` → phone calls, `openssl` + the mkcert CA → LAN PWA
  install, PAM → machine sign-in, `rsync`, `nmap` → VAPT, the Exploit-DB mirror,
  the speech-recognition venv). A missing optional tool is a warning and never
  fails the run — a health check that failed on a missing `nmap` would be
  useless. It exits non-zero only when core is unhealthy, and `doctor fix`
  re-runs the check after repairing.
- The checks are split into `doctor_checks` (prints + sets the flags) and
  `doctor` (driver, optional repair, re-check), so `fix` can re-run the same
  checks rather than duplicating them. **The function must `return
  "$DOCTOR_CORE_FAIL"`**: `doctor_checks || true` is there to survive
  `set -e`, but it swallows the status, so a driver that does not return
  explicitly always looks healthy to the shell.
- **`ldconfig -p | grep -q` is a trap under `set -o pipefail`**: `grep -q`
  closes the pipe on the first match, `ldconfig` dies of SIGPIPE (141), and the
  *pipeline* reports failure even though the library was found. The PAM check
  captures `ldconfig -p` into a variable and string-matches instead; the Python
  version check compares numerically rather than `… | sort -V | head -1`, which
  has the same SIGPIPE shape. Any new probe that pipes into a reader which
  exits early must capture first.
- `doctor fix` only installs what it can do safely and idempotently: create
  `.venv` + pip install, `npm install`, copy `backend/.env` from the example,
  clone the Exploit-DB mirror, run `scripts/install_sql_drivers.py`, and
  `apt-get install -y` the collected packages (deduped) via `sudo`. The
  speech-recognition venv is heavy, so it is opt-in behind `--whisper`. Node is
  *not* apt-installed (the distro package can be too old for Next 15) — a
  missing Node is reported with nvm/NodeSource guidance.
- `./apex update [ref]` snapshots the live SQLite DBs through SQLite's backup
  API, overrides the checkout with git, restores the DBs and `backend/.env`,
  then reinstalls deps, rebuilds the frontend and restarts. `./apex start`,
  `stop`, `restart` and `status` drive the systemd units and the port-3001 HTTPS
  dev frontend. Adding a command means: a `case` branch, a `usage()` line, and
  a README entry.

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

1. `frontend/lib/commands.ts` is the deterministic half - but the two entry
   points run it in opposite places, and that difference is deliberate.
   **Voice (`handleVoiceCommand`) runs the resolver first and the parsers
   second**: a spoken instruction is phrased freely ("bigger", "again", "that
   one", "put it back"), so it is read against the screen and the recent
   conversation first, and the keyword lists are only the fallback for what no
   catalogue can express (a reminder's text, a skill name). **Typed text
   (`sendMessage`) still runs the parser first**, because typing is deliberate
   and the deterministic path is instant - the reasoning layer only sees what it
   missed. The order in each path is asserted by
   `frontend/tests/commandSpec.test.cjs`, not merely documented. The bulk
   vocabulary (`bulkWindowAction`, one function for windows, terminals, file
   managers and notepads) is the fix for the cheap half of the problem - a
   missing regex slot. A request is bulk when a quantifier is present **or** the
   noun is plural on its own; a singular noun with no quantifier is deliberately
   not bulk, so "restore terminal" keeps meaning the focused window.
2. `frontend/lib/commandSpec.ts` describes what the browser can do to itself:
   `LOCAL_ACTIONS` (the catalogue), `buildUiState` (windows with the numbers
   shown on screen, their kind, minimized/maximized/desktop, plus tasks, plus
   whether a player is open) and `sanitizeActions`, the trust boundary.
3. `backend/tools/command_router.py` + `POST /api/resolve-command` ask a model
   to read one utterance against the catalogue, the state and (optionally) a
   `context` block, and answer with `{"actions": [...]}`.
   `frontend/lib/commandContext.ts::commandContextBlock` builds that block from
   the recent exchange, the open player's queue and the active skill; the route
   clips it (`MAX_CONTEXT_CHARS`), fences it in the prompt as data *and folds it
   into the cache key* - the same words against a different exchange are a
   different question, which is exactly what "do that again" would otherwise
   hit. `COMMAND_ROUTER_ENABLED` (default on) and `COMMAND_ROUTER_MODEL`
   (optional; ignored if the provider cannot serve it) are in
   `backend/config.py`.

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

### Interrupting a reply by talking (barge-in + "continue")

Beyond the always-on wake-word barge-in (saying the wake word during `speaking`
cuts the reply), the operator can opt into *voice-activity* interruption with a
checkbox + sensitivity slider in the settings tab
(`voice_interrupt_enabled`, `voice_interrupt_sensitivity` 1..10, persisted
through `/api/settings`). Both are client-side behaviour; the backend only
validates and stores them.

- While `speaking`, a second mic stream opened with
  `{ echoCancellation: true, noiseSuppression: true, autoGainControl: true }`
  feeds an `AnalyserNode`; echo cancellation is what keeps the assistant's own
  voice out of the "user is talking" signal. A run of loud frames (RMS above a
  sensitivity-derived threshold) calls `voiceInterrupt`.
- The pure decision logic — `sensitivityToThreshold`, `vadStep`,
  `initialVadState` — lives in `voiceCommands.ts` and is unit tested; `voice.ts`
  only wires it to the analyser and the `speaking` phase. The VAD is gated for
  ~400 ms at the start of a reply so its opening words cannot cut it off, and it
  ignores every phase except `speaking`.
- **A wake-word barge-in and a voice-activity interrupt are different actions.**
  The wake word is a deliberate new command, so it keeps no remainder. A
  voice-activity interrupt may be accidental: `voiceInterrupt` saves the untold
  remainder (the part of the in-progress chunk after the last `onboundary`
  `charIndex`, plus the queued chunks) into `resumeRef`, arms the follow-up
  window so the user's words are collected, and drops the TTS echo from the
  accumulator. Saying "continue" (`isResumeCommand`, EN + Greek) then resumes
  from where it stopped.
- `resumeRef` is only consumed when it actually holds a remainder; a plain
  "continue" with nothing to resume still reaches the agent as a normal request.
  A fresh `speak()`, a completed reply, sleep, and mic-off all clear it, so a
  stray "continue" can never resurrect an old reply.
- If an engine never fires boundary events, `spoken` stays 0 and the in-progress
  chunk replays in full — a small repeat, never a lost tail.

### Reminder alarms (a sound that rings until stopped)

`frontend/lib/alarm.ts` is the melody (`FANCY_ALARM_NOTES`, `alarmLoopSeconds`),
`AlarmOverlay.tsx` is the banner and `ApexProvider` owns the ring. A fired
reminder is not a timer that chimes once: it moves out of the schedule and into
`alarms`, and a single looping Web Audio cycle plays until the operator stops it.

- **The alarm is stopped by voice without a wake word.** `voice.ts` gets
  `alarmActive` + `onAlarmStop`, and checks `isAlarmStopPhrase` at the top of
  `onResult` in every phase. "Apex stop" is handled by slicing the wake word
  first; a bare "stop" / "σταμάτα" is only a stop *because* `alarmActive` is
  true, so with nothing ringing it is an ordinary command. This is why the check
  cannot live in the pure parser — the parser has no alarm state.
- `isAlarmStopPhrase` (in `commands.ts`) is anchored to the whole utterance, so
  "stop the timer" / "stop the download" stay requests; only object-less stop
  phrases and explicit alarm nouns match. Tests: `commands.test.cjs`,
  `alarm.test.cjs`.
- STOP and the X both clear the alarm (clicking elsewhere does not — a dismiss
  must be deliberate); `stopAlarm(id)` keeps ringing while other alarms remain
  and only tears the audio graph down when the last goes.

### Changing the reply language by voice

`parseLanguageCommand` in `commands.ts` turns "from now on we will speak Greek"
into `{ type: "language", language }` (EN + Greek). `executeLocalCommand`
persists `response_language`; the voice engine watches that setting and re-arms
itself (recognition language and TTS voice) — the "self restart". It is
deterministic, so it never spends a model turn. The matcher is anchored so a
sentence that merely mentions a language reaches the agent, and it runs on
`normalize()`d text: any Greek written in it must use folded forms (`μιλάς` ->
`μιλασ`, `εξής` -> `εξησ`), or it silently never matches.

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

## Multipart uploads bypass the /be rewrite

`GET/POST /be/api/*` goes through the Next rewrite to
`http://127.0.0.1:5001/api/*`. The rewrite clone-streams request bodies, which
re-sends them to gunicorn **chunked** (no Content-Length); gunicorn closes on
chunked bodies, so any upload past ~100 KB dies client-side as a connection
reset ("Failed to fetch") — symptoms look like a server fault but the backend
never sees the request.

Multipart uploads therefore **must not** use the rewrite. They go through
`frontend/app/api/upload/{memory,vision}/route.ts`, which read the whole
(`arrayBuffer`) body — Flask caps these at 10 MB — and forward it to the backend
with the original Content-Length, which gunicorn accepts at any size. The
forwarder copies `content-type`, `cookie` and `X-APEX-CSRF` and truncation-check
returns 413. `memory.upload` in `lib/api.ts` posts to `/api/upload/memory` and
`vision.upload` to `/api/upload/vision`. Add a new upload endpoint the same way
(a `forwardUpload` call in a route handler) instead of calling it through `/be`.

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

## Sub-agents (AGENTS tab)

Named autonomous workers the operator (or APEX) defines, each with its own model,
tool scope and workspace. `backend/subagents/` is the whole feature:
`manager.py` (owner-scoped CRUD, workspace provisioning, run orchestration and
the concurrency cap), `runner.py` (one run = one agent turn in its own
conversation), `routes.py` (the `/api/subagents*` REST surface), and
`tools/subagent_tools.py` is the model-facing `subagent_*` tools.
`frontend/components/AgentsPanel.tsx` is the tab.

- **A run is a full agent turn, so it is capped.** `SUBAGENT_MAX_CONCURRENT`
  (default 2) is a process-wide `BoundedSemaphore`; a background run that cannot
  take a slot finishes `error` with "too many sub-agents are running", never
  queues. `SUBAGENT_MAX_PER_USER` (default 25) bounds creation and
  `SUBAGENT_TIMEOUT_SECONDS` (default 900) bounds a run.
- **A sub-agent never gets the terminal or sub-agent tools.** `_SUBAGENT_EXCLUDED`
  in `runner.py` is applied as `exclude_tools` on its `AgentContext`; the
  terminal opens a browser window (pointless and window-littering for an
  unattended worker) and the management tools would let an agent recursively
  delegate. Recursion is closed by construction, not by prompt.
- **Its tool scope is `strict_tools`.** An empty tool list means "all tools",
  a non-empty list is exactly those names — the strict path in
  `AgentContext.active_tools` skips the always-on force-include (terminal,
  notepad, tasks, sub-agent tools) that a normal turn gets, so an agent's tools
  are what the operator picked and nothing else. `run_shell` runs in the agent's
  workspace (`ToolContext.workspace`), so shell work is confined to a directory
  the agent owns.
- **Ownership is a SQL predicate**, as everywhere else: another user's agent
  answers 404, never 403.
- **The reporting contract is the point.** `subagent_delegate` runs one agent
  *synchronously* and returns the report as the tool result, so APEX holds it and
  answers the operator in the same turn (chat or spoken). `subagent_fanout` is
  the same contract for many jobs: it takes a list of `{agent, task}`, runs them
  in parallel (`SubAgentManager.run_many`) and returns **every** report at once,
  so APEX composes one answer from all of them. A background run (Run Now, or an
  agent that finished with no browser attached) has no browser to answer, so
  `subagent_context_block(user_id, db)` appends the roster plus any unfinished
  report to the next turn's system prompt; `chat()` in `app.py` records the
  report ids it included and calls `db.mark_subagent_runs_read` once the turn's
  reply is produced. Without that ack the same report would be relayed on every
  later turn. The delegate and fan-out tools ack their own synchronous runs for
  the same reason. All acks are best-effort (`try/except`): a failure to mark
  seen repeats a report, never loses one.
- **APEX delegates on its own initiative, so the block is always present.**
  `subagent_context_block` returns its rules even with an empty roster — APEX
  cannot decide to create an agent for a complex job if it is never told the
  capability exists. The block tells it to *reuse first* (only `subagent_create`
  when no existing agent fits, and silently — no permission step), to split a
  multi-part job and run it through `subagent_fanout`, not to over-delegate a
  small task, and to tidy the roster with `subagent_cleanup`.
- **`run_many` queues beyond the concurrency cap, unlike `run_now`.** A single
  background run that cannot take a slot is `error` ("too many sub-agents are
  running"); a fan-out would silently drop jobs that way, so it runs on a
  `ThreadPoolExecutor` bounded by `_slots` and waits, returning a result triple
  `(job, run_or_None, error)` per job. A paused or foreign agent in the list
  fails only its own job (`error` string), never the batch.
- **`subagent_cleanup` deletes only honestly-stale agents.** It removes agents
  whose most recent run (or `created_at` if never run) is older than
  `older_than_days` (default `SUBAGENT_STALE_DAYS`, 30), owner-scoped; workspaces
  are left on disk. `SUBAGENT_FANOUT_MAX` (default 8) caps one fan-out call.
- `subagent_context_block` is appended to the system prompt as build-time,
  non-persisted context (like the task and window blocks), gated on
  `SUBAGENTS_ENABLED`.
- Events reach the browser through `ctx.emit`: `subagents_changed` (a model tool
  created/edited/deleted one), `subagent_started`, `subagent_progress`
  (throttled), `subagent_tool` and `subagent_report`. The tab re-reads on them —
  the event says *that* something changed, not the row. A fan-out's events are
  buffered until the blocking tool returns, so `AgentsPanel` also polls
  `loadSubagents({quiet:true})` every 5s while it is mounted, to show a run as it
  happens rather than only when it ends.
- Tests: `backend/tests/test_subagents.py`, `frontend/tests/subagents.test.cjs`.

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

## Universal integrations (MCP / SQL / REST / GraphQL)

`backend/integrations/` is the whole feature; `frontend/components/McpPanel.tsx`
is the MCP tab. One panel manages connections to MCP servers (stdio/http/sse),
SQL databases (PostgreSQL, MySQL, SQL Server, SQLite, Oracle), REST APIs
(discovered from an OpenAPI document) and GraphQL endpoints. Connections are
owner-scoped rows in `DATA_DIR/integrations.db` (chmod 0600); credentials are
Fernet-encrypted with a key derived from `SECRET_KEY` (PBKDF2,
`b"apex-integrations-v1"`) and are **never** returned to the browser — the API
returns only `credentials_set`, the *names* of the secrets it holds.

- **Read-only is the default and a server-side control.** `readonly=true` and
  `allowed_operations=["select"]` unless the operator changes them; the panel
  cannot be trusted to enforce it. Writers must also be enabled explicitly.
- **Per-connection policy lives in the record, not the config dict.**
  `readonly`, `allowed_operations`, `allowed_tables`, `denied_tables`,
  `max_rows`, `timeout_seconds` are stored as top-level columns. The connectors
  read them from the *config* dict they are handed, so
  `IntegrationManager._effective_config(record)` merges the record-level policy
  into that dict and is used by `_connector`, `_query_rest` and `_query_graphql`.
  Skipping the merge silently drops the operator's policy — the connection runs
  with library defaults instead. When a new connector appears, its manager entry
  point must go through `_effective_config` too.
- **Query validation is dialect-aware** (`dialects.py`): `validate` blocks
  `DROP`/`ALTER`/`TRUNCATE` and other DDL, applies the table allow/deny lists,
  enforces single-statement reads when `readonly`, and `apply_limit` injects the
  correct row cap per engine (`has_limit` — SQLite/PostgreSQL/MySQL use `LIMIT`,
  SQL Server uses `TOP`, Oracle uses `LIMIT`/`FETCH`/`ROWNUM`). Getting a key
  wrong there meant a write was classified as a read, or the cap was never
  applied.
- **MCP is a real subprocess, not a stub.** `mcp_client.py` speaks the official
  `mcp` SDK lazily (the import lives inside the methods, so the backend runs
  without the package). `McpConnector` adapts an MCP server to the connector
  protocol (`test/discover/call_tool/read_resource/get_prompt/describe/close`)
  so `manager._connector` and `_query_mcp` treat it like any other kind.
  - **SDK attribute names are snake_case**: the call result exposes
    `structured_content` (not `structuredContent`) and `is_error` (not
    `isError`). `_attr()` reads either spelling so an SDK rename cannot silently
    turn a structured result into an empty one.
  - Transports are stdio (default), http and sse; the kinds catalogue reports
    `mcp: ["mcp"]` with a separate `transports` list, and the form offers only
    the transports the SDK build supports.
  - **Remote MCP auth is an encrypted secret, not plaintext config.** The
    `mcp` kind's secret fields are `bearer_token` and `api_key`;
    `build_mcp_client` merges credentials into the config dict (setdefault) and
    `HttpMcpClient._headers` turns either into `Authorization: Bearer …`
    (falling back to `config["headers"]`). A stdio server's non-secret env vars
    ride in `config["env"]` (the form has a KEY=value box; it is labelled as
    plaintext, so tokens belong in the secret fields).
- **The model's provider stream is a generator of typed dicts.**
  `provider.chat_stream(messages, tools=None)` is iterated directly;
  `{"type":"text","content":...}` carries output and `{"type":"error"}` aborts.
  `nl_query._collect` matches that shape (and tolerates a callable/delta shape);
  a stub that feeds a different shape would pass while the real provider fails.
- **The per-connection prompt is appended to the turn, filtered by skill.**
  `IntegrationManager.blocks_for_skills` returns the block only for connections
  whose `skills` list is empty (all skills) or names the active skill; `chat()`
  appends it as build-time, non-persisted context, capped at
  `INTEGRATIONS_PROMPT_MAX_CHARS`.
- Tools (`integration_status`/`integration_query`/`integration_discover`) are
  force-included for every skill when `INTEGRATIONS_ENABLED`, the same way
  `terminal_command` is, and the handler re-reads the store each turn.
- The health monitor is a daemon thread started in `register_integration_routes`
  (not a route); `INTEGRATIONS_ENABLED=false` registers neither routes nor
  tools.
- **`routes.py` never raises a driver error as a 500**: an absent SQL driver is
  a normal "driver not installed" message, and another owner's connection
  answers 404, never 403 (the same object-access rule as the rest of the API).
- **Missing dependencies install themselves (`integrations/deps.py`).** The
  operator is never asked to run apt/pip. `load_driver_module` pip-installs a
  Python driver (psycopg2-binary / pymysql / pyodbc / oracledb) on the first
  ImportError; for SQL Server, `resolve_sqlserver_driver` first uses the named
  or registered ODBC driver, then an already-unpacked library, and otherwise
  downloads Microsoft's `msodbcsql18` **.deb** and unpacks it into
  `INTEGRATIONS_ODBC_DIR` (default `DATA_DIR/odbc`, no root) - pyodbc is handed
  the absolute `.so` path (`DRIVER={/abs/path/libmsodbcsql-18.x.so}`), which
  unixODBC loads directly, no `odbcinst.ini` needed. `_precheck` calls this
  before the driver's own opaque error; the result (including failure) is cached
  per process so a dead network is not retried per request. `INTEGRATIONS_AUTO_INSTALL`
  (default on) is the off switch; `INTEGRATIONS_ODBC_DEB_URL` pins a version.
  `_version_key` picks the newest package from the repo index exactly as the
  cron note warns numbers must be compared, not strings (`18.7 > 18.6`, but
  `"18.10" < "18.7"` as text). Setup pre-warms it via
  `scripts/install_sql_drivers.py` (also `python -m integrations.deps`), which
  `apex update` runs after the pip step. `DriverConnectorTests` disables
  auto-install in `setUp` so the suite never touches the network.
- **The command reasoning layer knows the tab.** `"mcp"` is in
  `PANEL_TAB_NAMES`; the local parser maps mcp / integration / connection (and
  Greek συνδεσ / ολοκληρωσ) to the tab, and the router catalogue renders the
  list from `PANEL_TAB_NAMES`, so a new tab name only has to be added once.
- Tests: `backend/tests/test_integrations.py` (real stdio MCP subprocess via
  `tests/support/mcp_demo_server.py`, real loopback HTTP for REST/GraphQL, real
  SQLite for SQL; the model provider is the only stub and it is fed the real
  stream shape; `DepsTests` covers auto-provisioning without the network),
  `frontend/tests/panel-commands.test.cjs` (the tab phrases).

## Audit log and suspicious-activity detection (`observability/`)

`backend/observability/` is a JSONL audit log with rotation, a rule-based
analyzer, agent tools, read-only REST and an autonomous scanner. The point is
that APEX can answer "what happened?" and "was that malicious?" about its own
skills, tools and chats - on request or by itself. Everything is gated by
`OBSERVABILITY_ENABLED` (default on) and `OBSERVABILITY_*` in `config.py`.

One writer, one lock, one file: `observability/store.py::EventLog` appends one
JSON object per line to `OBSERVABILITY_LOG_DIR/apex.jsonl` and rotates at
`OBSERVABILITY_MAX_BYTES` (default 10 MB) to `.1.gz`..`.N.gz` — the active file
stays plain text (cheap appends, tailable) and every rotated generation is
gzip-compressed (`OBSERVABILITY_COMPRESS=false` keeps them plain). `read()`,
`iter_all()` and `stats()` decompress transparently, and `_gzip_to` removes the
active file only after a successful compression, so a rotation that fails never
loses a record. `get_log()` is the process singleton. **`append` redacts
before it writes** (`observability/events.py::redact` masks sensitive *keys* and
truncates long strings), so a caller cannot leak a secret by forgetting - but
redaction is by key name, so a password typed verbatim into a chat message is
still stored. That is the trade the feature makes: it is an audit log.

Four choke points feed it, chosen because each is the only path to its
category:

- **HTTP**: `observability/middleware.py::install` registers a `before_request`
  (start time, and the request body only when `OBSERVABILITY_DEBUG`) and an
  `after_request` (method, path, status, duration, user, redacted
  request/response bodies). It uses Flask's `got_request_exception` **signal**,
  not `@app.errorhandler(Exception)`: a registered handler would intercept the
  exception before Flask's testing/debug propagation and silently change error
  semantics. The signal fires in both paths. `got_request_exception` is also why
  no route 500 is unlogged.
  The hook **skips its own noise**: `_SKIP_PREFIXES = ("/api/logs",)` (the UI
  polls the log API) and `_SKIP_SUFFIXES = ("/drain",)` — the terminal drain is
  a 160ms non-consuming byte-cursor poll (the same path `frontend/lib/pwa.ts`
  refuses to cache), and logging it would churn the rotation and bury real
  security events. The `terminal_command` tool call that produced the output is
  still recorded at the tool choke point, so no forensic signal is lost. A
  streamed/SSE body is never read (`_body` returns `None` for
  `is_streamed`/`direct_passthrough`), so a chat turn stream is not buffered.
- **Python logging**: a root `logging.Handler` forwards records, so every
  existing `app.logger.exception(...)` / `logger.warning(...)` lands in the same
  stream. One handler is reused and re-pointed at the current log so a test that
  swaps the singleton does not double-write. The `werkzeug` logger is quieted -
  the request hook already covers access logging.
- **Tools**: `tools/base.py::ToolRegistry.invoke` logs `args` and `result` after
  every call. This is the single point through which every engine (responses /
  agents_sdk / langgraph) runs a tool, so `terminal_command`'s command string -
  the thing the analyzer scans - is always recorded.
- **Chat / skill / agent**: `app.py`'s `/api/chat` records the user message,
  skill selection, engine error events and the assistant reply. The full reply
  text is stored (truncated), which is what makes "what did the agent do" and
  chat-content forensics possible.
- **Telegram and tasks are chat turns too, and they do not go through
  `/api/chat`.** A fix at the route cannot see them, so both write the same
  `chat_message` / `skill_selected` / `chat_reply` events themselves through a
  local `_audit` helper: `tools/telegram_tools.py::run_message_turn`
  (`source="telegram"`, plus `chat_id`/`username`) and
  `tools/tasks.py::run_task_turn` (`source="task"`, plus `task_id`). A Telegram
  pairing attempt that fails is logged as `event="auth_failed"`
  (`category="auth"`), so the brute-force rule counts a burst of bad pairing
  codes the same way it counts failed logins. **When adding a new way an agent
  turn can start, it needs its own write** - the request hook only sees browser
  traffic and the tool hook only sees tool calls, never the message or reply.

`observability/analyzer.py::analyze` runs signature rules (reverse shell,
`curl | bash`, credential access, privilege escalation, destructive commands,
exfiltration endpoints, crypto miners, firewall/anti-forensics) and behavioural
rules (an auth-failure / CSRF / 5xx burst). Two traps are already handled: it
matches a **flattened** payload (`_event_text` puts each string leaf on its own
line), because matching `json.dumps(args)` puts a quote before the command and
breaks a `(?:^|\s)`-anchored rule - the same "prefix matcher" class of bug the
command router documents; and the recon rule **ignores `vapt_*` tools**, because
VAPT runs nmap by design while the identical command through `terminal_command`
is exactly what must be caught.

- Agent tools live in `observability/tools.py` (`log_query` / `security_audit`),
  built through `tools/observability_tools.py` and force-included for every
  skill via `agent/base.py::OBSERVABILITY_TOOLS`, the same as `terminal_command`:
  the operator should not have to pick a skill to ask what happened.
- `observability/scanner.py` starts one daemon thread per process
  (`start_scanner` is guarded; `reset_scanner` is the test seam), scans the
  recent window and writes a persistent `security_alert` event per finding,
  re-raising at most once per `OBSERVABILITY_ALERT_COOLDOWN` so a sustained
  condition does not bury the log. That is "autonomous mode".
- `observability/routes.py` adds `GET /api/logs`, `/api/logs/stats`,
  `/api/logs/alerts` and `/api/logs/audit`, all `require_user`. They are
  read-only and default to `scope=all` (the audit log is machine-level, and any
  authenticated operator already has full machine control via
  `terminal_command`); `scope=self` filters to the caller's events, and an event
  with no owner (system/security) is visible in both scopes.
- Tests: `backend/tests/test_observability.py` (store + rotation + redaction,
  analyzer rules and their false-positive guards, middleware via a minimal app,
  routes, tools, scanner). Nothing there needs the DB or the live backend.

## Local Exploit-DB (`tools/exploitdb_tools.py`)

The VAPT skill's exploit intelligence: a local clone of the exploit-database
mirror (`searchsploit`, `files_exploits.csv`, `files_shellcodes.csv`, `exploits/`,
`shellcodes/`) that it searches for the product/version it fingerprinted. The
model-facing tools are `exploitdb_search` / `exploitdb_show` / `exploitdb_update`
/ `exploitdb_status`, registered in `load_default_tools` (empty when
`EXPLOITDB_ENABLED` is false); `VAPT.md` has `tools: ALL`, so it gets them, and a
"Fingerprint → exploit intelligence → targeted validation" section teaches the
workflow (e.g. detect WordPress → search `wordpress <version>` → `exploitdb_show`
→ run it with `vapt_run`).

- **Nothing here executes an exploit.** Search and fetch are read-only. The only
  side effects are the one-time `git clone`, the periodic `git pull`, and copying
  an exploit/shellcode file into a project's `artifacts/exploitdb/` as evidence.
- **Search is searchsploit itself**, not a CSV re-implementation:
  `search_exploits` runs `bash <mirror>/searchsploit --json <term>` and parses
  `RESULTS_EXPLOIT` + `RESULTS_SHELLCODE`, normalizing each entry to
  `{id, kind, title, date, platform, type, cves, verified, path}`. `kind` is
  `exploits` / `shellcodes` / `both`. A non-JSON or empty result is reported, not
  raised.
- **Lookup reads the CSVs directly** (`csv.DictReader`, exact `id` match across
  both files) so `exploitdb_show` does not depend on the search script; the file
  is read through `read_entry`, which resolves it inside the mirror root and
  refuses an escape. `copy_into_project` writes `EDB-<id>_<name>` under the
  target's `artifacts/exploitdb/` via `vapt_tools._ensure_project`.
- **First run initializes the repo.** `ensure_repo` clones `EXPLOITDB_REPO_URL`
  (`--depth 1`) into `EXPLOITDB_DIR` when `searchsploit`/CSVs are missing; a
  non-empty-but-incomplete directory is reported, never clobbered.
- **Updates are periodic and pull-only.** `start_updater` (called from `app.py`
  next to `register_vapt_routes`, guarded by `EXPLOITDB_ENABLED` +
  `EXPLOITDB_AUTO_UPDATE`) is a singleton daemon thread that settles ~2 min, then
  pulls when `EXPLOITDB_UPDATE_INTERVAL_HOURS` has elapsed. `update_repo` does
  `git pull --ff-only` and, if upstream history was rewritten, re-syncs with
  `fetch --depth 1` + `reset --hard FETCH_HEAD` (the mirror is disposable). A
  `.apex_last_update` marker in `EXPLOITDB_DIR` records the last success.
- The tools never raise: a git failure, a missing git binary, a dead network and
  an incomplete checkout all return a plain string for the model to report.
- Tests: `backend/tests/test_exploitdb_tools.py` (a throwaway mirror with a stub
  `searchsploit` printing canned JSON — search/kind/limit, lookup, read
  containment, copy-into-project, the four tools and the updater flags).

## Common extension points

- New search source: add a tool in `core_tools.py` (or a new module) and expose
  it through skill definitions.
- New memory ingest: extend `backend/memory/documents.py` for extraction and
  `backend/memory/store.py` for persistence.
- New UI panel: add it inside `frontend/components/ChatUI.tsx` or create a new
  component and wire it through `ApexProvider` if it needs shared state.

## Telegram channel (text/voice chat from the phone)

`backend/tools/telegram_tools.py` is the whole feature (handlers, pairing,
poller, settings) and `backend/tools/telegram.py` is its route module
(`/api/telegram/status|token|clear|pair|unpair`). `frontend/components/
TelegramSettings.tsx` is the settings card built on the same `shell()` modal
pattern as `SipSettings.tsx` (token modal + pairing dialog with a live poll).

- **The token is a single-home secret: the settings DB and nowhere else.**
  There is deliberately no env-file mirror. `SETTING_KEYS` is the exact write
  surface (`telegram_enabled`, `telegram_voice_replies`, `telegram_tts_engine`,
  `telegram_tts_voice`); `telegram_bot_token` is written *only* by
  `POST /api/telegram/token`, which validates the shape with
  `re.fullmatch(r"\d{1,12}:[A-Za-z0-9_-]{25,45}", ...)` and checks `getMe`
  against the live API (10 s `_ME_CACHE`). A settings POST that includes a
  `telegram_bot_token` key is ignored — there is a test for it. `public_settings`
  relabels the flag to `telegram_bot_token_set` (`MANAGED_KEYS`) so a stored
  secret can never be round-tripped.
- **Clearing must be hard.** `revoke_token` drops the DB row, calls
  `unlink_pair`, `cancel_all_pairing_codes`, deletes `os.environ[
  "TELEGRAM_BOT_TOKEN"]` and sets `config.TELEGRAM_BOT_TOKEN = ""` — a cleared
  token must not resurrect from an env fallback. `telegram_clear_config` is the
  route-level wrapper.
- **Pairing is one valid code, one chat, one user.** Codes are 6 chars
  (no vowels / `0Ol1`, so a code cannot spell a word), in memory only, per
  user, TTL `TELEGRAM_PAIRING_TTL_SECONDS` (default 600). Any existing code is
  cancelled when a new one is generated. Redemption is rate-limited before it
  is checked: `_REDEEM_LIMIT` (8) attempts per chat per `_REDEEM_WINDOW` (15
  min) fail silently. `_handle_message` **only ever answers `chat.type ==
  "private"`**, pairs only a `/start` carrying a valid unexpired code, and
  groups/other accounts get nothing. After pairing, only the paired chat runs
  turns; it runs as the browser user who redeemed the code.
- **Secrets and caps on the bot side.** Media download streams with a hard cap
  (`_MEDIA_MAX_BYTES`, 40 MB); a download past the cap is deleted and returns
  `None`, and temp media files are cleaned in `finally`. `redact()` masks
  `TELEGRAM_BOT_TOKEN=...` and `sip_password` values, so no handler output can
  leak the token back to the model.
- **The poller is a daemon thread, not a route.** `ensure_poller` starts it
  when routes register (so a restart is needed after enabling for the first
  time in a deployment that never started it). `_poll_loop` is one long-polling
  `getUpdates` loop; every exception is caught, redacted and recorded in
  `_LAST_ERROR` with a 5 s backoff (guard against an unbound `cfg` in the
  except path). The frontend reads health through the status endpoint, which
  also carries `tts_available`, `whisper_ready`, `ffmpeg_available` and
  `paired_username` but never the token.
- **Voice replies are a reply to the operator's *voice*, never the default.**
  `_handle_message` marks a message `was_voice` when it carried voice media;
  `_reply` synthesizes a spoken ogg/opus note only for that case (and only while
  `telegram_voice_replies` is on) and always falls back to text. A **text**
  message is answered with plain text — fast, full-length, tools and all — with
  no synthesis on the path at all. `run_message_turn(spoken_reply=...)` tells
  the model the answer will be read aloud (1-3 spoken sentences) only for voice
  notes; text turns get the unrestricted "answer fully, run the command" prompt.
- **The engine sees `ctx.history` verbatim.** `run_message_turn` (and
  `run_task_turn` in `tasks.py`) stores the new message in the DB *and* appends
  it to `history` — the model's `messages` are `[system] + ctx.history`, so a
  message that is only stored is a message the model never received, and it
  answers whatever the previous turn was (it "re-runs the last command"). The
  chat-UI path in `app.py` folds the new message into history the same way; do
  not reorder the two. This is the same class of bug as the window-remount
  cursor one: the DB is not the model's input. Tests:
  `TelegramTurnTests::test_the_just_sent_message_is_the_models_last_instruction`.
- **A turn announces itself as "typing…"**. `_chat_action` fires a
  `sendChatAction` before the model runs, because an agent turn is seconds long
  and the operator otherwise sees a silent bot and re-sends the message.
  Voice replies additionally send `record_voice` when the note is produced.
- **Voice replies reuse the SIP speech pipeline.** `effective_settings` reads
  `TELEGRAM_*` with `SIP_WHISPER_*` fallbacks; voice notes go through
  `sip_tools.synthesize` (edge-tts per `response_language`, `espeak` fallback)
  and `sip_tools.to_opus_ogg` (ffmpeg). A whisper/ffmpeg miss is a normal
  `telegram_*_available=false` status, never a raised error.
- **Tool registration is unconditional and settings-gated.** `load_default_
  tools` registers `telegram_send`/`telegram_status`/`telegram_send_file` for
  every skill (`TELEGRAM_TOOLS` in `agent/base.py`), the same way SIP is
  registered; the handler re-reads the settings so enabling in the tab applies
  on the next turn. `telegram_send` refuses to run without a
  `ToolContext.user_id`.
- **`telegram_send_file` ships local files to the paired chat, and the path
  allowlist is the security boundary.** The model picks the path, so only
  operator-owned files travel: resolved to realpath, must be a regular readable
  file under an allowed root (the service home, `config.DATA_DIR` — generated
  editor/visio files — or `/tmp`), refused if any component is `.env`, `shadow`,
  `settings.db`, `credentials.json`, `.aws`, `.gnupg`, or if it sits under
  `/etc`/`/var`, capped at `_SEND_MAX_BYTES` (50 MB). **The tool also accepts a
  signed APEX URL** (`/api/editor/download/…`, `/api/visio/frame/…`,
  `/api/files/download/…`, shell/fm/image-browser/obsidian shapes); it is
  normalized (host, `/be` prefix stripped) and resolved to the real file via
  `preview_tools._resolve_source`, which re-runs the generator's own
  ownership/containment checks against `ctx.user_id`. A refusal is a plain
  string reason, never a raise. Images ≤ `_PHOTO_MAX_BYTES` (10 MB) go via
  `sendPhoto` so they render inline; everything else (and larger images) is a
  `sendDocument`, preserving the original bytes. Caption is clipped to 1024.
- **Files are delivered mechanically, not by model memory.** The browser opens
  a visio snapshot from the `visio_frame` SSE event; on Telegram no browser
  exists, so `run_message_turn` installs a `push_event` hook
  (`_attach_telegram_emit`) that delivers that same event as a `sendPhoto` to
  the paired chat. And any `/api/...` signed link the model types into its
  reply instead of calling the tool — a pasted `image_url`, a generated file
  it echoed — is resolved and sent by `_deliver_links_in_reply`, which
  replaces the bare link with a `[sent: name]` marker. Both share the per-turn
  `_DELIVERED` map (`_reset_delivered` at the start of every telegram turn),
  so a link the event already delivered, or that `telegram_send_file` sent, is
  never sent twice in the same turn. The tool wrapper refuses a link that is
  already in the map ("Already sent …"). The Telegram system-prompt block
  (in `run_message_turn`) tells the model to attach produced files via
  this tool and never answer "open it here" links. Tests:
  `TelegramSendFileTests`, `TelegramAutoDeliveryTests`.
- **Route tests must use the app test client** (handlers call `jsonify`, which
  needs an app context) — like the SIP route tests. Module-global state
  (pairing codes, `_REDEEM_ATTEMPTS`, `_ME_CACHE`) is reset in the base
  `TelegramTestCase.setUp`, not in a subclass; `TelegramPairingTests` inherits
  that setUp but has no client. The pair-modal e2e state lives only in the
  browser session.
- Tests: `backend/tests/test_telegram_tools.py`.

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
