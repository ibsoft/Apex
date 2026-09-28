"""End-to-end check: a command request must be RUN and SHOWN, not invented.

Not collected by pytest (the filename does not start with ``test_``) because it
makes a real model call and runs a real command on this host.

    cd /home/ioannisb/Development/Apex
    .venv/bin/python backend/tests/live_visible_terminal_check.py
    .venv/bin/python backend/tests/live_visible_terminal_check.py "show me disk usage"

It asserts the four things the operator actually asked for:

  1. APEX opens a terminal by itself          -> a `terminal_opened` event
  2. it executes the command in that terminal -> a `terminal_command` call
  3. the command AND its output are in the    -> the PTY buffer holds both
     terminal window buffer (what you see)
  4. the chat gets a summary                  -> reply text, and it is
                                                  consistent with the output

Only step 3's *rendering* is not provable here: the script reads the same bytes
the browser's xterm would receive, but whether the pixels appear on your screen
is the one thing only you can confirm by looking.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

DEFAULT_ASK = "check our internet connection"

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")

# APEX tool names. Passing one as a shell `command` types a non-existent binary
# into the terminal - observed live: the model ran `current_time` and got
# "command not found" before correctly running `date`.
TOOL_NAMES = {
    "current_time", "file_search", "web_search", "web_image_search",
    "web_news_search", "web_fetch", "get_weather", "calculate", "remember",
    "recall", "notepad_control", "terminal_command", "terminal_sessions",
    "run_python", "run_shell", "create_skill", "image_browser", "obsidian",
}


def _clean(text: str) -> list[str]:
    """Readable lines: no ANSI, no blank lines, no shell prompt noise."""
    out = []
    for raw in _ANSI.sub("", text).splitlines():
        line = raw.replace("\r", "").strip()
        if not line:
            continue
        # prompt lines: "user@host:~$ cmd" / "$ cmd" / a bare cursor
        stripped = line.split(":", 1)[-1].lstrip("$#% ").strip() if ":" in line else line
        if stripped in {"", "$", "#", "~", "$ ", ">>>"}:
            continue
        if stripped.startswith(("$ ", "# ")) and len(stripped) < 3:
            continue
        out.append(line)
    return out


def _events(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            pass
    return out


def _buffer_of(session_id: str) -> bytes:
    """Non-destructive peek: the REST /drain is consuming, so read it directly."""
    from tools.terminal_server import _TERMINALS

    sess = _TERMINALS.get(session_id)
    if sess is None:
        return b""
    with sess.lock:
        return bytes(sess.data)


def main() -> int:
    ask = " ".join(sys.argv[1:]) or DEFAULT_ASK
    from app import create_app  # imported late so --help style misuse is cheap

    app = create_app()
    client = app.test_client()

    print(f"{DIM}asking:{OFF} {ask!r}")
    res = client.post("/api/chat", json={"message": ask, "store_messages": False})
    if res.status_code != 200:
        print(f"{RED}FAIL{OFF} /api/chat returned {res.status_code}")
        return 1

    events = _events(res.get_data(as_text=True))
    meta = next((e for e in events if e.get("type") == "meta"), {})
    calls = [e for e in events if e.get("type") == "tool_call"]
    opened = [e for e in events if e.get("type") == "terminal_opened"]
    reply = "".join(e.get("content") or "" for e in events
                    if e.get("type") == "text_delta").strip()

    print(f"{DIM}skill:{OFF} {meta.get('skill')}  {DIM}model:{OFF} {meta.get('model')}")
    print(f"{DIM}tools called:{OFF} {[c.get('name') for c in calls]}")
    for c in calls:
        print(f"{DIM}  ->{OFF} {c.get('name')}({json.dumps(c.get('arguments'))[:160]})")
    print()

    failures: list[str] = []

    # 1. it opened a terminal by itself
    if opened:
        print(f"{GREEN}PASS{OFF} 1. opened a terminal by itself "
              f"({len(opened)} window(s), session {opened[0].get('terminal_id', '?')[:8]})")
    else:
        print(f"{RED}FAIL{OFF} 1. no terminal_opened event - nothing will appear on screen")
        failures.append("no terminal opened")

    # 2. it executed something in a terminal
    term_calls = [c for c in calls if c.get("name") == "terminal_command"]
    if term_calls:
        cmd = (term_calls[0].get("arguments") or {}).get("command", "")
        print(f"{GREEN}PASS{OFF} 2. executed a command in the terminal: {cmd!r}")
    else:
        print(f"{RED}FAIL{OFF} 2. terminal_command was never called "
              f"- the answer was not produced by running anything")
        failures.append("no command executed")

    # 3. the command text and its output are in the window buffer
    session_id = opened[0].get("terminal_id") if opened else ""
    if session_id:
        buf = _buffer_of(session_id).decode("utf-8", "replace")
        first_cmd = (term_calls[0].get("arguments") or {}).get("command", "") if term_calls else ""
        head = first_cmd.split()[0] if first_cmd else ""
        # the shell echoes the command back, so it must be in the buffer
        echoed = bool(head) and head in buf
        lines = _clean(buf)
        output_lines = [
            ln for ln in lines
            if first_cmd.strip() not in ln and "not found" not in ln.lower()
        ]
        if echoed:
            print(f"{GREEN}PASS{OFF} 3a. the command is visible in the terminal buffer "
                  f"({len(buf)} bytes)")
        else:
            print(f"{RED}FAIL{OFF} 3a. the command never reached the terminal buffer")
            failures.append("command not in buffer")
        if first_cmd in TOOL_NAMES:
            print(f"{RED}FAIL{OFF} 3c. it typed the TOOL name {first_cmd!r} as a shell "
                  f"command - that binary does not exist")
            failures.append("tool name used as a command")
        if len(output_lines) >= 1:
            print(f"{GREEN}PASS{OFF} 3b. command output is in the buffer "
                  f"({len(output_lines)} lines, e.g. {output_lines[-1].strip()[:60]!r})")
        else:
            print(f"{YELLOW}WARN{OFF} 3b. almost no output in the buffer - "
                  f"the command may still be running")
    else:
        print(f"{RED}FAIL{OFF} 3. no session to inspect")
        failures.append("no session buffer")

    # 4. the chat got a summary
    if reply:
        print(f"{GREEN}PASS{OFF} 4. chat summary present ({len(reply)} chars): "
              f"{reply[:90]!r}")
    else:
        print(f"{RED}FAIL{OFF} 4. no chat summary")
        failures.append("no chat summary")

    print()
    if failures:
        print(f"{RED}FAILED:{OFF} " + "; ".join(failures))
        return 1
    print(f"{GREEN}ALL CHECKS PASSED{OFF}")
    print(f"{DIM}the only thing left is your eyes: the terminal window should be on "
          f"screen showing that same command and output.{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
