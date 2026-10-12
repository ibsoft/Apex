"""Engine: raw OpenAI Responses end-to-end loop (works on every provider).

This is the workhorse engine - a self-contained agent loop that runs the
model, executes any requested tools, feeds results back in, and streams text
the whole time. It is provider-agnostic (OpenAI / Ollama / torch), which is
why the other two engines are optional extras.

It is also the only engine that consumes *steers*. A steer is an instruction the
operator sends with ``/steer`` while a turn is already running (see
``agent/steer.py``). Because a text-only answer is a single model call with no
step boundary to inject into, a steer does not wait for the loop: when one
arrives mid-stream the current call is abandoned, the partial answer and the
instruction are appended, and a fresh call continues from there. That is what
makes "actually, make it JSON" reach the model instead of the next turn.
"""
from __future__ import annotations

import json
from typing import Iterator

from agent.steer import INBOX
from agent.base import AgentContext, AgentEngine
from config import config


def _steer_message(text: str) -> str:
    """Frame a steer so the model reads it as instruction, not as the operator
    starting a new, unrelated request."""
    return (
        "[Operator steer sent while you were working - additional instructions. "
        "Fold these into the task in progress and continue from where you left "
        "off; do not restart or repeat work you have already done.]\n" + text
    )


class ResponsesEngine(AgentEngine):
    name = "responses"

    def stream(self) -> Iterator[dict]:
        ctx = self.ctx
        provider = ctx.provider
        messages: list[dict] = [{"role": "system", "content": ctx.system_prompt}]
        messages += [dict(m) for m in ctx.history]

        schemas = ctx.tool_schemas()
        usage = {}

        conv_id = getattr(ctx, "conversation_id", "") or ""
        max_steps = _max_tool_steps(ctx)
        INBOX.begin(conv_id)
        steps = 0
        completed = False
        try:
            while steps < max_steps:
                steps += 1
                assistant_text = ""
                tool_calls: list[dict] = []
                # A skill that promises to actually DO things (shell: "for every
                # request you MUST use a shell tool") gets tool_choice="required"
                # on the FIRST model call only. Without it a small model just
                # answers from training data -- inventing a ping result that never
                # ran. Only the first call: after a tool result (or a steer) the
                # model must be free to answer normally, otherwise every turn
                # would loop into another tool call.
                force = steps == 1 and ctx.require_tool and bool(schemas)
                interrupted = False
                chunks = None
                try:
                    chunks = provider.chat_stream(
                        messages, schemas or None,
                        **({"tool_choice": "required"} if force else {}),
                    )
                    for chunk in chunks:
                        steers = INBOX.drain(conv_id)
                        if steers:
                            # Keep what was said so far, then fold the
                            # instruction in and start a fresh call. Consumed
                            # here (before handling the chunk) so a steer cannot
                            # be lost to an answer that finishes on this chunk.
                            for text in steers:
                                yield {"type": "steer_applied", "content": text}
                            messages.append({"role": "assistant", "content": assistant_text or None})
                            for text in steers:
                                messages.append({"role": "user", "content": _steer_message(text)})
                            interrupted = True
                            break
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
                finally:
                    # Leaving the inner loop (steer or completion) must close the
                    # provider's stream, or the connection is left open until GC.
                    close = getattr(chunks, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass

                if interrupted:
                    continue

                assistant_msg: dict = {
                    "role": "assistant",
                    "content": assistant_text or None,
                }
                if not tool_calls:
                    assistant_msg["tool_calls"] = None
                    completed = True
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

                # A steer can arrive while the tools run (no model call is in
                # flight to catch it above), so fold any that are waiting in
                # before the next call as well.
                pending = INBOX.drain(conv_id)
                for text in pending:
                    yield {"type": "steer_applied", "content": text}
                    messages.append({"role": "user", "content": _steer_message(text)})

            if not completed:
                yield {"type": "error",
                       "message": f"Max tool steps ({max_steps}) reached."}
                return
        finally:
            INBOX.end(conv_id)

        yield {"type": "done", "usage": usage}


def _max_tool_steps(ctx: AgentContext) -> int:
    """The step ceiling for this turn.

    A skill named in MAX_TOOL_STEPS_EXEMPT gets the larger ceiling: the
    skill_creator repair loop legitimately needs many create -> validate ->
    test -> fix iterations, and the default 12 cuts it off mid-repair. Exempt
    skills are still bounded by MAX_TOOL_STEPS_EXEMPT_LIMIT, so a runaway loop
    cannot spin forever.
    """
    name = (getattr(ctx, "skill_name", "") or "").strip()
    if name and name in config.MAX_TOOL_STEPS_EXEMPT:
        return config.MAX_TOOL_STEPS_EXEMPT_LIMIT
    return config.MAX_TOOL_STEPS


def _safe_args(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"raw": raw}
