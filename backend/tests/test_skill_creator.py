"""The skill creator must produce a complete, working skill pack.

Covers the pack tools - ``list_tools``, the extended ``create_skill``,
``create_script`` and ``set_env`` - plus the frontmatter traps (a ``---``
horizontal rule and a ``": "`` description) that would otherwise write a skill
file that never loads.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import config  # noqa: E402
from skills.manager import SkillManager  # noqa: E402
from tools.base import Tool, ToolContext, ToolRegistry  # noqa: E402
from tools.core_tools import build_core_tools  # noqa: E402


def _registry(*names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names or ("web_search", "web_fetch"):
        reg.register(Tool(name, f"{name} test tool.",
                          {"type": "object", "properties": {}},
                          lambda args, ctx: "ok"))
    return reg


class PackTestCase(unittest.TestCase):
    """Temp DATA_DIR + ENV_FILE, tools built against a small real registry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orig_data_dir = config.DATA_DIR
        self.orig_env_file = config.ENV_FILE
        config.DATA_DIR = Path(self.tmp.name)
        config.ENV_FILE = str(Path(self.tmp.name) / "backend.env")
        self.addCleanup(self._restore)
        # Register the real core set (which includes the pack tools themselves)
        # so `list_tools` and create_skill validation see a populated registry.
        registry = ToolRegistry()
        built = build_core_tools(registry, config)
        for tool in built:
            registry.register(tool)
        self.tools = {t.name: t for t in built}
        self.ctx = ToolContext()

    def _restore(self):
        config.DATA_DIR = self.orig_data_dir
        config.ENV_FILE = self.orig_env_file

    @property
    def skills_dir(self) -> Path:
        return Path(config.DATA_DIR) / "skills"

    def _create_skill(self, name="demo", **overrides):
        payload = {"name": name, "description": "A demo skill.",
                   "system_prompt": "Do the thing.", "tools": ["web_search"]}
        payload.update(overrides)
        return self.tools["create_skill"].call(payload, self.ctx)

    def _mode(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)


class ListToolsTests(PackTestCase):
    def test_lists_live_registry_names(self):
        rows = json.loads(self.tools["list_tools"].call({}, self.ctx))
        by_name = {r["name"]: r for r in rows}
        self.assertIn("web_search", by_name)
        self.assertIn("create_skill", by_name)
        self.assertIn("set_env", by_name)
        self.assertTrue(by_name["web_search"]["enabled"])
        self.assertFalse(by_name["web_search"]["dangerous"])
        # Descriptions are capped so the listing cannot blow up a context.
        self.assertLessEqual(len(by_name["web_search"]["description"]), 500)


class CreateSkillValidationTests(PackTestCase):
    def test_unknown_tool_is_rejected_before_anything_is_written(self):
        out = self._create_skill(tools=["web_search", "not_a_tool"])
        self.assertIn("Unknown tool", out)
        self.assertIn("not_a_tool", out)
        self.assertFalse((self.skills_dir / "demo.md").exists())

    def test_tools_all_skips_validation(self):
        out = self._create_skill(tools=["ALL"])
        self.assertIn("created", out.lower())
        skill = SkillManager(self.skills_dir).get("demo")
        self.assertEqual(skill.tools, [])

    def test_require_tool_and_exclude_tools_roundtrip(self):
        out = self._create_skill(require_tool=True, exclude_tools=["web_fetch"])
        self.assertIn("created", out.lower())
        skill = SkillManager(self.skills_dir).get("demo")
        self.assertTrue(skill.require_tool)
        self.assertEqual(skill.exclude_tools, ["web_fetch"])
        self.assertEqual(skill.tools, ["web_search"])

    def test_unknown_exclude_tool_is_rejected(self):
        out = self._create_skill(exclude_tools=["ghost_tool"])
        self.assertIn("Unknown tool", out)
        self.assertFalse((self.skills_dir / "demo.md").exists())

    def test_description_with_colon_still_loads(self):
        # Hand-built `description: ...` YAML breaks on ": " and safe_load then
        # rejects the file, so the skill exists but never appears.
        desc = "Risk drafts: structured and citable."
        out = self._create_skill(description=desc)
        self.assertIn("created", out.lower())
        skill = SkillManager(self.skills_dir).get("demo")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.description, desc)

    def test_horizontal_rule_in_prompt_does_not_split_frontmatter(self):
        prompt = "Rules for this skill:\n---\nNever guess a number."
        out = self._create_skill(system_prompt=prompt)
        self.assertIn("created", out.lower())
        skill = SkillManager(self.skills_dir).get("demo")
        self.assertIsNotNone(skill)
        self.assertIn("Never guess a number.", skill.system_prompt)

    def test_disabled_tool_warns_but_creates(self):
        reg = _registry("web_search")
        reg.register(Tool("run_shell", "headless shell",
                          {"type": "object", "properties": {}},
                          lambda args, ctx: "ok", enabled=False))
        tools = {t.name: t for t in build_core_tools(reg, config)}
        out = tools["create_skill"].call(
            {"name": "demo", "description": "x", "system_prompt": "y",
             "tools": ["web_search", "run_shell"]}, self.ctx)
        self.assertIn("created", out.lower())
        self.assertIn("run_shell", out)
        self.assertIn("disabled", out.lower())

    def test_shadowing_a_builtin_is_flagged(self):
        out = self._create_skill(name="research")
        self.assertIn("created", out.lower())
        self.assertIn("shadow", out.lower())


