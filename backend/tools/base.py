"""Tool framework: typed function-tooling shared across all three agent
engines (Responses API, Agents SDK, LangGraph) and all model providers
(OpenAI / Ollama / torch)."""
from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from config import config


@dataclass
class ToolContext:
    """Everything a tool may need at call time."""

    user_id: str = ""
    conversation_id: str = ""
    memory: Any = None            # MemoryStore or None
    emit: Optional[Callable[[dict], None]] = None  # push agent events mid-tool
    model: str = ""
    session: Any = None


@dataclass
class Tool:
    """One function-callable tool.

    ``parameters`` is a JSON Schema object for the arguments.
    """

    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, ToolContext], str]
    enabled: bool = True
    dangerous: bool = False

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def call(self, args: dict, ctx: ToolContext) -> str:
        try:
            result = self.handler(args or {}, ctx)
            if result is None:
                return "ok"
            return str(result)
        except Exception as exc:  # return errors to the model, don't crash
            tb = traceback.format_exc()
            return f"tool error: {type(exc).__name__}: {exc}\n{tb[-600:]}"


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> "ToolRegistry":
        self._tools[tool.name] = tool
        return self

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def active(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.enabled]

    def schemas(self, tool_names: list[str] | None = None) -> list[dict]:
        tools = self.active() if tool_names is None else [
            t for t in self.active() if t.name in tool_names
        ]
        return [t.to_openai() for t in tools]

    def names(self) -> list[str]:
        return [t.name for t in self.active()]

    def invoke(self, name: str, args: dict, ctx: ToolContext) -> str:
        tool = self.get(name)
        if tool is None:
            return f"tool error: unknown tool `{name}`"
        if not tool.enabled:
            return f"tool error: `{name}` is disabled"
        return tool.call(args, ctx)


def load_default_tools(registry: ToolRegistry, memory=Any, config=config):
    """Register the built-in tool set. Imported lazily to keep startup cheap."""
    from tools.core_tools import build_core_tools
    from tools.memory_tools import build_memory_tools
    from tools.obsidian_tools import build_obsidian_tools
    from tools.file_search import build_file_tools
    from tools.editor_tools import build_editor_tools
    from tools.vapt_tools import build_vapt_tools
    from tools.code_tools import build_code_tools

    for tool in build_core_tools(registry, config):
        registry.register(tool)
    if memory is not None:
        for tool in build_memory_tools(memory):
            registry.register(tool)
    for tool in build_file_tools(config):
        registry.register(tool)
    for tool in build_obsidian_tools(config):
        registry.register(tool)
    for tool in build_editor_tools(config):
        registry.register(tool)
    for tool in build_vapt_tools(config):
        registry.register(tool)
    for tool in build_code_tools(config):
        registry.register(tool)
    return registry