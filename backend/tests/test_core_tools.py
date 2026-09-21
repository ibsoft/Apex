"""Tests for dangerous/tools that run external processes."""
from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
