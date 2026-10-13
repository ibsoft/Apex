"""Telegram bot: the gate, pairing, settings, secrets and the routes.

Nothing here touches api.telegram.org: every network boundary (getMe, the
send* helpers, download_chat_file) is mocked or monkeypatched. What is proven is
the part a future edit can quietly break - who may use the bot, how a chat
becomes the paired operator, where the token does and does not live, and what
the settings tab is ever told.

Security posture asserted as code:
- the bot answers only a paired chat, only in private chats, and pairing
  redemptions are rate-limited so the six-character code cannot be brute-forced
  while it is alive;
- the token lives only in the settings DB - never in an env mirror - and
  "forget the bot" kills the stored row AND the import-time env fallback, so a
  cleared token cannot resurrect;
- ``public_settings`` never hands the token (or the paired chat id) to the
  browser.

Settings are read the way the tool reads them (settings table over env
defaults), and the poller that a settings POST would arm is never exercised: it
is a daemon that sleeps while disabled, so the send/receipt boundary is covered
through ``_handle_message`` directly.
"""
from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from test_auth_routes import AuthRouteTestCase

from config import config
from tools.telegram_tools import (
    _REDEEM_LIMIT,
    _note_redeem_failure,
    _redeem_permitted,
    effective_settings,
    enabled,
    generate_pairing_code,
    paired_link,
    pending_pairing_code,
    redact,
    redeem_pairing_code,
    revoke_token,
    send_message,
    to_opus_ogg,
    unlink_pair,
    validate_token,
)


def env_defaults(**over):
    base = dict(
        TELEGRAM_ENABLED=False,
        TELEGRAM_BOT_TOKEN="",
        TELEGRAM_WHISPER_PYTHON="",
        TELEGRAM_WHISPER_MODEL="",
        TELEGRAM_PAIRING_TTL_SECONDS=600,
        TELEGRAM_VOICE_REPLIES=True,
        TELEGRAM_TTS_ENGINE="espeak",
        TELEGRAM_TTS_VOICE="",
        TELEGRAM_POLL_TIMEOUT_SECONDS=25,
        AUTO_ROUTE_FROM_GENERAL=False,
        TASKS_HISTORY_WINDOW=20,
        TASKS_TIMEOUT_SECONDS=600,
        MEMORY_ENABLED=False,
        RESPONSE_LANGUAGE="en",
    )
    base.update(over)
    return SimpleNamespace(**base)


class TelegramTestCase(AuthRouteTestCase):
    """Isolated settings plus blanked env defaults, so no test reads the host's
    real token or enables a poller against production pairing state."""

    def setUp(self):
        super().setUp()
        for attr, value in (
            ("TELEGRAM_ENABLED", False),
            ("TELEGRAM_BOT_TOKEN", ""),
            ("TELEGRAM_WHISPER_PYTHON", ""),
            ("TELEGRAM_WHISPER_MODEL", ""),
            ("TELEGRAM_PAIRING_TTL_SECONDS", 600),
            ("TELEGRAM_VOICE_REPLIES", True),
            ("TELEGRAM_TTS_ENGINE", "espeak"),
            ("TELEGRAM_TTS_VOICE", ""),
            ("TELEGRAM_POLL_TIMEOUT_SECONDS", 25),
        ):
            p = patch.object(config, attr, value)
            p.start()
            self.addCleanup(p.stop)
        # Pairing codes, redemption counters and the getMe cache are module-
        # global; a leftover from one test must not decide the outcome of the
        # next.
        from tools.telegram_tools import _REDEEM_ATTEMPTS, cancel_all_pairing_codes

        cancel_all_pairing_codes()
        _REDEEM_ATTEMPTS.clear()
        from tools import telegram as telegram_routes

        telegram_routes._ME_CACHE.update({"token": "", "at": 0.0, "result": None})

    def cfg(self, **rows):
        for key, value in rows.items():
            self.db.set_setting(key, value)
        return effective_settings(config)


GOOD_TOKENS = [
    "123456789:ABCDefghIJKLMnopqrstuvwxyz1234",
    "1234567890:AAaaBBccDDeeFFggHHiiJJkkLLmmnn_op",
]
BAD_TOKENS = [
    "", "1:x", "not-a-token", "abc:xyz", "123:nospaces", "123456789:shortsecret",
    "123456789:oops has a space in it xxxxxxxxxxxxxxx",
]


