"""Model-facing tools that drive the operator's PTY terminal windows.

The terminal window is the operator's *visible, interactive* shell: any command
written here appears on screen and runs in their login shell just as if they
typed it. Unlike `run_shell` (headless subprocess), output is captured for the
model *without* consuming the window's stream — the operator sees everything.

Interactive prompts (sudo, ssh passphrase, password) are left for the operator
to answer by typing directly into the terminal window. This tool never feeds
passwords; when a prompt is detected it tells the model to hand over to the
operator.

Requires ``ENABLE_RUN_SHELL`` (the terminal server itself is gated on it).
"""
from __future__ import annotations

import re
import time
from typing import Callable

from tools.base import Tool, ToolContext


# --------------------------------------------------------------------------- #
# ANSI / control stripping: the model should read text, not escape soup.
# --------------------------------------------------------------------------- #
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"          # CSI sequences (colors, cursor moves)
    r"|\x1b\][^\x07]*\x07"                 # OSC title / hyperlink sequences
    r"|\x1b[()][0-9A-B]"                   # charset selects
    r"|\x1b[=>]|[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f]"  # stray controls (keep \t\n\r)
)


def _strip_ansi(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"\r(?=[^\n])", "", text)  # carriage-return rewrites flatten
    return _ANSI_RE.sub("", text)


AUTO_OPENED_NOTE = (
    "No terminal window was open, so one was opened for you automatically — "
    "the command above ran in it and the operator can see it, type in it and "
    "answer any password prompt by hand. Just call the tool again next time; "
    "never ask the operator to open a terminal."
)

NO_TERMINAL_MSG = (
    "No terminal window is open. Nothing has run. The next terminal_command "
    "call opens one automatically, so just call it instead of asking the "
    "operator to open a terminal."
)

# Exact-once guard: the model's retry loop often re-sends the same command right
# after running it (eager to see output). Re-sending within this window would
# execute it twice, so the duplicate is observed, never re-run.
_RUN_REPEAT_GUARD_S = 5.0


def _user_sessions(user_id: str) -> list:
    from tools.terminal_server import _TERMINALS

    return sorted(
        (s for s in _TERMINALS.values() if s.user_id == user_id and not s.closed),
        key=lambda s: s.last_touch,
        reverse=True,
    )


