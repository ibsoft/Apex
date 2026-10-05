/** The reasoning layer behind local commands.
 *
 *  `parseLocalCommand` is a list of regexes, and a regex only ever matches the
 *  phrases its author wrote down. "restore all terminals" was a phrase nobody
 *  wrote down: the single-target branches need a number or an empty tail, the
 *  plural noun and its quantifier matched nothing, the utterance fell through to
 *  the agent, and nothing happened - the agent has no tool that can un-minimize
 *  a window either. Same for "unhide every console", "show me the file manager
 *  again", "bring back the windows I closed".
 *
 *  The fix is not forty more regexes. It is giving a model the one thing regexes
 *  cannot have: the state of the screen. This module is the contract between the
 *  browser and that model.
 *
 *  Three parts, and the order matters:
 *
 *  1. {@link LOCAL_ACTIONS} - every action the browser can perform on itself,
 *     described in the model's words. The browser advertises exactly what it can
 *     do, so the model cannot be offered an action that does not exist here.
 *  2. {@link buildUiState} - what is on screen right now: every window with its
 *     number, kind, minimized/maximized flags and desktop, plus the scheduled
 *     tasks. This is what turns "the other terminal" or "the one I closed" from
 *     an unanswerable phrase into a number.
 *  3. {@link sanitizeActions} - the trust boundary. Model output is untrusted
 *     JSON: anything not in the catalogue is dropped, numbers are range-checked
 *     against the state, and the three consequential actions (end the session,
 *     lock the screen, type into the chat box) are only accepted when the
 *     deterministic parser independently agrees.
 *
 *  The deterministic parsers still run first and still win. This is the fallback
 *  for what they do not recognise, so a turn that already works costs nothing and
 *  behaves exactly as before.
 */
import type { BulkWindowAction, LocalCommand } from "./commands";
import { parseLocalCommand } from "./commands";
import type { PanelTabName } from "./panelBridge";
import { PANEL_TAB_NAMES } from "./panelBridge";
import type { AppWindow, WindowArrangement } from "./windows";
import { DESKTOPS, isFilesWindow, isTerminalWindow, terminalNumber } from "./windows";

/** One action the browser can perform on itself, described for the model. */
export type LocalActionSpec = {
  type: LocalCommand["type"];
  /** Omitted for a type that has exactly one action (`silence`, `signout`, ...). */
  action?: string;
  /** One line: what this does, in the words an operator would use. */
  about: string;
  /** Fields beyond `type`/`action`, and what each one means *for this type*.
   *  A window number and a terminal number are both "1", and they address
   *  different windows, so the target has to be spelled out per type. */
  params?: Record<string, string>;
};

const ARRANGEMENTS: WindowArrangement[] = ["cascade", "grid", "tile-h", "tile-v", "center"];

const BULK: Array<[BulkWindowAction, string]> = [
  ["close_all", "close every one of them"],
  ["minimize_all", "minimize every one of them (hide them, keep them running)"],
  ["maximize_all", "maximize every one of them to fill the screen"],
  ["restore_all", "un-minimize and un-maximize every one of them, so they are all visible again"],
];

const SINGLE_STATE: Array<[string, string]> = [
  ["open", "open it if it is not already open"],
  ["close", "close it"],
  ["focus", "bring it to the front and make it the active one"],
  ["minimize", "hide it without closing it"],
  ["maximize", "make it fill the screen"],
  ["restore", "un-minimize and un-maximize it, so it is visible again at normal size"],
];

const DESKTOP_ABOUT = {
  switch: "Show a different virtual desktop (workspace).",
  next: "Go to the next virtual desktop.",
  previous: "Go to the previous virtual desktop.",
  move: "Move a window to a different virtual desktop.",
} as const;

