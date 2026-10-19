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


# Tools available in EVERY skill, whatever its `tools:` list says.
#
# notepad_control drives the live editor, terminal_command / terminal_sessions
# let the model act on the machine, and the task tools let it schedule its own
# future work. None of them is a specialist capability: a skill that "cannot
# create a task" would answer "I can't schedule that" to an ordinary request,
# and asking the operator to open a terminal first is not an acceptable answer
# either. A deployment turns the task tools off with TASKS_ENABLED, which
# unregisters them, so this constant is a floor and not a grant.
ALWAYS_ON_TOOLS = frozenset({
    "notepad_control",
    "terminal_command",
    "terminal_sessions",
    "task_schedule",
    "task_list",
    "task_status",
    "task_update",
    "task_delete",
    "task_run_now",
    "task_index",
    # The music/video players are the same class of capability as the terminal:
    # "play some jazz" must not depend on which skill the router picked, and a
    # compound request ("run df -h and put on some music") has to reach the
    # player even though the host-state skill owns that turn. The handler gates
    # itself on YOUTUBE_ENABLED, so inclusion is never a grant of anything that
    # a deployment turned off.
    "media_player",
})

#: Telegram tools are force-included only while the bot is enabled: unlike the
#: always-on floor above, they are a capability a deployment can be *drifting*
#: past (a token not yet stored, a chat not yet paired). Leaving them inert
#: would make the model promise messages that can never be sent.
TELEGRAM_TOOLS = frozenset({"telegram_send", "telegram_status", "telegram_send_file"})

#: Integration tools are force-included for every skill while the feature is on,
#: the same reasoning as terminal_command: the operator should not have to pick a
#: skill before the agent can look something up in a connected database or MCP
#: server. Registration itself is gated by INTEGRATIONS_ENABLED.
INTEGRATION_TOOLS = frozenset({
    "integration_query",
    "integration_discover",
    "integration_status",
})

#: Audit-log tools are force-included for every skill while the feature is on,
#: so "was that suspicious?" or "what happened?" works without the operator
#: picking a skill first. Registration itself is gated by OBSERVABILITY_ENABLED.
OBSERVABILITY_TOOLS = frozenset({"log_query", "security_audit"})

#: Sub-agent tools are force-included for every skill while the feature is on:
#: managing and delegating to sub-agents is a first-class capability, not a
#: skill the operator must select. Registration is gated by SUBAGENTS_ENABLED.
#: A sub-agent turn excludes these (see subagents.runner) so an agent cannot
#: recursively delegate to yet another agent.
SUBAGENT_TOOLS = frozenset({
    "subagent_list",
    "subagent_create",
    "subagent_update",
    "subagent_delete",
    "subagent_status",
    "subagent_delegate",
    "subagent_fanout",
    "subagent_cleanup",
    "subagent_tools",
})


def _runtime_truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


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
    skill_name: str = ""                # active skill, for per-skill policy
    memory: object = None
    runtime: dict = field(default_factory=dict)
    voice_mode: bool = False
    user_name: str = ""
    output_destination: str = ""
    focused_terminal: str = ""   # browser-focused terminal session id, if any
    # Session ids in the operator's on-screen order, so terminal_command reads
    # `terminal=N` against the number painted in the title bar.
    terminal_map: list[str] = field(default_factory=list)
    # Skill frontmatter require_tool: force at least one tool call on the first
    # model call of a turn, so a skill whose contract is "run it, don't explain
    # it" cannot quietly answer from training data instead.
    require_tool: bool = False
    # Skills may veto tools entirely (see Skill.exclude_tools).
    exclude_tools: list[str] = field(default_factory=list)
    # Sub-agent turns: `skill_tools` is the agent's exact tool list and nothing
    # is force-included, so a read-only agent does not silently get terminal.
    strict_tools: bool = False
    # Working directory for shell/terminal work. Empty means "use the default
    # (repo/home) behaviour". A sub-agent sets this to its own workspace.
    workspace: str = ""
    # Set only for a scheduled task that explicitly asks to place a phone call.
    autonomous_call_authorized: bool = False
    # Events queued by tools through ToolContext.emit. Engines drain them right
    # after the tool result so mid-tool events (a terminal window that had to be
    # opened, a memory write) reach the browser in causal order.
    pending_events: list[dict] = field(default_factory=list)

    def push_event(self, event: dict) -> None:
        """Queue a mid-tool event for the engine to yield to the client."""
        if isinstance(event, dict) and event.get("type"):
            self.pending_events.append(event)

    def drain_events(self) -> list[dict]:
        queued, self.pending_events = self.pending_events, []
        return queued

    def active_tools(self) -> list[Tool]:
        banned = set(self.exclude_tools or ())
        if self.strict_tools:
            # A sub-agent's tool list is authoritative: exactly these, nothing
            # force-included, so scoping an agent actually scopes it.
            names = set(self.skill_tools or ())
            return [
                t for t in self.tools.active()
                if t.name in names and t.name not in banned
            ]
        if not self.skill_tools:
            pool = self.tools.active()
            if banned:
                return [t for t in pool if t.name not in banned]
            return pool
        names = (set(self.skill_tools) | ALWAYS_ON_TOOLS | INTEGRATION_TOOLS
                 | OBSERVABILITY_TOOLS | SUBAGENT_TOOLS)
        if _runtime_truthy(self.runtime.get("telegram_enabled")):
            names |= TELEGRAM_TOOLS
        return [
            t for t in self.tools.active()
            if t.name in names and t.name not in banned
        ]

    def tool_schemas(self) -> list[dict]:
        return [t.to_openai() for t in self.active_tools()]

    def make_tool_context(self) -> ToolContext:
        return ToolContext(
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            memory=self.memory,
            model=self.provider_kind,
            provider=self.provider,
            focused_terminal=self.focused_terminal,
            terminal_map=self.terminal_map,
            output_destination=self.output_destination,
            workspace=self.workspace,
            autonomous_call_authorized=self.autonomous_call_authorized,
            emit=self.push_event,
        )


class AgentEngine:
    """Base class. Subclasses implement ``stream()`` yielding event dicts."""

    name = "base"

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx

    def stream(self) -> Iterator[dict]:
        raise NotImplementedError

    def _drained(self) -> Iterator[dict]:
        """Yield whatever tools queued through ctx.emit during the last call."""
        yield from self.ctx.drain_events()

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
        yield from self._drained()


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