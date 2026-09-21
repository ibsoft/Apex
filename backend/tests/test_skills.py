"""Tests for skill discovery and frontmatter expansion."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from agent.base import AgentContext
from skills.manager import SkillManager, _expand_config_vars
from tools.base import load_default_tools, ToolRegistry


class ConfigExpansionTests(unittest.TestCase):
    def test_expand_simple_var(self):
        value = _expand_config_vars("$CODE_MODEL")
        self.assertIsInstance(value, str)
        # Default CODE_MODEL falls back to CHATGPT_MODEL
        self.assertTrue(value)

    def test_expand_braced_var(self):
        value = _expand_config_vars("model-${CODE_MODEL}-suffix")
        self.assertIn("model-", value)
        self.assertIn("-suffix", value)

    def test_expand_unknown_var_is_empty(self):
        value = _expand_config_vars("$UNKNOWN_VAR_XYZ")
        self.assertEqual(value, "")

    def test_no_expansion_for_plain_string(self):
        self.assertEqual(_expand_config_vars("gpt-4o-mini"), "gpt-4o-mini")


class SkillManagerTests(unittest.TestCase):
    def test_code_skill_model_expanded_from_config(self):
        mgr = SkillManager()
        code = mgr.get("code")
        self.assertIsNotNone(code)
        self.assertEqual(code.model, os.getenv("CODE_MODEL", os.getenv("CHATGPT_MODEL", "gpt-5-codex")))

    def test_builtin_skills_loaded(self):
        mgr = SkillManager()
        names = {s.name for s in mgr.all()}
        self.assertIn("code", names)
        self.assertIn("general", names)
        self.assertIn("research", names)
        self.assertIn("translator", names)
        self.assertIn("shell", names)

    def test_shell_skill_uses_run_shell(self):
        mgr = SkillManager()
        shell = mgr.get("shell")
        self.assertIsNotNone(shell)
        self.assertIn("run_shell", shell.tools)

    def test_shell_skill_passes_run_shell_to_agent(self):
        mgr = SkillManager()
        skill = mgr.select("shell")
        registry = ToolRegistry()
        load_default_tools(registry, memory=None)
        ctx = AgentContext(
            user_id="u",
            conversation_id="c",
            system_prompt="",
            history=[],
            provider=object(),
            provider_kind="openai",
            engine_name="responses",
            tools=registry,
            skill_tools=list(skill.tools),
        )
        names = {t.name for t in ctx.active_tools()}
        self.assertIn("run_shell", names)
        schema_names = {s["function"]["name"] for s in ctx.tool_schemas()}
        self.assertIn("run_shell", schema_names)


if __name__ == "__main__":
    unittest.main()
