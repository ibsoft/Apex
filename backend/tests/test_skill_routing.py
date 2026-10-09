"""Live-host questions must reach the skill that actually runs commands.

The LLM router routed "check out our internet connection" to `general`. The
`general` skill has no reason to call a tool, so the model answered from
training data and reported a ping that never happened. The prompt said it must
measure; the model weighed that against a cheaper-looking option and won.

So this decision cannot be a model judgement. It is keyword-based, runs before
the LLM router, and therefore before the router cache, and it only defers when
the message is genuinely about writing code.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skills.manager import (  # noqa: E402
    HOST_STATE_SKILL,
    Skill,
    SkillManager,
    force_email_skill,
    force_host_skill,
    route_skill,
)

DEFINITIONS = Path(__file__).resolve().parents[1] / "skills" / "definitions"

EMAIL_SKILL = Skill(
    name="email_manager",
    description="Check and send POP3/SMTP email in the background.",
    system_prompt="Operate the mailbox.",
    tools=["run_shell"],
)


class ExplodingProvider:
    """Any LLM call is a bug here - the decision must be deterministic."""

    def chat_stream(self, *a, **kw):  # pragma: no cover
        raise AssertionError("router must not be consulted for host questions")

    def chat(self, *a, **kw):  # pragma: no cover
        raise AssertionError("router must not be consulted for host questions")


class ForceHostSkillTests(unittest.TestCase):
    def setUp(self):
        self.skills = SkillManager(DEFINITIONS).all()

    def _force(self, text: str):
        return force_host_skill(text, self.skills)

    def test_the_reported_phrasings_are_forced(self):
        # These are the phrasings that actually reached the model as `general`.
        for text in (
            "check out our internet connection",
            "check our network status",
            "check out our internet connection for internet",
            "am I online",
            "is the internet working",
            "check the wifi",
            "test my connection speed",
            "check dns",
            "check the network adapter",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._force(text), HOST_STATE_SKILL)

    def test_the_actual_live_messages_are_forced(self):
        # Taken verbatim from the failing conversations in the message store,
        # typos included. These are the strings that reached the model as
        # `general` and produced a fabricated ping result.
        for text in (
            "check out our internet connection",
            "check our network conenction for interne",
            "check our network status",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._force(text), HOST_STATE_SKILL)

    def test_typos_do_not_defeat_the_guard(self):
        for text in (
            "check our netwrok conenction",
            "is the interent working",
            "check the termnal disk usage",
            "disk usagge",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._force(text), HOST_STATE_SKILL)

    def test_network_tools_and_addresses_are_forced(self):
        for text in (
            "run nmap on 192.168.1.3",
            "is 192.168.1.1 the gateway",
            "traceroute to 8.8.8.8",
            "show me netstat",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._force(text), HOST_STATE_SKILL)

    def test_other_host_state_is_forced(self):
        for text in (
            "show disk usage",
            "how much memory is free",
            "which ports are listening",
            "is systemd failing",
            "show me the last system logs",
            "what is the hostname",
            "check the battery",
            "open a terminal",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._force(text), HOST_STATE_SKILL)

    def test_ordinary_chat_is_left_to_the_router(self):
        for text in (
            "hello",
            "what is your name",
            "who won the match last night",
            "thanks!",
            "write me a poem about rain",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self._force(text))

    def test_code_work_is_not_hijacked(self):
        # Code turns may still use a terminal, but the message is not asking
        # about the state of the host, so the guard must stay out of the way.
        for text in (
            "fix the failing unit test in api_client.py",
            "write a python function to parse json",
            "why does this regex fail?",
            "refactor the database schema",
            "explain the stack trace from my flask api",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self._force(text))

    def test_it_degrades_gracefully_without_a_shell_skill(self):
        self.assertIsNone(force_host_skill("check the internet",
                                           SkillManager(DEFINITIONS).all()
                                           and [s for s in self.skills
                                                if s.name != HOST_STATE_SKILL]))

    def test_empty_text_is_safe(self):
        self.assertIsNone(self._force(""))
        self.assertIsNone(self._force("   "))


class ForceEmailSkillTests(unittest.TestCase):
    """Mail requests must reach the skill that can actually send.

    The host-state guard would otherwise force "run df -h and email me the
    result" to the `shell` skill, which owns the terminal but has no way to send
    mail. The email skill is user-made, so it is discovered by name/description.
    """

    def setUp(self):
        self.skills = SkillManager(DEFINITIONS).all() + [EMAIL_SKILL]

    def _force(self, text):
        return force_email_skill(text, self.skills)

    def test_mail_requests_are_forced(self):
        for text in (
            "check my email",
            "send the results to ops@example.com",
            "mail me the report",
            "what is in my inbox",
            "run df -h and email me the result",
            "send the output of the scan to a@b.gr",
            "στείλε τα αποτελέσματα στο email μου",
            "στείλε μου τα αποτελέσματα με ηλεκτρονικό ταχυδρομείο",
            "στείλε τα στο μέιλ μου",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._force(text), "email_manager")

    def test_ordinary_host_work_is_left_alone(self):
        for text in (
            "show disk usage",
            "check out our internet connection",
            "ping 192.168.1.3",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self._force(text))

    def test_code_about_email_is_not_hijacked(self):
        for text in (
            "write a python function to send email",
            "explain the smtp stack trace",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self._force(text))

    def test_no_email_skill_means_no_routing(self):
        without = [s for s in self.skills if s.name != "email_manager"]
        self.assertIsNone(force_email_skill("check my email", without))
        self.assertIsNone(force_email_skill("", self.skills))
        self.assertIsNone(force_email_skill("   ", self.skills))


class RouteSkillBypassesTheModelTests(unittest.TestCase):
    def setUp(self):
        self.skills = SkillManager(DEFINITIONS).all()

    def test_route_skill_never_calls_the_model_for_host_questions(self):
        got = route_skill(
            "check out our internet connection",
            self.skills,
            ExplodingProvider(),
            fallback="general",
        )
        self.assertEqual(got, HOST_STATE_SKILL)

    def test_the_router_cache_cannot_pin_a_host_question_to_general(self):
        # A previous (wrong) routing decision must not survive as a cache hit.
        from skills import manager

        key = "check out our internet connection"
        manager._ROUTER_CACHE[key] = "general"
        try:
            got = route_skill(key, self.skills, ExplodingProvider(),
                              fallback="general")
            self.assertEqual(got, HOST_STATE_SKILL)
        finally:
            manager._ROUTER_CACHE.pop(key, None)

    def test_run_command_and_email_beats_the_host_guard(self):
        # The command is host work, but the destination is mail: the email
        # skill has run_shell, so it can do both; the shell skill cannot send.
        skills = self.skills + [EMAIL_SKILL]
        got = route_skill("run df -h and email me the result", skills,
                          ExplodingProvider(), fallback="general")
        self.assertEqual(got, "email_manager")


if __name__ == "__main__":
    unittest.main()