class TelegramSettingsMergeTests(TelegramTestCase):
    def test_database_overrides_env_and_normalises_engine(self):
        cfg = self.cfg(telegram_enabled=True, telegram_tts_engine="EDGE", telegram_tts_voice="el-GR-AthinaNeural")
        self.assertTrue(cfg["telegram_enabled"])
        self.assertEqual(cfg["telegram_tts_engine"], "edge")
        self.assertEqual(cfg["telegram_tts_voice"], "el-GR-AthinaNeural")

    def test_unknown_engine_falls_back_to_espeak(self):
        cfg = self.cfg(telegram_tts_engine="siri")
        self.assertEqual(cfg["telegram_tts_engine"], "espeak")

    def test_voice_replies_defaults_from_env(self):
        cfg = self.cfg()
        self.assertTrue(cfg["telegram_voice_replies"])

    def test_enabled_reflects_the_settings_row(self):
        self.assertFalse(enabled())
        self.db.set_setting("telegram_enabled", True)
        self.assertTrue(enabled())
        self.db.set_setting("telegram_enabled", False)
        self.assertFalse(enabled())


class TelegramSecretTests(TelegramTestCase):
    def test_redact_removes_the_token(self):
        cfg = self.cfg(telegram_bot_token="123456789:supersecret-secret-secret")
        out = redact("conflict - another long poller '123456789:supersecret-secret-secret'", cfg)
        self.assertNotIn("supersecret", out)
        self.assertIn("***", out)

    def test_validate_token_shape(self):
        for good in GOOD_TOKENS:
            self.assertTrue(validate_token(good), good)
        for bad in BAD_TOKENS:
            self.assertFalse(validate_token(bad), bad)

    def test_token_is_never_mirrored_into_the_env_file(self):
        """The token has exactly one home: the settings DB. Nothing is ever
        written for anyone to read, and the env default stays untouched."""
        self.cfg(telegram_bot_token=GOOD_TOKENS[0])
        from tools.telegram_tools import SETTING_KEYS

        self.assertNotIn("telegram_bot_token", SETTING_KEYS)
        rows = self.db.all_settings()
        self.assertIn("telegram_bot_token", rows)
        self.assertEqual(rows["telegram_bot_token"], GOOD_TOKENS[0])
        # And it did not leak into the repo's env file.
        env_path = Path(__file__).resolve().parents[1] / ".env"
        if env_path.exists():
            self.assertNotIn("TELEGRAM_BOT_TOKEN=", env_path.read_text())

    def test_revoke_clears_row_pairing_and_env_fallback(self):
        code = generate_pairing_code(config, "alice")
        self.assertEqual(redeem_pairing_code(code, "555", "alice_user"), "alice")
        self.cfg(telegram_bot_token=GOOD_TOKENS[0])
        # Simulate an import-time env default: the fallback revoke must kill it.
        os.environ["TELEGRAM_BOT_TOKEN"] = GOOD_TOKENS[1]
        setattr(config, "TELEGRAM_BOT_TOKEN", GOOD_TOKENS[1])

        revoke_token(config)

        self.assertNotIn("telegram_bot_token", self.db.all_settings())
        self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)
        self.assertEqual(getattr(config, "TELEGRAM_BOT_TOKEN", None), "")
        self.assertEqual(paired_link(), {})
        self.assertIsNone(pending_pairing_code("alice"))
        # Even the merged settings now see nothing to fall back to.
        self.assertEqual(effective_settings(config)["telegram_bot_token"], "")


class TelegramPairingTests(TelegramTestCase):
    def test_generated_code_is_single_use(self):
        code = generate_pairing_code(config, "alice")
        pending = pending_pairing_code("alice")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["code"], code)

        self.assertEqual(redeem_pairing_code(code, "555", "alice_user"), "alice")
        link = paired_link()
        self.assertEqual(link["chat_id"], "555")
        self.assertEqual(link["user_id"], "alice")
        self.assertEqual(link["username"], "alice_user")
        self.assertIsNone(redeem_pairing_code(code, "666", "other"))

    def test_wrong_or_expired_code_is_rejected(self):
        code = generate_pairing_code(config, "alice")
        self.assertIsNone(redeem_pairing_code(code + "X", "555", ""))
        p = patch.object(config, "TELEGRAM_PAIRING_TTL_SECONDS", -1)
        p.start()
        self.addCleanup(p.stop)
        stale = generate_pairing_code(config, "alice")
        self.assertIsNone(redeem_pairing_code(stale, "555", ""))

    def test_codes_are_scoped_to_their_user(self):
        generate_pairing_code(config, "alice")
        self.assertIsNone(pending_pairing_code("bob"))
        self.assertIsNotNone(pending_pairing_code("alice"))

    def test_unlink_clears_the_pair(self):
        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "u")
        unlink_pair()
        self.assertEqual(paired_link(), {})

    def test_redemption_is_rate_limited_per_chat(self):
        chat = "777"
        self.assertTrue(_redeem_permitted(chat))
        for _ in range(_REDEEM_LIMIT):
            self.assertTrue(_redeem_permitted(chat))
            _note_redeem_failure(chat)
        # At the limit: no more attempts from this chat until the window passes.
        self.assertFalse(_redeem_permitted(chat))
        # A different chat is untouched.
        self.assertTrue(_redeem_permitted("888"))


