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
        # Actually SIGHUP the shells. Clearing the dict only drops the
        # references: the login shells would keep running and starve the
        # timing-sensitive tests below (each one waits for real pty output).
        for sess in list(_TERMINALS.values()):
            try:
                sess.close()
            except Exception:
                pass
        _TERMINALS.clear()
        self.temporary.cleanup()

    def create_session(self) -> str:
        response = self.client.post("/api/terminal/session", json={"rows": 24, "cols": 80})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["terminal_id"]

    def peek_text(self, terminal_id: str) -> str:
        """Non-destructive read of a session's pty buffer.

        The HTTP drain endpoint CONSUMES what it returns (it advances `base`
        and trims `data`), so it cannot be polled: the first call takes the
        output and every later call sees an empty stream. The session object
        is reachable from the test, so read its buffer directly instead.
        """
        sess = _TERMINALS.get(terminal_id)
        if sess is None:
            return ""
        with sess.lock:
            raw = bytes(sess.data)
        return _strip_ansi(raw)

    def drain_text(self, terminal_id: str) -> str:
        """One-shot consuming read, for asserting the browser route works."""
        data = self.client.get(f"/api/terminal/session/{terminal_id}/drain",
                               query_string={"from": 0}).get_json()["data"]
        return _strip_ansi(base64.b64decode(data))

    def drain_until(self, terminal_id: str, needle: str, want: int = 1,
                    timeout: float = 10.0, settle: float = 0.3) -> str:
        """Poll the real pty stream until `needle` has appeared `want` times.

        A fixed sleep is load-sensitive: on a busy machine the login shell has
        not produced its output yet and the test fails for no real reason. This
        waits for the actual condition, then settles briefly so a genuine
        over-count (a real double write) is still caught by the assertion.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.peek_text(terminal_id).count(needle) >= want:
                break
            time.sleep(0.05)
        time.sleep(settle)
        return self.peek_text(terminal_id)

    def invoke(self, name: str, args: dict, user: str = "alice", focused_terminal: str = "",
               terminal_map: tuple = ()) -> str:
        tool = self.tools[name]
        return tool.call(args, ToolContext(user_id=user, conversation_id="c1", focused_terminal=focused_terminal,
                                           terminal_map=terminal_map))

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

    def test_no_terminal_open_opens_one_instead_of_asking(self):
        # The model must never be told to ask the operator to open a terminal:
        # it opens one itself and runs the command.
        reply = self.invoke("terminal_command", {"command": "echo auto-open-77"})
        self.assertIn("auto-open-77", reply)
        self.assertIn("opened for you automatically", reply)
        self.assertNotIn("open terminal", reply.lower().replace("terminal window", ""))
        self.assertNotIn("Ask the operator", reply)

    def test_auto_open_emits_terminal_opened_for_the_browser(self):
        # The browser must learn the new session id so the operator SEES the
        # terminal the model opened (and can answer sudo prompts in it).
        events: list[dict] = []
        tool = self.tools["terminal_command"]
        out = tool.call(
            {"command": "echo emit-check-12"},
            ToolContext(user_id="alice", conversation_id="c1", emit=events.append),
        )
        self.assertIn("emit-check-12", out)
        opened = [e for e in events if e.get("type") == "terminal_opened"]
        self.assertEqual(len(opened), 1, events)
        session_id = opened[0]["terminal_id"]
        # It is a real, registered session the browser can drain and type into.
        from tools.terminal_server import _TERMINALS

        self.assertIn(session_id, _TERMINALS)
        drain = self.client.get(f"/api/terminal/session/{session_id}/drain",
                                query_string={"from": 0})
        self.assertEqual(drain.status_code, 200)
        text = self.drain_until(session_id, "emit-check-12")
        self.assertIn("emit-check-12", text)

    def test_second_command_reuses_the_auto_opened_terminal(self):
        first = self.invoke("terminal_command", {"command": "echo first-auto-31"})
        self.assertIn("opened for you automatically", first)
        second = self.invoke("terminal_command", {"command": "echo second-auto-32"})
        self.assertIn("second-auto-32", second)
        # Only one window was ever needed: the note is not repeated.
        self.assertNotIn("opened for you automatically", second)
        self.assertEqual(len(_TERMINALS), 1)

    def test_auto_open_can_be_declined_explicitly(self):
        reply = self.invoke("terminal_command", {"command": "echo x", "auto_open": False})
        self.assertIn("No terminal window is open", reply)
        self.assertEqual(len(_TERMINALS), 0)

    def test_sign_in_and_enable_gates_still_apply_to_auto_open(self):
        # A signed-out turn must not spawn a shell, and a disabled config must
        # not either — auto-opening never bypasses the gates.
        reply = self.invoke("terminal_command", {"command": "ls"}, user="")
        self.assertIn("Sign in", reply)
        disabled = {t.name: t for t in build_terminal_tools(self.disabled_config, capture=instant_capture)}
        reply = disabled["terminal_command"].call(
            {"command": "ls"}, ToolContext(user_id="alice"))
        self.assertIn("ENABLE_RUN_SHELL", reply)
        self.assertEqual(len(_TERMINALS), 0)

    # ---- opening several terminals ----------------------------------------

    def test_count_opens_several_terminals_and_runs_in_the_newest(self):
        # "open 4 terminals" by prompt: the model opens them all itself.
        events: list[dict] = []
        out = self.tools["terminal_command"].call(
            {"command": "echo many-77", "count": 4},
            ToolContext(user_id="alice", conversation_id="c1", emit=events.append),
        )
        self.assertIn("many-77", out)
        self.assertIn("Opened 4 new terminal windows", out)
        self.assertEqual(len(_TERMINALS), 4)
        opened = [e["terminal_id"] for e in events if e.get("type") == "terminal_opened"]
        self.assertEqual(len(opened), 4, events)
        # Every one is announced so the browser can show all four.
        for session_id in opened:
            self.assertIn(session_id, _TERMINALS)

    def test_new_terminal_adds_one_more_instead_of_reusing(self):
        first = self.invoke("terminal_command", {"command": "echo one-51"})
        self.assertIn("one-51", first)
        second = self.invoke("terminal_command", {"command": "echo two-52", "new_terminal": True})
        self.assertIn("Opened 1 new terminal window", second)
        self.assertNotIn("opened for you automatically", second)
        self.assertEqual(len(_TERMINALS), 2)

    def test_new_terminal_accepts_string_flags_from_the_model(self):
        # Models send "true"/"false"/"1" as often as real booleans.
        out = self.invoke("terminal_command", {"command": "echo strflag-61", "new_terminal": "true"})
        self.assertIn("Opened 1 new terminal window", out)
        self.assertEqual(len(_TERMINALS), 1)

    def test_plain_call_still_reuses_the_single_terminal(self):
        self.invoke("terminal_command", {"command": "echo reuse-71"})
        self.invoke("terminal_command", {"command": "echo reuse-72"})
        self.assertEqual(len(_TERMINALS), 1)

    def test_opening_many_never_evicts_a_window_the_user_has_open(self):
        # Three live terminals, then "count": 8. The cap must be reported, not
        # enforced by silently closing the user's windows.
        for i in range(3):
            self.invoke("terminal_command", {"command": f"echo keep-{i}", "new_terminal": True})
        before = set(_TERMINALS)
        reply = self.invoke("terminal_command", {"command": "echo too-many", "count": 8})
        # The per-user cap is 8, so 5 more still fit and are opened — and not
        # one of the three existing windows is closed to make room.
        self.assertIn("Opened 5 new terminal windows", reply)
        self.assertEqual(len(_TERMINALS), 8)
        self.assertTrue(before <= set(_TERMINALS), "no window may be closed for the user")

    def test_at_the_cap_the_tool_refuses_instead_of_closing_anything(self):
        for i in range(8):
            self.invoke("terminal_command", {"command": f"echo fill-{i}", "count": 1, "new_terminal": True})
        self.assertEqual(len(_TERMINALS), 8)
        before = set(_TERMINALS)
        reply = self.invoke("terminal_command", {"command": "echo nope", "new_terminal": True})
        self.assertIn("already", reply)
        self.assertIn("terminal_close", reply)
        self.assertEqual(set(_TERMINALS), before)

    def test_count_is_clamped_so_a_wild_model_cannot_spawn_hundreds(self):
        out = self.invoke("terminal_command", {"command": "echo clamp-81", "count": 4000})
        self.assertIn("Opened 8 new terminal windows", out)
        self.assertEqual(len(_TERMINALS), 8)

    def test_bad_count_falls_back_to_a_single_new_terminal(self):
        for bad in (0, -3, "many", None, 2.5):
            with self.subTest(count=bad):
                _TERMINALS.clear()
                out = self.invoke("terminal_command", {"command": "echo bad-91", "count": bad})
                if bad == 0 or bad == -3 or bad is None:
                    # Nonsense counts simply reuse the existing behaviour.
                    self.assertEqual(len(_TERMINALS), 1)
                else:
                    self.assertIn("echo bad-91", out)

    def test_each_opened_terminal_runs_its_own_command(self):
        # The point of several windows: independent jobs, no interference.
        self.invoke("terminal_command", {"command": "echo jobA-41", "new_terminal": True})
        self.invoke("terminal_command", {"command": "echo jobB-42", "new_terminal": True})
        seen = "".join(self.drain_until(sid, "job", 1) for sid in list(_TERMINALS))
        self.assertIn("jobA-41", seen)
        self.assertIn("jobB-42", seen)

    def test_sessions_listing_never_asks_the_operator_to_open_a_terminal(self):
        reply = self.invoke("terminal_sessions", {})
        self.assertNotIn("open terminal", reply.lower())
        self.assertIn("opens one automatically", reply)
        self.assertEqual(len(_TERMINALS), 0)

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
        text = self.drain_until(terminal_id, "type-only-marker")
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
        text = self.drain_until(terminal_id, "confirm-once-93", 3)
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
        text = self.drain_until(terminal_id, "second-run-62", 3)
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
        text = self.drain_until(terminal_id, "dup-once-42", 3)
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
        self.assertIn("focus-target-A77", self.drain_until(older, "focus-target-A77"))
        self.assertNotIn("focus-target-A77", self.peek_text(newer))

    def test_a_spoken_terminal_number_means_the_number_on_screen(self):
        """`terminal=2` must be the window painted "2", not the 2nd session in
        the backend's own (most-recently-active-first) order."""
        first = self.create_session()
        second = self.create_session()
        # The operator sees terminal 1 = `second` and terminal 2 = `first` (the
        # window list is the reverse of the backend's session order).
        reply = self.invoke("terminal_command", {"command": "echo onscreen-two-B22", "terminal": 2},
                            terminal_map=(second, first))
        self.assertNotIn("No terminal session", reply)
        self.assertIn("onscreen-two-B22", self.drain_until(first, "onscreen-two-B22"))
        self.assertNotIn("onscreen-two-B22", self.peek_text(second))

        reply = self.invoke("terminal_command", {"command": "echo onscreen-one-C33", "terminal": 1},
                            terminal_map=(second, first))
        self.assertIn("onscreen-one-C33", self.drain_until(second, "onscreen-one-C33"))
        self.assertNotIn("onscreen-one-C33", self.peek_text(first))

    def test_a_number_sent_as_text_means_the_same_window(self):
        """Models send both 2 and "2" for the same thing.

        A digit string used to go straight to the id-prefix lookup, so "2" ran
        in whatever session happened to sort first instead of the window the
        operator is looking at. Both spellings must land in the same place.
        """
        first = self.create_session()
        second = self.create_session()
        reply = self.invoke("terminal_command", {"command": "echo text-two-F66", "terminal": "2"},
                            terminal_map=(second, first))
        self.assertNotIn("No terminal session", reply)
        self.assertIn("text-two-F66", self.drain_until(first, "text-two-F66"))
        self.assertNotIn("text-two-F66", self.peek_text(second))

        reply = self.invoke("terminal_command", {"command": "echo text-one-G77", "terminal": "1"},
                            terminal_map=(second, first))
        self.assertIn("text-one-G77", self.drain_until(second, "text-one-G77"))
        self.assertNotIn("text-one-G77", self.peek_text(first))

    def test_a_full_session_id_still_wins_over_the_number(self):
        """The exact-id check runs first, so a digit-looking id is not
        mistaken for a window number."""
        first = self.create_session()
        second = self.create_session()
        # `second` sits at on-screen position 1 but is asked for by its id.
        self.invoke("terminal_command", {"command": "echo exact-id-H88", "terminal": second},
                    terminal_map=(second, first))
        self.assertIn("exact-id-H88", self.drain_until(second, "exact-id-H88"))
        self.assertNotIn("exact-id-H88", self.peek_text(first))

    def test_an_explicit_session_id_beats_the_on_screen_number(self):
        first = self.create_session()
        second = self.create_session()
        self.invoke("terminal_command", {"command": "echo by-id-D44", "terminal": second[:8]},
                    terminal_map=(second, first))
        self.assertIn("by-id-D44", self.drain_until(second, "by-id-D44"))
        self.assertNotIn("by-id-D44", self.peek_text(first))

    def test_numbers_still_work_without_an_on_screen_map(self):
        only = self.create_session()
        self.invoke("terminal_command", {"command": "echo plain-one-E55", "terminal": 1})
        self.assertIn("plain-one-E55", self.drain_until(only, "plain-one-E55"))

    def test_unmatched_focused_terminal_falls_back_to_newest(self):
        older = self.create_session()
        newest = self.create_session()
        reply = self.invoke("terminal_command", {"command": "echo focus-fallback-B88", "terminal": "0"},
                            focused_terminal="does-not-exist")
        self.assertIn("focus-fallback-B88", reply)
        self.assertIn(f"{newest[:8]}", self.invoke("terminal_sessions", {}))
        self.assertIn("focus-fallback-B88", self.drain_until(newest, "focus-fallback-B88"))
        self.assertNotIn("focus-fallback-B88", self.peek_text(older))

    # ---- dead sessions are invisible to the model ---------------------------

    def test_closed_sessions_are_not_listed_or_targeted(self):
        # Kill the shell behind a session: the reader marks it closed. The
        # model-facing tools must NOT list or target it — only a fresh window
        # the operator can actually use counts.
        terminal_id = self.create_session()
        self.client.delete(f"/api/terminal/session/{terminal_id}")
        reply = self.invoke("terminal_sessions", {})
        self.assertIn("No terminal window is open", reply)
        # With no live window left, the command still runs — in a brand new one,
        # never in the dead session.
        reply = self.invoke("terminal_command", {"command": "echo after-close-9"})
        self.assertIn("after-close-9", reply)
        self.assertIn("opened for you automatically", reply)
        self.assertNotIn(terminal_id, _TERMINALS)

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