def _truthy(value) -> bool:
    """Model-supplied booleans arrive as real bools or as "true"/"1" strings."""
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _coerce_count(value, cap: int = 8) -> int:
    """How many terminals the model asked for, clamped to something sane.

    A model asking for "count": 4000 is not a request to spawn 4000 ptys.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    if n < 1:
        return 1
    return min(n, max(1, cap))


def _open_session(ctx: ToolContext, cfg) -> object:
    """Open a terminal window for the model, without being asked to.

    The session is a normal PTY session owned by the user, so the browser can
    adopt the same id and show it. ``terminal_opened`` tells the frontend to do
    exactly that, which is why the operator still sees every command APEX runs
    and can answer sudo/passphrase prompts by hand.
    """
    from tools.terminal_server import open_session

    rows, cols = 24, 80
    if ctx.session is not None:
        # A browser-adopted session reports its real size; keep the auto-opened
        # window the same shape as a manually opened one when we know it.
        rows = max(2, min(200, int(getattr(ctx.session, "rows", 24) or 24)))
        cols = max(2, min(500, int(getattr(ctx.session, "cols", 80) or 80)))
    sess = open_session(ctx.user_id, cfg, rows, cols)
    if ctx.emit:
        ctx.emit({"type": "terminal_opened", "terminal_id": sess.id})
    return sess


def _pick_session(sessions: list, terminal: object) -> tuple:
    """Resolve the `terminal` argument (index or id prefix) to a session.

    Returns (session, label); when ambiguous returns (None, error-text).
    """
    if terminal in (None, "", 0, "0"):
        return sessions[0], ""  # most recently active terminal
    if isinstance(terminal, str):
        needle = terminal.strip()
        matches = [s for s in sessions if s.id == needle]
        if not matches and not needle.isdigit():
            # The schema advertises a session id *prefix*: the ids shown in
            # terminal_sessions are truncated, so a prefix must resolve.
            matches = [s for s in sessions if s.id.startswith(needle)]
        if not matches and needle.isdigit():
            matches = [sessions[int(needle) - 1]] if 0 < int(needle) <= len(sessions) else []
        if not matches:
            return None, f"No terminal session matches `{terminal}`."
        if len(matches) > 1:
            return None, f"`{terminal}` matches {len(matches)} terminal sessions — use more characters."
        return matches[0], ""
    # number index, 1-based
    index = int(terminal)
    if index < 1 or index > len(sessions):
        return None, f"Terminal {index} does not exist (only {len(sessions)} open)."
    return sessions[index - 1], ""


def _resolve_terminal(sessions: list, terminal: object, ctx) -> tuple:
    """Resolve `terminal`, preferring the operator's on-screen numbering.

    The number painted in a terminal's title bar is its position among the
    visible terminal windows, and that is the number the operator says ("run top
    on terminal 2"). The backend's own session list can be in a different order
    — most-recently-active first — so when the browser sent that visible order
    (``ctx.terminal_map``) a number must be read from it. An explicit session id
    still wins: it is unambiguous regardless of any ordering.
    """
    if isinstance(terminal, str) and terminal.strip():
        wanted = terminal.strip()
        # An exact session id is unambiguous and always wins, digits or not.
        exact = next((s for s in sessions if s.id == wanted), None)
        if exact is not None:
            return exact, ""
        # A digit string is a *spoken window number*, not an id. A model that
        # passes "2" as text must reach the same terminal as the integer 2, or
        # the command lands wherever the id-prefix lookup happens to land -
        # the backend orders sessions most-recently-active-first, which is not
        # what the operator sees.
        if wanted.isdigit():
            ordered = [s for sid in getattr(ctx, "terminal_map", ()) or ()
                       for s in sessions if s.id == sid]
            index = int(wanted)
            if ordered and 1 <= index <= len(ordered):
                return ordered[index - 1], ""
        return _pick_session(sessions, wanted)
    if terminal not in (None, "", 0, "0"):
        ordered = [s for sid in getattr(ctx, "terminal_map", ()) or ()
                   for s in sessions if s.id == sid]
        if ordered:
            try:
                index = int(terminal)
            except (TypeError, ValueError):
                return _pick_session(sessions, terminal)
            if 1 <= index <= len(ordered):
                return ordered[index - 1], ""
        return _pick_session(sessions, terminal)
    # No explicit target: the operator's focused terminal window, else the most
    # recently active one.
    focused_id = getattr(ctx, "focused_terminal", "")
    focused = next((s for s in sessions if s.id == focused_id), None)
    return (focused or sessions[0]), ""


def _log(message: str, *, sess=None, **fields) -> None:
    """One line per command, with the session id.

    A command can land in a session the browser is not watching; that is
    invisible from the browser side (the window simply never updates) and was
    impossible to tell apart from "no command ran". The session id in the
    journal settles it.
    """
    parts = [message]
    if sess is not None:
        parts.append(f"session={getattr(sess, 'id', '?')[:8]}")
    for key, value in fields.items():
        text = str(value).replace("\n", " ")
        parts.append(f"{key}={text[:120]}")
    try:
        import sys as _sys

        print("[terminal] " + " ".join(parts), file=_sys.stderr, flush=True)
    except Exception:
        pass


def _capture(sess, start: int, timeout: float, quiet: float, max_bytes: int) -> tuple[str, bool]:
    """Best-effort tail of the *model* output stream after `start`.

    Uses the session's independent model queue so the terminal window's own
    stream is never disturbed. Returns (decoded_text, still_running)."""
    out = bytearray()
    cursor = start
    last_new = time.time()
    last_motion = time.time()
    deadline = time.time() + timeout
    while time.time() < deadline:
        with sess.lock:
            closed = sess.closed
            cursor_now = sess.model_base + sess._model_len
        if cursor_now != cursor:
            cursor, chunk, closed = sess.model_read(cursor, limit=max_bytes)
            if chunk:
                out += chunk
                last_new = time.time()
                last_motion = time.time()
                if len(out) >= max_bytes:
                    break
        if closed:
            break
        if out and time.time() - last_new > quiet:
            break   # a quiet pause after output usually means the command ran
        if not out and time.time() - last_motion > quiet:
            break   # nothing at all yet (command may still be running)
        time.sleep(0.05)
    still = not sess.closed and time.time() >= deadline
    return _strip_ansi(bytes(out[-max_bytes:])), still


def build_terminal_tools(cfg, capture: Callable | None = None) -> list[Tool]:
    """Build the terminal tools. `capture` may be injected in tests; it defaults
    to the internal _capture best-effort reader."""
    read_capture = capture or _capture

    def _enabled() -> bool:
        return bool(getattr(cfg, "ENABLE_RUN_SHELL", False))

    def _require_sessions(ctx: ToolContext, terminal_arg: object, auto_open: bool = True):
        """Resolve the target session, opening a window first if none exists.

        Never tells the operator to open a terminal: if no window is open the
        model opens one itself and keeps going. Only a genuine failure (no pty,
        shell cannot start) returns an error string."""
        if not ctx.user_id:
            return None, "Sign in before using a terminal window."
        if not _enabled():
            return None, "Terminal commands are disabled (set ENABLE_RUN_SHELL=true)."
        sessions = _user_sessions(ctx.user_id)
        if not sessions:
            if not auto_open:
                return None, NO_TERMINAL_MSG
            try:
                return _open_session(ctx, cfg), ""
            except Exception as exc:
                return None, f"Could not open a terminal window: {exc}"
        # No explicit target: default to the operator's FOCUSED terminal window
        # if known; otherwise the most recently active one. Numbers are read
        # against the on-screen order the browser sent.
        return _resolve_terminal(sessions, terminal_arg, ctx)

    def t_terminal_command(args, ctx: ToolContext):
        command = (args.get("command") or "").strip()
        if not command:
            return "Provide a shell command to type into the terminal window."
        mode = str(args.get("mode") or "run").strip().lower()
        if mode not in ("run", "type"):
            return 'mode must be "run" (execute) or "type" (write without executing).'
        auto_open = args.get("auto_open", True)
        if isinstance(auto_open, str):
            auto_open = auto_open.strip().lower() not in {"0", "false", "no", "off"}
        count = _coerce_count(args.get("count", 1))
        want_new = _truthy(args.get("new_terminal")) or count > 1
        if want_new:
            # The model is allowed several terminals at once — for parallel jobs
            # or just because it was asked to "open 4 terminals". Opening is
            # capped by the free room, never by evicting one the user is using.
            from tools.terminal_server import max_sessions_per_user, room_for

            if not ctx.user_id:
                return "Sign in before using a terminal window."
            if not _enabled():
                return "Terminal commands are disabled (set ENABLE_RUN_SHELL=true)."
            room = room_for(ctx.user_id, count)
            if room <= 0:
                return (
                    f"All {max_sessions_per_user()} terminal windows are already "
                    "open. Nothing was closed. Close one with terminal_close, or "
                    "reuse an existing window by leaving new_terminal unset."
                )
            opened = []
            for _ in range(room):
                try:
                    opened.append(_open_session(ctx, cfg))
                except Exception as exc:
                    if not opened:
                        return f"Could not open a terminal window: {exc}"
                    break   # keep the terminals that did start
            # Run in the newest one; the earlier ones are there for parallel work.
            sess = opened[-1]
            err = ""
            ids = ", ".join(s.id for s in opened)
            opened_note = (
                f"\n\nOpened {len(opened)} new terminal window"
                f"{'s' if len(opened) > 1 else ''} ({ids}) and ran the command in "
                f"{sess.id}. Each one is a normal window the operator can see and "
                "type in. Pass new_terminal=true or count=N to open more, and "
                f'terminal="{sess.id}" (or its 1-based index) to target one.'
            )
        else:
            had_session = bool(_user_sessions(ctx.user_id)) if ctx.user_id else False
            sess, err = _require_sessions(ctx, args.get("terminal"), auto_open=bool(auto_open))
            if sess is None:
                return err
            opened_note = "" if had_session else f"\n\n{AUTO_OPENED_NOTE}"
        if sess.closed:
            return "That terminal session is closed — a new one was opened, call the tool again."
        payload = command.encode("utf-8")
        repeat = False
        enter_only = False
        start = sess.model_cursor
        try:
            if mode == "run":
                # A trailing Enter is what makes the shell execute the command.
                if not payload.endswith(b"\n"):
                    payload += b"\n"
                # Exact-once guard: never re-send a command that is already
                # running (the model's retry loop otherwise re-executes it).
                # Claim under the lock before writing so concurrent calls
                # cannot double-send either.
                with sess.lock:
                    repeat = (
                        sess.last_run_payload == payload
                        and time.time() - sess.last_run_at < _RUN_REPEAT_GUARD_S
                    )
                    if not repeat:
                        sess.last_run_payload = payload
                        sess.last_run_at = time.time()
                        # Confirm flow: the command was already WRITTEN by an
                        # earlier mode="type" call and still sits on the prompt.
                        # Re-typing it would corrupt the line (e.g. "free -hfree
                        # -h"), so only press Enter to execute the visible text.
                        enter_only = sess.last_typed == command.encode("utf-8")
                        sess.last_typed = b""
                if not repeat:
                    sess.write(b"\n" if enter_only else payload)
                    sess.touch()
            else:
                # type mode: write verbatim, NO Enter — nothing executes.
                sess.write(payload)
                sess.touch()
                with sess.lock:
                    sess.last_typed = payload
        except RuntimeError:
            _log("write failed: session closed", sess=sess, command=command)
            return "That terminal session is closed — open a new one."
        except OSError as exc:
            _log("write failed", sess=sess, command=command, error=str(exc))
            return f"Could not write to the terminal: {exc}"
        _log("command written", sess=sess, command=command, mode=mode)

        if mode == "type":
            return (
                f'Typed "{command}" into the terminal window WITHOUT pressing '
                "Enter — nothing has run. If the operator now wants it executed, "
                'call terminal_command with mode="run", exactly once.'
                + opened_note
            )

        want_capture = bool(args.get("capture", True))
        repeat_note = (
            f'The command "{command}" was already sent to this terminal and is '
            "running, so it was NOT sent again"
        )
        if enter_only:
            prefix = (
                f'The command "{command}" was already typed on the terminal '
                "window; pressed Enter to execute it as confirmed"
            )
        else:
            prefix = f"The command \"{command}\" was sent to the terminal"
        if repeat:
            if not want_capture:
                return f"{repeat_note}. Do not re-run it." + opened_note
            text, still = read_capture(
                sess, start,
                timeout=float(args.get("timeout", 25)),
                quiet=float(args.get("quiet", 1.2)),
                max_bytes=int(args.get("max_bytes", 6000)) or 6000,
            )
            note = ""
            if still:
                note = "\n[still running — output continues in the terminal window]"
            return (
                f"{repeat_note}.\nOutput so far:\n"
                f"{text or '(none yet)'}{note}{opened_note}"
            )
        if not want_capture:
            if enter_only:
                return (
                    f'The command "{command}" was already typed on the terminal '
                    "window; pressed Enter to execute it [Enter only, text not re-written]."
                    + opened_note
                )
            return f"Typed into the terminal [@{len(payload)} bytes].{opened_note}"
        text, still = read_capture(
            sess, start,
            timeout=float(args.get("timeout", 25)),
            quiet=float(args.get("quiet", 1.2)),
            max_bytes=int(args.get("max_bytes", 6000)) or 6000,
        )
        note = ""
        if re.search(r"(?i)\bpassword for\b|\(?sudo\)? ?password|password:|passphrase", text):
            note = (
                "\n\n[sudo/authentication prompt] The terminal is waiting for a "
                "password. The operator must type it manually in the terminal "
                "window — never echo or automate it. Tell them: \"type your "
                "password in the terminal window\".\n"
            )
        if still:
            note += "\n[still running — output continues in the terminal window]"
        return f"{prefix}. Output so far:\n{text or '(none yet)'}{note}{opened_note}"

    def t_terminal_sessions(args, ctx: ToolContext):
        _ = args
        if not ctx.user_id:
            return "Sign in before listing terminal windows."
        if not _enabled():
            return "Terminal commands are disabled (set ENABLE_RUN_SHELL=true)."
        sessions = _user_sessions(ctx.user_id)
        if not sessions:
            return (
                "No terminal window is open yet. Nothing needs doing about that: "
                "calling terminal_command opens one automatically and runs the "
                "command in it, and the operator sees it appear on screen."
            )
        lines = ["Open terminal windows (the focused one is the default target for terminal_command):"]
        for i, sess in enumerate(sessions, 1):
            mark = " (focused)" if sess.id == ctx.focused_terminal else ""
            lines.append(
                f"  {i}. {sess.id[:8]} ({sess.cols}x{sess.rows}) — last activity {_ago(sess.last_touch)}{mark}"
            )
        return "\n".join(lines)

    return [
        Tool(
            "terminal_command",
            "Drive the operator's visible terminal window (their PTY login shell) the exact way the operator asked. If NO terminal window is open, this tool OPENS ONE BY ITSELF and runs the command in it — never ask the operator to open a terminal, never tell them to do it for you, just call this tool. mode=\"run\" (default) types the command plus a final Enter and can capture its output — the command EXECUTES, and only once; never call it again for the same command that is already running (a re-send is auto-blocked for 5s). If the SAME command was already WRITTEN into the window by a previous mode=\"type\" call and the operator then says confirm/execute/go ahead, call mode=\"run\" with that same command again — the tool sees it is already typed and only presses Enter, so the line is never doubled. mode=\"type\" only WRITES the text into the window WITHOUT pressing Enter, so NOTHING executes — use it when the operator asked to write/prepare/fill in a command rather than run it. Ideal for live/interactive work (ping, tail -f, ssh, nano) and for sudo: type `sudo <command>` and let the operator answer the password by hand in the window. Use terminal= index or 8-char session id to choose a window; omit to target the operator's FOCUSED terminal window (fallback: most recently active). You may open as many terminals as the job needs, by yourself, without asking: set new_terminal=true for one more, or count=N to open N at once and run the command in the newest (use the others for parallel jobs). The per-user cap is never exceeded and no window the operator is using is ever closed for you.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "One shell command line, verbatim. run mode adds a single trailing Enter for you; type mode adds nothing."},
                    "mode": {"type": "string", "enum": ["run", "type"], "default": "run", "description": "run: write the command + Enter and execute it (once). type: only write the text WITHOUT Enter, so it is NOT executed."},
                    "terminal": {"description": "Optional: 1-based terminal index or session id prefix (see terminal_sessions). Defaults to the most recently active terminal."},
                    "new_terminal": {"type": "boolean", "default": False, "description": "Open an ADDITIONAL terminal even when one is already open, and run the command in it. Use for parallel jobs or when the operator asked for another window. Never required for the first command: a terminal is opened automatically when none exists."},
                    "count": {"type": "integer", "default": 1, "minimum": 1, "maximum": 8, "description": "Open this many NEW terminals at once (1-8) and run the command in the newest one. Implies new_terminal. Use when the operator says \"open 4 terminals\" or several jobs should run side by side."},
                    "auto_open": {"type": "boolean", "default": True, "description": "Open a terminal automatically when none is open (default). Set false only to deliberately do nothing instead."},
                    "capture": {"type": "boolean", "default": True, "description": "Wait briefly and return the command's early output back to the model."},
                    "timeout": {"type": "number", "default": 25, "description": "Max seconds to wait for output when capture=true."},
                    "quiet": {"type": "number", "default": 1.2, "description": "Seconds of output silence before returning the result."},
                    "max_bytes": {"type": "integer", "default": 6000, "description": "Cap for captured output."},
                },
                "required": ["command"],
            },
            t_terminal_command,
            dangerous=True,
        ),
        Tool(
            "terminal_sessions",
            "List the operator's open terminal windows (multiple may be open) with their index/session id and size, marking the focused one — the default terminal_command target.",
            {"type": "object", "properties": {}},
            t_terminal_sessions,
        ),
    ]


def _ago(ts: float) -> str:
    secs = max(0, int(time.time() - ts))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"