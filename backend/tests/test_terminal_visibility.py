"""The model-opened terminal must be visible in a window, not just in the chat.

`terminal_command` auto-opens a PTY and announces it with a `terminal_opened`
event. Two things have to hold for the operator to actually SEE the command:

1. the event reaches the SSE stream, and
2. the session's pty buffer still holds the command and its output afterwards,
   because the browser window drains it from cursor 0 once it attaches.

The window usually attaches *after* the tool finished (the event is drained
after the tool result, and the tool waits for output first), so anything that
consumes the buffer in between silently blanks the window.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.base import AgentContext
from agent.responses import ResponsesEngine
from tools.base import ToolRegistry
from tools.terminal_server import _TERMINALS
from tools.terminal_tools import build_terminal_tools


class ScriptedProvider:
    """Replays one terminal_command tool call, then finishes."""

    kind = "openai"

    def __init__(self, name="terminal_command", arguments=None):
        self._name = name
        self._arguments = arguments or {"command": "echo VISIBLE-IN-WINDOW"}
        self._step = 0

    def chat_stream(self, messages, schemas=None):
        self._step += 1
        if self._step == 1:
            yield {"type": "tool_calls", "calls": [{
                "id": "c1", "name": self._name, "arguments": json.dumps(self._arguments),
            }]}
        else:
            yield {"type": "text", "content": "done"}
            yield {"type": "usage", "input": 1, "output": 1}


class ModelOpenedTerminalIsVisibleTests(unittest.TestCase):
    def setUp(self):
        _TERMINALS.clear()
        self.cfg = SimpleNamespace(ENABLE_RUN_SHELL=True, TERMINAL_IDLE_SECONDS=300)
        registry = ToolRegistry()
        for tool in build_terminal_tools(self.cfg):
            registry.register(tool)
        self.registry = registry

    def tearDown(self):
        for sess in list(_TERMINALS.values()):
            try:
                sess.close()
            except Exception:
                pass
        _TERMINALS.clear()

    def _run(self, arguments=None):
        provider = ScriptedProvider(arguments=arguments)
        ctx = AgentContext(
            user_id="alice", conversation_id="c1", system_prompt="p", history=[],
            provider=provider, provider_kind="openai", engine_name="responses",
            tools=self.registry, skill_tools=[],
        )
        events = list(ResponsesEngine(ctx).stream())
        return events

    def _window_view(self, session_id: str) -> str:
        """Exactly what a window that just attached would render."""
        sess = _TERMINALS[session_id]
        cursor, chunk, _ = sess.drain(0)
        return chunk.decode("utf-8", "replace")

    def test_stream_announces_the_opened_terminal(self):
        events = self._run()
        opened = [e for e in events if e.get("type") == "terminal_opened"]
        self.assertEqual(len(opened), 1, [e.get("type") for e in events])
        self.assertTrue(opened[0]["terminal_id"])
        # The command output also comes back to the model (that is the chat).
        results = [e for e in events if e.get("type") == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertIn("VISIBLE-IN-WINDOW", results[0]["output"])

    def test_the_window_still_receives_the_command_and_its_output(self):
        events = self._run()
        session_id = next(e for e in events if e.get("type") == "terminal_opened")["terminal_id"]
        view = self._window_view(session_id)
        self.assertIn("echo VISIBLE-IN-WINDOW", view, "the window must show the command")
        self.assertIn("VISIBLE-IN-WINDOW", view, "the window must show the output")

    def test_a_second_command_in_the_same_turn_is_also_visible(self):
        # The model opening one window and then running in it is the common
        # shape; both commands must reach that window.
        events = self._run()
        session_id = next(e for e in events if e.get("type") == "terminal_opened")["terminal_id"]
        self.registry.invoke(
            "terminal_command", {"command": "echo SECOND-VISIBLE"},
            self._ctx_for(session_id))
        view = self._window_view(session_id)
        self.assertIn("echo VISIBLE-IN-WINDOW", view)
        self.assertIn("echo SECOND-VISIBLE", view)
        self.assertIn("SECOND-VISIBLE", view)

    def _ctx_for(self, session_id: str):
        from tools.base import ToolContext

        return ToolContext(user_id="alice", conversation_id="c1")

    def test_opening_several_windows_announces_every_one(self):
        events = self._run({"command": "echo PARALLEL", "count": 3})
        opened = [e["terminal_id"] for e in events if e.get("type") == "terminal_opened"]
        self.assertEqual(len(opened), 3, events)
        self.assertEqual(len(set(opened)), 3, "each window needs its own session")
        for session_id in opened:
            self.assertIn(session_id, _TERMINALS)


if __name__ == "__main__":
    unittest.main()
