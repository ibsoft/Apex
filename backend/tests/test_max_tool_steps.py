"""The per-turn tool-step ceiling.

A skill named in MAX_TOOL_STEPS_EXEMPT (skill_creator) gets the larger
MAX_TOOL_STEPS_EXEMPT_LIMIT because its repair loop legitimately needs many
create -> validate -> test -> fix iterations; the default ceiling cuts it off
mid-repair. Every other skill keeps the default, and the exempt ceiling is
still a bound.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.base import AgentContext
from agent.responses import ResponsesEngine, _max_tool_steps
from tools.base import Tool, ToolRegistry


class _LoopingProvider:
    """Always asks for the same tool, so only the step ceiling ends the loop."""

    kind = "openai"

    def __init__(self, name: str = "noop"):
        self.name = name

    def chat_stream(self, messages, schemas=None):
        yield {"type": "tool_calls", "calls": [{
            "id": "c1", "name": self.name, "arguments": "{}",
        }]}


class MaxToolStepsTests(unittest.TestCase):
    def test_exempt_skill_gets_the_larger_ceiling(self):
        with patch("agent.responses.config.MAX_TOOL_STEPS", 12), \
             patch("agent.responses.config.MAX_TOOL_STEPS_EXEMPT", ["skill_creator"]), \
             patch("agent.responses.config.MAX_TOOL_STEPS_EXEMPT_LIMIT", 100):
            self.assertEqual(
                _max_tool_steps(SimpleNamespace(skill_name="skill_creator")), 100)

    def test_ordinary_skill_keeps_the_default(self):
        with patch("agent.responses.config.MAX_TOOL_STEPS", 12), \
             patch("agent.responses.config.MAX_TOOL_STEPS_EXEMPT", ["skill_creator"]), \
             patch("agent.responses.config.MAX_TOOL_STEPS_EXEMPT_LIMIT", 100):
            self.assertEqual(_max_tool_steps(SimpleNamespace(skill_name="shell")), 12)

    def test_no_skill_name_is_not_exempt(self):
        with patch("agent.responses.config.MAX_TOOL_STEPS", 12), \
             patch("agent.responses.config.MAX_TOOL_STEPS_EXEMPT", ["skill_creator"]):
            self.assertEqual(_max_tool_steps(SimpleNamespace()), 12)

    def test_the_engine_loop_stops_at_the_exempt_ceiling(self):
        registry = ToolRegistry()
        registry.register(Tool(
            name="noop", description="", parameters={"type": "object", "properties": {}},
            handler=lambda args, ctx: "ok",
        ))
        ctx = AgentContext(
            user_id="u", conversation_id="c", system_prompt="p", history=[],
            provider=_LoopingProvider(), provider_kind="openai", engine_name="responses",
            tools=registry, skill_tools=[], skill_name="skill_creator",
        )
        with patch("agent.responses.config.MAX_TOOL_STEPS_EXEMPT", ["skill_creator"]), \
             patch("agent.responses.config.MAX_TOOL_STEPS_EXEMPT_LIMIT", 3):
            events = list(ResponsesEngine(ctx).stream())
        calls = [e for e in events if e.get("type") == "tool_call"]
        self.assertEqual(len(calls), 3)
        errors = [e for e in events if e.get("type") == "error"]
        self.assertTrue(errors)
        self.assertIn("3", errors[0]["message"])

    def test_an_ordinary_turn_uses_the_default_ceiling(self):
        registry = ToolRegistry()
        registry.register(Tool(
            name="noop", description="", parameters={"type": "object", "properties": {}},
            handler=lambda args, ctx: "ok",
        ))
        ctx = AgentContext(
            user_id="u", conversation_id="c", system_prompt="p", history=[],
            provider=_LoopingProvider(), provider_kind="openai", engine_name="responses",
            tools=registry, skill_tools=[], skill_name="shell",
        )
        with patch("agent.responses.config.MAX_TOOL_STEPS", 2), \
             patch("agent.responses.config.MAX_TOOL_STEPS_EXEMPT", ["skill_creator"]), \
             patch("agent.responses.config.MAX_TOOL_STEPS_EXEMPT_LIMIT", 100):
            events = list(ResponsesEngine(ctx).stream())
        calls = [e for e in events if e.get("type") == "tool_call"]
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
