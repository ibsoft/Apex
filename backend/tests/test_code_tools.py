"""Tests for the CODE skill: project lifecycle (git repos), daily progress
journals, GitHub token store/validation and push construction."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask, session

from skills.manager import SkillManager
from tools.base import ToolContext
from tools.code_tools import (
    GITHUB_MARKER,
    build_code_tools,
    github_token_clear,
    github_token_get,
    github_token_set,
    _venv_python,
    register_code_routes,
    translate_github_marker,
)


def _config(root: Path, **overrides) -> SimpleNamespace:
    cfg = SimpleNamespace(
        CODE_PROJECTS=root / "projects",
        CODE_BRANCH="main",
        CODE_GITHUB_DIR=root / "tokens",
        SECRET_KEY="test-secret",
        BASE_URL="http://localhost",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class CodeToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _config(self.root)
        self.ctx = ToolContext(user_id="alice")
        self.tools = {t.name: t for t in build_code_tools(self.config)}

        self.app = Flask(__name__)
        self.app.secret_key = self.config.SECRET_KEY
        register_code_routes(
            self.app,
            lambda: {"id": session["user_id"]} if "user_id" in session else None,
            self.config,
        )
        self.client = self.app.test_client()
        github_token_clear(self.config, "alice")
        github_token_clear(self.config, "bob")

    def tearDown(self):
        self.temporary.cleanup()

    def login(self, user):
        with self.client.session_transaction() as sess:
            sess.clear()
            if user:
                sess["user_id"] = user

    def _start(self, name="my-app", stack=None):
        args = {"project": name}
        if stack:
            args["stack"] = stack
        return self.tools["code_start"].call(args, self.ctx)

    # ---- gating / validation ------------------------------------------------
    def test_missing_projects_root_guidance(self):
        cfg = SimpleNamespace(CODE_PROJECTS="", CODE_BRANCH="main", CODE_GITHUB_DIR=self.root / "t")
        tools = {t.name: t for t in build_code_tools(cfg)}
        result = tools["code_start"].call({"project": "x"}, self.ctx)
        self.assertIn("CODE_PROJECTS", result)

    def test_start_requires_project_name(self):
        result = self.tools["code_start"].call({"project": " "}, self.ctx)
        self.assertIn("project name", result.lower())

    def test_start_rejects_slash_project(self):
        for bad in ("../escape", "a/b", "a\\b", ".."):
            result = self.tools["code_start"].call({"project": bad}, self.ctx)
            self.assertIn("not a valid project name", result)

    # ---- git project lifecycle ---------------------------------------------
    def test_start_creates_git_repo_and_commits(self):
        result = self._start()
        self.assertIn("ready", result)
        proj = self.root / "projects" / "my-app"
        self.assertTrue((proj / ".git").is_dir())
        self.assertTrue((proj / "README.md").is_file())
        self.assertTrue((proj / ".gitignore").is_file())
        rc, out, _ = self._git(proj, ["log", "-1", "--format=%s"])
        self.assertEqual(rc, 0)
        self.assertIn("Initial scaffold", out)
        rc, out, _ = self._git(proj, ["branch", "--show-current"])
        self.assertEqual(out.strip(), "main")

    def test_start_scaffolds_stack(self):
        result = self._start(stack="flask")
        self.assertIn("flask", result)
        self.assertTrue((self.root / "projects" / "my-app" / "app.py").is_file())
        self.assertTrue((self.root / "projects" / "my-app" / "requirements.txt").is_file())

    def test_python_stack_gets_project_venv(self):
        result = self._start(stack="flask")
        self.assertIn(".venv", result)
        venv_python = _venv_python(self.root / "projects" / "my-app")
        self.assertTrue(venv_python.is_file(), ".venv should contain a python binary")
        self.assertTrue((self.root / "projects" / "my-app" / ".venv" / "bin" / "pip").is_file())
        rc, out, _ = self._git(self.root / "projects" / "my-app", ["check-ignore", ".venv/bin/python"])
        self.assertEqual(rc, 0, ".venv must be gitignored")
        rc, out, _ = self._git(self.root / "projects" / "my-app", ["status", "--porcelain"])
        self.assertEqual(out.strip(), "", "venv must not appear as uncommitted")

    def test_non_python_stack_skips_venv(self):
        self._start(stack="web")
        self.assertFalse((self.root / "projects" / "my-app" / ".venv").exists())

    def test_start_reuses_existing(self):
        self._start()
        result = self._start()
        self.assertIn("already exists", result)
        self.assertIn("Branch", result)

    def test_projects_lists(self):
        self.assertEqual(
            self.tools["code_projects"].call({}, self.ctx),
            "No code projects yet — start one with a project name.",
        )
        self._start("alpha")
        self._start("beta")
        result = self.tools["code_projects"].call({}, self.ctx)
        self.assertIn("alpha", result)
        self.assertIn("beta", result)
        self.assertIn("0 uncommitted", result)

    def test_use_opens_existing(self):
        self._start()
        result = self.tools["code_use"].call({"project": "my-app"}, self.ctx)
        self.assertIn("my-app", result)
        self.assertIn("Branch main", result)
        missing = self.tools["code_use"].call({"project": "ghost"}, self.ctx)
        self.assertIn("start it first", missing)

    def test_destroy_requires_confirm(self):
        self._start()
        refused = self.tools["code_destroy"].call({"project": "my-app"}, self.ctx)
        self.assertIn("confirm=true", refused)
        self.assertTrue((self.root / "projects" / "my-app").is_dir())
        result = self.tools["code_destroy"].call({"project": "my-app", "confirm": True}, self.ctx)
        self.assertIn("deleted", result)
        self.assertFalse((self.root / "projects" / "my-app").exists())

    # ---- progress journal ---------------------------------------------------
    def test_progress_append_and_read(self):
        self._start()
        logged = self.tools["code_progress"].call(
            {"project": "my-app", "note": "implemented the auth flow"}, self.ctx,
        )
        self.assertIn("implemented the auth flow", logged)
        today = self.root / "projects" / "my-app" / "progress"
        files = list(today.glob("*.md"))
        self.assertEqual(len(files), 1)
        read = self.tools["code_progress"].call({"project": "my-app"}, self.ctx)
        self.assertIn("implemented the auth flow", read)

    def test_progress_open_items(self):
        self._start()
        self.tools["code_progress"].call(
            {"project": "my-app", "question": "should we use PostgreSQL?"}, self.ctx,
        )
        self.tools["code_progress"].call(
            {"project": "my-app", "action": "add a docker-compose for the stack"}, self.ctx,
        )
        self.tools["code_progress"].call(
            {"project": "my-app", "feature": "export to PDF later"}, self.ctx,
        )
        read = self.tools["code_progress"].call({"project": "my-app"}, self.ctx)
        self.assertIn("should we use PostgreSQL?", read)
        self.assertIn("add a docker-compose", read)
        self.assertIn("export to PDF later", read)

    # ---- GitHub marker / store ---------------------------------------------
    def test_github_status_marker_when_no_token(self):
        result = self.tools["code_github_status"].call({}, self.ctx)
        self.assertIn(GITHUB_MARKER, result)

    def test_translate_marker(self):
        ok, reason = translate_github_marker(f"{GITHUB_MARKER}No GitHub token stored.")
        self.assertTrue(ok)
        self.assertIn("GitHub", reason)
        ok, _ = translate_github_marker("normal output")
        self.assertFalse(ok)

    @patch("tools.code_tools.requests.get")
    def test_token_store_validate_and_isolation(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"login": "octocat"}
        login, err = github_token_set(self.config, "alice", "ghp_secret")
        self.assertEqual(login, "octocat")
        self.assertEqual(err, "")
        path = self.root / "tokens" / "alice.json"
        self.assertTrue(path.is_file())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        cred = github_token_get(self.config, "alice")
        self.assertEqual(cred["login"], "octocat")
        self.assertEqual(cred["token"], "ghp_secret")
        self.assertIsNone(github_token_get(self.config, "bob"))
        github_token_clear(self.config, "alice")
        self.assertIsNone(github_token_get(self.config, "alice"))

    @patch("tools.code_tools.requests.get")
    def test_token_rejected(self, mock_get):
        mock_get.return_value.status_code = 401
        mock_get.return_value.text = '{"message":"Bad credentials"}'
        login, err = github_token_set(self.config, "alice", "wrong")
        self.assertEqual(login, "")
        self.assertIn("401", err)

    @patch("tools.code_tools.requests.get")
    def test_push_requires_token_marker(self, mock_get):
        self._start()
        result = self.tools["code_push"].call(
            {"project": "my-app", "message": "wip"}, self.ctx,
        )
        self.assertIn(GITHUB_MARKER, result)
        mock_get.assert_not_called()

    def test_push_creates_repo_and_pushes_without_leaking_token(self):
        self._start()

        def fake_run(cmd, **kwargs):
            name = cmd[1]
            if name in ("add", "remote", "-c"):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if name == "commit":
                return SimpleNamespace(returncode=0, stdout="changed", stderr="")
            if name in ("rev-parse", "status", "log"):
                return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        with patch("tools.code_tools.requests.get") as mock_get, \
                patch("tools.code_tools.requests.post") as mock_post, \
                patch("tools.code_tools.subprocess.run", side_effect=fake_run) as mock_run:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"login": "octocat"}
            github_token_set(self.config, "alice", "ghp_secret")
            mock_get.return_value.status_code = 404  # repo does not exist yet
            mock_post.return_value.status_code = 201
            mock_post.return_value.text = "{}"

            result = self.tools["code_push"].call(
                {"project": "my-app", "message": "feature: auth"}, self.ctx,
            )

        self.assertIn("pushed to", result)
        self.assertIn("github.com/octocat/my-app", result)

        create_payload = mock_post.call_args.kwargs["json"]
        self.assertTrue(create_payload.get("private"))

        push_calls = [c for c in mock_run.call_args_list if c.args[0][0] == "git" and c.args[0][1] == "-c"]
        self.assertTrue(push_calls, "push should run through git with a credential helper")
        push_cmd = push_calls[-1].args[0]
        self.assertNotIn("ghp_secret", push_cmd)
        joined = " ".join(push_cmd)
        self.assertIn("credential.helper", joined)
        self.assertIn("https://github.com/octocat/my-app.git", joined)
        self.assertNotIn("ghp_secret", " ".join(str(a) for c in mock_run.call_args_list for a in c.args))
        env = push_calls[-1].kwargs.get("env") or {}
        self.assertEqual(env.get("APEX_GH_TOKEN"), "ghp_secret")
        self.assertEqual(env.get("APEX_GH_LOGIN"), "octocat")

    # ---- routes -------------------------------------------------------------
    @patch("tools.code_tools.requests.get")
    def test_token_route_set_status_clear(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"login": "octocat"}
        self.login("alice")
        resp = self.client.post("/api/code/github-token", json={"token": "ghp_x"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        status = self.client.get("/api/code/github/status").get_json()
        self.assertEqual(status["token"], "available")
        self.assertEqual(status["login"], "octocat")
        self.assertEqual(self.client.delete("/api/code/github-token").status_code, 200)
        self.assertEqual(self.client.get("/api/code/github/status").get_json()["token"], "unavailable")

    def test_token_route_requires_login(self):
        self.login(None)
        self.assertEqual(self.client.post("/api/code/github-token", json={"token": "x"}).status_code, 401)

    # ---- skill discovery ----------------------------------------------------
    def test_code_skill_uses_all_tools(self):
        manager = SkillManager(Path(__file__).resolve().parents[1] / "skills" / "definitions")
        skill = manager.get("code")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.tools, [])  # ALL -> all active tools

    @staticmethod
    def _git(cwd: Path, args: list[str]):
        import subprocess
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr


if __name__ == "__main__":
    unittest.main()