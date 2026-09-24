"""Tests for the VAPT skill: project lifecycle, command/script runners,
artifacts, report download links, sudo credential store and routes."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask, session

from skills.manager import SkillManager
from tools.base import ToolContext
from tools.vapt_tools import (
    SUDO_MARKER,
    build_vapt_tools,
    register_vapt_routes,
    sudocred_clear,
    sudocred_get,
    sudocred_set,
    translate_sudo_marker,
)


def _config(root: Path, enabled: bool = True, **overrides) -> SimpleNamespace:
    base = dict(
        VAPT_ENABLED=enabled,
        VAPT_PROJECTS=root / "projects",
        VAPT_MAX_PARALLEL=3,
        VAPT_CMD_TIMEOUT=60,
        VAPT_REPORT_TTL_SECONDS=3600,
        VAPT_SUDO_SINGLE_USE_SECONDS=5,
        VAPT_SUDO_TTL_MINUTES=0,
        SECRET_KEY="test-secret",
        BASE_URL="http://localhost",
        ENABLE_RUN_SHELL=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class VaptToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _config(self.root)
        self.ctx = ToolContext(user_id="alice")

        built = build_vapt_tools(self.config)
        self.by_name = {t.name: t for t in built}

        self.app = Flask(__name__)
        self.app.secret_key = self.config.SECRET_KEY
        register_vapt_routes(
            self.app,
            lambda: {"id": session["user_id"]} if "user_id" in session else None,
            self.config,
        )
        self.client = self.app.test_client()

    def tearDown(self):
        sudocred_clear("alice")
        sudocred_clear("bob")
        self.temporary.cleanup()

    def login(self, user):
        with self.client.session_transaction() as sess:
            sess.clear()
            if user:
                sess["user_id"] = user

    # ---- gating -------------------------------------------------------------
    def test_disabled_by_config(self):
        cfg = _config(self.root, enabled=False)
        tools = {t.name: t for t in build_vapt_tools(cfg)}
        result = tools["vapt_run"].call({"target": "t", "command": "echo hi"}, self.ctx)
        self.assertIn("disabled", result)

    # ---- project init -------------------------------------------------------
    def test_project_init_creates_structure(self):
        result = self.by_name["vapt_project_init"].call({"target": "example.com"}, self.ctx)
        proj = self.root / "projects" / "example.com"
        self.assertIn("Project ready", result)
        for sub in ("artifacts", "scripts", "reports"):
            self.assertTrue((proj / sub).is_dir())
        self.assertTrue((proj / "project.json").is_file())

    def test_project_init_rejects_traversal(self):
        result = self.by_name["vapt_project_init"].call({"target": "../../escape"}, self.ctx)
        # The target is treated as a name and pinned inside the projects root.
        root = self.root / "projects"
        projects = [p for p in root.iterdir() if p.is_dir()]
        self.assertEqual(len(projects), 1)
        self.assertTrue(projects[0].is_relative_to(root))
        self.assertNotEqual(projects[0], root)
        self.assertIn("Project ready", result)

    # ---- vapt_run -----------------------------------------------------------
    def test_run_missing_tool(self):
        result = self.by_name["vapt_run"].call(
            {"target": "example.com", "command": "definitely_not_a_real_tool_xyz --scan"},
            self.ctx,
        )
        self.assertIn("not installed", result)

    def test_run_captures_artifact_and_meta(self):
        result = self.by_name["vapt_run"].call(
            {"target": "example.com", "command": "printf artifact-bytes", "tag": "probe", "phase": "recon"},
            self.ctx,
        )
        self.assertIn("exit 0", result)
        self.assertIn("artifact", result)
        artifacts = self.root / "projects" / "example.com" / "artifacts"
        outs = list(artifacts.rglob("*.out"))
        metas = list(artifacts.rglob("*.meta.json"))
        self.assertEqual(len(outs), 1)
        self.assertEqual(len(metas), 1)
        self.assertIn("artifact-bytes", outs[0].read_text(encoding="utf-8"))
        meta = json.loads(metas[0].read_text(encoding="utf-8"))
        self.assertEqual(meta["rc"], 0)
        self.assertEqual(meta["command"], "printf artifact-bytes")

    def test_run_sudo_without_credential_returns_marker(self):
        sudocred_clear("alice")
        result = self.by_name["vapt_run"].call(
            {"target": "example.com", "command": "id", "sudo": True},
            self.ctx,
        )
        self.assertIn(SUDO_MARKER, result)

    @patch("tools.vapt_tools.subprocess.run")
    def test_run_sudo_feeds_credential_via_stdin(self, mock_run):
        sudocred_set("alice", "s3cret!", save=True, config=self.config)
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="uid=0(root)", stderr="")
        result = self.by_name["vapt_run"].call(
            {"target": "example.com", "command": "id", "sudo": True, "tag": "priv"},
            self.ctx,
        )
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[0], "sudo")
        self.assertIn("-S", cmd)
        self.assertEqual(mock_run.call_args.kwargs["input"], "s3cret!\n")
        self.assertNotIn("s3cret!", result)
        self.assertIn("uid=0(root)", result)

    @patch("tools.vapt_tools.subprocess.run")
    def test_run_redacts_password_from_output(self, mock_run):
        sudocred_set("alice", "hunter2", save=True, config=self.config)
        mock_run.return_value = SimpleNamespace(
            returncode=0, stdout="operation hunter2 completed", stderr=""
        )
        result = self.by_name["vapt_run"].call(
            {"target": "example.com", "command": "priv-action", "sudo": True, "tag": "pw"},
            self.ctx,
        )
        self.assertNotIn("hunter2", result)

    # ---- vapt_script --------------------------------------------------------
    def test_script_sequential_writes_artifacts(self):
        result = self.by_name["vapt_script"].call(
            {"target": "example.com", "name": "recon1",
             "commands": ["printf alpha", "printf beta"], "phase": "recon"},
            self.ctx,
        )
        self.assertIn("Batch", result)
        scripts = list((self.root / "projects" / "example.com" / "scripts").glob("*.sh"))
        self.assertEqual(len(scripts), 1)
        for marker in ("alpha", "beta"):
            self.assertIn(marker, result)
        artifacts = (self.root / "projects" / "example.com" / "artifacts" / "scripts")
        self.assertEqual(len(list(artifacts.glob("*.out"))), 2)

    def test_script_parallel_reports_rcs(self):
        result = self.by_name["vapt_script"].call(
            {"target": "example.com", "name": "par", "commands": ["true", "true"], "parallel": True},
            self.ctx,
        )
        self.assertIn("parallel", result)
        self.assertIn("rc=0", result)

    def test_script_detects_missing_tools(self):
        result = self.by_name["vapt_script"].call(
            {"target": "example.com", "name": "bad",
             "commands": ["definitely_not_a_real_tool_xyz"]},
            self.ctx,
        )
        self.assertIn("not installed", result)

    # ---- vapt_missing -------------------------------------------------------
    def test_missing_lists_unavailable_binaries(self):
        result = self.by_name["vapt_missing"].call(
            {"commands": ["definitely_not_a_real_tool_xyz", "printf"]}, self.ctx,
        )
        self.assertIn("definitely_not_a_real_tool_xyz", result)
        self.assertNotIn("printf", result)

    # ---- vapt_projects / vapt_destroy --------------------------------------
    def test_projects_lists_engagements(self):
        self.assertEqual("No VAPT projects yet.", self.by_name["vapt_projects"].call({}, self.ctx))
        self.by_name["vapt_project_init"].call({"target": "example.com"}, self.ctx)
        self.by_name["vapt_run"].call(
            {"target": "example.com", "command": "printf x", "phase": "sca"}, self.ctx,
        )
        result = self.by_name["vapt_projects"].call({}, self.ctx)
        self.assertIn("example.com", result)
        self.assertIn("artifacts", result)
        self.assertIn("0 reports", result)
        self.by_name["vapt_report"].call(
            {"target": "example.com", "markdown": "# R"}, self.ctx,
        )
        result = self.by_name["vapt_projects"].call({}, self.ctx)
        self.assertIn("1 reports", result)

    def test_destroy_requires_confirmation(self):
        self.by_name["vapt_project_init"].call({"target": "example.com"}, self.ctx)
        refused = self.by_name["vapt_destroy"].call({"target": "example.com"}, self.ctx)
        self.assertIn("confirm=true", refused)
        self.assertTrue((self.root / "projects" / "example.com").is_dir())
        result = self.by_name["vapt_destroy"].call(
            {"target": "example.com", "confirm": True}, self.ctx,
        )
        self.assertIn("deleted", result)
        self.assertFalse((self.root / "projects" / "example.com").exists())

    def test_destroy_missing_target(self):
        result = self.by_name["vapt_destroy"].call(
            {"target": "ghost.example", "confirm": True}, self.ctx,
        )
        self.assertIn("nothing to delete", result)

    def test_report_addendum_appended(self):
        url = json.loads(self.by_name["vapt_report"].call(
            {"target": "example.com", "markdown": "# Base\n", "addendum": "New finding added."},
            self.ctx,
        ))["report_url"]
        self.login("alice")
        body = self.client.get(url).get_data(as_text=True)
        self.assertIn("## Addendum", body)
        self.assertIn("New finding added.", body)

    # ---- vapt_read ----------------------------------------------------------
    def test_read_lists_and_returns_content(self):
        self.by_name["vapt_run"].call(
            {"target": "example.com", "command": "printf secret-data", "tag": "probe", "phase": "recon"},
            self.ctx,
        )
        listing = self.by_name["vapt_read"].call({"target": "example.com"}, self.ctx)
        self.assertIn("recon", listing)
        read = self.by_name["vapt_read"].call(
            {"target": "example.com", "artifact": "recon/001_probe.out"}, self.ctx,
        )
        self.assertIn("secret-data", read)

    def test_read_rejects_traversal(self):
        self.by_name["vapt_project_init"].call({"target": "example.com"}, self.ctx)
        result = self.by_name["vapt_read"].call(
            {"target": "example.com", "artifact": "../../../etc/passwd"}, self.ctx,
        )
        self.assertIn("escapes", result)

    # ---- vapt_report + download --------------------------------------------
    def test_report_writes_markdown_and_download_works(self):
        body = "# VAPT Report\n\nTarget: example.com\n"
        result = json.loads(self.by_name["vapt_report"].call(
            {"target": "example.com", "markdown": body}, self.ctx,
        ))
        self.assertIn("report_url", result)
        self.assertTrue(result["report_url"].startswith("http://localhost/api/vapt/download/"))
        report_dir = self.root / "projects" / "example.com" / "reports"
        md_files = list(report_dir.glob("*.md"))
        self.assertEqual(len(md_files), 1)
        self.assertIn("# VAPT Report", md_files[0].read_text(encoding="utf-8"))

        self.login("alice")
        self.assertEqual(self.client.get(result["report_url"]).status_code, 200)
        response = self.client.get(result["report_url"])
        self.assertIn("attachment;", response.headers["Content-Disposition"])
        self.assertIn("example.com", response.headers["Content-Disposition"])

    def test_report_download_auth_and_fingerprint(self):
        body = "# R\n"
        url = json.loads(self.by_name["vapt_report"].call(
            {"target": "example.com", "markdown": body}, self.ctx,
        ))["report_url"]
        self.login(None)
        self.assertEqual(self.client.get(url).status_code, 401)
        self.login("bob")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.login("alice")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url + "x").status_code, 404)

    def test_report_link_expires(self):
        body = "# R\n"
        url = json.loads(self.by_name["vapt_report"].call(
            {"target": "example.com", "markdown": body}, self.ctx,
        ))["report_url"]
        self.login("alice")
        with patch("itsdangerous.timed.time.time", return_value=time.time() + 7200):
            self.assertEqual(self.client.get(url).status_code, 410)

    def test_report_requires_body(self):
        result = self.by_name["vapt_report"].call({"target": "example.com"}, ToolContext(user_id="alice"))
        self.assertIn("markdown", result)

    # ---- sudo credential store ---------------------------------------------
    def test_credential_store_single_use_expiry(self):
        cfg = _config(self.root, VAPT_SUDO_SINGLE_USE_SECONDS=1)
        sudocred_set("alice", "pw1", save=False, config=cfg)
        self.assertIsNotNone(sudocred_get("alice"))
        with patch("tools.vapt_tools.time.time", return_value=time.time() + 5):
            self.assertIsNone(sudocred_get("alice"))

    def test_credential_store_saved_indefinite(self):
        cfg = _config(self.root, VAPT_SUDO_TTL_MINUTES=0)
        sudocred_set("alice", "pw1", save=True, config=cfg)
        with patch("tools.vapt_tools.time.time", return_value=time.time() + 3600):
            self.assertIsNotNone(sudocred_get("alice"))
        sudocred_clear("alice")
        self.assertIsNone(sudocred_get("alice"))

    def test_credential_store_per_user_isolation(self):
        sudocred_set("alice", "pw1", save=True, config=self.config)
        self.assertIsNone(sudocred_get("bob"))
        sudocred_set("bob", "pw2", save=True, config=self.config)
        self.assertEqual(sudocred_get("alice")["password"], "pw1")
        self.assertEqual(sudocred_get("bob")["password"], "pw2")

    # ---- routes -------------------------------------------------------------
    def test_password_route_set_and_status(self):
        self.login("alice")
        resp = self.client.post("/api/vapt/password", json={"password": "s3cret", "save": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        status = self.client.get("/api/vapt/status").get_json()
        self.assertEqual(status["password"], "available")
        self.assertTrue(status["saved"])

    def test_password_route_works_with_vapt_disabled(self):
        # The sudo popup must work for the shell skill even when VAPT is off.
        cfg = _config(self.root, enabled=False)
        app = Flask(__name__)
        app.secret_key = cfg.SECRET_KEY
        register_vapt_routes(
            app, lambda: {"id": "alice"} if "user_id" in session else None, cfg,
        )
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "alice"
        resp = client.post("/api/vapt/password", json={"password": "pw", "save": False})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(client.get("/api/vapt/status").get_json()["password"], "available")
        self.assertEqual(client.delete("/api/vapt/password").status_code, 200)

    def test_password_route_clear(self):
        self.login("alice")
        self.client.post("/api/vapt/password", json={"password": "s3cret", "save": True})
        self.assertEqual(self.client.delete("/api/vapt/password").status_code, 200)
        self.assertEqual(self.client.get("/api/vapt/status").get_json()["password"], "unavailable")

    def test_password_route_requires_login(self):
        self.login(None)
        self.assertEqual(self.client.post("/api/vapt/password", json={"password": "x"}).status_code, 401)

    # ---- marker translation -------------------------------------------------
    def test_translate_marker(self):
        ok, reason = translate_sudo_marker(f"done\n{SUDO_MARKER}sudo credential required")
        self.assertTrue(ok)
        self.assertIn("sudo", reason)
        ok, _ = translate_sudo_marker("normal output")
        self.assertFalse(ok)

    # ---- skill discovery ----------------------------------------------------
    def test_vapt_skill_discovery(self):
        manager = SkillManager(Path(__file__).resolve().parents[1] / "skills" / "definitions")
        skill = manager.get("VAPT")
        self.assertIsNotNone(skill)
        for name in ("vapt_project_init", "vapt_run", "vapt_script", "vapt_read", "vapt_report"):
            self.assertIn(name, skill.tools)


if __name__ == "__main__":
    unittest.main()