"""Engine: OpenAI Agents SDK (agents/agntcy) with our tools as FunctionTools.

Model selection is injected directly:
  * OpenAI platform (server key or OAuth) -> Responses model (async client)
  * Ollama / any OpenAI-compatible base_url -> Chat Completions model
  * Torch has no OpenAI-compatible client -> clear error (use responses engine)

The SDK streams internally (async); a daemon thread + queue bridge it into the
synchronous engine-generator contract used by the SSE transport.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import Iterator

from agent.base import AgentContext, AgentEngine


class AgentsSdkEngine(AgentEngine):
    name = "agents_sdk"

    def stream(self) -> Iterator[dict]:
        ctx = self.ctx
        opts = ctx.provider.client_options()
        if opts is None:
            yield {
                "type": "error",
                "message": (
                    "The Agents SDK engine needs an OpenAI-compatible model "
                    "backend (openai / ollama). Torch uses the raw responses "
                    "engine instead."
                ),
            }
            return

        q: "queue.Queue[dict | None]" = queue.Queue()

        def emit(event: dict):
            q.put(event)

        from agents import Agent, FunctionTool

        def make_tools(use_responses: bool):
            tools = []
            for tool in ctx.active_tools():
                name, desc, schema = tool.name, tool.description, tool.parameters

                async def invoke(context, args_json, _t=tool):
                    args = args_json if isinstance(args_json, dict) else {}
                    if isinstance(args_json, str):
                        try:
                            args = json.loads(args_json or "{}")
                        except Exception:
                            args = {"raw": args_json}
                    parsed = args if isinstance(args, dict) else {}
                    emit({
                        "type": "tool_call",
                        "name": _t.name,
                        "id": f"apex-{_t.name}",
                        "arguments": parsed,
                    })
                    out = _t.call(parsed, ctx.make_tool_context())
                    emit({"type": "tool_result", "name": _t.name, "output": out})
                    return out

                tools.append(
                    FunctionTool(
                        name=name,
                        description=desc,
                        params_json_schema=schema if isinstance(schema, dict) else {},
                        on_invoke_tool=invoke,
                        # chat-completions backends (ollama/vllm) shouldn't be
                        # forced into OpenAI-strict schema mode
                        strict_json_schema=use_responses,
                    )
                )
            return tools

        def async_run():
            async def inner():
                try:
                    import openai

                    from agents.models.openai_chatcompletions import (
                        OpenAIChatCompletionsModel,
                    )
                    from agents.models.openai_responses import (
                        OpenAIResponsesModel,
                    )

                    kind = ctx.provider_kind
                    is_ollama = kind == "ollama" or bool(
                        opts.get("base_url") and "api.openai.com" not in (opts.get("base_url") or "")
                    )
                    async_client = openai.AsyncOpenAI(
                        api_key=opts["api_key"] or "not-needed",
                        base_url=opts["base_url"],
                        timeout=120,
                        max_retries=2,
                    )
                    if is_ollama:
                        model = OpenAIChatCompletionsModel(
                            model=ctx.provider.cfg.model, openai_client=async_client
                        )
                    else:
                        model = OpenAIResponsesModel(
                            model=ctx.provider.cfg.model, openai_client=async_client
                        )

                    agent = Agent(
                        name="apex",
                        instructions=ctx.system_prompt,
                        model=model,
                        tools=make_tools(use_responses=not is_ollama),
                    )
                    result = Runner.run_streamed(
                        agent, input=list(ctx.history) or "Hello."
                    )
                    async for event in result.stream_events():
                        if event.type == "raw_response_event":
                            data = event.data
                            if getattr(data, "type", "") == "response.output_text.delta":
                                emit({"type": "text_delta", "content": data.delta})
                        elif event.type == "run_item_stream_event":
                            item = event.item
                            if getattr(item, "type", "") == "function_call":
                                emit({
                                    "type": "tool_call",
                                    "name": item.name,
                                    "id": item.call_id or f"apex-{item.name}",
                                    "arguments": dict(item.arguments or {}),
                                })
                    emit({"type": "done", "usage": {"input": 0, "output": 0}})
                except Exception as exc:
                    emit({"type": "error", "message": f"agents_sdk :: {exc}"})
                finally:
                    q.put(None)

            asyncio.run(inner())

        from agents import Runner

        thread = threading.Thread(target=async_run, daemon=True)
        thread.start()
        while True:
            event = q.get()
            if event is None:
                break
            yield event