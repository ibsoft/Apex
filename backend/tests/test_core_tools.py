"""Tests for dangerous/tools that run external processes."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import config  # noqa: E402
from skills.manager import SkillManager  # noqa: E402
from tools.base import Tool, ToolContext, ToolRegistry  # noqa: E402
from tools.core_tools import build_core_tools  # noqa: E402
from tools.vapt_tools import SUDO_MARKER, sudocred_clear, sudocred_get, sudocred_set  # noqa: E402


class MockCfg:
    ENABLE_RUN_PYTHON = False
    ENABLE_RUN_SHELL = True
    RUN_SHELL_TIMEOUT = 10
    WEB_SEARCH_ENGINE = "duckduckgo"
    WEB_SEARCH_DDG_URL = "https://html.duckduckgo.com/html/"
    WEB_SEARCH_DDG_REGION = "us-en"
    WEB_SEARCH_TIMEOUT = 20
    WEB_SEARCH_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) APEX-assistant/1.0"


class RunShellTests(unittest.TestCase):
    def setUp(self):
        registry = type("R", (), {"get": lambda self, key: None})()
        self.tools = {t.name: t for t in build_core_tools(registry, MockCfg())}

    def test_run_shell_echo(self):
        out = self.tools["run_shell"].call({"command": "echo hello-apex"}, ToolContext())
        self.assertIn("hello-apex", out)

    def test_run_shell_disabled_by_default(self):
        class DisabledCfg(MockCfg):
            ENABLE_RUN_SHELL = False

        registry = type("R", (), {"get": lambda self, key: None})()
        tools = {t.name: t for t in build_core_tools(registry, DisabledCfg())}
        out = tools["run_shell"].call({"command": "echo hello"}, ToolContext())
        self.assertIn("disabled", out.lower())

    def test_run_shell_no_command(self):
        out = self.tools["run_shell"].call({"command": ""}, ToolContext())
        self.assertIn("Provide", out)

    def test_run_shell_sudo_requires_credential(self):
        sudocred_clear("alice")
        out = self.tools["run_shell"].call(
            {"command": "id", "sudo": True}, ToolContext(user_id="alice"),
        )
        self.assertIn(SUDO_MARKER, out)

    @patch("tools.vapt_tools.subprocess.run")
    def test_run_shell_sudo_runs_with_credential(self, mock_run):
        sudocred_set("alice", "s3cret", save=True, config=SimpleNamespace(VAPT_SUDO_TTL_MINUTES=0))
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="uid=0(root)", stderr="")
        out = self.tools["run_shell"].call(
            {"command": "id", "sudo": True}, ToolContext(user_id="alice"),
        )
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[0], "sudo")
        self.assertEqual(mock_run.call_args.kwargs["input"], "s3cret\n")
        self.assertIn("uid=0(root)", out)
        self.assertNotIn("s3cret", out)
        sudocred_clear("alice")

    @patch("tools.vapt_tools.subprocess.run")
    def test_run_shell_sudo_wrong_password_forgets_credential(self, mock_run):
        sudocred_set("alice", "badpw", save=True, config=SimpleNamespace(VAPT_SUDO_TTL_MINUTES=0))
        mock_run.return_value = SimpleNamespace(
            returncode=1, stdout="", stderr="Sorry, try again.\n[sudo] password for alice:"
        )
        out = self.tools["run_shell"].call(
            {"command": "id", "sudo": True}, ToolContext(user_id="alice"),
        )
        self.assertIn(SUDO_MARKER, out)
        self.assertIsNone(sudocred_get("alice"))


class CreateSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_data_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        # A registry that really knows these tools, so an unknown name means
        # something: create_skill now validates `tools` against it.
        registry = ToolRegistry()
        for name in ("web_search", "web_fetch"):
            registry.register(
                Tool(name, f"{name} test tool", {"type": "object", "properties": {}},
                     lambda args, ctx: "ok"))
        self.tools = {t.name: t for t in build_core_tools(registry, config)}

    def tearDown(self):
        config.DATA_DIR = self.orig_data_dir
        self.tmp.cleanup()

    def test_create_skill(self):
        out = self.tools["create_skill"].call(
            {
                "name": "test_skill",
                "description": "A test skill.",
                "system_prompt": "You are a test skill.",
                "tools": ["web_search"],
            },
            ToolContext(),
        )
        self.assertIn("created", out.lower())
        path = Path(self.tmp.name) / "skills" / "test_skill.md"
        self.assertTrue(path.exists())
        content = path.read_text()
        self.assertIn("name: test_skill", content)
        self.assertIn("tools: web_search", content)
        mgr = SkillManager(Path(self.tmp.name) / "skills")
        skill = mgr.get("test_skill")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.tools, ["web_search"])

    def test_create_skill_rejects_bad_name(self):
        out = self.tools["create_skill"].call(
            {
                "name": "bad name!",
                "description": "x",
                "system_prompt": "y",
            },
            ToolContext(),
        )
        self.assertIn("must contain only", out)


if __name__ == "__main__":
    unittest.main()
