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

## Common extension points

- New search source: add a tool in `core_tools.py` (or a new module) and expose
  it through skill definitions.
- New memory ingest: extend `backend/memory/documents.py` for extraction and
  `backend/memory/store.py` for persistence.
- New UI panel: add it inside `frontend/components/ChatUI.tsx` or create a new
  component and wire it through `ApexProvider` if it needs shared state.
