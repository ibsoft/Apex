"""Tools can push events to the browser through ToolContext.emit.

The queue is AgentContext-level: a tool calls ctx.emit(...) and the engine
yields whatever it queued right after that tool's result, so mid-tool events
reach the SSE stream in causal order instead of being dropped.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.base import AgentContext, AgentEngine
from tools.base import Tool, ToolContext, ToolRegistry


def make_ctx() -> AgentContext:
    return AgentContext(
        user_id="alice",
        conversation_id="c1",
        system_prompt="p",
        history=[],
        provider=object(),
        provider_kind="openai",
        engine_name="responses",
        tools=[],
        skill_tools=[],
    )


class EmitWiringTests(unittest.TestCase):
    def test_make_tool_context_actually_carries_emit(self):
        # This used to be left unset, so every ctx.emit() in the codebase was a
        # silent no-op and tools could never talk to the UI.
        ctx = make_ctx()
        self.assertIsNotNone(ctx.make_tool_context().emit)

    def test_push_event_queues_only_real_events(self):
        ctx = make_ctx()
        tool_ctx = ctx.make_tool_context()
        tool_ctx.emit({"type": "terminal_opened", "terminal_id": "abc"})
        tool_ctx.emit({"terminal_id": "no-type"})   # ignored: no event type
        tool_ctx.emit("not a dict")                 # ignored
        tool_ctx.emit(None)                         # ignored
        self.assertEqual(
            ctx.drain_events(), [{"type": "terminal_opened", "terminal_id": "abc"}]
        )

    def test_drain_empties_the_queue(self):
        ctx = make_ctx()
        ctx.push_event({"type": "memory"})
        self.assertEqual(len(ctx.drain_events()), 1)
        self.assertEqual(ctx.drain_events(), [])

    def test_a_tool_can_emit_through_its_context(self):
        ctx = make_ctx()

        def handler(args, tool_ctx: ToolContext) -> str:
            tool_ctx.emit({"type": "terminal_opened", "terminal_id": "s1"})
            return "ran it"

        tool = Tool("demo", "demo", {"type": "object", "properties": {}}, handler)
        out = tool.call({}, ctx.make_tool_context())
        self.assertEqual(out, "ran it")
        self.assertEqual(ctx.drain_events(), [{"type": "terminal_opened", "terminal_id": "s1"}])


class EngineDrainTests(unittest.TestCase):
    def test_engine_drain_yields_queued_events(self):
        ctx = make_ctx()
        ctx.push_event({"type": "terminal_opened", "terminal_id": "s1"})
        self.assertEqual(
            list(AgentEngine(ctx)._drained()),
            [{"type": "terminal_opened", "terminal_id": "s1"}],
        )
        # A second drain must not repeat them.
        self.assertEqual(list(AgentEngine(ctx)._drained()), [])


if __name__ == "__main__":
    unittest.main()


class ActiveToolsTests(unittest.TestCase):
    """Every skill can open a terminal, so the model never has to ask for one."""

    def _ctx(self, skill_tools: list[str]) -> AgentContext:
        noop = lambda args, ctx: ""
        registry = ToolRegistry()
        for tool in (
            Tool("terminal_command", "t", {"type": "object", "properties": {}}, noop),
            Tool("terminal_sessions", "t", {"type": "object", "properties": {}}, noop),
            Tool("notepad_control", "n", {"type": "object", "properties": {}}, noop),
            Tool("web_search", "w", {"type": "object", "properties": {}}, noop),
        ):
            registry.register(tool)
        return AgentContext(
            user_id="alice", conversation_id="c1", system_prompt="p", history=[],
            provider=object(), provider_kind="openai", engine_name="responses",
            tools=registry, skill_tools=skill_tools,
        )

    def test_terminal_tools_are_available_in_a_skill_that_lists_nothing(self):
        # A skill scoped to, say, translation must still be able to run a
        # command instead of telling the user to open a terminal.
        names = {t.name for t in self._ctx(["web_search"]).active_tools()}
        self.assertEqual(names, {"web_search", "notepad_control",
                                 "terminal_command", "terminal_sessions"})

    def test_skill_tools_still_win_and_nothing_else_leaks_in(self):
        names = {t.name for t in self._ctx(["web_search"]).active_tools()}
        self.assertNotIn("terminal_sessions_x", names)
        self.assertEqual(len(names), 4)

    def test_no_skill_filter_means_every_active_tool(self):
        names = {t.name for t in self._ctx([]).active_tools()}
        self.assertEqual(names, {"terminal_command", "terminal_sessions",
                                 "notepad_control", "web_search"})