class TelegramMessageDispatchTests(TelegramTestCase):
    """The only place messages get accepted: pairing first, then the paired chat."""

    def msg(self, chat_id="555", first="An Operator", username="alice_user", text="hello", chat_type="private"):
        return {
            "chat": {"id": int(chat_id), "type": chat_type},
            "from": {"id": 707, "username": username, "first_name": first},
            "text": text,
        }

    def given_bot(self):
        return self.cfg(telegram_enabled=True, telegram_bot_token=GOOD_TOKENS[0])

    def test_unpaired_chat_is_ignored_when_no_code_or_start(self):
        cfg = self.given_bot()
        with patch("tools.telegram_tools.send_message") as send, \
             patch("tools.telegram_tools.run_message_turn") as run:
            from tools.telegram_tools import _handle_message
            _handle_message(config, cfg, self.msg(text="hi there"))
        send.assert_not_called()
        run.assert_not_called()

    def test_start_hints_at_pairing(self):
        cfg = self.given_bot()
        with patch("tools.telegram_tools.send_message") as send:
            from tools.telegram_tools import _handle_message
            _handle_message(config, cfg, self.msg(text="/start"))
        send.assert_called_once()
        self.assertIn("pairing code", send.call_args.args[2])

    def test_a_code_pairs_that_chat(self):
        cfg = self.given_bot()
        code = generate_pairing_code(config, "alice")
        with patch("tools.telegram_tools.send_message") as send:
            from tools.telegram_tools import _handle_message
            _handle_message(config, cfg, self.msg(text=code))
        send.assert_called_once()
        self.assertIn("Paired", send.call_args.args[2])
        self.assertEqual(paired_link()["chat_id"], "555")

    def test_groups_are_never_answered_nor_pairing_hinted(self):
        cfg = self.given_bot()
        with patch("tools.telegram_tools.send_message") as send:
            from tools.telegram_tools import _handle_message
            _handle_message(config, cfg, self.msg(text="/start", chat_type="group"))
            _handle_message(config, cfg, self.msg(text="hi", chat_type="supergroup"))
        send.assert_not_called()

    def test_wrong_codes_are_silent_and_eventually_blocked(self):
        cfg = self.given_bot()
        from tools.telegram_tools import _handle_message
        for i in range(_REDEEM_LIMIT + 3):
            with patch("tools.telegram_tools.send_message") as send:
                _handle_message(config, cfg, self.msg(text=f"XXXX{i}", chat_id="999"))
            send.assert_not_called()
        # The last too many attempts were never even tried against the codes.
        self.assertFalse(_redeem_permitted("999"))

    def test_a_paired_chat_runs_a_turn_and_gets_the_reply(self):
        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "alice_user")
        cfg = self.given_bot()
        with patch("tools.telegram_tools.send_voice_note", return_value=({"ok": True}, "")) as voice, \
             patch("tools.telegram_tools.send_message") as text, \
             patch("tools.telegram_tools.run_message_turn", return_value=("All good.", "")) as run, \
             patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")) as api:
            from tools.telegram_tools import _handle_message
            _handle_message(config, cfg, self.msg(text="check the disk"))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[3], "check the disk")
        self.assertFalse(run.call_args.kwargs.get("spoken_reply"))
        # A text message is answered with text, fast; no synthesis is attempted.
        voice.assert_not_called()
        text.assert_called_once()
        self.assertEqual(text.call_args.args[2], "All good.")
        # The UI was told to show "typing…" before the turn, so asking "how are
        # you" no longer looks like the bot is silently re-running the last job.
        actions = [c for c in api.call_args_list if c.args and c.args[0] == "sendChatAction"]
        self.assertEqual([a.kwargs["json"]["action"] for a in actions], ["typing"])

    def test_a_voice_note_is_answered_with_a_voice_note(self):
        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "alice_user")
        cfg = self.given_bot()
        note = {"chat": {"id": 555, "type": "private"},
                "voice": {"file_id": "BgAA-file"}, "text": None}
        with patch("tools.telegram_tools.download_chat_file",
                   return_value=Path(self._tmp.name) / "note.ogg") as dl, \
             patch("tools.telegram_tools.transcribe", return_value="how full is the disk") as tr, \
             patch("tools.telegram_tools.ensure_wav", side_effect=lambda p, *a: p) as wav, \
             patch("tools.telegram_tools.send_voice_note", return_value=({"ok": True}, "")) as voice, \
             patch("tools.telegram_tools.run_message_turn", return_value=("71% used.", "")) as run, \
             patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")):
            from tools.telegram_tools import _handle_message
            _handle_message(config, cfg, note)
        dl.assert_called_once()
        tr.assert_called_once()
        wav.assert_called_once()
        run.assert_called_once()
        self.assertTrue(run.call_args.kwargs.get("spoken_reply"))
        voice.assert_called_once()

    def test_voice_note_with_voice_replies_off_gets_text_only(self):
        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "alice_user")
        cfg = self.cfg(telegram_enabled=True, telegram_bot_token=GOOD_TOKENS[0],
                       telegram_voice_replies=False)
        note = {"chat": {"id": 555, "type": "private"},
                "voice": {"file_id": "BgAA-file"}, "text": None}
        with patch("tools.telegram_tools.download_chat_file",
                   return_value=Path(self._tmp.name) / "note.ogg"), \
             patch("tools.telegram_tools.transcribe", return_value="how full is the disk"), \
             patch("tools.telegram_tools.send_voice_note", return_value=({"ok": True}, "")) as voice, \
             patch("tools.telegram_tools.send_message") as text, \
             patch("tools.telegram_tools.run_message_turn", return_value=("71% used.", "")) as run, \
             patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")):
            from tools.telegram_tools import _handle_message
            _handle_message(config, cfg, note)
        run.assert_called_once()
        self.assertFalse(run.call_args.kwargs.get("spoken_reply"))
        voice.assert_not_called()
        text.assert_called_once()
        self.assertEqual(text.call_args.args[2], "71% used.")

    def test_another_chat_is_still_ignored_after_pairing(self):
        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "alice_user")
        cfg = self.given_bot()
        with patch("tools.telegram_tools.send_message") as send, \
             patch("tools.telegram_tools.run_message_turn") as run:
            from tools.telegram_tools import _handle_message
            _handle_message(config, cfg, self.msg(chat_id="666", text="back off"))
        send.assert_not_called()
        run.assert_not_called()

    def test_voice_media_is_downloaded_transcribed_and_cleaned(self):
        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "alice_user")
        cfg = self.given_bot()
        note = {"chat": {"id": 555, "type": "private"},
                "voice": {"file_id": "BgAA-file"}, "text": None}
        with patch("tools.telegram_tools.download_chat_file", return_value=None) as dl, \
             patch("tools.telegram_tools.transcribe") as tr, \
             patch("tools.telegram_tools.run_message_turn", return_value=("ok", "")), \
             patch("tools.telegram_tools.send_message") as send, \
             patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")), \
             patch("tools.telegram_tools.tempfile.gettempdir", return_value=self._tmp.name):
            from tools.telegram_tools import _handle_message
            _handle_message(config, cfg, note)
        dl.assert_called_once()
        tr.assert_not_called()
        send.assert_called_once()
        self.assertIn("could not read", send.call_args.args[2])


