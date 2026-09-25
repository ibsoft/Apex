"""Agent engine protocol: all engines emit the same stream of events so the
transport (SSE) and the frontend don't care whether the brain is a raw
Responses API loop, the OpenAI Agents SDK, or a LangGraph state machine."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from dataclasses import dataclass, field
from typing import Iterator, Optional

from tools.base import Tool, ToolContext, ToolRegistry


@dataclass
class AgentContext:
    user_id: str
    conversation_id: str
    system_prompt: str
    history: list[dict]                 # chat-shaped messages (no system)
    provider: object                    # ModelProvider instance
    provider_kind: str                  # openai | ollama | torch
    engine_name: str
    tools: ToolRegistry
    skill_tools: list[str]              # [] = all active
    memory: object = None
    runtime: dict = field(default_factory=dict)
    voice_mode: bool = False
    user_name: str = ""
    output_destination: str = ""
    focused_terminal: str = ""   # browser-focused terminal session id, if any

    def active_tools(self) -> list[Tool]:
        if not self.skill_tools:
            return self.tools.active()
        names = set(self.skill_tools) | {"notepad_control"}
        return [t for t in self.tools.active() if t.name in names]

    def tool_schemas(self) -> list[dict]:
        return [t.to_openai() for t in self.active_tools()]

    def make_tool_context(self) -> ToolContext:
        return ToolContext(
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            memory=self.memory,
            model=self.provider_kind,
            focused_terminal=self.focused_terminal,
            output_destination=self.output_destination,
        )


class AgentEngine:
    """Base class. Subclasses implement ``stream()`` yielding event dicts."""

    name = "base"

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx

    def stream(self) -> Iterator[dict]:
        raise NotImplementedError

    def _emit_tool_round(self, name: str, arguments: str):
        yield {
            "type": "tool_call",
            "name": name,
            "id": f"apex-{name}",
            "arguments": json.loads(arguments or "{}"),
        }
        try:
            out = self.ctx.tools.invoke(
                name,
                json.loads(arguments or "{}"),
                self.ctx.make_tool_context(),
            )
        except Exception as exc:
            out = f"tool error: {exc}"
        yield {
            "type": "tool_result",
            "name": name,
            "output": out,
        }


def run_async_generator(agen_factory, args, on_event):
    """Run an async generator in a daemon thread; relay events to `on_event`.

    Used by engines that are naturally async (OpenAI Agents SDK).
    """
    q: "queue.Queue[Optional[dict]]" = queue.Queue()
    errors: list[BaseException] = []

    def _run():
        async def main():
            agen = agen_factory(*args)
            async for event in agen:
                q.put(event)

        try:
            asyncio.run(main())
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
            q.put(None)
        finally:
            q.put(None)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    while True:
        item = q.get()
        if item is None:
            break
        on_event(item)
    if errors:
        raise errors[0]


__all__ = [
    "AgentContext",
    "AgentEngine",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "run_async_generator",
]