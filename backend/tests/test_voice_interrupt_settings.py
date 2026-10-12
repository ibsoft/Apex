"""Voice interruption settings: the checkbox + sensitivity slider that let the
operator stop APEX mid-reply by talking (recoverable with "continue").

Client-side behaviour lives in the browser; the backend only persists and
validates the two keys. DB-touching tests reuse the isolated database helper
from test_soul so they can never reach live data.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_soul import isolated_db, signed_in  # same throwaway-DB guarantee


class VoiceInterruptSettingsTests(unittest.TestCase):
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
                "voice_interrupt_enabled": True,
                "voice_interrupt_sensitivity": 9,
            })
            self.assertEqual(response.status_code, 200)
            settings = response.get_json()["settings"]
            self.assertIs(settings["voice_interrupt_enabled"], True)
            self.assertEqual(settings["voice_interrupt_sensitivity"], 9)
            self.assertIs(database.get_setting("voice_interrupt_enabled"), True)

    def test_enabled_is_coerced_to_a_boolean_flag(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            for raw, expected in (("true", True), ("on", True), ("0", False), (False, False)):
                client.post("/api/settings", json={"voice_interrupt_enabled": raw})
                settings = client.get("/api/settings").get_json()["settings"]
                self.assertIs(
                    settings["voice_interrupt_enabled"],
                    expected,
                    f"enabled={raw!r} must be a bool",
                )

    def test_sensitivity_is_clamped_to_1_10(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            client.post("/api/settings", json={"voice_interrupt_sensitivity": 999})
            settings = client.get("/api/settings").get_json()["settings"]
            self.assertEqual(settings["voice_interrupt_sensitivity"], 10)
            client.post("/api/settings", json={"voice_interrupt_sensitivity": -3})
            settings = client.get("/api/settings").get_json()["settings"]
            self.assertEqual(settings["voice_interrupt_sensitivity"], 1)

    def test_unknown_keys_are_still_ignored(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            response = client.post("/api/settings", json={
                "voice_interrupt_made_up": True,
                "engine": "responses",
            })
            self.assertEqual(response.status_code, 200)
            settings = response.get_json()["settings"]
            self.assertNotIn("voice_interrupt_made_up", settings)


if __name__ == "__main__":
    unittest.main()