const TASK_ABOUT = {
  list: "List the scheduled tasks.",
  show: "Read one scheduled task out in detail.",
  run: "Run a scheduled task now, without waiting for its schedule.",
  pause: "Stop a scheduled task from running again, but keep it.",
  resume: "Let a paused scheduled task run again.",
  delete: "Delete a scheduled task for good.",
  open: "Show the TASKS tab.",
} as const;

/** Every action the browser can perform on itself.
 *
 *  Deliberately excluded, each for a reason that is not "we forgot":
 *
 *  - `skill`, and the `general` fallback: choosing a skill is what the backend's
 *    own router (`route_skill`) already does, with a model of its own.
 *  - `timer`, `reminder`, `images`, `operator`: these carry *content* that has
 *    to be extracted verbatim ("remind me in ten minutes to call John"). A
 *    paraphrased "in ten minutes" is a different reminder, so these stay with
 *    the deterministic parsers that slice the operator's own words out.
 *  - `notepad` content actions (write/replace/format/...): same reason. A window
 *    action is a verb with no payload; an editing action is a payload, and the
 *    browser already has an editor that speaks for itself.
 */
export const LOCAL_ACTIONS: LocalActionSpec[] = [
  ...(["window", "terminal", "files", "notepad"] as const).flatMap((type) => [
    ...SINGLE_STATE.filter(([action]) => !(type === "terminal" && action === "open")).map<LocalActionSpec>(
      ([action, about]) => ({
        type,
        action,
        about: `The ${describeType(type)}: ${about}.`,
        params: targetParams(type),
      }),
    ),
    ...BULK.map<LocalActionSpec>(([action, about]) => ({
      type,
      action,
      about: `Every ${pluralOf(type)} at once - ${about}. Use this for "all", "every", "both", or a bare plural, and never with a number.`,
    })),
  ]),
  { type: "window", action: "list", about: "Say what windows are open and what each one holds." },
  {
    type: "window",
    action: "next",
    about: "Show the next item in the focused window (a multi-image or multi-page window).",
  },
  {
    type: "window",
    action: "previous",
    about: "Show the previous item in the focused window.",
  },
  {
    type: "window",
    action: "arrange",
    about: "Lay the windows out in a pattern instead of their cascade.",
    params: { arrangement: `One of: ${ARRANGEMENTS.join(", ")}.` },
  },
  {
    type: "window",
    action: "note",
    about: "Attach a short written note to a window. The text is the note itself.",
    params: { ...targetParams("window"), note: "The note text, written out in full." },
  },
  {
    type: "terminal",
    action: "open",
    about: "Open a terminal. Without a number this focuses an existing one if there is one. Give count when the operator asked for several - a count always means new terminals, never focusing one.",
    params: {
      ...targetParams("terminal"),
      count: "How many new terminals to open, when the operator asked for several. Leave it out to focus an existing one.",
    },
  },
  {
    type: "terminal",
    action: "note",
    about: "Attach a short written note to a terminal window.",
    params: { ...targetParams("terminal"), note: "The note text, written out in full." },
  },
  ...(["switch", "next", "previous", "move"] as const).map<LocalActionSpec>((action) => ({
    type: "desktop",
    action,
    about: DESKTOP_ABOUT[action],
    params: {
      desktop: `The virtual desktop number, 1 to ${DESKTOPS}, as the operator counts them.`,
      ...(action === "move"
        ? {
            targets:
              "The window numbers to move. Terminals are addressed by their own number, everything else by its position in the window list.",
          }
        : {}),
    },
  })),
  {
    type: "panel",
    action: "open",
    about: "Show the side panel, optionally on a particular tab.",
    params: { tab: `One of: ${PANEL_TAB_NAMES.join(", ")}.` },
  },
  { type: "panel", action: "close", about: "Hide the side panel." },
  { type: "panel", action: "toggle", about: "Show the panel if it is hidden, hide it if it is showing." },
  ...(["list", "show", "run", "pause", "resume", "delete", "open"] as const).map<LocalActionSpec>((action) => ({
    type: "task",
    action,
    about: TASK_ABOUT[action],
    params: {
      target: "The task number as the TASKS tab shows it, counted in the order the tab lists them.",
    },
  })),
  { type: "chatinput", action: "write", about: "Type text into the chat box WITHOUT sending it.",
    params: { text: "The text to put in the box, written out in full." } },
  { type: "chatinput", action: "send", about: "Send whatever is currently in the chat box." },
  { type: "silence", about: "Stop talking until addressed again." },
  { type: "autonomy", about: "Turn unsolicited messages on or off.",
    params: { enabled: "true to let APEX speak first, false to leave it alone." } },
  { type: "cancelTimers", about: "Cancel every running timer." },
  { type: "cancelReminders", about: "Cancel every reminder." },
  { type: "lock", about: "Lock the screen now." },
  { type: "signout", about: "End this session and sign out." },
];

