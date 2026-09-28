"""Engine: raw OpenAI Responses end-to-end loop (works on every provider).

This is the workhorse engine - a self-contained agent loop that runs the
model, executes any requested tools, feeds results back in, and streams text
the whole time. It is provider-agnostic (OpenAI / Ollama / torch), which is
why the other two engines are optional extras.
"""
from __future__ import annotations

import json
from typing import Iterator

from agent.base import AgentContext, AgentEngine
from config import config


class ResponsesEngine(AgentEngine):
    name = "responses"

    def stream(self) -> Iterator[dict]:
        ctx = self.ctx
        provider = ctx.provider
        messages: list[dict] = [{"role": "system", "content": ctx.system_prompt}]
        messages += [dict(m) for m in ctx.history]

        schemas = ctx.tool_schemas()
        usage = {}

        for step in range(config.MAX_TOOL_STEPS):
            assistant_text = ""
            tool_calls: list[dict] = []
            # A skill that promises to actually DO things (shell: "for every
            # request you MUST use a shell tool") gets tool_choice="required" on
            # the FIRST model call only. Without it a small model just answers
            # from training data -- inventing a ping result that never ran.
            # Only step 0: after a tool result the model must be free to answer
            # normally, otherwise every turn would loop into another tool call.
            force = step == 0 and ctx.require_tool and bool(schemas)
            try:
                chunks = provider.chat_stream(
                    messages, schemas or None,
                    **({"tool_choice": "required"} if force else {}),
                )
                for chunk in chunks:
                    ctype = chunk.get("type")
                    if ctype == "text":
                        assistant_text += chunk["content"]
                        yield {"type": "text_delta", "content": chunk["content"]}
                    elif ctype == "tool_calls":
                        tool_calls = chunk["calls"]
                    elif ctype == "usage":
                        usage = chunk
                    elif ctype == "error":
                        yield {"type": "error", "message": chunk["message"]}
                        return
            except Exception as exc:
                yield {"type": "error", "message": f"{provider.kind} :: {exc}"}
                return

            assistant_msg: dict = {
                "role": "assistant",
                "content": assistant_text or None,
            }
            if not tool_calls:
                assistant_msg["tool_calls"] = None
                break
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls
            ]
            messages.append(assistant_msg)

            for tc in tool_calls:
                yield {
                    "type": "tool_call",
                    "name": tc["name"],
                    "id": tc["id"],
                    "arguments": _safe_args(tc["arguments"]),
                }
                out = ctx.tools.invoke(
                    tc["name"], _safe_args(tc["arguments"]), ctx.make_tool_context()
                )
                yield {"type": "tool_result", "name": tc["name"], "output": out}
                yield from ctx.drain_events()
                messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": out}
                )
        else:
            yield {"type": "error",
                   "message": f"Max tool steps ({config.MAX_TOOL_STEPS}) reached."}
            return

        yield {"type": "done", "usage": usage}


def _safe_args(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"raw": raw}