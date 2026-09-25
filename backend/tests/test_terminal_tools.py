"""Tests for the model-facing terminal tools: gating, session targeting, the
capture path, sudo-prompt handoff, and the session listing tool."""
from __future__ import annotations

import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from tools.base import ToolContext
from tools.terminal_tools import build_terminal_tools, _strip_ansi
from tools.terminal_server import register_terminal_routes, _TERMINALS


def instant_capture(sess, start, timeout, quiet, max_bytes):
    """Test stand-in for the internal _capture: let the pty reader flush, then snapshot."""
    time.sleep(0.25)
    with sess.lock:
        raw = bytes(sess.data[max(0, start - sess.base):])
    return _strip_ansi(raw), False


class TerminalToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        _TERMINALS.clear()  # revert leaks between tests
        self.enabled_config = SimpleNamespace(ENABLE_RUN_SHELL=True, TERMINAL_IDLE_SECONDS=300)
        self.disabled_config = SimpleNamespace(ENABLE_RUN_SHELL=False, TERMINAL_IDLE_SECONDS=300)
        self.tools = {t.name: t for t in build_terminal_tools(self.enabled_config, capture=instant_capture)}
        self.app = Flask(__name__)
        self.app.secret_key = "test-secret"

        def require_user():
            from flask import session

            return {"id": session["user_id"]} if session.get("user_id") else None

        register_terminal_routes(self.app, require_user, self.enabled_config)
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "alice"

    def tearDown(self):
        self.temporary.cleanup()

    def create_session(self) -> str:
        response = self.client.post("/api/terminal/session", json={"rows": 24, "cols": 80})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["terminal_id"]

    def invoke(self, name: str, args: dict, user: str = "alice", focused_terminal: str = "") -> str:
        tool = self.tools[name]
        return tool.call(args, ToolContext(user_id=user, conversation_id="c1", focused_terminal=focused_terminal))

    # ---- gating ------------------------------------------------------------

    def test_requires_sign_in(self):
        reply = self.invoke("terminal_command", {"command": "ls"}, user="")
        self.assertIn("Sign in", reply)

    def test_gated_by_run_shell(self):
        disabled = {t.name: t for t in build_terminal_tools(self.disabled_config, capture=instant_capture)}
        reply = disabled["terminal_command"].call(
            {"command": "ls"}, ToolContext(user_id="alice"))
        self.assertIn("ENABLE_RUN_SHELL", reply)
        reply = disabled["terminal_sessions"].call({}, ToolContext(user_id="alice"))
        self.assertIn("ENABLE_RUN_SHELL", reply)

    def test_no_terminal_open_errs(self):
        reply = self.invoke("terminal_command", {"command": "ls"})
        self.assertIn("No terminal window is open", reply)

    # ---- end-to-end through a real session ---------------------------------

    def test_commands_are_typed_into_the_terminal_and_captured(self):
        terminal_id = self.create_session()
        reply = self.invoke("terminal_command", {"command": "echo terminal-tool-marker"})
        self.assertIn("terminal-tool-marker", reply)
        self.assertNotIn("still running", reply)
        # The window stream is untouched: the browser can still drain it.
        drain = self.client.get(f"/api/terminal/session/{terminal_id}/drain",
                                query_string={"from": 0}).get_json()
        self.assertTrue(drain["data"])
        self.assertIn("terminal-tool-marker", _strip_ansi(base64.b64decode(drain["data"])))

    def test_sudo_prompt_detection_hands_off_to_the_operator(self):
        self.create_session()
        reply = self.invoke("terminal_command", {"command": "echo 'Password:'"})
        self.assertIn("[sudo/authentication prompt]", reply)

    # ---- write vs run distinction ------------------------------------------

    def test_type_mode_writes_without_executing(self):
        terminal_id = self.create_session()
        reply = self.invoke("terminal_command", {"command": "echo type-only-marker", "mode": "type"})
        self.assertIn("nothing has run", reply)
        time.sleep(0.4)
        drain = self.client.get(f"/api/terminal/session/{terminal_id}/drain",
                                query_string={"from": 0}).get_json()
        text = _strip_ansi(base64.b64decode(drain["data"]))
        # The text was echoed by the pty (once) and readline may redraw the
        # prompt line (twice), but it was never submitted: a real run would
        # also print the command's output line, pushing the count to 3.
        count = text.count("type-only-marker")
        self.assertTrue(1 <= count <= 2, f"typed text must not execute (count={count}): {text}")

    def test_invalid_mode_is_rejected(self):
        self.create_session()
        reply = self.invoke("terminal_command", {"command": "ls", "mode": "launch"})
        self.assertIn('mode must be "run"', reply)

    # ---- confirm flow: run a command that was already typed ----------------

    def test_run_after_type_presses_enter_without_rewriting_the_line(self):
        terminal_id = self.create_session()
        typed = self.invoke("terminal_command", {"command": "echo confirm-once-93", "mode": "type"})
        self.assertIn("nothing has run", typed)
        # Operator says "confirm": the same command is re-issued as a run. The
        # tool must NOT append the text a second time — only press Enter, so
        # the visible line executes once.
        confirmed = self.invoke("terminal_command", {"command": "echo confirm-once-93"})
        self.assertIn("pressed Enter to execute it", confirmed)
        time.sleep(0.4)
        drain = self.client.get(f"/api/terminal/session/{terminal_id}/drain",
                                query_string={"from": 0}).get_json()
        text = _strip_ansi(base64.b64decode(drain["data"]))
        # One typed echo + a single run => exactly 3 markers (echo, readline
        # redraw, output). A doubled write would leave the mangled command
        # "echo confirm-once-93echo confirm-once-93" and no clean output line.
        self.assertEqual(text.count("confirm-once-93"), 3)
        self.assertNotIn("confirm-once-93echo", text)

    def test_run_after_type_with_a_different_command_writes_normally(self):
        terminal_id = self.create_session()
        self.invoke("terminal_command", {"command": "echo first-typed-51", "mode": "type"})
        reply = self.invoke("terminal_command", {"command": "echo second-run-62"})
        self.assertIn("second-run-62", reply)
        time.sleep(0.4)
        drain = self.client.get(f"/api/terminal/session/{terminal_id}/drain",
                                query_string={"from": 0}).get_json()
        text = _strip_ansi(base64.b64decode(drain["data"]))
        # The different command was written normally and executed once.
        self.assertEqual(text.count("second-run-62"), 3)

    def test_confirm_flag_clears_after_execution(self):
        terminal_id = self.create_session()
        self.invoke("terminal_command", {"command": "echo stale-typed-08", "mode": "type"})
        self.invoke("terminal_command", {"command": "echo stale-typed-08"})  # confirmed -> Enter
        # The pending flag is consumed; a later manual run MUST re-write the line.
        from tools.terminal_server import _TERMINALS
        _TERMINALS[terminal_id].last_run_at = 0.0  # let the 5s guard lapse
        reply = self.invoke("terminal_command", {"command": "echo stale-typed-08"})
        self.assertIn("was sent to the terminal", reply)
        self.assertNotIn("already typed", reply)

    # ---- exact-once guard: a retried command must not run twice -------------

    def test_run_mode_retry_is_observed_not_re_executed(self):
        terminal_id = self.create_session()
        first = self.invoke("terminal_command", {"command": "echo dup-once-42"})
        self.assertIn("dup-once-42", first)
        second = self.invoke("terminal_command", {"command": "echo dup-once-42"})
        self.assertIn("NOT sent again", second)
        time.sleep(0.4)
        drain = self.client.get(f"/api/terminal/session/{terminal_id}/drain",
                                query_string={"from": 0}).get_json()
        text = _strip_ansi(base64.b64decode(drain["data"]))
        # One single run prints the marker 3 times: pty echo, readline's
        # redraw of the command line, and the command's own output line.
        # A double run would produce 6.
        self.assertEqual(text.count("dup-once-42"), 3)

    def test_duplicate_guard_allows_different_commands(self):
        self.create_session()
        self.invoke("terminal_command", {"command": "echo aaa-111"})
        reply = self.invoke("terminal_command", {"command": "echo bbb-222"})
        self.assertIn("bbb-222", reply)
        self.assertNotIn("NOT sent again", reply)

    def test_duplicate_guard_expires_so_later_repeats_are_allowed(self):
        terminal_id = self.create_session()
        self.invoke("terminal_command", {"command": "echo rearm-77"})
        from tools.terminal_server import _TERMINALS
        _TERMINALS[terminal_id].last_run_at = 0.0  # pretend the window passed
        reply = self.invoke("terminal_command", {"command": "echo rearm-77"})
        self.assertIn("rearm-77", reply)
        self.assertNotIn("NOT sent again", reply)

    # ---- targeting and listing ---------------------------------------------

    def test_session_listing_shows_open_terminals_and_none_state(self):
        reply = self.invoke("terminal_sessions", {})
        self.assertIn("No terminal window is open", reply)
        self.create_session()
        reply = self.invoke("terminal_sessions", {})
        self.assertIn("1.", reply)

    def test_terminal_targeting_by_id_and_index(self):
        first = self.create_session()
        second = self.create_session()
        # A bogus id yields a clear error.
        reply = self.invoke("terminal_command", {"command": "echo x", "terminal": "nope"})
        self.assertIn("No terminal session matches", reply)
        # The oldest session is index 2 in recency order; address it by full id.
        reply = self.invoke("terminal_command", {"command": "echo targeted-old-window", "terminal": first})
        self.assertIn("targeted-old-window", reply)

    def test_bad_index_reports_open_count(self):
        self.create_session()
        reply = self.invoke("terminal_command", {"command": "echo x", "terminal": 7})
        self.assertIn("only 1 open", reply)

    # ---- focused-terminal defaulting ----------------------------------------

    def test_commands_default_to_the_focused_terminal_not_the_newest(self):
        older = self.create_session()
        newer = self.create_session()
        reply = self.invoke("terminal_command", {"command": "echo focus-target-A77", "terminal": ""},
                            focused_terminal=older)
        self.assertIn("focus-target-A77", reply)
        # terminal_sessions marks the focused one
        listing = self.invoke("terminal_sessions", {}, focused_terminal=older)
        self.assertIn(f"{older[:8]} (", listing)
        self.assertIn("(focused)", listing)
        # Only the OLDER (focused) session received the command, not the newest.
        time.sleep(0.4)
        text_of = lambda tid: _strip_ansi(base64.b64decode(
            self.client.get(f"/api/terminal/session/{tid}/drain",
                            query_string={"from": 0}).get_json()["data"]))
        self.assertIn("focus-target-A77", text_of(older))
        self.assertNotIn("focus-target-A77", text_of(newer))

    def test_unmatched_focused_terminal_falls_back_to_newest(self):
        older = self.create_session()
        newest = self.create_session()
        reply = self.invoke("terminal_command", {"command": "echo focus-fallback-B88", "terminal": "0"},
                            focused_terminal="does-not-exist")
        self.assertIn("focus-fallback-B88", reply)
        self.assertIn(f"{newest[:8]}", self.invoke("terminal_sessions", {}))
        time.sleep(0.4)
        text_of = lambda tid: _strip_ansi(base64.b64decode(
            self.client.get(f"/api/terminal/session/{tid}/drain",
                            query_string={"from": 0}).get_json()["data"]))
        self.assertIn("focus-fallback-B88", text_of(newest))
        self.assertNotIn("focus-fallback-B88", text_of(older))

    # ---- dead sessions are invisible to the model ---------------------------

    def test_closed_sessions_are_not_listed_or_targeted(self):
        # Kill the shell behind a session: the reader marks it closed. The
        # model-facing tools must NOT list or target it — only a fresh window
        # the operator can actually use counts.
        terminal_id = self.create_session()
        self.client.delete(f"/api/terminal/session/{terminal_id}")
        reply = self.invoke("terminal_sessions", {})
        self.assertIn("No terminal window is open", reply)
        reply = self.invoke("terminal_command", {"command": "echo x"})
        self.assertIn("No terminal window is open", reply)

    def test_opening_a_new_terminal_drops_stale_closed_sessions(self):
        first = self.create_session()
        # Simulate a session that died server-side but was never deleted: the
        # registry still holds it. Opening a fresh terminal must clean it up so
        # it can never be listed as a usable window.
        from tools.terminal_server import _TERMINALS as reg

        reg[first].close()
        second = self.create_session()
        reply = self.invoke("terminal_sessions", {})
        self.assertIn(second[:8], reply)
        self.assertNotIn(first[:8], reply)


if __name__ == "__main__":
    unittest.main()