/** Actions that only the deterministic parser may produce.
 *
 *  These three are the ones where being wrong is expensive rather than annoying:
 *  `signout` ends the session, `lock` hides the app behind the lock screen, and
 *  `chatinput write` puts words in a box the operator is about to send. The
 *  session rules in AGENTS.md require the sign-out phrase to be anchored to the
 *  whole utterance precisely so that *talking about* signing out cannot end the
 *  session - and that guarantee is the regex parser's, so it is the regex
 *  parser's to keep. The model may propose any of these; the sanitizer drops the
 *  proposal unless the parser independently read the same action out of the same
 *  words. */
export const CONFIRMATORY_ACTIONS = new Set(["signout", "lock", "chatinput"]);

/** How many actions one utterance may produce.
 *
 *  "Close the browser window and restore terminal 2" is two. This is a cap on
 *  damage from a hallucinating model, not a claim about how much people say:
 *  anything longer than this is a task for the agent, which can actually carry
 *  it out step by step. */
export const MAX_ACTION_CHAIN = 4;

function describeType(type: string): string {
  return {
    window: "preview window (an image, PDF or document)",
    terminal: "terminal window (a shell)",
    files: "File Manager window",
    notepad: "Notepad window (the rich text editor)",
  }[type] ?? type;
}

function pluralOf(type: string): string {
  return {
    window: "preview window",
    terminal: "terminal window",
    files: "File Manager window",
    notepad: "Notepad window",
  }[type] ?? type;
}

/** How `target` is counted for each type. This is the difference that made
 *  "restore window 2" and "restore terminal 2" unanswerable: they are both "2",
 *  and they address different windows. */
function targetParams(type: string): Record<string, string> {
  const how = {
    window: "The window's position in the full open-window list, 1-based, as shown in the window list.",
    terminal: "The terminal's own number, 1-based, counted among terminal windows only (the T1/T2 badge), NOT among all windows.",
    files: "The File Manager window's position among File Manager windows, 1-based.",
    notepad: "The Notepad window's position among Notepad windows, 1-based.",
  }[type];
  return { target: `${how} Omit it when the operator means the focused one.` };
}

/* ---------- the state of the screen ---------- */

export type UiWindowState = {
  /** Position in the full open-window list, 1-based: the number the window list shows. */
  n: number;
  /** Terminals are numbered separately; null for everything else. */
  terminal: number | null;
  kind: string;
  title: string;
  minimized: boolean;
  maximized: boolean;
  desktop: number;
  focused: boolean;
};

export type UiState = {
  windows: UiWindowState[];
  desktops: number;
  activeDesktop: number;
  focusedWindow: number | null;
  focusedTerminal: number | null;
  /** Shown as `number - label (status)`, in the same order the TASKS tab lists. */
  tasks: string[];
};

/** Snapshot what the browser currently shows.
 *
 *  Everything here is derived from React state that already exists; nothing is
 *  queried from the backend, so building it is free and cannot fail.
 *
 *  Notepad is detected more strictly than `isNotepadWindow`, which also accepts
 *  any text preview. A .txt file open in a window is not an editor the browser
 *  can restore, and telling the model it is would invite it to pick `notepad`
 *  for a window that has no editor behind it. */
