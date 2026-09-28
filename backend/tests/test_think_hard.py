"""Think-hard model: a single turn the user asks APEX to think hard on runs on
THINK_HARD_MODEL instead of the main model, when THINK_HARD_MODEL_ENABLED is on.

Also covers the settings/config plumbing. DB-touching tests reuse the isolated
database helper from test_soul so they can never reach live data.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import config
from app import resolve_model

from test_soul import isolated_db, signed_in  # same throwaway-DB guarantee


def resolve(**kwargs):
    base = dict(
        provider_name="openai",
        requested="",
        rt={},
        skill_model="",
        is_subscription=False,
    )
    base.update(kwargs)
    return resolve_model(**base)


class ThinkHardPrecedenceTests(unittest.TestCase):
    def test_normal_turn_never_switches_model(self):
        model, note = resolve(requested="gpt-4o")
        self.assertEqual(model, "gpt-4o")
        self.assertEqual(note, "")

    def test_think_hard_turn_wins_over_the_model_the_ui_sent(self):
        # The UI always sends the main model; switching away is the whole point.
        model, note = resolve(
            requested="gpt-4o",
            hard_requested=True,
            hard_enabled=True,
            hard_model="gpt-5",
        )
        self.assertEqual(model, "gpt-5")
        self.assertEqual(note, "")

    def test_think_hard_turn_wins_over_skill_and_runtime_model(self):
        model, _ = resolve(
            rt={"model": "saved-model"},
            skill_model="skill-model",
            hard_requested=True,
            hard_enabled=True,
            hard_model="gpt-5",
        )
        self.assertEqual(model, "gpt-5")

    def test_disabled_switch_falls_back_and_explains(self):
        model, note = resolve(
            requested="gpt-4o",
            hard_requested=True,
            hard_enabled=False,
            hard_model="gpt-5",
        )
        self.assertEqual(model, "gpt-4o")
        self.assertIn("not set up yet", note)

    def test_missing_model_falls_back_and_explains(self):
        model, note = resolve(
            requested="gpt-4o",
            hard_requested=True,
            hard_enabled=True,
            hard_model="   ",
        )
        self.assertEqual(model, "gpt-4o")
        self.assertIn("no think-hard model is configured", note)

    def test_surrounding_whitespace_in_the_model_is_ignored(self):
        model, note = resolve(
            hard_requested=True, hard_enabled=True, hard_model="  gpt-5  "
        )
        self.assertEqual(model, "gpt-5")
        self.assertEqual(note, "")

    def test_local_provider_rejects_a_model_it_does_not_serve(self):
        # ollama/kimi would raise on an unknown model name, so the turn must fall
        # back rather than fail.
        model, note = resolve(
            provider_name="ollama",
            hard_requested=True,
            hard_enabled=True,
            hard_model="gpt-5",
            allowed_models={"llama3.1:8b"},
        )
        self.assertEqual(model, config.OLLAMA_MODEL)
        self.assertIn("not available on the ollama provider", note)

    def test_local_provider_accepts_a_model_it_serves(self):
        model, note = resolve(
            provider_name="ollama",
            hard_requested=True,
            hard_enabled=True,
            hard_model="llama3.1:8b",
            allowed_models={"llama3.1:8b"},
        )
        self.assertEqual(model, "llama3.1:8b")
        self.assertEqual(note, "")

    def test_hosted_providers_accept_any_configured_name(self):
        # openai/codex/torch take the name as-is, so no allow-list is consulted.
        model, note = resolve(
            provider_name="openai",
            requested="gpt-4o",
            hard_requested=True,
            hard_enabled=True,
            hard_model="o3-deep",
            allowed_models=None,
        )
        self.assertEqual(model, "o3-deep")
        self.assertEqual(note, "")

    def test_default_resolution_is_unchanged(self):
        self.assertEqual(resolve()[0], config.DEFAULT_MODEL)
        self.assertEqual(
            resolve(is_subscription=True)[0], config.CHATGPT_MODEL
        )
        self.assertEqual(resolve(provider_name="codex")[0], config.CODEX_MODEL)
        self.assertEqual(resolve(provider_name="torch")[0], config.TORCH_MODEL)
        self.assertEqual(
            resolve(provider_name="kimi", rt={"kimi_model": "k2"})[0], "k2"
        )


class ThinkHardSettingsTests(unittest.TestCase):
    """Round-trips through a real (temporary) database, not a mock."""

    def setUp(self):
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

    def _client(self, app_module, database):
        database.upsert_user(
            user_id="alice", name="Alice", email="", picture="", tokens={}, token_scopes="{}"
        )
        client = app_module.create_app().test_client()
        with client.session_transaction() as session:
            session["user_id"] = "alice"
        return client

    def test_settings_round_trip(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            response = client.post("/api/settings", json={
                "think_hard_model": "  gpt-5  ",
                "think_hard_model_enabled": True,
            })
            self.assertEqual(response.status_code, 200)
            settings = response.get_json()["settings"]
            self.assertEqual(settings["think_hard_model"], "gpt-5")
            self.assertIs(settings["think_hard_model_enabled"], True)
            self.assertEqual(database.get_setting("think_hard_model"), "gpt-5")

    def test_enabled_is_coerced_to_a_boolean_flag(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            for raw, expected in (("true", "1"), ("on", "1"), ("0", "0"), (False, "0")):
                client.post("/api/settings", json={"think_hard_model_enabled": raw})
                config_payload = client.get("/api/config").get_json()
                self.assertEqual(
                    config_payload["think_hard_model_enabled"],
                    expected == "1",
                    f"enabled={raw!r} must be a bool",
                )

    def test_config_falls_back_to_env_when_nothing_is_saved(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            with patch.object(config, "THINK_HARD_MODEL", "env-model"), \
                 patch.object(config, "THINK_HARD_MODEL_ENABLED", True):
                payload = client.get("/api/config").get_json()
        self.assertEqual(payload["think_hard_model"], "env-model")
        self.assertTrue(payload["think_hard_model_enabled"])

    def test_saved_value_overrides_env(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            client.post("/api/settings", json={
                "think_hard_model": "saved-model",
                "think_hard_model_enabled": True,
            })
            with patch.object(config, "THINK_HARD_MODEL", "env-model"), \
                 patch.object(config, "THINK_HARD_MODEL_ENABLED", False):
                payload = client.get("/api/config").get_json()
        self.assertEqual(payload["think_hard_model"], "saved-model")
        self.assertTrue(payload["think_hard_model_enabled"])

    def test_clearing_the_field_falls_back_to_the_env_default(self):
        # The Settings input sends null when emptied. That must clear the saved
        # override and hand control back to THINK_HARD_MODEL, exactly like the
        # main Model row falls back to DEFAULT_MODEL.
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            client.post("/api/settings", json={"think_hard_model": "saved-model"})
            client.post("/api/settings", json={"think_hard_model": None})
            self.assertEqual(database.get_setting("think_hard_model"), "")
            with patch.object(config, "THINK_HARD_MODEL", "env-model"):
                payload = client.get("/api/config").get_json()
        self.assertEqual(payload["think_hard_model"], "env-model")


if __name__ == "__main__":
    unittest.main()
