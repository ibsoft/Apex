"""Tests for the PTY terminal server: session lifecycle, owner enforcement,
gating, and a real end-to-end echo through the pty."""
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

from tools.terminal_server import register_terminal_routes, _TERMINALS


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.enabled_config = SimpleNamespace(ENABLE_RUN_SHELL=True, TERMINAL_IDLE_SECONDS=300)
        self.disabled_config = SimpleNamespace(ENABLE_RUN_SHELL=False, TERMINAL_IDLE_SECONDS=300)
        self.app = Flask(__name__)
        self.app.secret_key = "test-secret"

        def require_user():
            from flask import session

            return {"id": session["user_id"]} if session.get("user_id") else None

        register_terminal_routes(self.app, require_user, self.enabled_config)
        self.client = self.app.test_client()
        self.login("alice")

    def tearDown(self):
        self.temporary.cleanup()

    def login(self, user):
        with self.client.session_transaction() as sess:
            sess.clear()
            if user:
                sess["user_id"] = user

    def create(self) -> str:
        response = self.client.post("/api/terminal/session", json={"rows": 24, "cols": 80})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["terminal_id"]

    def read_until(self, terminal_id: str, token: bytes, timeout: float = 10.0, since: int = 0) -> int:
        seen = bytearray()
        deadline = time.time() + timeout
        cursor = since
        while time.time() < deadline:
            response = self.client.get(f"/api/terminal/session/{terminal_id}/drain", query_string={"from": cursor})
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            cursor = payload["from"]
            seen += base64.b64decode(payload["data"])
            if token in seen:
                return cursor
            if payload["closed"]:
                break
            time.sleep(0.04)
        raise AssertionError(f"never saw {token!r}; last output: {bytes(seen[-160:])!r}")

    # ---- lifecycle ---------------------------------------------------------

    def test_create_requires_sign_in(self):
        self.login(None)
        response = self.client.post("/api/terminal/session", json={})
        self.assertEqual(response.status_code, 401)

    def test_terminal_is_gated_by_run_shell(self):
        disabled_flask = Flask(__name__)
        disabled_flask.secret_key = "test-secret"
        # Build a separate client against the same routes (routes are app-bound).
        register_terminal_routes(disabled_flask, self._require_user_factory, self.disabled_config)
        client = disabled_flask.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "alice"
        response = client.post("/api/terminal/session", json={})
        self.assertEqual(response.status_code, 403)
        self.assertIn("ENABLE_RUN_SHELL", response.get_json()["error"])

    def _require_user_factory(self):
        from flask import session

        return {"id": session["user_id"]} if session.get("user_id") else None

    def test_shell_echo_roundtrip(self):
        terminal_id = self.create()
        self.read_until(terminal_id, b"$ ")  # wait for the prompt
        response = self.client.post(
            f"/api/terminal/session/{terminal_id}/input",
            json={"data": base64.b64encode(b"echo terminal-echo-test\n").decode("ascii")},
        )
        self.assertEqual(response.status_code, 200)
        self.read_until(terminal_id, b"terminal-echo-test")

    def test_input_and_resize_survive_owner_checks(self):
        terminal_id = self.create()
        self.login("bob")
        response = self.client.post(
            f"/api/terminal/session/{terminal_id}/input",
            json={"data": base64.b64encode(b"echo nope\n").decode("ascii")},
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.post(f"/api/terminal/session/{terminal_id}/resize", json={"cols": 100, "rows": 30})
        self.assertEqual(response.status_code, 404)
        response = self.client.get(f"/api/terminal/session/{terminal_id}/drain")
        self.assertEqual(response.status_code, 404)

    def test_drain_requires_sign_in(self):
        terminal_id = self.create()
        self.login(None)
        response = self.client.get(f"/api/terminal/session/{terminal_id}/drain")
        self.assertEqual(response.status_code, 401)

    def test_resize_updates_and_accepts_bad_payloads(self):
        terminal_id = self.create()
        response = self.client.post(f"/api/terminal/session/{terminal_id}/resize", json={"cols": 120, "rows": 40})
        self.assertEqual(response.status_code, 200)
        response = self.client.post(f"/api/terminal/session/{terminal_id}/resize", json={"cols": "abc"})
        self.assertEqual(response.status_code, 200)  # falls back to current size

    def test_delete_closes_session_and_removes_it(self):
        terminal_id = self.create()
        response = self.client.delete(f"/api/terminal/session/{terminal_id}")
        self.assertEqual(response.status_code, 200)
        # Drain after close reports closed; the session is gone for further ops.
        response = self.client.delete(f"/api/terminal/session/{terminal_id}")
        self.assertEqual(response.status_code, 404)

    def test_close_using_sighup_parity(self):
        terminal_id = self.create()
        self.client.delete(f"/api/terminal/session/{terminal_id}")
        response = self.client.get(f"/api/terminal/session/{terminal_id}/drain")
        self.assertEqual(response.status_code, 404)

    def drain_until(self, terminal_id: str, token: bytes, timeout: float = 10.0, since: int = 0) -> tuple[int, bytearray]:
        cursor = since
        seen = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            response = self.client.get(
                f"/api/terminal/session/{terminal_id}/drain", query_string={"from": cursor}
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            cursor = payload["from"]
            seen += base64.b64decode(payload["data"])
            if token in seen:
                return cursor, seen
            if payload["closed"]:
                break
            time.sleep(0.04)
        raise AssertionError(f"never saw {token!r}; last output: {bytes(seen[-160:])!r}")

    def test_model_stream_is_independent_of_browser_drain(self):
        terminal_id = self.create()
        sess = _TERMINALS[terminal_id]
        # The browser consumes the prompt on its own drain stream…
        browser_cursor, browser_seen = self.drain_until(terminal_id, b"$ ")
        self.assertIn(b"$ ", browser_seen)
        # …while the model stream still reads those exact bytes from its own 0.
        mc = 0
        model_seen = bytearray()
        deadline = time.time() + 10
        while time.time() < deadline:
            mc, chunk, closed = sess.model_read(mc, limit=65536)
            model_seen += chunk
            if b"$ " in model_seen:
                break
            time.sleep(0.04)
        self.assertIn(b"$ ", model_seen)
        # A command's output is visible on BOTH streams — nothing is stolen.
        self.client.post(
            f"/api/terminal/session/{terminal_id}/input",
            json={"data": base64.b64encode(b"echo model-stream-check\n").decode("ascii")},
        )
        tail = bytearray()
        deadline = time.time() + 10
        while time.time() < deadline:
            mc, chunk, closed = sess.model_read(mc, limit=65536)
            tail += chunk
            if b"model-stream-check" in tail:
                break
            time.sleep(0.04)
        self.assertIn(b"model-stream-check", tail)
        _, browser_tail = self.drain_until(terminal_id, b"model-stream-check", since=browser_cursor)
        self.assertIn(b"model-stream-check", browser_tail)

    def test_reader_survives_high_fd_numbers(self):
        # Regression: the reader used select(), which raises ValueError for
        # fds >= 1024, killing the loop and leaving a silent terminal window.
        import os
        import resource as _resource

        leaks: list[int] = []
        try:
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (1048576, 1048576))
        except (ValueError, OSError):
            pass
        try:
            while max(leaks, default=-1) < 1030:
                leaks.append(os.open("/dev/null", os.O_RDONLY))
            terminal_id = self.create()
            cursor, seen = self.drain_until(terminal_id, b"$ ")
            self.assertIn(b"$ ", seen)
            self.client.post(
                f"/api/terminal/session/{terminal_id}/input",
                json={"data": base64.b64encode(b"echo high-fd-ok\n").decode("ascii")},
            )
            _, echo = self.drain_until(terminal_id, b"high-fd-ok", since=cursor)
            self.assertIn(b"high-fd-ok", echo)
        finally:
            for fd in leaks:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_drain_cursor_semantics(self):
        terminal_id = self.create()
        prompt_cursor = self.read_until(terminal_id, b"$ ")  # consume the prompt
        # Draining from the consumed cursor yields no duplicate prompt bytes.
        response = self.client.get(
            f"/api/terminal/session/{terminal_id}/drain", query_string={"from": prompt_cursor}
        )
        rest = base64.b64decode(response.get_json()["data"])
        self.assertNotIn(b"$ ", rest)
        # Feed a unique marker and confirm it arrives exactly once, with a
        # monotonically advancing cursor.
        self.client.post(
            f"/api/terminal/session/{terminal_id}/input",
            json={"data": base64.b64encode(b"echo marker-uniq-<>-marker\n").decode("ascii")},
        )
        full = bytearray()
        cursor = prompt_cursor
        deadline = time.time() + 10
        while time.time() < deadline:
            response = self.client.get(
                f"/api/terminal/session/{terminal_id}/drain", query_string={"from": cursor}
            )
            payload = response.get_json()
            self.assertGreaterEqual(payload["from"], cursor)
            cursor = payload["from"]
            full += base64.b64decode(payload["data"])
            if b"marker-uniq-<>-marker" in full:
                break
            time.sleep(0.04)
        self.assertEqual(full.count(b"marker-uniq-<>-marker"), 1)


if __name__ == "__main__":
    unittest.main()