export function buildUiState(input: {
  windows: AppWindow[];
  focusedWindowId: string | null;
  activeDesktop: number;
  tasks?: Array<{ id: number; description?: string; status?: string; schedule?: string }>;
}): UiState {
  const { windows, focusedWindowId, activeDesktop } = input;
  const isPad = (w: AppWindow) =>
    w.kind === "notepad" || (w.items[w.index] ?? w.items[0])?.url?.startsWith("notepad:") === true;
  return {
    windows: windows.map((w, i) => ({
      n: i + 1,
      terminal: isTerminalWindow(w) ? terminalNumber(windows, w) : null,
      kind: isTerminalWindow(w) ? "terminal" : isFilesWindow(w) ? "files" : isPad(w) ? "notepad" : w.kind,
      title: itemTitleOf(w),
      minimized: !!w.minimized,
      maximized: !!w.maximized,
      desktop: w.desktop + 1,
      focused: w.id === focusedWindowId,
    })),
    desktops: DESKTOPS,
    activeDesktop: activeDesktop + 1,
    focusedWindow: windows.findIndex((w) => w.id === focusedWindowId) + 1 || null,
    focusedTerminal: (() => {
      const t = windows.find((w) => w.id === focusedWindowId);
      return t && isTerminalWindow(t) ? terminalNumber(windows, t) : null;
    })(),
    tasks: (input.tasks ?? []).map((t) => `${t.id} - ${t.description ?? "(no description)"} [${t.status ?? "unknown"}]`),
  };
}

function itemTitleOf(w: AppWindow): string {
  const item = w.items[w.index] ?? w.items[0];
  return item?.title || w.kind;
}

/** Render the state as the lines the model reads. Numbers first, prose second:
 *  the numbers are what it has to echo back in a `target`. */
export function renderUiState(state: UiState): string {
  const lines = state.windows.map((w) => {
    const id = w.terminal !== null ? `terminal #${w.terminal}` : `window #${w.n}`;
    const flags = [
      w.minimized ? "minimized" : "",
      w.maximized ? "maximized" : "",
      w.focused ? "focused" : "",
    ].filter(Boolean);
    return `- ${id} "${w.title}" (${w.kind}${flags.length ? `, ${flags.join(", ")}` : ""}) on desktop ${w.desktop}`;
  });
  const header = [
    `Virtual desktops: ${state.desktops}, currently on ${state.activeDesktop}.`,
    state.focusedWindow !== null ? `Focused window: #${state.focusedWindow}.` : "No window is focused.",
  ];
  if (lines.length) header.push("Open windows:");
  if (state.tasks.length) header.push("Scheduled tasks:", ...state.tasks.map((t) => `- ${t}`));
  return header.concat(lines).join("\n");
}

/** Render the catalogue as the model's menu. */
export function renderActionCatalogue(actions: LocalActionSpec[] = LOCAL_ACTIONS): string {
  const byType = new Map<string, LocalActionSpec[]>();
  for (const spec of actions) {
    const list = byType.get(spec.type) ?? [];
    list.push(spec);
    byType.set(spec.type, list);
  }
  return [...byType.entries()]
    .map(([type, specs]) => {
      const lines = specs.map((spec) => {
        const params = spec.params
          ? `  { ${Object.entries(spec.params).map(([k, v]) => `${k}: ${v}`).join("; ")} }`
          : "";
        const name = spec.action ? `${type}.${spec.action}` : type;
        return `  ${name} - ${spec.about}${params}`;
      });
      return `${type}:\n${lines.join("\n")}`;
    })
    .join("\n");
}

/* ---------- the trust boundary ---------- */

const ALLOWED = new Map<string, LocalActionSpec>();
for (const spec of LOCAL_ACTIONS) ALLOWED.set(specKey(spec.type, spec.action ?? ""), spec);