class TelegramTurnTests(TelegramTestCase):
    """run_message_turn must hand the *new* message to the engine, not just store it."""

    def test_the_just_sent_message_is_the_models_last_instruction(self):
        captured: dict = {}

        class _FakeProvider:
            pass

        def fake_build(name, ctx):
            captured["ctx"] = ctx
            return name

        with patch("models.providers.ProviderManager") as pm, \
             patch("agent.factory.build_engine", side_effect=fake_build), \
             patch("tools.tasks.collect_stream", return_value=("All good.", "")), \
             patch("skills.manager.route_skill", return_value="general"):
            pm.return_value.build.return_value = _FakeProvider()
            from tools.telegram_tools import run_message_turn

            run_message_turn(config, "alice", "555", "run df -h", "aliceu")
        # Second turn in the SAME thread: the previous exchange is in history,
        # and the new message must arrive as the last entry - not get swallowed
        # so the model "re-runs the last command" instead of answering.
        with patch("models.providers.ProviderManager") as pm, \
             patch("agent.factory.build_engine", side_effect=fake_build), \
             patch("tools.tasks.collect_stream", return_value=("I am fine.", "")), \
             patch("skills.manager.route_skill", return_value="general"):
            pm.return_value.build.return_value = _FakeProvider()
            from tools.telegram_tools import run_message_turn

            report, err = run_message_turn(config, "alice", "555", "how are you", "aliceu")
        self.assertEqual((report, err), ("I am fine.", ""))
        ctx = captured["ctx"]
        self.assertEqual(ctx.history[-1], {"role": "user", "content": "how are you"})
        self.assertEqual(ctx.history[-2]["role"], "assistant")
        self.assertIn("run df -h", [m["content"] for m in ctx.history])
        self.assertEqual(ctx.skill_name, "general")