class CreateScriptTests(PackTestCase):
    def setUp(self):
        super().setUp()
        self.assertIn("created", self._create_skill().lower())
        self.pack = self.skills_dir / "demo"

    def test_writes_executable_python_script(self):
        out = self.tools["create_script"].call(
            {"skill": "demo", "filename": "fetch.py",
             "content": "#!/usr/bin/env python3\nprint('hi')\n"}, self.ctx)
        target = self.pack / "fetch.py"
        self.assertTrue(target.exists())
        self.assertEqual(self._mode(target), 0o700)
        self.assertIn(str(target), out)
        self.assertIn(f"python3 {target}", out)

    def test_non_script_suffix_stays_data(self):
        self.tools["create_script"].call(
            {"skill": "demo", "filename": "notes.md", "content": "# notes\n"},
            self.ctx)
        self.assertEqual(self._mode(self.pack / "notes.md"), 0o600)

    def test_nested_subdirectory_is_created(self):
        out = self.tools["create_script"].call(
            {"skill": "demo", "filename": "bin/run.sh",
             "content": "#!/bin/bash\ntrue\n"}, self.ctx)
        target = self.pack / "bin" / "run.sh"
        self.assertTrue(target.exists())
        self.assertIn(f"bash {target}", out)

    def test_traversal_is_rejected(self):
        out = self.tools["create_script"].call(
            {"skill": "demo", "filename": "../../evil.py", "content": "x = 1"},
            self.ctx)
        self.assertIn("escapes", out)
        self.assertFalse((Path(config.DATA_DIR) / "evil.py").exists())

    def test_absolute_path_is_rejected(self):
        out = self.tools["create_script"].call(
            {"skill": "demo", "filename": "/tmp/evil.py", "content": "x = 1"},
            self.ctx)
        self.assertIn("relative", out)
        self.assertFalse(Path("/tmp/evil.py").exists())

    def test_dot_env_must_go_through_set_env(self):
        out = self.tools["create_script"].call(
            {"skill": "demo", "filename": ".env", "content": "A=1"}, self.ctx)
        self.assertIn("set_env", out)
        self.assertFalse((self.pack / ".env").exists())

    def test_unknown_skill_is_rejected(self):
        out = self.tools["create_script"].call(
            {"skill": "ghost", "filename": "x.py", "content": "x = 1"}, self.ctx)
        self.assertIn("no skill named", out)

    def test_overwrite_guard(self):
        payload = {"skill": "demo", "filename": "fetch.py", "content": "v = 1"}
        first = self.tools["create_script"].call(payload, self.ctx)
        self.assertIn("fetch.py", first)
        second = self.tools["create_script"].call({**payload, "content": "v = 2"},
                                                  self.ctx)
        self.assertIn("already exists", second)
        third = self.tools["create_script"].call(
            {**payload, "content": "v = 3", "overwrite": True}, self.ctx)
        self.assertIn("fetch.py", third)
        self.assertEqual((self.pack / "fetch.py").read_text(), "v = 3")