/** Types whose extra fields are free text from the operator's own sentence. */
const FREE_TEXT: Record<string, string[]> = { window: ["note"], chatinput: ["text"] };

function specKey(type: string, action: string): string {
  return `${type}.${action}`;
}

function sanitizeParam(
  spec: LocalActionSpec,
  key: string,
  value: unknown,
  state: UiState,
): unknown {
  const text = FREE_TEXT[spec.type]?.includes(key);
  if (text) {
    // An editing payload is quoted verbatim, so it must be the operator's words
    // and not an instruction of the model's own. A cap keeps one reply from
    // becoming a 4 kB payload.
    return typeof value === "string" && value.trim() ? value.trim().slice(0, 2000) : undefined;
  }
  if (key === "enabled") return typeof value === "boolean" ? value : undefined;
  if (key === "arrangement") {
    const wanted = String(value ?? "").toLowerCase().replace(/[\s-]+/g, "_");
    return ARRANGEMENTS.includes(wanted as WindowArrangement) ? wanted : undefined;
  }
  if (key === "tab") {
    const wanted = String(value ?? "").toLowerCase() as PanelTabName;
    return PANEL_TAB_NAMES.includes(wanted) ? wanted : undefined;
  }
  if (key === "targets") {
    const list = Array.isArray(value) ? value : [value];
    const nums = list.map(sanitizeNumber).filter((n): n is number => n !== undefined);
    const valid = nums.filter((n) => windowExists(state, n));
    return valid.length ? valid : undefined;
  }
  if (key === "desktop") {
    const n = sanitizeNumber(value);
    return n !== undefined && n >= 1 && n <= state.desktops ? n - 1 : undefined;
  }
  // `target` and `count`: a number, and it has to address something that exists.
  const n = sanitizeNumber(value);
  if (n === undefined || n < 1) return undefined;
  if (key === "count") return Math.min(n, 10);
  return windowExists(state, n) ? n : undefined;
}

/** A window number is only honoured if it addresses a window on screen. The
 *  model is told the numbering, but a number it invented would otherwise become
 *  "No terminal window is open" for a window the operator can see. */
function windowExists(state: UiState, n: number): boolean {
  return state.windows.some((w) => w.n === n || w.terminal === n);
}

function sanitizeNumber(value: unknown): number | undefined {
  const n = typeof value === "number" ? value : typeof value === "string" ? Number(value.trim()) : NaN;
  return Number.isFinite(n) ? Math.trunc(n) : undefined;
}

/** One model answer, reshaped into the `type`/`action`/`params` the catalogue is
 *  keyed by, or null if there is nothing usable in it.
 *
 *  Three shapes reach here from a real model, and all three are legitimate
 *  readings of a catalogue whose keys are printed dotted (`terminal.open`):
 *
 *  - `{"type": "terminal", "action": "open", "count": 2}` - the documented one.
 *  - `{"type": "terminal.open", "count": 2}` - the model copies the catalogue key
 *    into `type` and never sets `action`. Looking this up as-is builds the key
 *    `terminal.open.`, which is in no map, so the whole chain is silently
 *    dropped and the turn reaches the agent instead - which is how "show me two
 *    terminals and a notepad" came to open two terminals and no notepad.
 *  - `{"terminal.open": {"count": 2}}` - the key used as an object key.
 *
 *  Rewriting the shape is safe in a way that adding new actions is not: the
 *  result still has to pass the `ALLOWED` lookup below, so a rewritten entry is
 *  accepted only if it names an action that genuinely exists. Splitting on the
 *  first dot cannot invent one, because `type` and `action` are then checked
 *  against the catalogue separately. */
