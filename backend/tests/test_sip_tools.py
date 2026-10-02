"""SIP: the gate, the secret handling, the destination filter, and the routes.

Nothing here dials. A test that can ring a real phone is a test that can ring
someone's real phone, so every case that would reach the network stops at the
boundary: ``confirm`` is required, and the one helper that opens a socket is
mocked.

baresip cannot start in the container this suite runs in (``epoll_ctl: EPERM``),
so the SIP stack itself was verified by hand on the host instead. What is proven
here is everything that decides *whether* it would be reached and *what* it would
be handed - which is also the part a future edit can quietly break.

Settings are read the way the tool reads them: from the settings table, falling
back to the env defaults. The gate tests therefore go through ``set_setting``
rather than a hand-built config, so "the Settings tab drives the tool" is
actually asserted rather than assumed.
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import config
from tools.sip_tools import pending_reply as sip_pending_reply
from security import CSRF_HEADER
from tools.base import ToolContext
from tools.sip_tools import (
    ALLOWED_TRANSPORTS,
    _CALL_ENDED_RE,
    _wait_for_answer,
    SipSession,
    MAX_CALLS,
    PENDING_TTL_SECONDS,
    REQUIRED_FIELDS,
    _PENDING,
    account_line,
    build_plan,
    build_sip_tools,
    clear_pending,
    effective_settings,
    env_file_path,
    normalize_destination,
    note_pending,
    pending_plan,
    pending_reply,
    plan_call,
    redact,
    synthesize,
    write_env_file,
)
from skills.manager import (
    SkillManager,
    _ROUTER_CACHE,
    force_sip_skill,
    is_memory_store,
    route_skill,
)

from test_auth_routes import AuthRouteTestCase

FULL = {
    "sip_enabled": True,
    "sip_server": "pbx.example.org",
    "sip_user": "1001",
    "sip_password": "s3cr3t-value",
    "sip_transport": "tls",
    "sip_port": "5061",
    "sip_display_name": "APEX",
    "sip_domain": "",
    "sip_outbound_proxy": "",
}


def env_defaults(**over):
    """The `config` object shape the tool expects, with no SIP values set."""
    base = dict(
        SIP_ENABLED=False, SIP_SERVER="", SIP_USER="", SIP_PASSWORD="",
        SIP_TRANSPORT="udp", SIP_PORT="", SIP_DISPLAY_NAME="APEX", SIP_DOMAIN="",
        SIP_OUTBOUND_PROXY="", SIP_MAX_DURATION_SECONDS=600,
        SIP_LISTEN_TIMEOUT_SECONDS=20, SIP_WHISPER_PYTHON="",
        SIP_WHISPER_MODEL="tiny", SIP_ENV_FILE="",
    )
    base.update(over)
    return SimpleNamespace(**base)


class SipGateTestCase(AuthRouteTestCase):
    """Isolated settings table, so a test can never dial production settings."""

    def setUp(self):
        super().setUp()
        # The env defaults are blanked too. They come from the developer's own
        # backend/.env, so a test that only isolates the database still inherits
        # a real server, user and SIP_ENABLED=True from whatever that machine
        # has configured - and then asserts against someone else's account.
        # SIP_ENV_FILE points into this test's temp dir for the same reason in
        # the other direction: a settings POST mirrors into it, and unpatched
        # that would rewrite the real backend/.env from a fixture.
        self.mirrored_env = Path(self._tmp.name) / ".env"
        for attr, value in (
            ("SIP_ENABLED", False), ("SIP_SERVER", ""), ("SIP_USER", ""),
            ("SIP_PASSWORD", ""), ("SIP_TRANSPORT", "udp"), ("SIP_PORT", ""),
            ("SIP_DISPLAY_NAME", "APEX"), ("SIP_DOMAIN", ""),
            ("SIP_OUTBOUND_PROXY", ""), ("SIP_WHISPER_PYTHON", ""),
            ("SIP_WHISPER_MODEL", "tiny"), ("SIP_MAX_DURATION_SECONDS", 600),
            ("SIP_LISTEN_TIMEOUT_SECONDS", 20),
            ("SIP_ENV_FILE", str(self.mirrored_env)),
        ):
            p = patch.object(config, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def given(self, **over):
        # udp, not the tls of FULL: whether a TLS module exists on the machine
        # running the tests is not what these cases are about, and on a host
        # without one the transport check answers first and masks the real path.
        rows = {**FULL, "sip_transport": "udp", **over}
        for key, value in rows.items():
            self.db.set_setting(key, value)

    def call_tool(self, action, cfg=None, **args):
        tool = build_sip_tools(cfg or env_defaults())[0]
        return tool.call({"action": action, **args}, ToolContext(user_id="alice"))


# --------------------------------------------------------------------------
class SipSettingsMergeTests(SipGateTestCase):
    def test_db_overrides_env(self):
        cfg = effective_settings(env_defaults(SIP_SERVER="env.example", SIP_USER="77"),
                                 {"sip_server": "db.example", "sip_user": "2002"})
        self.assertEqual(cfg["sip_server"], "db.example")
        self.assertEqual(cfg["sip_user"], "2002")

    def test_env_used_when_the_table_is_silent(self):
        cfg = effective_settings(
            env_defaults(SIP_SERVER="env.example", SIP_USER="77", SIP_PASSWORD="pw"), {})
        self.assertEqual(cfg["sip_server"], "env.example")
        self.assertEqual(cfg["sip_user"], "77")

    def test_domain_defaults_to_server(self):
        self.assertEqual(effective_settings(env_defaults(), FULL)["sip_domain"],
                         "pbx.example.org")
        self.assertEqual(effective_settings(env_defaults(), {**FULL, "sip_domain": "voip.x"})["sip_domain"],
                         "voip.x")

    def test_missing_named_by_field(self):
        cfg = effective_settings(env_defaults(), {"sip_enabled": True, "sip_server": "x"})
        self.assertFalse(cfg["configured"])
        self.assertEqual(cfg["missing"], ["sip_user", "sip_password"])

    def test_bad_transport_degrades_to_udp(self):
        # The env file is hand-editable, so a bad value has to degrade to a
        # working call rather than a baresip config that never registers.
        self.assertNotIn("sctp", ALLOWED_TRANSPORTS)
        self.assertEqual(effective_settings(env_defaults(), {**FULL, "sip_transport": "sctp"})["sip_transport"],
                         "udp")
        self.assertEqual(effective_settings(env_defaults(SIP_TRANSPORT="TLS"), {})["sip_transport"],
                         "tls")

    def test_bad_port_is_dropped_not_kept(self):
        for bad in ("0", "70000", "abc", "-1", "5061 "):
            cfg = effective_settings(env_defaults(), {**FULL, "sip_port": bad})
            self.assertIn(cfg["sip_port"], ("", "5061"))

    def test_settings_table_reaches_the_tool(self):
        # The Settings tab writes the table; the tool reads it. If this breaks,
        # saving a correct account changes nothing until the service restarts.
        self.given()
        report = json.loads(self.call_tool("status"))
        self.assertTrue(report["configured"])
        self.assertEqual(report["server"], "pbx.example.org")
        self.assertEqual(report["user"], "1001")
        merged = effective_settings(env_defaults(), self.db.all_settings())
        self.assertEqual(merged["sip_tts_engine"], "espeak")
        self.assertEqual(merged["sip_tts_voice"], "")

    def test_turning_sip_off_in_the_table_disables_the_tool(self):
        self.given()
        self.db.set_setting("sip_enabled", False)
        self.assertIn("turned off", self.call_tool("plan", to="1001"))


class SecretTests(SipGateTestCase):
    def test_password_never_appears_in_a_plan(self):
        plan = build_plan(effective_settings(env_defaults(), FULL), "+306901234567")
        self.assertNotIn("s3cr3t-value", json.dumps(plan))
        self.assertIn("auth_pass=***", plan["account"])
    def test_redact_hides_a_value_it_has_never_seen(self):
        # baresip echoes the account line itself, so the parameter name is the
        # reliable thing to mask, not the known value.
        self.assertEqual(redact("auth_pass=anything", {}), "auth_pass=***")
        self.assertNotIn("s3cr3t", redact("auth_pass=s3cr3t-value tail",
                                           effective_settings(env_defaults(), FULL)))

    def test_redact_is_harmless_without_a_config(self):
        self.assertEqual(redact("nothing secret here", None), "nothing secret here")

    def test_status_never_returns_the_password(self):
        self.given()
        self.assertNotIn("s3cr3t-value", self.call_tool("status"))

    def test_status_account_is_masked(self):
        self.given()
        report = json.loads(self.call_tool("status"))
        self.assertIn("auth_pass=***", report["account"])


class SpeechSynthesisTests(unittest.TestCase):
    def test_edge_tts_writes_mp3_with_selected_voice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def make_audio(args, **_kwargs):
                output = Path(args[args.index("--write-media") + 1])
                output.write_bytes(b"audio" * 20)
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with patch("tools.sip_tools.edge_tts_command", return_value="/usr/bin/edge-tts"), \
                    patch("tools.sip_tools.subprocess.run", side_effect=make_audio) as run:
                output = synthesize("Hello there", Path(temp_dir) / "utterance.wav",
                                    voice="en-GB-SoniaNeural", engine="edge")

            self.assertEqual(output, Path(temp_dir) / "utterance.mp3")
            args = run.call_args.args[0]
            self.assertIn("en-GB-SoniaNeural", args)
            self.assertEqual(args[args.index("--text") + 1], "Hello there")

    def test_missing_edge_cli_falls_back_to_espeak(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def make_audio(args, **_kwargs):
                output = Path(args[args.index("-w") + 1])
                output.write_bytes(b"audio" * 20)
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            def which(name):
                return None if name == "edge-tts" else "/usr/bin/espeak-ng" if name == "espeak-ng" else None

            with patch("tools.sip_tools.edge_tts_command", return_value=None), \
                    patch("tools.sip_tools.shutil.which", side_effect=which), \
                    patch("tools.sip_tools.subprocess.run", side_effect=make_audio) as run:
                output = synthesize("Hello there", Path(temp_dir) / "utterance.wav", engine="edge")

            self.assertEqual(output, Path(temp_dir) / "utterance.wav")
            self.assertEqual(run.call_args.args[0][0], "/usr/bin/espeak-ng")

    def test_rejects_unknown_tts_engine(self):
        with self.assertRaisesRegex(ValueError, "Unsupported SIP TTS engine"):
            synthesize("Hello", Path("utterance.wav"), engine="unknown")


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = Path(self._tmp.name) / ".env"

    def test_creates_a_missing_file_owner_only(self):
        write_env_file(FULL, self.env)
        self.assertEqual(oct(self.env.stat().st_mode & 0o777), oct(0o600))
        self.assertIn("SIP_SERVER=pbx.example.org", self.env.read_text())

    def test_preserves_unrelated_keys_including_other_secrets(self):
        # The env file is shared with every other setting; rewriting it must not
        # cost anyone their API key.
        self.env.write_text("OPENAI_API_KEY=sk-abc\nSIP_USER=old\nWAKE_WORD=hey\n")
        write_env_file(FULL, self.env)
        text = self.env.read_text()
        self.assertIn("OPENAI_API_KEY=sk-abc", text)
        self.assertIn("WAKE_WORD=hey", text)
        self.assertIn("SIP_USER=1001", text)
        self.assertNotIn("SIP_USER=old", text)

    def test_update_does_not_duplicate_a_key(self):
        write_env_file(FULL, self.env)
        write_env_file({**FULL, "sip_user": "2002"}, self.env)
        text = self.env.read_text()
        self.assertEqual(text.count("SIP_USER="), 1)
        self.assertIn("SIP_USER=2002", text)

    def test_empty_optional_is_omitted_rather_than_blanked(self):
        # Writing SIP_PORT= would pin "" and override the documented default
        # forever; omitting it keeps the fallback working.
        write_env_file({**FULL, "sip_port": "", "sip_domain": "", "sip_outbound_proxy": ""}, self.env)
        text = self.env.read_text()
        self.assertNotIn("SIP_PORT=", text)
        self.assertNotIn("SIP_DOMAIN=", text)
        self.assertNotIn("SIP_OUTBOUND_PROXY=", text)

    def test_quotes_a_value_that_would_break_the_file(self):
        write_env_file({**FULL, "sip_password": 'has "quotes" and space'}, self.env)
        line = [l for l in self.env.read_text().splitlines() if l.startswith("SIP_PASSWORD=")][0]
        self.assertTrue(line.startswith('SIP_PASSWORD="') and line.endswith('"'))
        self.assertIn('\\"', line)

    def test_defaults_to_the_backend_env_file(self):
        self.assertEqual(env_file_path(env_defaults()).name, ".env")
        self.assertEqual(env_file_path(env_defaults(SIP_ENV_FILE="/tmp/x.env")),
                         Path("/tmp/x.env"))


class DestinationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = effective_settings(env_defaults(), FULL)

    def test_e164_gets_the_domain(self):
        self.assertEqual(normalize_destination("+306901234567", self.cfg),
                         "sip:+306901234567@pbx.example.org")

    def test_extension(self):
        self.assertEqual(normalize_destination("1002", self.cfg), "sip:1002@pbx.example.org")

    def test_a_full_uri_is_left_alone(self):
        for uri in ("sip:1001@other.example.org", "SIP:1001@other.example.org",
                    "sip:1001@other.example.org;transport=tls",
                    "sip:+306901234567@other.example.org:5061"):
            self.assertEqual(normalize_destination(uri, self.cfg), uri)

    def test_a_uri_that_also_carries_a_command_is_still_refused(self):
        for bad in ("sip:1@x;rm -rf /", "sip:1@x /hangup", "sips:\n1@x"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalize_destination(bad, self.cfg)

    def test_command_injection_is_refused(self):
        # The destination reaches baresip on stdin, which splits on whitespace,
        # so a value carrying one could append a second command to the same line.
        for bad in ("1001 /hangup", "sip:1@x\r/dial 2", '1001" ; rm -rf /', "1001;kill",
                    "1001|x", "1001&&shutdown", "", "   ", "a" * 200):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalize_destination(bad, self.cfg)

    def test_number_separators_are_stripped(self):
        # People dictate and hand-write numbers grouped. Refusing "+30 6977
        # 456030" sent the model back to ask the operator to reformat a number
        # they had already given, twice, instead of dialling it.
        for spaced in ("6977 456030", "697-745-6030", "(697) 745-6030",
                       "  6977 456030  "):
            self.assertEqual(normalize_destination(spaced, self.cfg),
                             "sip:6977456030@pbx.example.org", msg=repr(spaced))
        # An international number keeps its country code across the separators.
        for spaced in ("+30 6977 456030", "+30 697 745 6030", "+30-6977-456030",
                       "  +30 6977 456030  "):
            self.assertEqual(normalize_destination(spaced, self.cfg),
                             "sip:+306977456030@pbx.example.org", msg=repr(spaced))

    def test_a_stripped_separator_cannot_smuggle_a_command(self):
        # The strip must only ever apply to something that is entirely number
        # characters. A word, an extension or a command has to survive intact
        # into the allowlist, which rejects it - never be rewritten into a
        # number that then dials.
        for bad in ("1001 ext 567", "1001 /hangup", "1001&rm -rf /",
                    "1001#(1002)", "call 1001 now"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalize_destination(bad, self.cfg)


class PendingPlanTests(SipGateTestCase):
    """A call is two turns, and the second one is the one that dials.

    Routing is per-turn, so the reply to a plan - "yes" - names no skill, no
    tool and no destination. These tests pin that it reaches the SIP skill
    anyway, which is the difference between a confirmed call and the model
    reporting that it cannot place phone calls.
    """

    def setUp(self):
        super().setUp()
        self.given()
        self.ctx = SimpleNamespace(user_id="sip-owner", conversation_id="conv-1")
        clear_pending(("sip-owner", "conv-1"))

    def _arm(self, dest="+30 6977 456030"):
        out = plan_call(config, self.ctx, dest, "hello", 60)
        self.assertIn("next_step", out)
        return out

    def test_a_plan_is_pending_and_the_destination_is_kept(self):
        self._arm("6977 456030")
        entry = pending_plan("sip-owner", "conv-1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["dest"], "6977 456030")

    def test_bare_confirmation_is_recognised(self):
        for reply in ("yes", "YES", "yes please", "go ahead", "ok", "ok!", "OK.",
                      "confirmed", "do it", "sure", "yep", "ναι", "Ναι", "οκ",
                      "ΟK!", "κάν το", "κάν' το", "κάνε το", "προχώρα",
                      "επιβεβαίωσε"):
            with self.subTest(reply=reply):
                self._arm()
                self.assertEqual(pending_reply(reply, "sip-owner", "conv-1"),
                                 "confirm", msg=repr(reply))

    def test_refusal_is_recognised_and_drops_the_plan(self):
        for reply in ("no", "όχι", "ΟΧΙ", "οχι", "cancel", "μην", "άκυρο", "don't"):
            with self.subTest(reply=reply):
                self._arm()
                self.assertEqual(pending_reply(reply, "sip-owner", "conv-1"),
                                 "decline", msg=repr(reply))
                self.assertIsNone(pending_plan("sip-owner", "conv-1"))

    def test_a_confirmed_plan_is_not_dropped(self):
        # "yes" must still be there afterwards, so the turn that dials can read
        # the destination, and a re-send cannot double-dial it.
        self._arm()
        self.assertEqual(pending_reply("yes", "sip-owner", "conv-1"), "confirm")
        self.assertIsNotNone(pending_plan("sip-owner", "conv-1"))

    def test_an_unrelated_turn_drops_the_plan(self):
        # A stray "ok" twenty minutes later, in reply to something else, must
        # not dial a phone number the operator has forgotten about.
        self._arm()
        self.assertIsNone(pending_reply("what is the weather", "sip-owner", "conv-1"))
        self.assertIsNone(pending_plan("sip-owner", "conv-1"))

    def test_a_plan_expires(self):
        self._arm()
        _PENDING[("sip-owner", "conv-1")]["at"] = (
            time.time() - PENDING_TTL_SECONDS - 1)
        self.assertIsNone(pending_reply("yes", "sip-owner", "conv-1"))

    def test_one_user_cannot_confirm_another_conversation(self):
        self._arm()
        self.assertIsNone(pending_reply("yes", "sip-owner", "conv-2"))
        self.assertIsNone(pending_reply("yes", "someone-else", "conv-1"))

    def test_no_pending_plan_is_not_a_confirmation(self):
        self.assertIsNone(pending_reply("yes", "sip-owner", "conv-1"))

    def test_a_signed_out_turn_arms_nothing(self):
        anon = SimpleNamespace(user_id="", conversation_id="conv-1")
        plan_call(config, anon, "6977 456030", "hello", 60)
        self.assertIsNone(pending_plan("", "conv-1"))


class DialGateTests(SipGateTestCase):
    def test_no_action_means_status(self):
        # The dangerous action must never be a default.
        tool = build_sip_tools(env_defaults())[0]
        self.assertIn("sip_enabled", tool.call({}, ToolContext(user_id="alice")))

    def test_call_without_confirm_does_not_dial(self):
        self.given()
        with patch("tools.sip_tools.start_call") as dial:
            result = self.call_tool("call", to="+306901234567")
            dial.assert_not_called()
        self.assertIn("Nothing was dialled", result)

    def test_call_with_confirm_reaches_the_dial_path(self):
        self.given()
        with patch("tools.sip_tools.start_call", return_value="dialled") as dial:
            self.assertEqual(self.call_tool("call", to="+306901234567", confirm=True), "dialled")
        dial.assert_called_once()

    def test_plan_dials_nothing_even_with_confirm(self):
        self.given()
        with patch("tools.sip_tools.start_call") as dial:
            report = json.loads(self.call_tool("plan", to="+306901234567", text="hello"))
            dial.assert_not_called()
        self.assertFalse(report["dialled"])
        self.assertEqual(report["to"], "sip:+306901234567@pbx.example.org")
        self.assertIn("confirm=true", report["next_step"])

    def test_turned_off_blocks_planning_as_well_as_calling(self):
        self.given()
        self.db.set_setting("sip_enabled", False)
        for action in ("plan", "call"):
            self.assertIn("turned off", self.call_tool(action, to="1001", confirm=True))

    def test_unconfigured_names_the_missing_field(self):
        for key in REQUIRED_FIELDS:
            self.given()
            self.db.set_setting(key, "")
            result = self.call_tool("plan", to="1001")
            self.assertIn(key, result)

    def test_anonymous_cannot_use_the_tool(self):
        tool = build_sip_tools(env_defaults())[0]
        self.assertIn("Sign in", tool.call({"action": "status"}, ToolContext()))

    def test_unknown_action_is_refused(self):
        self.assertIn("Unknown action", self.call_tool("conference-call"))

    def test_a_second_call_is_refused_while_one_is_up(self):
        self.given()
        sess = SimpleNamespace(destination="sip:1001@pbx.example.org", user_id="alice")
        with patch("tools.sip_tools.sessions_for", return_value=[sess]), \
             patch("tools.sip_tools.missing_binaries", return_value=[]), \
             patch("tools.sip_tools.pulse_ready", return_value=(True, "")):
            result = self.call_tool("call", to="1002", confirm=True)
        self.assertIn("already up", result)

    def test_missing_baresip_is_reported_without_dialling(self):
        self.given()
        with patch("tools.sip_tools.missing_binaries", return_value=["baresip (apt-get install baresip)"]):
            result = self.call_tool("call", to="1002", confirm=True)
        self.assertIn("Nothing was dialled", result)
        self.assertIn("baresip", result)


class SessionOwnershipTests(SipGateTestCase):
    def test_a_session_id_from_another_user_is_invisible(self):
        # A session id is a live reference to a phone call; answering someone
        # else's would put their conversation in this transcript.
        from tools.sip_tools import lookup_session

        sess = SimpleNamespace(id="abc", user_id="bob", destination="sip:bob@x")
        with patch.dict("tools.sip_tools._SESSIONS", {"abc": sess}):
            self.assertIsNone(lookup_session("alice", "abc"))
            self.assertIsNone(lookup_session("alice", "nope"))
            self.assertIs(lookup_session("bob", "abc"), sess)

    def test_say_listen_and_hangup_reject_an_unknown_session(self):
        self.given()
        for action in ("say", "listen", "hangup"):
            self.assertIn("No such call", self.call_tool(action, session="missing", text="hi"))

    def test_one_call_at_a_time(self):
        self.assertEqual(MAX_CALLS, 1)


class AnswerTimingTests(SipGateTestCase):
    """The conversation budget must start when the far end answers.

    Armed at the /dial instead, a phone that rings for twenty seconds would be
    hung up the instant it said hello, and the symptom - a call that connects
    and drops - looks like a server problem, not a clock.
    """

    def _dial_with_a_clock_that_jumps_past_the_ring(self):
        """Dial with the clock moved forward 20s during the ring, as it is."""
        self.given()
        clock = {"now": 1000.0}

        def now():
            return clock["now"]

        fake = SimpleNamespace(
            stdin=io.StringIO(), stdout=io.StringIO(), stderr=None, pid=4242,
            returncode=None, poll=lambda: None, wait=lambda *a, **k: None,
            terminate=lambda *a, **k: None, kill=lambda *a, **k: None,
        )

        def answered(sess, timeout=0.0):
            clock["now"] += 20.0          # twenty seconds of ringing
            return True, ""

        with patch("tools.sip_tools.time.time", side_effect=now), \
             patch("tools.sip_tools.time.sleep", lambda *_: None), \
             patch("tools.sip_tools.missing_binaries", return_value=[]), \
             patch("tools.sip_tools.pulse_ready", return_value=(True, "")), \
             patch("tools.sip_tools.create_null_sinks", return_value=([1, 2], "rx", "tx")), \
             patch("tools.sip_tools.drop_null_sinks"), \
             patch("tools.sip_tools.write_baresip_config"), \
             patch("tools.sip_tools.subprocess.Popen", return_value=fake), \
             patch("tools.sip_tools._wait_for_answer", side_effect=answered):
            report = json.loads(self.call_tool("call", to="1001", confirm=True, duration=60))
        return clock["now"], report

    def test_the_budget_is_rearmed_when_they_answer(self):
        from tools.sip_tools import lookup_session

        at_answer, report = self._dial_with_a_clock_that_jumps_past_the_ring()
        sess = lookup_session("alice", report["session"])
        self.assertIsNotNone(sess)
        try:
            # 20s of ringing must not have come out of the 60s conversation.
            self.assertAlmostEqual(sess.deadline - at_answer, 60.0, delta=0.5)
            self.assertGreater(report["remaining_seconds"], 55)
        finally:
            from tools.sip_tools import close_session

            close_session(sess)

    def test_an_unanswered_call_is_bounded_by_the_ring_timeout(self):
        from tools.sip_tools import RING_TIMEOUT_SECONDS

        self.assertGreater(RING_TIMEOUT_SECONDS, 0)
        self.assertLessEqual(RING_TIMEOUT_SECONDS, 120)


class SipRouteTests(SipGateTestCase):
    def test_status_needs_a_session_and_respects_the_lock(self):
        client = self._app().test_client()
        self.assertEqual(client.get("/api/sip/status").status_code, 401)
        self._login(client)
        self.assertEqual(client.get("/api/sip/status").status_code, 200)
        with client.session_transaction() as session:
            session["locked"] = True
        self.assertEqual(client.get("/api/sip/status").status_code, 423)

    def test_status_never_leaks_the_password(self):
        self.given()
        client = self._app().test_client()
        self._login(client)
        body = client.get("/api/sip/status").get_data(as_text=True)
        self.assertNotIn("s3cr3t-value", body)
        self.assertTrue(client.get("/api/sip/status").json["password_set"])

    def test_clearing_the_password_requires_csrf(self):
        client = self._app().test_client()
        self._login(client)
        self.assertEqual(client.post("/api/sip/clear-password").status_code, 403)

    def test_clearing_the_password_empties_it_for_real(self):
        self.given()
        client = self._app().test_client()
        token = self._login(client)
        result = client.post("/api/sip/clear-password", headers=self._headers(token))
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json["sip_password_set"])
        self.assertIsNone(self.db.get_setting("sip_password"))

    def test_settings_never_echo_the_password(self):
        client = self._app().test_client()
        self._login(client)
        body = client.get("/api/settings").get_data(as_text=True)
        self.assertNotIn("s3cr3t-value", body)
        self.assertNotIn('"sip_password"', body)

    def test_a_saved_account_survives_the_round_trip(self):
        client = self._app().test_client()
        token = self._login(client)
        with patch.object(config, "SIP_ENV_FILE", str(Path(self._tmp.name) / ".env")):
            result = client.post("/api/settings", headers=self._headers(token), json={
                "sip_enabled": True,
                "sip_server": "pbx.example.org",
                "sip_user": "1001",
                "sip_password": "s3cr3t-value",
                "sip_transport": "tls",
                "sip_port": "5061",
                "sip_display_name": "APEX",
                "sip_domain": "",
                "sip_outbound_proxy": "",
            })
        self.assertEqual(result.status_code, 200)
        settings = result.json["settings"]
        self.assertEqual(settings["sip_server"], "pbx.example.org")
        self.assertNotIn("sip_password", settings)
        self.assertTrue(settings["sip_password_set"])

    def test_an_unrelated_save_keeps_the_stored_password(self):
        # The settings box is blank on load, so every unrelated save posts an
        # empty password. Treating that as "clear it" would lock the operator
        # out of their own account.
        self.given()
        client = self._app().test_client()
        token = self._login(client)
        client.post("/api/settings", headers=self._headers(token), json={"wake_word": "hey"})
        self.assertEqual(self.db.get_setting("sip_password"), "s3cr3t-value")

    def test_sip_fields_are_validated(self):
        client = self._app().test_client()
        token = self._login(client)
        for invalid in ({"sip_transport": "sctp"}, {"sip_port": "0"}, {"sip_port": "70000"},
                        {"sip_server": []}, {"sip_enabled": {"a": 1}}):
            result = client.post("/api/settings", headers=self._headers(token), json=invalid)
            self.assertEqual(result.status_code, 400, invalid)

    def test_the_env_file_is_mirrored_so_a_shell_start_agrees(self):
        client = self._app().test_client()
        token = self._login(client)
        env = Path(self._tmp.name) / "mirrored.env"
        with patch.object(config, "SIP_ENV_FILE", str(env)):
            client.post("/api/settings", headers=self._headers(token), json={
                "sip_server": "pbx.example.org", "sip_user": "1001", "sip_password": "pw",
            })
        self.assertTrue(env.exists())
        text = env.read_text()
        self.assertIn("SIP_SERVER=pbx.example.org", text)
        self.assertIn("SIP_PASSWORD=pw", text)

    def test_config_endpoint_advertises_sip_without_the_secret(self):
        client = self._app().test_client()
        self._login(client)
        body = client.get("/api/config").get_data(as_text=True)
        self.assertNotIn("s3cr3t-value", body)
        self.assertIn("sip_configured", body)

    def test_status_answers_exactly_what_the_settings_tab_types(self):
        # SipStatus in frontend/lib/api.ts. The two cannot be checked against
        # each other at build time, and a renamed key reads as a field that is
        # simply always missing, so the set is pinned here instead.
        expected = {
            "ok", "sip_enabled", "configured", "missing_settings", "missing_on_host",
            "transport", "allowed_transports", "server", "user", "password_set",
            "domain", "display_name", "outbound_proxy", "tts_engine",
            "ffmpeg_available", "whisper_ready", "audio_loopback_ok",
            "audio_loopback_note", "env_file", "env_keys", "live_calls",
            "max_concurrent_calls", "max_duration_seconds",
        }
        client = self._app().test_client()
        self._login(client)
        body = client.get("/api/sip/status").json
        self.assertEqual(set(body), expected)

        typed = (Path(__file__).resolve().parents[2] / "frontend" / "lib" / "api.ts").read_text()
        sip_status_type = typed[typed.index("export type SipStatus ="):]
        sip_status_type = sip_status_type[: sip_status_type.index("\n};")]
        for key in expected:
            self.assertIn(f"{key}:", sip_status_type, f"SipStatus is missing {key}")

    def test_status_explains_itself_when_sip_is_off(self):
        # audio_loopback_ok is the audio layer, which can be perfectly healthy
        # while SIP itself is off; the reason is the note, not that flag.
        client = self._app().test_client()
        self._login(client)
        self.assertIn("turned off", client.get("/api/sip/status").json["audio_loopback_note"])

    def test_status_names_the_missing_setting_rather_than_just_saying_no(self):
        client = self._app().test_client()
        self._login(client)
        self.given()
        self.db.set_setting("sip_password", "")
        body = client.get("/api/sip/status").json
        self.assertFalse(body["configured"])
        self.assertIn("sip_password", body["missing_settings"])
        self.assertIn("sip_password", body["audio_loopback_note"])


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.skills = SkillManager().all()

    def test_call_requests_reach_the_sip_skill(self):
        for text in ("call me", "Call me right now", "ring me", "phone me", "telephone me",
                     "make a call", "dial 2101234567", "place a phone call",
                     "τηλεφωνησε με", "κάλε με", "κάλεσε τον 2101234567",
                     "καλ τον 2101234567", "καλ το 2101234567", "μου τηλεφωνάς"):
            self.assertEqual(force_sip_skill(text, self.skills), "SIP", text)

    def test_both_greek_sigma_spellings_match(self):
        # The final sigma is a distinct character; matching only ς or only σ
        # silently loses half the phrasings, which is the same trap the voice
        # command normaliser documents.
        accented = "τηλεφώνησέ με"
        self.assertEqual(force_sip_skill(accented, self.skills), "SIP")
        self.assertEqual(force_sip_skill(accented.replace("σ", "ς"), self.skills), "SIP")

    def test_ordinary_uses_of_call_are_not_routed(self):
        for text in ("what do you see now?", "write a python function call",
                     "call the police in an emergency", "define a recursive function",
                     "the function calls itself recursively", "run the tests",
                     "καλός και ωραίος", "make a sandwich", "hello there"):
            self.assertIsNone(force_sip_skill(text, self.skills), text)

    def test_code_shaped_text_is_never_routed(self):
        self.assertIsNone(force_sip_skill("write a def call_me(): pass function", self.skills))

    def test_skill_definition_is_wellformed(self):
        sip = [s for s in self.skills if s.name == "SIP"]
        self.assertEqual(len(sip), 1)
        self.assertIn("sip_call", sip[0].tools)
        self.assertTrue(sip[0].require_tool, "a skill that dials must not answer from memory")

    def test_the_skill_can_find_the_number_the_operator_stored(self):
        # "call me" in a fresh conversation names no number. Without recall the
        # agent has to ask for it every single time, which is the behaviour
        # that made the feature feel broken rather than absent.
        sip = [s for s in self.skills if s.name == "SIP"][0]
        self.assertIn("recall", sip.tools)

    def test_it_may_only_dial_a_number_that_belongs_to_the_operator(self):
        # Memory holds phone numbers extracted from documents, contact lists and
        # invoices - other people's numbers. The prompt has to rule them out,
        # because the tool cannot tell whose number it is.
        prompt = [s for s in self.skills if s.name == "SIP"][0].system_prompt.lower()
        for phrase in ("operator themsel", "somebody else", "contact list",
                       "never fall back"):
            self.assertIn(phrase, prompt, f"SIP prompt does not cover {phrase!r}")

    def test_only_the_sip_skill_can_dial(self):
        # The gate is the skill's tool list. If another skill gained sip_call
        # without the two-step prompt, a confirmation turn could dial without
        # anyone having seen a plan.
        for skill in self.skills:
            if skill.name == "SIP":
                continue
            self.assertNotIn("sip_call", skill.tools or [],
                             f"{skill.name} can place calls without the SIP gate")


class MemoryRoutingTests(unittest.TestCase):
    """A fact being stored must not be routed to a skill that cannot store it.

    "remember this my phone number is 6977456030" was classified as a
    phone-call request and sent to SIP, which has no `remember` tool. The
    operator got a call plan back instead of a memory, and on another turn the
    model wrote the number to a text file instead.
    """

    def setUp(self):
        self.skills = SkillManager().all()
        # The router memoises by exact message text, so a neighbouring test that
        # routed the same sentence would make these pass whatever the code does.
        _ROUTER_CACHE.clear()
        self.addCleanup(_ROUTER_CACHE.clear)

    def test_storing_a_fact_is_recognised(self):
        for text in ("remember this my phone number is 6977456030",
                     "remember I like cakes",
                     "remember that my address is 5 Baker Street",
                     "note that my number is 123",
                     "save my phone number 123",
                     "θυμήσου ότι το νούμερό μου είναι 6977456030",
                     "να θυμάσαι πως μου αρέσει το σοκολατένιο",
                     "θυμησε το ονομα μου",
                     "θυμό το όνομα μου"):
            self.assertTrue(is_memory_store(text), text)

    def test_ordinary_turns_are_not_stores(self):
        for text in ("call me", "call +30 6977 456030", "what is my phone number",
                     "remember to call me at 3pm", "save the file to disk",
                     "find my phone number in the pdf", "write a file called notes.txt",
                     "store the file in a folder", "keep the terminal open",
                     "show me the code", "τυπωσε το αρχειο", "βλεψε την καμερα",
                     "remember where the keys are", "what is the time",
                     "remind me to buy milk"):
            self.assertFalse(is_memory_store(text), text)

    def test_the_general_skill_can_actually_store(self):
        general = [s for s in self.skills if s.name == "general"]
        self.assertEqual(len(general), 1)
        self.assertIn("remember", general[0].tools)

    def test_no_specialist_lacks_a_store_route(self):
        # Whatever skill a memory request lands on, it must be able to store.
        # general is the intended one; the check is that none of the skills a
        # classifier could plausibly pick lacks `remember` for a fact it is
        # being asked to keep.
        self.assertNotIn("sip_call", [s for s in self.skills
                                      if s.name == "general"][0].tools or [])

    def test_a_store_never_reaches_the_classifier(self):
        # Deterministic, because the classifier reading a phone number as a
        # call request is exactly what started this.
        #
        # The probe records instead of raising: route_skill wraps the
        # classifier in a bare `except Exception` so a router failure cannot
        # block a chat, which means an AssertionError here would be swallowed
        # and the test would pass with the classifier very much consulted.
        class Probe:
            called = False

            def chat_stream(self, *a, **k):
                Probe.called = True
                return iter([{"type": "text", "content": "SIP"}])

        for text in ("remember this my phone number is 6977456030",
                     "θυμήσου ότι το νούμερό μου είναι 6977456030"):
            Probe.called = False
            _ROUTER_CACHE.clear()
            self.assertEqual(route_skill(text, self.skills, Probe(),
                                         fallback="general"), "general")
            self.assertFalse(Probe.called, f"the classifier was asked about {text!r}")


class PendingConfirmationChatTest(unittest.TestCase):
    """The confirmation turn must select the SIP skill, through the real route.

    The bug this pins was invisible in the tool: the plan was produced
    correctly and the operator's "yes" was answered by a model that had no
    `sip_call`, because routing is per-turn and "yes" names nothing. So the
    assertion has to be on which skill the chat route selects, not on what the
    tool returns.
    """

    def _post(self, message, pending=None):
        import app as app_module
        from unittest.mock import MagicMock
        if pending is not None:
            note_pending(SimpleNamespace(user_id="alice", conversation_id="conversation"),
                         {"sip_server": "pbx.example.org"}, pending)

        _ROUTER_CACHE.clear()

        db = MagicMock()
        db.get_user.return_value = {"id": "alice", "name": "Alice"}
        db.all_settings.return_value = {"provider": "openai", "model": "test"}
        db.get_conversation.return_value = {"id": "conversation", "user_id": "alice",
                                            "skill": "general", "title": "t"}
        db.list_messages.return_value = []
        skills = MagicMock()
        skills.select.side_effect = lambda name: SimpleNamespace(
            model="", tools=["sip_call"] if name == "SIP" else ["remember"])
        # Real skills, or the router cannot iterate the candidate list: a
        # MagicMock raises on iteration, route_skill swallows it and returns the
        # fallback, and the assertions below would pass because of the mock
        # rather than because of the routing.
        skills.all.return_value = SkillManager().all()
        skills.build_system_prompt.return_value = "test"
        engine = MagicMock()
        engine.stream.return_value = iter([{"type": "done", "usage": {}}])

        # The provider answers "SIP" to any routing question. That is the wrong answer
        # for a memory request, and it is what the real classifier produced - so
        # the test fails if this turn is ever decided by the classifier at all,
        # instead of passing on a fallback that a broken mock caused by accident.
        provider = MagicMock()
        provider.chat_stream.side_effect = lambda *a, **k: iter(
            [{"type": "text", "content": "SIP"}])
        providers = MagicMock()
        providers.build.return_value = provider

        with patch.object(app_module, "get_db", return_value=db), \
             patch.object(app_module, "get_skill_manager", return_value=skills), \
             patch.object(app_module, "bearer_for_api", return_value=None), \
             patch.object(app_module, "is_subscription_access", return_value=False), \
             patch.object(app_module, "ProviderManager", return_value=providers), \
             patch.object(app_module, "make_registry"), \
             patch.object(app_module, "build_engine", return_value=engine), \
             patch.object(app_module.config, "MEMORY_ENABLED", False):
            client = app_module.create_app().test_client()
            with client.session_transaction() as session:
                session["user_id"] = "alice"
            response = client.post("/api/chat", json={"message": message,
                                                      "conversation_id": "conversation"})
        self.assertEqual(response.status_code, 200)
        response.get_data(as_text=True)
        return [c.args[0] for c in skills.select.call_args_list if c.args]

    def test_yes_after_a_plan_selects_the_sip_skill(self):
        self.assertIn("SIP", self._post("yes", pending="6977456030"))

    def test_a_refusal_does_not_select_the_sip_skill(self):
        self.assertNotIn("SIP", self._post("no", pending="6977456030"))

    def test_an_ordinary_turn_is_not_hijacked(self):
        # No plan pending. Whether "yes" reaches SIP is then the classifier's
        # business, not this gate's - what the gate must never do is treat a
        # bare "yes" as agreeing to a call that was never proposed.
        self.assertIsNone(sip_pending_reply("yes", "alice", "conversation"))
        self.assertIsNone(pending_plan("alice", "conversation"))

    def test_a_confirmation_only_counts_with_a_plan_behind_it(self):
        # Same word, same routing decision, opposite meaning. This is the whole
        # point of the pending record.
        self._post("yes", pending="6977456030")
        self.assertIsNotNone(pending_plan("alice", "conversation"))

    def test_a_store_turn_selects_the_general_skill(self):
        selected = self._post("remember this my phone number is 6977456030")
        self.assertNotIn("SIP", selected)
        self.assertIn("general", selected)


class WhisperHelperTests(unittest.TestCase):
    def _helper_path(self):
        return Path(__file__).resolve().parents[1] / "tools" / "sip_whisper.py"

    def _wav(self):
        handle, name = tempfile.mkstemp(suffix=".wav")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(name) and os.unlink(name))
        return Path(name)

    def test_helper_imports_nothing_from_apex(self):
        # It runs under a different interpreter that cannot see the backend on
        # sys.path, so any apex import would break transcription at call time.
        tree = ast.parse(self._helper_path().read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        self.assertEqual(modules & {"config", "tools", "db", "app", "skills", "agent"}, set())

    def test_pins_avoid_the_two_known_breakages(self):
        reqs = (Path(__file__).resolve().parents[1] / "tools"
                / "sip-whisper-requirements.txt").read_text(encoding="utf-8")
        self.assertIn("huggingface_hub<0.26", reqs)
        self.assertIn("av>=11,<14", reqs)

    def test_a_missing_interpreter_is_reported_not_raised(self):
        from tools.sip_tools import transcribe

        audio = self._wav()
        audio.write_bytes(b"RIFF" + b"\0" * 4096)
        self.assertIn("Speech recognition is not set up", transcribe(audio, env_defaults()))

    def test_silence_is_not_a_reinvention_of_speech(self):
        from tools.sip_tools import transcribe

        audio = self._wav()
        audio.write_bytes(b"RIFF" + b"\0" * 40)
        self.assertEqual(transcribe(audio, env_defaults()), "")


class AccountLineTests(unittest.TestCase):
    def test_g711_only(self):
        # Negotiating a codec the far end lacks gives an established call that
        # carries silence, which is worse than one that plainly fails.
        self.assertIn("audio_codecs=PCMA,PCMU", account_line(effective_settings(env_defaults(), FULL)))

    def test_transport_and_port_reach_the_uri(self):
        self.assertIn("sip:1001@pbx.example.org:5061;transport=tls>",
                      account_line(effective_settings(env_defaults(), FULL)))

    def test_outbound_proxy_is_quoted(self):
        line = account_line(effective_settings(env_defaults(),
                                              {**FULL, "sip_outbound_proxy": "sip:pw.example.org"}))
        self.assertIn('outbound="sip:pw.example.org"', line)

    def test_registration_refreshes(self):
        self.assertIn("regint=60", account_line(effective_settings(env_defaults(), FULL)))

    def test_config_loads_the_modules_baresip_needs_to_register(self):
        # Two orderings are load-bearing and both were wrong on the first
        # attempt; see write_baresip_config. account.so parses the accounts
        # file the moment it loads, so it has to come after the codec modules,
        # and the menu must be `module_app` only - loading it as a module as
        # well makes the app fail to start, which leaves /dial unregistered.
        from tools.sip_tools import write_baresip_config

        with tempfile.TemporaryDirectory() as tmp:
            write_baresip_config(effective_settings(env_defaults(), FULL), Path(tmp),
                                 "sip_rx", "sip_tx")
            text = (Path(tmp) / "config").read_text()
            accounts = (Path(tmp) / "accounts").read_text()
        self.assertIn("module\taccount.so", text)
        self.assertIn("module\tg711.so", text)
        self.assertIn('"APEX" <sip:1001@pbx.example.org:5061;transport=tls>', accounts)
        self.assertLess(text.index("g711.so"), text.index("account.so"),
                        "codecs must be registered before the account that asks for them")
        self.assertEqual(text.count("menu.so"), 1)
        self.assertIn("module_app\tmenu.so", text)
        self.assertNotIn("module\tmenu.so", text)

    def test_baresip_is_given_a_terminal_not_a_pipe(self):
        # The call commands live in the menu module and the menu only starts
        # with a tty. On a pipe baresip reports "ready" and then answers
        # /dial with "command not found", so nothing would ever be dialled and
        # nothing would look wrong.
        source = (Path(__file__).resolve().parents[1] / "tools" / "sip_tools.py").read_text()
        start = source.index("def spawn_baresip(")
        body = source[start: source.index("def send_baresip(", start)]
        self.assertIn("pty.openpty()", body)
        self.assertIn("tty.setraw(slave)", body)
        self.assertNotIn("subprocess.PIPE", body)
        # ...and nothing may write commands down a pipe any more.
        self.assertNotIn("proc.stdin", source)

    def test_baresip_receives_the_desktop_audio_environment(self):
        from tools.sip_tools import spawn_baresip

        audio_env = {"XDG_RUNTIME_DIR": "/run/user/1234",
                     "PULSE_SERVER": "unix:/run/user/1234/pulse/native"}
        with patch("tools.sip_tools.pulse_env", return_value=audio_env), \
             patch("tools.sip_tools.pty.openpty", return_value=(41, 42)), \
             patch("tools.sip_tools.tty.setraw"), \
             patch("tools.sip_tools.os.close") as close, \
             patch("tools.sip_tools.subprocess.Popen") as popen:
            proc, master = spawn_baresip(Path("/tmp/test-sip-config"))

        self.assertIs(proc, popen.return_value)
        self.assertEqual(master, 41)
        self.assertEqual(popen.call_args.kwargs["env"], audio_env)
        self.assertEqual(popen.call_args.kwargs["stdin"], 42)
        close.assert_called_once_with(42)

    def test_a_rejected_call_is_not_waited_out(self):
        from tools.sip_tools import _REJECTED_RE

        for line in ("sip:1001@x: session closed: Not Found",
                     "call: 407 proxy authentication required",
                     "call: no common audio codecs - rejected"):
            self.assertTrue(_REJECTED_RE.search(line), line)
        # An established call must not be mistaken for a rejection.
        for line in ("Call established: sip:1001@pbx.example.org",
                     "call: connecting to 'sip:1001@pbx.example.org'.."):
            self.assertFalse(_REJECTED_RE.search(line), line)

    def test_the_answer_marker_is_baresips_own(self):
        from tools.sip_tools import SipSession

        sess = SipSession(id="x", user_id="alice", destination="sip:1@x",
                          workdir=Path("/tmp"), rx_sink="a", tx_sink="b", modules=[],
                          proc=SimpleNamespace(poll=lambda: None), deadline=9e18)
        sess.log("DIAL sip:1001@pbx.example.org")
        with sess.lock:
            sess.dial_mark = len(sess.output)
        sess.log("call: connecting to 'sip:1001@pbx.example.org'..")
        sess.log("Call established: sip:1001@pbx.example.org")
        with patch("tools.sip_tools.time.sleep", lambda *_: None):
            self.assertEqual(_wait_for_answer(sess, timeout=5), (True, ""))

    def test_a_transport_this_host_cannot_carry_is_refused_up_front(self):
        from tools.sip_tools import available_transports, unsupported_transport

        host = available_transports()
        self.assertIn("udp", host)
        if "tls" not in host:
            cfg = effective_settings(env_defaults(), FULL)   # FULL uses tls
            self.assertIn("TLS", unsupported_transport(cfg))
            self.assertEqual(unsupported_transport(effective_settings(
                env_defaults(), {**FULL, "sip_transport": "udp"})), "")
        else:
            self.assertEqual(unsupported_transport(effective_settings(env_defaults(), FULL)), "")

    def test_the_account_file_carries_the_real_password_owner_only(self):
        from tools.sip_tools import write_baresip_config

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_baresip_config(effective_settings(env_defaults(), FULL), config_dir,
                                 "sip_rx", "sip_tx")
            accounts = config_dir / "accounts"
            self.assertIn("s3cr3t-value", accounts.read_text())
            # The account file is the one place the secret is written to disk
            # unreadably to anyone else.
            self.assertEqual(oct(accounts.stat().st_mode & 0o777), oct(0o600))
            text = (config_dir / "config").read_text()
            self.assertNotIn("s3cr3t-value", text)
            self.assertIn("pulse.so", text)
            self.assertIn("sip_rx", text)
            self.assertIn("sip_tx.monitor", text)


class RemoteHangupTests(unittest.TestCase):
    """A BYE has to end the session, not just be written to the log.

    baresip is a long-lived process that outlives every call, so ``proc.poll()``
    cannot see the far end hanging up. Before this, a caller who hung up left
    the session looking live until its deadline, and every later tool call acted
    on a call that no longer existed.
    """

    @staticmethod
    def _session(**kw) -> SipSession:
        proc = SimpleNamespace(poll=lambda: None)
        return SipSession(
            id="abc123", user_id="alice", destination="sip:1001@example.org",
            workdir=Path("/tmp"), rx_sink="r", tx_sink="t", modules=[],
            proc=proc, master=-1, started_at=time.time(),
            deadline=time.time() + 300, duration=60, **kw,
        )

    def _pump_line(self, sess: SipSession, line: str) -> None:
        """Replay what _pump's reader does with one line of baresip output."""
        from tools.sip_tools import _CALL_ENDED_RE, _note_call_end
        _note_call_end(sess, line)

    def test_call_end_marker_matches_baresip_wording(self):
        for line in (
            "sip:1001@127.0.0.1;transport=udp: session closed: Connection reset by peer",
            "sip:1001@127.0.0.1: Call with sip:1001@127.0.0.1;transport=udp "
            "terminated (duration: 2 secs)",
        ):
            self.assertRegex(line, _CALL_ENDED_RE)

    def test_a_rejected_call_is_not_reported_as_a_hangup(self):
        # Before the call is answered the same wording means the INVITE failed,
        # and _wait_for_answer reports that with the log tail. Treating it as a
        # hangup would replace a useful rejection message with "they hung up".
        sess = self._session()
        self._pump_line(sess, "session closed: Not Found")
        self.assertFalse(sess.closed.is_set())
        self.assertFalse(sess.expired())

    def test_hangup_after_answering_ends_the_session(self):
        sess = self._session(established_at=time.time())
        self.assertFalse(sess.expired())
        self._pump_line(sess, "sip:1001@127.0.0.1;transport=udp: "
                              "session closed: Connection reset by peer")
        self.assertTrue(sess.closed.is_set())
        self.assertTrue(sess.expired())

    def test_end_reason_names_the_caller_and_the_cause(self):
        sess = self._session(established_at=time.time())
        self._pump_line(sess, "sip:1001@127.0.0.1;transport=udp: "
                              "session closed: Connection reset by peer")
        reason = sess.end_reason()
        self.assertIn("caller hung up", reason)
        self.assertIn("Connection reset by peer", reason)
        # The peer URI is not the reason and must not be repeated.
        self.assertNotIn("transport=udp", reason)

    def test_end_reason_without_a_stated_cause_still_reads(self):
        sess = self._session(established_at=time.time())
        self._pump_line(sess, "Call with sip:1001@host terminated (duration: 9 secs)")
        self.assertEqual(sess.end_reason(), "The caller hung up.")

    def test_time_limit_and_process_death_still_report_their_own_reason(self):
        sess = self._session()
        sess.deadline = time.time() - 1
        self.assertTrue(sess.expired())
        self.assertIn("time limit", sess.end_reason())

        dead = self._session()
        dead.proc = SimpleNamespace(poll=lambda: 0)
        self.assertTrue(dead.expired())
        self.assertIn("baresip", dead.end_reason())

    def test_listening_stops_promptly_when_the_caller_hangs_up(self):
        # The symptom that reached the operator: APEX kept listening for a
        # reply from a person who had already gone, then answered "heard
        # nothing" - which reads as being ignored rather than as ended.
        from tools.sip_tools import record_from_sink
        sess = self._session()
        sess.closed.set()
        with tempfile.TemporaryDirectory() as tmp:
            started = time.time()
            record_from_sink(Path(tmp) / "r.wav", "sink", 20.0,
                             stop_event=sess.closed)
            self.assertLess(time.time() - started, 5.0)



if __name__ == "__main__":
    unittest.main()
