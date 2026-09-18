"""Engine factory: pick the requested engine and assemble the AgentContext."""
from __future__ import annotations

from agent.agents_sdk import AgentsSdkEngine
from agent.base import AgentContext, AgentEngine, ToolRegistry
from agent.langgraph import LangGraphEngine
from agent.responses import ResponsesEngine


def build_engine(
    engine_name: str,
    ctx: AgentContext,
) -> AgentEngine:
    name = (engine_name or "").lower()
    if name in ("agents_sdk", "agents", "sdk", "openai-agents"):
        return AgentsSdkEngine(ctx)
    if name in ("langgraph", "lang"):
        return LangGraphEngine(ctx)
    return ResponsesEngine(ctx)


def make_registry(memory=None) -> ToolRegistry:
    from tools.base import load_default_tools

    registry = ToolRegistry()
    load_default_tools(registry, memory=memory)
    return registry


__all__ = ["build_engine", "make_registry", "AgentContext", "AgentEngine"]