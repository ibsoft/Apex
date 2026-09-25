"""Tests for run_shell on-screen capture and its download / preview routes.

Covers: save_shell_output signing + file location, the /api/shell/download route
(auth, ownership, tamper), the preview renderer classifying a shell link as a
text file, and run_shell with on_screen=true returning a link the frontend can
open in a window.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask

from tools.base import ToolContext
from tools.core_tools import build_core_tools
from tools.shell_out import save_shell_output, register_shell_routes


class _Registry:
    def get(self, key):
        return None


class ShellOutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.config = SimpleNamespace(
            DATA_DIR=self.data_dir,
            SECRET_KEY="test-secret",
            BASE_URL="http://localhost",
            SHELL_OUT_TTL_SECONDS=3600,
            ENABLE_RUN_SHELL=True,
            RUN_SHELL_TIMEOUT=10,
        )
        self.app = Flask(__name__)
        self.app.secret_key = self.config.SECRET_KEY

        def require_user():
            from flask import session

            return {"id": session["user_id"]} if session.get("user_id") else None

        register_shell_routes(self.app, require_user, self.config)
        from tools.preview_tools import register_preview_routes

        register_preview_routes(self.app, require_user, self.config)
        self.client = self.app.test_client()
        self.login("alice")
        self.tools = {t.name: t for t in build_core_tools(_Registry(), self.config)}

    def tearDown(self):
        self.temporary.cleanup()

    def login(self, user):
        with self.client.session_transaction() as sess:
            sess.clear()
            if user:
                sess["user_id"] = user

    def token_of(self, url):
        return url.rsplit("/", 1)[-1]

    def shell_files(self):
        directory = self.data_dir / "generated" / "shell" / "alice"
        return sorted(directory.glob("*.txt")) if directory.is_dir() else []

    # ---- capture ------------------------------------------------------------

    def test_save_shell_output_writes_user_bound_file_and_returns_url(self):
        url, filename = save_shell_output(self.config, "alice", "ls -la", "total 4\nfoo.txt\n")
        self.assertTrue(url.startswith("http://localhost/api/shell/download/"))
        files = self.shell_files()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, filename)
        self.assertIn("foo.txt", files[0].read_text(encoding="utf-8"))

    def test_save_shell_output_requires_user(self):
        with self.assertRaises(ValueError):
            save_shell_output(self.config, "", "ls", "x")

    def test_expired_files_are_cleaned_up(self):
        save_shell_output(self.config, "alice", "ls", "fresh")
        stale = self.shell_files()[0]
        old = stale.with_name("shell_stale.txt")
        old.write_text("old", encoding="utf-8")
        stamp = time.time() - 10_000
        import os

        os.utime(old, (stamp, stamp))
        save_shell_output(self.config, "alice", "pwd", "home")
        self.assertFalse(old.exists(), "stale file should be cleaned up")

    # ---- download route -----------------------------------------------------

    def test_download_serves_attachment(self):
        url, filename = save_shell_output(self.config, "alice", "ls", "hello-download")
        response = self.client.get(f"/api/shell/download/{self.token_of(url)}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("hello-download", response.get_data(as_text=True))
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))

    def test_download_requires_sign_in(self):
        url, _ = save_shell_output(self.config, "alice", "ls", "x")
        self.login(None)
        self.assertEqual(self.client.get(f"/api/shell/download/{self.token_of(url)}").status_code, 401)

    def test_download_enforces_owner(self):
        url, _ = save_shell_output(self.config, "alice", "ls", "x")
        self.login("bob")
        self.assertEqual(self.client.get(f"/api/shell/download/{self.token_of(url)}").status_code, 403)

    def test_tampered_token_rejected(self):
        url, _ = save_shell_output(self.config, "alice", "ls", "x")
        self.assertEqual(
            self.client.get(f"/api/shell/download/{self.token_of(url)}x").status_code,
            404,
        )

    # ---- preview ------------------------------------------------------------

    def test_preview_renders_shell_output_as_text(self):
        url, _ = save_shell_output(self.config, "alice", "ls", "window-content")
        token = self.token_of(url)
        kind = self.client.get("/api/preview/kind", query_string={"url": f"/api/shell/download/{token}"})
        self.assertEqual(kind.get_json()["kind"], "text")
        response = self.client.get("/api/preview/render", query_string={"url": f"/api/shell/download/{token}"})
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("<pre>", text)
        self.assertIn("window-content", text)

    def test_preview_of_other_users_shell_link_rejected(self):
        url, _ = save_shell_output(self.config, "alice", "ls", "secret")
        self.login("bob")
        response = self.client.get(
            "/api/preview/render", query_string={"url": f"/api/shell/download/{self.token_of(url)}"}
        )
        self.assertEqual(response.status_code, 403)

    # ---- run_shell on_screen ------------------------------------------------

    def test_run_shell_on_screen_returns_link_and_saves_file(self):
        out = self.tools["run_shell"].call(
            {"command": "echo visible-output", "on_screen": True},
            ToolContext(user_id="alice"),
        )
        self.assertIn("visible-output", out)
        self.assertIn("/api/shell/download/", out)
        self.assertIn("[", out)
        self.assertIn("](", out)
        files = self.shell_files()
        self.assertEqual(len(files), 1)
        self.assertIn("visible-output", files[0].read_text(encoding="utf-8"))

    def test_run_shell_on_screen_requires_sign_in(self):
        out = self.tools["run_shell"].call(
            {"command": "echo hi", "on_screen": True},
            ToolContext(),
        )
        self.assertIn("Sign in", out)


if __name__ == "__main__":
    unittest.main()