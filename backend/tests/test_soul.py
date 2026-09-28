"""SOUL.md: the operator-authored persona appended to every system prompt."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db as db_module
from config import config
from db import Database
from skills.manager import SkillManager
from soul import SOUL_HEADER, normalize_soul, soul_prompt_block


@contextmanager
def isolated_db():
    """Force every `get_db()` inside a request onto a throwaway file.

    A test must never be able to write to the live DATA_DIR database: an
    earlier version of this file posted settings to production data because a
    mock did not stay active for the request. This redirects the module-level
    singleton, so even code paths a test forgot to patch (the route closures in
    `create_app`) cannot reach the real database.
    """
    original = db_module._db
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(config, "DB_PATH", Path(tmp) / "test.db"):
            db_module._db = None
            try:
                yield db_module.get_db()
            finally:
                db_module._db = original


@contextmanager
def signed_in(app_module):
    """Sign-in and provider stubs that every settings route needs."""
    patchers = [
        patch.object(app_module, name, value)
        for name, value in (
            ("bearer_for_api", None),
            ("is_subscription_access", False),
            ("get_skill_manager", lambda: SkillManager()),
        )
    ]
    for patcher in patchers:
        patcher.start()
    try:
        yield
    finally:
        for patcher in patchers:
            patcher.stop()


class SoulBlockTests(unittest.TestCase):
    def test_empty_values_produce_no_block(self):
        for value in (None, "", "   \n\t ", 0, [], {}, False):
            self.assertEqual(normalize_soul(value), "")
            self.assertEqual(soul_prompt_block(value), "")

    def test_block_is_header_preamble_and_text(self):
        block = soul_prompt_block("  Be calm and exact.  ")
        self.assertTrue(block.startswith(SOUL_HEADER))
        self.assertIn("Be calm and exact.", block)
        # Instructions, not data: it must read as something to follow.
        self.assertIn("Follow them", block)

    def test_newlines_are_normalized_before_being_embedded(self):
        self.assertEqual(normalize_soul("a\r\nb\rc"), "a\nb\nc")

    def test_non_string_input_is_rejected_not_coerced(self):
        # Settings arrive as JSON; anything but a string must not reach the prompt.
        self.assertEqual(normalize_soul(1234), "")
        self.assertEqual(normalize_soul(["Be calm."]), "")

    def test_text_is_truncated_to_the_configured_limit(self):
        with patch.object(config, "SOUL_MAX_CHARS", 10):
            self.assertEqual(normalize_soul("x" * 40), "x" * 10)
            # Truncation must not leave dangling whitespace in the prompt.
            self.assertEqual(normalize_soul("abcde     fghijkl"), "abcde")
            self.assertEqual(normalize_soul("x" * 5), "x" * 5)

    def test_limit_of_zero_means_unbounded(self):
        with patch.object(config, "SOUL_MAX_CHARS", 0):
            self.assertEqual(len(normalize_soul("y" * 5000)), 5000)


class SoulSettingsRouteTests(unittest.TestCase):
    """Round-trips through a real (temporary) database, not a mock."""

    def setUp(self):
        # Keep the routes off the network and out of the memory store, which
        # caches its own database handle and would outlive the temp dir.
        self._memory = patch.object(config, "MEMORY_ENABLED", False)
        self._memory.start()
        self.addCleanup(self._memory.stop)
        self._engines = patch.object(
            type(config), "agent_engines_available", property(lambda self: ["responses"])
        )
        self._engines.start()
        self.addCleanup(self._engines.stop)
        for name in ("list_ollama_models", "list_kimi_models"):
            patcher = patch(f"models.providers.{name}", return_value=[])
            patcher.start()
            self.addCleanup(patcher.stop)

    def _client(self, app_module, database, signed_in_user=True):
        database.upsert_user(
            user_id="alice", name="Alice", email="", picture="", tokens={}, token_scopes="{}"
        )
        client = app_module.create_app().test_client()
        if signed_in_user:
            with client.session_transaction() as session:
                session["user_id"] = "alice"
        return client

    def test_soul_round_trips_through_the_database(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            response = client.post("/api/settings", json={"soul": "  Be calm and exact.  "})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["settings"]["soul"], "Be calm and exact.")
            # It really landed in the database, not just in the response.
            self.assertEqual(database.get_setting("soul"), "Be calm and exact.")
            # And it survives a fresh read, as a page reload would see it.
            reread = Database(config.DB_PATH)
            self.assertEqual(reread.get_setting("soul"), "Be calm and exact.")

    def test_config_endpoint_exposes_soul_and_its_limit(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            client.post("/api/settings", json={"soul": "Be calm and exact."})
            payload = client.get("/api/config").get_json()
        self.assertEqual(payload["soul"], "Be calm and exact.")
        self.assertEqual(payload["soul_max_chars"], config.SOUL_MAX_CHARS)

    def test_overlong_soul_is_truncated_on_write(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module), \
             patch.object(config, "SOUL_MAX_CHARS", 8):
            client = self._client(app_module, database)
            client.post("/api/settings", json={"soul": "abcdefghijkl"})
            self.assertEqual(database.get_setting("soul"), "abcdefgh")

    def test_clearing_soul_persists_an_empty_string(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            client.post("/api/settings", json={"soul": "Be calm."})
            client.post("/api/settings", json={"soul": ""})
            self.assertEqual(database.get_setting("soul"), "")
            self.assertNotIn(SOUL_HEADER, client.get("/api/config").get_json()["soul"])

    def test_unknown_keys_are_still_ignored(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            client.post("/api/settings", json={"not_a_setting": "x"})
            self.assertIsNone(database.get_setting("not_a_setting"))

    def test_settings_require_a_signed_in_user(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module), \
             patch.object(type(config), "dev_auto_login", property(lambda self: False)):
            client = self._client(app_module, database, signed_in_user=False)
            response = client.post("/api/settings", json={"soul": "Be calm."})
            self.assertEqual(response.status_code, 401)
            self.assertIsNone(database.get_setting("soul"))


class SoulChatPromptTests(unittest.TestCase):
    """The persona must reach the engine's system prompt, and only when set."""

    def _run(self, settings):
        import app as app_module

        db = MagicMock()
        db.get_user.return_value = {"id": "alice", "name": "Alice"}
        db.all_settings.return_value = {"provider": "openai", "model": "test", **settings}
        db.get_conversation.return_value = {"id": "conversation", "user_id": "alice", "skill": "general"}
        db.list_messages.return_value = []
        engine = MagicMock()
        engine.stream.return_value = iter([{"type": "done", "usage": {}}])
        # isolated_db is belt and braces here: the mocked db already keeps the
        # request away from the real one, but a leaked code path must still not
        # be able to write to it.
        with isolated_db(), \
             patch.object(app_module, "get_db", return_value=db), \
             patch.object(app_module, "get_skill_manager", return_value=SkillManager()), \
             patch.object(app_module, "bearer_for_api", return_value=None), \
             patch.object(app_module, "is_subscription_access", return_value=False), \
             patch.object(app_module, "ProviderManager"), \
             patch.object(app_module, "make_registry"), \
             patch.object(app_module, "build_engine", return_value=engine) as build, \
             patch.object(app_module.config, "MEMORY_ENABLED", False), \
             patch.object(app_module.config, "MEMORY_SUMMARIZE", False):
            client = app_module.create_app().test_client()
            with client.session_transaction() as session:
                session["user_id"] = "alice"
            response = client.post("/api/chat", json={
                "message": "hello",
                "conversation_id": "conversation",
                "store_messages": False,
            })
            self.assertEqual(response.status_code, 200)
            response.get_data(as_text=True)
        return build.call_args.args[1].system_prompt, db

    def test_soul_is_appended_to_the_system_prompt(self):
        prompt, db = self._run({"soul": "You are dry and never pad an answer."})
        self.assertIn(SOUL_HEADER, prompt)
        self.assertIn("You are dry and never pad an answer.", prompt)
        # The persona must come after the skill so it stays the last instruction.
        self.assertGreater(prompt.index(SOUL_HEADER), prompt.index("## Skill:"))
        # Prompt-only: the persona is never written to stored messages.
        for call in db.add_message.call_args_list:
            self.assertNotIn("SOUL.md", str(call.args))
            self.assertNotIn("never pad an answer", str(call.args))

    def test_no_soul_setting_means_no_block(self):
        for settings in ({}, {"soul": ""}, {"soul": "   "}):
            self.assertNotIn(SOUL_HEADER, self._run(settings)[0])


if __name__ == "__main__":
    unittest.main()