class TelegramRouteTests(TelegramTestCase):
    def client(self):
        client = self._app().test_client()
        self._csrf = self._login(client)
        return client

    def _headers_token(self, client):
        return self._headers(self._csrf)

    def test_status_payload_carries_no_secret(self):
        self.cfg(telegram_bot_token=GOOD_TOKENS[0])
        client = self.client()
        with patch("tools.telegram.get_me", return_value={"id": 42, "username": "apex_bot", "first_name": "APEX"}):
            resp = client.get("/api/telegram/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["token_set"], True)
        self.assertEqual(body["bot"]["username"], "apex_bot")
        self.assertEqual(body["paired"], False)
        raw = resp.get_data(as_text=True)
        self.assertNotIn(GOOD_TOKENS[0], raw)
        self.assertNotIn("telegram_bot_token", raw)
        self.assertNotIn("supersecret", raw)

    def test_paired_status_via_route(self):
        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "alice_user")
        client = self.client()
        with patch("tools.telegram.get_me", return_value={"id": 42, "username": "apex_bot"}):
            resp = client.get("/api/telegram/status")
        body = resp.get_json()
        self.assertTrue(body["paired"])
        self.assertEqual(body["paired_username"], "alice_user")

    def test_set_token_validates_against_getme(self):
        client = self.client()
        with patch("tools.telegram.get_me", return_value={"id": 42, "username": "apex_bot"}):
            resp = client.post("/api/telegram/token", headers=self._headers_token(client), json={"token": GOOD_TOKENS[0]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual(body["bot_username"], "apex_bot")
        self.assertEqual(effective_settings(config)["telegram_bot_token"], GOOD_TOKENS[0])
        self.assertNotIn("env_file", body)

    def test_set_token_rejects_a_garbage_token(self):
        client = self.client()
        resp = client.post("/api/telegram/token", headers=self._headers_token(client), json={"token": "steve"})
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self.db.get_setting("telegram_bot_token"))

    def test_set_token_rejects_a_token_getme_does_not_know(self):
        client = self.client()
        with patch("tools.telegram.get_me", return_value=None):
            resp = client.post("/api/telegram/token", headers=self._headers_token(client), json={"token": GOOD_TOKENS[0]})
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self.db.get_setting("telegram_bot_token"))

    def test_pair_endpoint_returns_a_code(self):
        self.cfg(telegram_bot_token=GOOD_TOKENS[0])
        client = self.client()
        with patch("tools.telegram.get_me", return_value={"id": 42, "username": "apex_bot"}):
            resp = client.post("/api/telegram/pair", headers=self._headers_token(client))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual(len(body["code"]), 6)
        self.assertIsNotNone(pending_pairing_code("alice"))

    def test_pair_requires_a_token_first(self):
        client = self.client()
        resp = client.post("/api/telegram/pair", headers=self._headers_token(client))
        self.assertEqual(resp.status_code, 400)

    def test_clear_revokes_everything(self):
        self.cfg(telegram_bot_token=GOOD_TOKENS[0])
        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "alice_user")
        os.environ["TELEGRAM_BOT_TOKEN"] = GOOD_TOKENS[1]
        setattr(config, "TELEGRAM_BOT_TOKEN", GOOD_TOKENS[1])
        client = self.client()
        resp = client.post("/api/telegram/clear", headers=self._headers_token(client))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertNotIn("telegram_bot_token", self.db.all_settings())
        self.assertEqual(paired_link(), {})
        self.assertEqual(effective_settings(config)["telegram_bot_token"], "")

    def test_unpair_returns_200(self):
        client = self.client()
        resp = client.post("/api/telegram/unpair", headers=self._headers_token(client))
        self.assertEqual(resp.status_code, 200)

    def test_public_settings_masks_token_and_drops_managed_keys(self):
        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "alice_user")
        self.cfg(telegram_bot_token=GOOD_TOKENS[0], telegram_enabled=True)
        client = self.client()
        resp = client.get("/api/settings")
        body = resp.get_json()["settings"]
        self.assertEqual(body["telegram_bot_token_set"], True)
        self.assertEqual(body["telegram_enabled"], True)
        self.assertNotIn("telegram_bot_token", body)
        self.assertNotIn("telegram_paired_chat_id", body)

    def test_settings_post_cannot_write_the_token(self):
        """The token's only door is /api/telegram/token; a settings POST with a
        token-shaped key must be ignored, not stored."""
        client = self.client()
        token = self._headers_token(client)
        resp = client.post("/api/settings", headers=token, json={"telegram_bot_token": GOOD_TOKENS[0]})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.db.get_setting("telegram_bot_token"))

    def _headers_token(self, client):
        return self._headers(self._csrf)