function normalizeEntry(
  entry: unknown,
): { type: string; action: string; params: Record<string, unknown> } | null {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) return null;
  const source = entry as Record<string, unknown>;

  /* `{"terminal.open": {"count": 2}}`: exactly one key, no `type` of its own,
     and that key names an action. The nested object holds the parameters. */
  const keys = Object.keys(source);
  if (keys.length === 1 && typeof source.type === "undefined") {
    const only = keys[0];
    const nested = source[only];
    if (nested && typeof nested === "object" && !Array.isArray(nested)) {
      return normalizeEntry({ ...(nested as Record<string, unknown>), type: only });
    }
  }

  let type = typeof source.type === "string" ? source.type.trim().toLowerCase() : "";
  let action = typeof source.action === "string" ? source.action.trim().toLowerCase() : "";
  const params: Record<string, unknown> = {};
  for (const [field, value] of Object.entries(source)) {
    if (field === "type" || field === "action") continue;
    params[field] = value;
  }
  if (!type) return null;
  // `type` may itself be the dotted catalogue key, with or without an `action`.
  const dot = type.indexOf(".");
  if (dot > 0) {
    const head = type.slice(0, dot);
    const tail = type.slice(dot + 1).trim();
    type = head;
    // An explicit `action` wins; it is the more specific of the two.
    if (!action) action = tail;
  }
  if (!type) return null;
  return { type, action, params };
}

/** Turn raw model output into commands that are safe to run, dropping anything
 *  that is not in the catalogue.
 *
 *  Returns [] for "this was a request for the agent", which is the most common
 *  correct answer: the model is asked to recognise the turns it must not handle.
 *  A partially valid chain is returned without its invalid entries, so one bad
 *  action does not throw away the good ones - but an empty result never falls
 *  back to running half of what was asked. */
export function sanitizeActions(raw: unknown, input: {
  state: UiState;
  utterance: string;
  language: string;
  skills: Array<{ name: string }>;
}): LocalCommand[] {
  const list = Array.isArray(raw)
    ? raw
    : raw && typeof raw === "object" && Array.isArray((raw as { actions?: unknown }).actions)
      ? (raw as { actions: unknown[] }).actions
      : [];
  const parsed = parseLocalCommand(input.utterance, input.language, input.skills);
  const out: LocalCommand[] = [];
  const seen = new Set<string>();
  for (const entry of list.slice(0, MAX_ACTION_CHAIN)) {
    const candidate = normalizeEntry(entry);
    if (!candidate) continue;
    const { type, action, params: modelParams } = candidate;
    const key = specKey(type, action);
    const spec = ALLOWED.get(key);
    if (!spec) continue;
    // The three consequential actions need the deterministic parser to agree.
    if (CONFIRMATORY_ACTIONS.has(type)) {
      if (!parsed || parsed.type !== type) continue;
      // Both sides are read as "action or nothing": { type: "signout" } carries
      // no action field, so comparing against the model's "" would reject the
      // one action this check exists to allow.
      const parsedAction = (parsed as { action?: string }).action ?? "";
      if (parsedAction !== action) continue;
    }
    const command: Record<string, unknown> = { type, ...(action ? { action } : {}) };
    /* A target the screen does not show is a *wrong address*, not a missing
     * optional field. Stripping it would quietly turn "close window 4" into
     * "close the focused window" - a different window from the one that was
     * named, which for a close is not a thing to guess at. The whole action
     * goes instead; an action nobody gave a number for is unaffected, and that
     * is the case where the focused window is the right default. */
    let misaddressed = false;
    for (const field of Object.keys(spec.params ?? {})) {
      if (modelParams[field] === undefined) continue;
      const value = sanitizeParam(spec, field, modelParams[field], input.state);
      if (value === undefined) {
        if (field === "target" || field === "targets") misaddressed = true;
        continue;
      }
      command[field] = value;
    }
    if (misaddressed) continue;
    const dedupe = JSON.stringify(command);
    if (seen.has(dedupe)) continue;
    seen.add(dedupe);
    out.push(command as LocalCommand);
  }
  return out;
}

/** One request shape for {@link sanitizeActions}, for callers that already have
 *  the pieces. */
export type ResolveInput = Parameters<typeof sanitizeActions>[1];