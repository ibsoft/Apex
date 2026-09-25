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


NO_TERMINAL_MSG = (
    'No terminal window is open right now. Ask the operator to say "open '
    "terminal\" (voice or text) so there is a window to run commands in."
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


def _pick_session(sessions: list, terminal: object) -> tuple:
    """Resolve the `terminal` argument (index or id prefix) to a session.

    Returns (session, label); when ambiguous returns (None, error-text).
    """
    if terminal in (None, "", 0, "0"):
        return sessions[0], ""  # most recently active terminal
    if isinstance(terminal, str):
        matches = [s for s in sessions if s.id == terminal]
        if not matches and terminal.isdigit():
            matches = [sessions[int(terminal) - 1]] if 0 < int(terminal) <= len(sessions) else []
        if not matches:
            return None, f"No terminal session matches `{terminal}`."
        return matches[0], ""
    # number index, 1-based
    index = int(terminal)
    if index < 1 or index > len(sessions):
        return None, f"Terminal {index} does not exist (only {len(sessions)} open)."
    return sessions[index - 1], ""


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

    def _require_sessions(ctx: ToolContext, terminal_arg: object):
        if not ctx.user_id:
            return None, "Sign in before using a terminal window."
        if not _enabled():
            return None, "Terminal commands are disabled (set ENABLE_RUN_SHELL=true)."
        sessions = _user_sessions(ctx.user_id)
        if not sessions:
            return None, NO_TERMINAL_MSG
        # No explicit target: default to the operator's FOCUSED terminal window
        # if known; otherwise the most recently active one.
        if terminal_arg in (None, "", 0, "0") and ctx.focused_terminal:
            focused = next((s for s in sessions if s.id == ctx.focused_terminal), None)
            if focused is not None:
                return focused, ""
        sess, err = _pick_session(sessions, terminal_arg)
        if sess is None:
            return None, err
        return sess, ""

    def t_terminal_command(args, ctx: ToolContext):
        command = (args.get("command") or "").strip()
        if not command:
            return "Provide a shell command to type into the terminal window."
        mode = str(args.get("mode") or "run").strip().lower()
        if mode not in ("run", "type"):
            return 'mode must be "run" (execute) or "type" (write without executing).'
        sess, err = _require_sessions(ctx, args.get("terminal"))
        if sess is None:
            return err
        if sess.closed:
            return "That terminal session is closed — open a new one."
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
            return "That terminal session is closed — open a new one."
        except OSError as exc:
            return f"Could not write to the terminal: {exc}"

        if mode == "type":
            return (
                f'Typed "{command}" into the terminal window WITHOUT pressing '
                "Enter — nothing has run. If the operator now wants it executed, "
                'call terminal_command with mode="run", exactly once.'
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
                return f"{repeat_note}. Do not re-run it."
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
                f"{text or '(none yet)'}{note}"
            )
        if not want_capture:
            if enter_only:
                return (
                    f'The command "{command}" was already typed on the terminal '
                    "window; pressed Enter to execute it [Enter only, text not re-written]."
                )
            return f"Typed into the terminal [@{len(payload)} bytes]."
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
        return f"{prefix}. Output so far:\n{text or '(none yet)'}{note}"

    def t_terminal_sessions(args, ctx: ToolContext):
        _ = args
        if not ctx.user_id:
            return "Sign in before listing terminal windows."
        if not _enabled():
            return "Terminal commands are disabled (set ENABLE_RUN_SHELL=true)."
        sessions = _user_sessions(ctx.user_id)
        if not sessions:
            return NO_TERMINAL_MSG
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
            "Drive the operator's visible terminal window (their PTY login shell) the exact way the operator asked. mode=\"run\" (default) types the command plus a final Enter and can capture its output — the command EXECUTES, and only once; never call it again for the same command that is already running (a re-send is auto-blocked for 5s). If the SAME command was already WRITTEN into the window by a previous mode=\"type\" call and the operator then says confirm/execute/go ahead, call mode=\"run\" with that same command again — the tool sees it is already typed and only presses Enter, so the line is never doubled. mode=\"type\" only WRITES the text into the window WITHOUT pressing Enter, so NOTHING executes — use it when the operator asked to write/prepare/fill in a command rather than run it. Ideal for live/interactive work (ping, tail -f, ssh, nano) and for sudo: type `sudo <command>` and let the operator answer the password by hand in the window. Use terminal= index or 8-char session id to choose a window; omit to target the operator's FOCUSED terminal window (fallback: most recently active). Several terminals can be open at once.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "One shell command line, verbatim. run mode adds a single trailing Enter for you; type mode adds nothing."},
                    "mode": {"type": "string", "enum": ["run", "type"], "default": "run", "description": "run: write the command + Enter and execute it (once). type: only write the text WITHOUT Enter, so it is NOT executed."},
                    "terminal": {"description": "Optional: 1-based terminal index or session id prefix (see terminal_sessions). Defaults to the most recently active terminal."},
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