class TelegramVoiceNoteTestCase(TelegramTestCase):
    def test_to_opus_ogg_returns_none_without_ffmpeg(self):
        import tools.telegram_tools as tg
        src = Path(self._tmp.name) / "in.wav"
        with patch.object(tg.shutil, "which", side_effect=lambda *a, **k: None):
            self.assertIsNone(to_opus_ogg(src, Path(self._tmp.name) / "out.ogg"))

    def test_send_message_clips_too_long_text(self):
        with patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")) as req:
            send_message("tok", "555", "x" * 5000)
        payload = req.call_args.kwargs["json"]
        self.assertLessEqual(len(payload["text"]), 4096)


class TelegramSendFileTests(TelegramTestCase):
    """telegram_send_file: only owned, sendable files reach the paired chat."""

    def test_path_allowed_rejects_system_env_and_secret_files(self):
        from tools.telegram_tools import _send_path_allowed

        allowed = Path(self._tmp.name) / "shot.png"
        allowed.write_bytes(b"png")
        self.assertEqual(_send_path_allowed(allowed, config), "")
        self.assertIn("system paths", _send_path_allowed(Path("/etc/passwd"), config))
        self.assertIn("system paths", _send_path_allowed(Path("/var/log/syslog"), config))
        env = Path(self._tmp.name) / ".env"
        env.write_bytes(b"SECRET=1")
        self.assertIn("credential or settings", _send_path_allowed(env, config))
        db = Path(self._tmp.name) / "settings.db"
        db.write_bytes(b"db")
        self.assertIn("credential or settings", _send_path_allowed(db, config))
        shadow = Path(self._tmp.name) / "shadow"
        shadow.write_bytes(b"x")
        self.assertIn("credential or settings", _send_path_allowed(shadow, config))
        self.assertIn("does not exist", _send_path_allowed(Path(self._tmp.name) / "missing", config))
        self.assertIn("not a readable file", _send_path_allowed(Path("/dev/null"), config))

    def test_send_file_sends_an_image_as_photo_and_others_as_document(self):
        from tools.telegram_tools import send_file

        img = Path(self._tmp.name) / "shot.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        with patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")) as req:
            send_file("tok", "555", img, "a picture", config=config)
        call = req.call_args
        self.assertEqual(call.args[0], "sendPhoto")
        uploaded = call.kwargs["files"]["photo"]
        self.assertEqual(uploaded[0], "shot.png")
        self.assertEqual(call.kwargs["data"]["caption"], "a picture")

        txt = Path(self._tmp.name) / "report.txt"
        txt.write_text("hello")
        with patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")) as req:
            send_file("tok", "555", txt, config=config)
        self.assertEqual(req.call_args.args[0], "sendDocument")

    def test_send_file_resolves_signed_apex_links_to_the_real_file(self):
        from tools.telegram_tools import _is_apex_url, _normalize_signed_url, send_file

        self.assertTrue(_is_apex_url("/api/visio/frame/abc"))
        self.assertTrue(_is_apex_url("http://localhost:3000/be/api/editor/download/xyz?x=1"))
        self.assertTrue(_is_apex_url("http://127.0.0.1:5001/api/files/download/qq"))
        self.assertFalse(_is_apex_url("/tmp/report.txt"))
        self.assertFalse(_is_apex_url(""))
        self.assertEqual(
            _normalize_signed_url("http://localhost:3000/be/api/files/download/qq"),
            "/api/files/download/qq",
        )

        img = Path(self._tmp.name) / "frame.jpg"
        img.write_bytes(b"jpeg")
        fake = patch("tools.preview_tools._resolve_source", return_value=(img, "frame.jpg"))
        fake.start()
        self.addCleanup(fake.stop)
        with patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")) as req:
            result, err = send_file(
                "tok", "555", "/api/visio/frame/tok", "here", user_id="user-alice", config=config
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(err, "")
        self.assertEqual(req.call_args.args[0], "sendPhoto")
        self.assertEqual(req.call_args.kwargs["files"]["photo"][0], "frame.jpg")
        # a link that fails to resolve is a refused send, never a raise
        fake.return_value = (None, "Snapshot is unavailable.")

        from tools.preview_tools import _PreviewError

        bad = patch(
            "tools.preview_tools._resolve_source",
            side_effect=_PreviewError("Snapshot is unavailable.", 404),
        )
        bad.start()
        self.addCleanup(bad.stop)
        with patch("tools.telegram_tools.bot_request") as req:
            result, err = send_file("tok", "555", "/api/visio/frame/tok", config=config)
        self.assertIsNone(result)
        self.assertIn("Snapshot is unavailable", err)
        req.assert_not_called()

    def test_send_file_clips_caption_and_refuses_bad_paths(self):
        from tools.telegram_tools import send_file

        img = Path(self._tmp.name) / "shot.png"
        img.write_bytes(b"png" * 32)
        with patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")) as req:
            send_file("tok", "555", img, "c" * 5000, config=config)
        self.assertLessEqual(len(req.call_args.kwargs["data"]["caption"]), 1024)

        with patch("tools.telegram_tools.bot_request") as req:
            result, err = send_file("tok", "555", Path("/etc/passwd"), "", config=config)
        self.assertIsNone(result)
        self.assertIn("system paths", err)
        req.assert_not_called()

    def test_telegram_send_file_tool_sends_via_paired_chat(self):
        from tools.telegram_tools import telegram_send_file

        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "aliceu")
        self.cfg(telegram_enabled=True, telegram_bot_token=GOOD_TOKENS[0])
        f = Path(self._tmp.name) / "doc.pdf"
        f.write_bytes(b"%PDF-1.4")
        from tools.base import ToolContext

        ctx = ToolContext(user_id="user-alice", conversation_id="c1")
        with patch("tools.telegram_tools.send_file", return_value=({"ok": True}, "")) as send:
            reply = telegram_send_file(config, ctx, str(f), "here it is")
        self.assertIn("Sent doc.pdf", reply)
        self.assertEqual(send.call_args.args[1], "555")

        with patch("tools.telegram_tools.send_file", return_value=(None, "not allowed")) as send:
            reply = telegram_send_file(config, ctx, "/etc/passwd")
        self.assertIn("Could not send", reply)
        send.assert_called_once()


