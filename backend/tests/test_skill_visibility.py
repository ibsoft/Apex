"""Skills must keep the operator's output VISIBLE, not just in the chat.

`run_shell` is headless: everything it produces lands in the chat bubble and
nowhere else. `terminal_command` runs in a window the user can watch and scroll.
When a skill prompt steers routine commands at `run_shell`, the user asks for
something ("check the network status") and sees a chat message with no terminal
window at all — the failure this guards against.

So: every skill that can run commands must say which tool is the default, and
must still tell the model to open a terminal itself rather than ask.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFINITIONS = Path(__file__).resolve().parents[1] / "skills" / "definitions"

# Skills that legitimately hand the user to run_shell for hidden/background work.
# They still have to name terminal_command as the visible default.
HEADLESS_OK = {"VAPT.md"}


def _text(name: str) -> str:
    return (DEFINITIONS / name).read_text(encoding="utf-8")


def _skills_mentioning_run_shell() -> list[str]:
    return sorted(p.name for p in DEFINITIONS.glob("*.md") if "run_shell" in p.read_text(encoding="utf-8"))


class VisibleTerminalDefaultTests(unittest.TestCase):
    def test_some_skills_can_reach_run_shell(self):
        # Guards the guard: if this ever comes back empty the test below is
        # silently passing for the wrong reason.
        self.assertIn("general.md", _skills_mentioning_run_shell())

    def test_skills_that_mention_run_shell_also_name_the_visible_default(self):
        for name in _skills_mentioning_run_shell():
            if name in HEADLESS_OK:
                continue
            with self.subTest(skill=name):
                body = _text(name)
                self.assertIn(
                    "terminal_command", body,
                    f"{name} mentions run_shell but never names terminal_command",
                )
                self.assertRegex(
                    body, r"(?i)(headless|hidden|invisible)",
                    f"{name} must explain that run_shell output is headless",
                )

    def test_the_main_skills_prefer_the_visible_terminal(self):
        for name in ("general.md", "code.md"):
            with self.subTest(skill=name):
                body = _text(name).lower()
                self.assertIn("terminal_command", body)
                # ...and the reason must be stated, not just the preference.
                self.assertTrue(
                    "headless" in body or "chat" in body,
                    f"{name} must say why the visible terminal is preferred",
                )

    def test_no_skill_tells_the_user_to_open_a_terminal_first(self):
        # The behaviour the whole terminal feature exists to prevent. The phrase
        # is allowed ONLY as a prohibition ("NEVER ask the user to open a
        # terminal first"), so each occurrence must sit inside a negation.
        phrase = re.compile(
            r"(?i)(ask the (?:user|operator) to (?:say|open)\s*[\"']?open\s*[\"']?terminal"
            r"|open a terminal first|please open (?:a |your )?terminal)",
        )
        negation = re.compile(r"(?i)\b(never|do not|don't|no need to|not)\b")
        for path in sorted(DEFINITIONS.glob("*.md")):
            body = path.read_text(encoding="utf-8")
            for match in phrase.finditer(body):
                with self.subTest(skill=path.name, phrase=match.group(0)):
                    before = body[max(0, match.start() - 30):match.start()]
                    self.assertRegex(
                        before, negation,
                        f"{path.name} tells the user to open a terminal: {match.group(0)!r}",
                    )

    def test_skills_that_run_commands_say_opening_is_automatic(self):
        for name in ("general.md", "shell.md"):
            with self.subTest(skill=name):
                body = _text(name).lower()
                self.assertIn("opens one by itself", body)
                self.assertIn("never ask", body)

    def test_state_questions_must_be_measured_not_remembered(self):
        """A question about the host's live state must be answered by running a
        command. Without this rule the model answers from training data and
        invents the numbers — a reported ping result that never happened.
        """
        for name in ("general.md", "shell.md"):
            with self.subTest(skill=name):
                body = _text(name).lower()
                needle = (
                    "never answer such a question from memory"
                    if name == "general.md"
                    else "never invent measurements"
                )
                self.assertIn(
                    needle, body,
                    f"{name} must forbid answering live-state questions from memory",
                )
                # ...and it must name the tools that measure those things.
                self.assertIn("terminal_command", body)

    def test_the_measure_rule_covers_the_usual_diagnostics(self):
        body = _text("general.md").lower()
        for topic in ("connectivity", "dns", "disk", "memory", "ports"):
            with self.subTest(topic=topic):
                self.assertIn(topic, body)


if __name__ == "__main__":
    unittest.main()
