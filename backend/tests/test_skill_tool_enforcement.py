"""A skill that promises to DO things must not be able to quietly not do them.

Two independent failures motivated this:

1. "check out our internet connection" was answered with a fabricated ping
   (8.8.8.8, 0% loss, 20ms avg) and `"tools": []` in the stored metadata. The
   model had a prompt telling it to use a shell tool, treated that as advice,
   and answered from training data.
2. The operator's requirement is that every command run is VISIBLE in a
   terminal window. `run_shell` is headless, so offering it to the sysadmin
   skill made "always visible" a matter of the model's mood.

So the fix is enforced in code, not prose:
  * Skill.require_tool  -> engine passes tool_choice="required" on step 0
  * Skill.exclude_tools -> the tool is never offered to the model at all
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from types import SimpleNamespace  # noqa: E402

from agent.base import AgentContext  # noqa: E402
from agent.responses import ResponsesEngine  # noqa: E402
from skills.manager import SkillManager, _tool_list  # noqa: E402
from tools.base import ToolRegistry  # noqa: E402
from tools.core_tools import build_core_tools  # noqa: E402
from tools.terminal_tools import build_terminal_tools  # noqa: E402

DEFINITIONS = Path(__file__).resolve().parents[1] / "skills" / "definitions"


def _registry() -> ToolRegistry:
    """A registry that really has run_shell, so its absence means something."""
    cfg = SimpleNamespace(ENABLE_RUN_SHELL=True, TERMINAL_IDLE_SECONDS=300)
    reg = ToolRegistry()
    for tool in build_terminal_tools(cfg):
        reg.register(tool)
    # run_shell lives in the core set, so it has to be present for the
    # exclusion tests to mean anything.
    for tool in build_core_tools(reg, cfg):
        reg.register(tool)
    return reg


def _ctx(**kw) -> AgentContext:
    base = dict(
        user_id="u",
        conversation_id="c",
        system_prompt="s",
        history=[],
        provider=object(),
        provider_kind="openai",
        engine_name="responses",
        tools=_registry(),
        skill_tools=[],
    )
    base.update(kw)
    return AgentContext(**base)


class ExcludeToolsTests(unittest.TestCase):
    def test_tool_list_accepts_string_and_yaml_list(self):
        self.assertEqual(_tool_list("run_shell"), ["run_shell"])
        self.assertEqual(_tool_list("a, b ,c"), ["a", "b", "c"])
        self.assertEqual(_tool_list(["a", " b "]), ["a", "b"])
        self.assertEqual(_tool_list(None), [])
        self.assertEqual(_tool_list(""), [])

    def test_exclusion_removes_the_tool_from_an_all_tools_skill(self):
        # tools=[] means "all active", which is how shell.md is written. The
        # exclusion has to survive that, otherwise it does nothing.
        ctx = _ctx(skill_tools=[], exclude_tools=["run_shell"])
        names = [t.name for t in ctx.active_tools()]
        self.assertIn("terminal_command", names)
        self.assertNotIn("run_shell", names)

    def test_exclusion_also_applies_to_an_explicit_tool_list(self):
        ctx = _ctx(
            skill_tools=["run_shell", "terminal_command"],
            exclude_tools=["run_shell"],
        )
        names = [t.name for t in ctx.active_tools()]
        self.assertNotIn("run_shell", names)
        self.assertIn("terminal_command", names)

    def test_excluding_something_absent_is_harmless(self):
        ctx = _ctx(skill_tools=[], exclude_tools=["not_a_tool"])
        self.assertIn("terminal_command", [t.name for t in ctx.active_tools()])


class RecordingProvider:
    """Records the kwargs of every call so the test can assert on tool_choice."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat_stream(self, messages, tools=None, tool_choice=None):
        self.calls.append(
            {"n": len(self.calls), "tool_choice": tool_choice,
             "has_tools": bool(tools)}
        )
        if self.script:
            return iter(self.script.pop(0))
        return iter([{"type": "text", "content": "done"}])


class ToolChoiceEnforcementTests(unittest.TestCase):
    def _run(self, require_tool: bool, script):
        prov = RecordingProvider(script)
        ctx = _ctx(provider=prov, require_tool=require_tool)
        list(ResponsesEngine(ctx).stream())
        return prov.calls

    def test_a_skill_that_must_act_gets_required_on_the_first_call(self):
        call = {
            "type": "tool_calls",
            "calls": [{"id": "c1", "name": "current_time", "arguments": "{}"}],
        }
        calls = self._run(True, [[call]])
        self.assertEqual(calls[0]["tool_choice"], "required")

    def test_later_calls_are_free_to_just_answer(self):
        # Otherwise every turn would loop: forced tool -> result -> forced
        # tool -> ... and the user would watch the agent run commands forever.
        call = {
            "type": "tool_calls",
            "calls": [{"id": "c1", "name": "current_time", "arguments": "{}"}],
        }
        calls = self._run(True, [[call], [{"type": "text", "content": "hi"}]])
        self.assertEqual(calls[0]["tool_choice"], "required")
        self.assertIsNone(calls[1]["tool_choice"])

    def test_ordinary_skills_are_never_forced(self):
        calls = self._run(False, [[{"type": "text", "content": "hello"}]])
        self.assertIsNone(calls[0]["tool_choice"])

    def test_no_forcing_when_the_skill_has_no_tools(self):
        # tool_choice="required" with no tools is an API error, so the engine
        # must gate on there being something to call.
        prov = RecordingProvider([[{"type": "text", "content": "hello"}]])
        ctx = _ctx(provider=prov, require_tool=True, tools=ToolRegistry())
        list(ResponsesEngine(ctx).stream())
        self.assertFalse(prov.calls[0]["has_tools"])
        self.assertIsNone(prov.calls[0]["tool_choice"])


class ShellSkillVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.mgr = SkillManager(DEFINITIONS)
        self.shell = self.mgr.select("shell")

    def test_shell_skill_must_actually_use_a_tool(self):
        self.assertTrue(
            self.shell.require_tool,
            "shell.md must set require_tool: true or the model may answer "
            "live-state questions from memory",
        )

    def test_shell_skill_is_denied_the_headless_shell(self):
        self.assertIn("run_shell", self.shell.exclude_tools)

    def test_shell_prompt_never_offers_run_shell_as_an_option(self):
        body = (DEFINITIONS / "shell.md").read_text(encoding="utf-8")
        # The only allowed mentions are the frontmatter veto and the sentence
        # explaining that it is unavailable.
        for line in body.splitlines():
            if "run_shell" not in line:
                continue
            if line.startswith("exclude_tools:"):
                continue
            with self.subTest(line=line[:70]):
                self.assertRegex(
                    line, r"(?i)(unavailable|no headless fallback|deliberately)",
                    "shell.md still tells the model to use run_shell",
                )

    def test_shell_prompt_points_at_the_visible_terminal(self):
        body = (DEFINITIONS / "shell.md").read_text(encoding="utf-8")
        self.assertIn("terminal_command", body)
        self.assertIn("visibl", body.lower())  # visible / visibly


if __name__ == "__main__":
    unittest.main()