class TelegramAutoDeliveryTests(TelegramTestCase):
    """Files reach the paired chat by construction: a visio_frame event and any
    /api/... link the model types into its reply are delivered automatically,
    never left as a link, and never sent twice in one turn."""

    def _frame(self, content=b"jpeg"):
        frame = Path(self._tmp.name) / "frame.jpg"
        if not frame.exists():
            frame.write_bytes(content)
        return frame

    def _resolve_patch(self, frame=None):
        p = patch(
            "tools.preview_tools._resolve_source",
            return_value=(frame or self._frame(), "frame.jpg"),
        )
        p.start()
        self.addCleanup(p.stop)

    def test_snapshot_emit_delivers_the_frame_to_the_chat(self):
        from tools.telegram_tools import _reset_delivered, run_message_turn

        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "aliceu")
        self.cfg(telegram_enabled=True, telegram_bot_token=GOOD_TOKENS[0])
        self._resolve_patch()
        captured: dict = {}

        class _FakeProvider:
            pass

        def fake_build(name, ctx):
            captured["ctx"] = ctx
            return ctx

        def fake_collect(engine, timeout):
            tctx = engine.make_tool_context()
            tctx.emit({"type": "visio_frame", "url": "http://192.168.1.10:5001/api/visio/frame/tok",
                       "title": "Camera snapshot"})
            return ("I see a desk and a monitor.", "")
            # The engine must have been given our emit hook.

        _reset_delivered()
        with patch("models.providers.ProviderManager") as pm, \
             patch("agent.factory.build_engine", side_effect=fake_build), \
             patch("tools.tasks.collect_stream", side_effect=fake_collect), \
             patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")) as req, \
             patch("skills.manager.route_skill", return_value="general"):
            pm.return_value.build.return_value = _FakeProvider()
            report, err = run_message_turn(config, "alice", "555", "what do you see", "aliceu")
        self.assertEqual((report, err), ("I see a desk and a monitor.", ""))
        call = req.call_args
        self.assertEqual(call.args[0], "sendPhoto")
        self.assertEqual(call.kwargs["data"]["chat_id"], "555")

    def test_a_typed_signed_link_is_delivered_and_quoted_as_sent(self):
        from tools.telegram_tools import _deliver_links_in_reply, _reset_delivered

        _reset_delivered()
        self._resolve_patch()
        with patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")) as req:
            text, n = _deliver_links_in_reply(
                config, "tok", "555", "user-alice",
                "Here it is: /api/visio/frame/tok - take a look.",
            )
        self.assertEqual(n, 1)
        self.assertNotIn("/api/visio", text)
        self.assertIn("[sent: frame.jpg]", text)
        self.assertEqual(req.call_args.args[0], "sendPhoto")

    def test_same_link_typed_twice_is_sent_once(self):
        from tools.telegram_tools import _deliver_links_in_reply, _reset_delivered

        _reset_delivered()
        self._resolve_patch()
        with patch("tools.telegram_tools.bot_request", return_value=({"ok": True}, "")) as req:
            text, n = _deliver_links_in_reply(
                config, "tok", "555", "user-alice",
                "/api/visio/frame/tok and again /api/visio/frame/tok",
            )
        self.assertEqual(n, 1)
        self.assertEqual(req.call_count, 1)
        self.assertEqual(text.count("[sent: frame.jpg]"), 2)

    def test_unresolveable_and_non_apex_links_are_left_untouched(self):
        from tools.preview_tools import _PreviewError
        from tools.telegram_tools import _deliver_links_in_reply, _reset_delivered

        _reset_delivered()
        bad = patch(
            "tools.preview_tools._resolve_source",
            side_effect=_PreviewError("Snapshot is unavailable.", 404),
        )
        bad.start()
        self.addCleanup(bad.stop)
        with patch("tools.telegram_tools.bot_request") as req:
            text, n = _deliver_links_in_reply(
                config, "tok", "555", "user-alice",
                "expired: /api/visio/frame/dead and web: https://example.com/x",
            )
        self.assertEqual(n, 0)
        self.assertIn("/api/visio/frame/dead", text)
        self.assertIn("https://example.com/x", text)
        req.assert_not_called()

    def test_tool_wont_resend_a_snapshot_already_delivered(self):
        from tools.base import ToolContext
        from tools.telegram_tools import _DELIVERED, _DELIVERED_LOCK, _reset_delivered, telegram_send_file

        redeem_pairing_code(generate_pairing_code(config, "alice"), "555", "aliceu")
        self.cfg(telegram_enabled=True, telegram_bot_token=GOOD_TOKENS[0])
        _reset_delivered()
        with _DELIVERED_LOCK:
            _DELIVERED["/api/visio/frame/tok"] = "frame.jpg"
        ctx = ToolContext(user_id="user-alice", conversation_id="c1")
        with patch("tools.telegram_tools.send_file") as send:
            reply = telegram_send_file(config, ctx, "/api/visio/frame/tok")
        self.assertIn("Already sent", reply)
        send.assert_not_called()


class TelegramDatabasePermissionsTests(unittest.TestCase):
    """The settings table holds secrets; the sqlite file must not be readable
    by other local users."""

    def test_db_file_is_chmodded_0600_on_open(self):
        import tempfile

        from db import Database

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "secrets.db"
        db = Database(str(path))
        db.set_setting("telegram_bot_token", "123456789:ABCDefghIJKLMnopqrstuvwxyz1234")
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()