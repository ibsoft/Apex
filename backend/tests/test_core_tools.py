"""Tests for dangerous/tools that run external processes."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from config import config
from skills.manager import SkillManager
from tools.base import ToolContext
from tools.core_tools import build_core_tools


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


class CreateSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_data_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        registry = type("R", (), {"get": lambda self, key: None})()
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
