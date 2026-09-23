"""Tests for skill discovery, frontmatter expansion and routing."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.base import AgentContext
from skills.manager import SkillManager, _expand_config_vars, route_skill
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


class FakeProvider:
    """Yields a fixed text response, one character at a time."""

    def __init__(self, text: str):
        self.text = text

    def chat_stream(self, messages, tools=None):
        for ch in self.text:
            yield {"type": "text", "content": ch}


class SkillRoutingTests(unittest.TestCase):
    def test_route_skill_matches_exact_skill_name(self):
        mgr = SkillManager()
        skills = mgr.all()
        provider = FakeProvider("research")
        self.assertEqual(route_skill("find me sources on climate", skills, provider), "research")

    def test_route_skill_matches_case_insensitive(self):
        mgr = SkillManager()
        skills = mgr.all()
        provider = FakeProvider("EDITOR")
        self.assertEqual(route_skill("make a word report", skills, provider), "EDITOR")

    def test_route_skill_falls_back_to_general(self):
        mgr = SkillManager()
        skills = mgr.all()
        provider = FakeProvider("unknown")
        self.assertEqual(route_skill("hello there", skills, provider), "general")

    def test_route_skill_ignores_noise_around_name(self):
        mgr = SkillManager()
        skills = mgr.all()
        provider = FakeProvider("code is the best fit")
        self.assertEqual(route_skill("write a python function", skills, provider), "code")

    def test_route_skill_returns_fallback_on_error(self):
        class BadProvider:
            def chat_stream(self, messages, tools=None):
                raise RuntimeError("boom")

        mgr = SkillManager()
        skills = mgr.all()
        self.assertEqual(route_skill("anything", skills, BadProvider()), "general")

    def test_route_skill_caches_identical_message(self):
        mgr = SkillManager()
        skills = mgr.all()
        calls = []

        class CountingProvider:
            def chat_stream(self, messages, tools=None):
                calls.append(1)
                return iter([{"type": "text", "content": "translator"}])

        provider = CountingProvider()
        self.assertEqual(route_skill("translate hello", skills, provider), "translator")
        self.assertEqual(route_skill("translate hello", skills, provider), "translator")
        self.assertEqual(len(calls), 1)

    def test_keyword_override_corrects_code_misrouted_to_obsidian(self):
        mgr = SkillManager()
        skills = mgr.all()
        provider = FakeProvider("obsidian")
        self.assertEqual(
            route_skill("write a Python function", skills, provider),
            "code",
        )

    def test_keyword_override_keeps_obsidian_for_note_requests(self):
        mgr = SkillManager()
        skills = mgr.all()
        provider = FakeProvider("code")
        self.assertEqual(
            route_skill("open my daily note in Obsidian", skills, provider),
            "obsidian",
        )

    def test_keyword_override_respects_explicit_code(self):
        mgr = SkillManager()
        skills = mgr.all()
        provider = FakeProvider("code")
        self.assertEqual(
            route_skill("debug this JavaScript bug", skills, provider),
            "code",
        )


class SkillDeleteTests(unittest.TestCase):
    def test_delete_removes_user_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            skills_dir.mkdir()
            (skills_dir / "custom.md").write_text("---\nname: custom\ndescription: x\n---\nbody")
            mgr = SkillManager(skills_dir)
            self.assertTrue(mgr.get("custom"))
            self.assertTrue(mgr.delete("custom"))
            self.assertIsNone(mgr.get("custom"))
            self.assertFalse((skills_dir / "custom.md").exists())

    def test_delete_refuses_builtin_skill(self):
        mgr = SkillManager()
        self.assertFalse(mgr.delete("general"))
        self.assertIsNotNone(mgr.get("general"))

    def test_delete_unknown_skill_returns_false(self):
        mgr = SkillManager()
        self.assertFalse(mgr.delete("does-not-exist"))


class SkillEndpointTests(unittest.TestCase):
    def _make_client(self, user_id="alice"):
        import app as app_module
        db = MagicMock()
        db.get_user.return_value = {"id": user_id, "name": "Alice"}
        application = app_module.create_app()
        client = application.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
        return app_module, db, client

    def test_delete_user_skill_endpoint_removes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            skills_dir.mkdir()
            (skills_dir / "custom.md").write_text("---\nname: custom\ndescription: x\n---\nbody")
            app_module, db, client = self._make_client()
            with patch.object(app_module, "get_db", return_value=db), \
                 patch.object(app_module, "get_skill_manager", return_value=SkillManager(skills_dir)):
                resp = client.delete("/api/skills/custom")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.get_json(), {"ok": True})
                self.assertFalse((skills_dir / "custom.md").exists())

    def test_delete_builtin_skill_endpoint_returns_403(self):
        app_module, db, client = self._make_client()
        with patch.object(app_module, "get_db", return_value=db), \
             patch.object(app_module, "get_skill_manager", return_value=SkillManager()):
            resp = client.delete("/api/skills/general")
            self.assertEqual(resp.status_code, 403)

    def test_delete_unknown_skill_endpoint_returns_404(self):
        app_module, db, client = self._make_client()
        with patch.object(app_module, "get_db", return_value=db), \
             patch.object(app_module, "get_skill_manager", return_value=SkillManager()):
            resp = client.delete("/api/skills/does-not-exist")
            self.assertEqual(resp.status_code, 404)

    def test_delete_endpoint_requires_auth(self):
        import app as app_module
        with patch.object(app_module, "get_db", return_value=MagicMock()), \
             patch.object(app_module.config, "DEV_AUTO_LOGIN", ""), \
             patch.object(app_module.config, "DEV_MODE", False), \
             patch.object(app_module.config, "OPENAI_CLIENT_ID", "test-client"):
            application = app_module.create_app()
            client = application.test_client()
            resp = client.delete("/api/skills/custom")
            self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