class SetEnvTests(PackTestCase):
    def setUp(self):
        super().setUp()
        self.assertIn("created", self._create_skill().lower())
        self.pack = self.skills_dir / "demo"
        self.pack.mkdir(parents=True, exist_ok=True)
        self.env_file = self.pack / ".env"

    def test_skill_scope_writes_secret_without_echoing_it(self):
        out = self.tools["set_env"].call(
            {"key": "MY_API_TOKEN", "value": "s3cret-value", "scope": "skill",
             "skill": "demo"}, self.ctx)
        self.assertTrue(self.env_file.exists())
        self.assertEqual(self._mode(self.env_file), 0o600)
        self.assertIn("MY_API_TOKEN=s3cret-value", self.env_file.read_text())
        self.assertNotIn("s3cret-value", out)

    def test_skill_scope_preserves_other_lines(self):
        self.env_file.write_text("OTHER_KEY=keepme\nMY_API_TOKEN=old\n")
        os.chmod(self.env_file, 0o600)
        self.tools["set_env"].call(
            {"key": "MY_API_TOKEN", "value": "new", "scope": "skill",
             "skill": "demo"}, self.ctx)
        lines = self.env_file.read_text().splitlines()
        self.assertIn("OTHER_KEY=keepme", lines)
        self.assertIn("MY_API_TOKEN=new", lines)

    def test_remove_deletes_the_line(self):
        self.env_file.write_text("KEEP=1\nGONE=bye\n")
        self.tools["set_env"].call(
            {"key": "GONE", "remove": True, "scope": "skill", "skill": "demo"},
            self.ctx)
        text = self.env_file.read_text()
        self.assertNotIn("GONE", text)
        self.assertIn("KEEP=1", text)

    def test_empty_value_is_refused_with_hint_to_remove(self):
        out = self.tools["set_env"].call(
            {"key": "MY_API_TOKEN", "value": "", "scope": "skill",
             "skill": "demo"}, self.ctx)
        self.assertIn("remove=true", out)
        self.assertFalse(self.env_file.exists())

    def test_remove_on_missing_file_is_a_no_op_message(self):
        out = self.tools["set_env"].call(
            {"key": "NOPE", "remove": True, "scope": "skill", "skill": "demo"},
            self.ctx)
        self.assertIn("not set", out)
        self.assertFalse(self.env_file.exists())

    def test_unknown_skill_is_rejected(self):
        out = self.tools["set_env"].call(
            {"key": "MY_API_TOKEN", "value": "x", "scope": "skill",
             "skill": "ghost"}, self.ctx)
        self.assertIn("no skill named", out)

    def test_invalid_key_is_rejected(self):
        for bad in ("lower_case", "9STARTS_WITH_DIGIT", "HAS-DASH"):
            out = self.tools["set_env"].call(
                {"key": bad, "value": "x", "scope": "skill", "skill": "demo"},
                self.ctx)
            self.assertIn("UPPER_SNAKE_CASE", out, bad)

    def test_backend_scope_writes_env_file_and_applies_live(self):
        key = "APEX_TEST_SKILL_LIVE_VAR"
        secret = "sk-distinctive-42-value"
        self.addCleanup(lambda: os.environ.pop(key, None))
        self.addCleanup(lambda: config.__dict__.pop(key, None))
        env_path = Path(config.ENV_FILE)
        env_path.write_text("EXISTING=untouched\n")
        out = self.tools["set_env"].call(
            {"key": key, "value": secret, "scope": "backend"}, self.ctx)
        self.assertIn("Applied live", out)
        self.assertEqual(os.environ.get(key), secret)
        self.assertEqual(getattr(config, key), secret)
        lines = env_path.read_text().splitlines()
        self.assertIn("EXISTING=untouched", lines)
        self.assertIn(f"{key}={secret}", lines)
        self.assertEqual(self._mode(env_path), 0o600)
        self.assertNotIn(secret, out)  # value never echoed

        out = self.tools["set_env"].call(
            {"key": key, "remove": True, "scope": "backend"}, self.ctx)
        self.assertIn("removed", out)
        self.assertNotIn(key, os.environ)
        self.assertEqual(getattr(config, key), "")
        self.assertNotIn(f"{key}=", env_path.read_text())

    def test_backend_scope_flags_an_unknown_key(self):
        key = "APEX_TOTALLY_UNKNOWN_KEY"
        self.addCleanup(lambda: os.environ.pop(key, None))
        self.addCleanup(lambda: config.__dict__.pop(key, None))
        out = self.tools["set_env"].call(
            {"key": key, "value": "x", "scope": "backend"}, self.ctx)
        self.assertIn("not a known config variable", out)

    def test_scope_must_be_known(self):
        out = self.tools["set_env"].call(
            {"key": "MY_KEY", "value": "x", "scope": "global"}, self.ctx)
        self.assertIn("scope must be", out)


if __name__ == "__main__":
    unittest.main()
