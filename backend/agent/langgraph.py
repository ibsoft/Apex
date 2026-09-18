"""Engine: LangGraph (state machine agent with our tools as a tool node).

Runs on any OpenAI-compatible backend (openai / ollama / vLLM / LM Studio).
Torch is handled by the raw responses engine; if selected here we explain that
clearly instead of crashing.
"""
from __future__ import annotations

from typing import Iterator

from agent.base import AgentContext, AgentEngine


class LangGraphEngine(AgentEngine):
    name = "langgraph"

    def stream(self) -> Iterator[dict]:
        ctx = self.ctx
        opts = ctx.provider.client_options()
        if opts is None:
            yield {
                "type": "error",
                "message": (
                    "LangGraph needs an OpenAI-compatible model backend "
                    "(openai / ollama). Torch uses the raw responses engine."
                ),
            }
            return

        from langchain_openai import ChatOpenAI
        from langchain_core.messages import AIMessageChunk, ToolMessage
        from langgraph.graph import END, StateGraph

        if sysframe() == "messages":
            from langgraph.graph import MessagesState as llmState
        else:  # older langgraph
            from langgraph.graph import add_messages
            from typing import Annotated, TypedDict

            class llmState(TypedDict):
                messages: Annotated[list, add_messages]

        if sysframe() == "messages":
            from langgraph.prebuilt import tools_condition
        else:
            from langgraph.prebuilt import tools_condition  # same module

        llm = ChatOpenAI(
            model=ctx.provider.cfg.model,
            base_url=opts["base_url"],
            api_key=opts["api_key"] or "not-needed",
            temperature=ctx.runtime.get("temperature", 0.7),
            streaming=True,
        )
        schemas = ctx.tool_schemas()

        # Tool events are logged here by the custom tools node and drained in
        # stream order, so tool_call / tool_result appear live in the UI.
        tool_log: list[dict] = []

        def tools_node(state):
            last = state["messages"][-1]
            outputs = []
            for call in getattr(last, "tool_calls", []) or []:
                name, args = call.get("name", ""), call.get("args", {})
                tool_log.append({
                    "type": "tool_call",
                    "name": name,
                    "id": call.get("id", f"apex-{name}"),
                    "arguments": args if isinstance(args, dict) else {},
                })
                out = ctx.tools.invoke(name, args or {}, ctx.make_tool_context())
                tool_log.append({"type": "tool_result", "name": name, "output": out})
                outputs.append(
                    ToolMessage(content=str(out), tool_call_id=call["id"], name=name)
                )
            return {"messages": outputs}

        def agent_node(state):
            bound = llm.bind_tools(schemas) if schemas else llm
            return {"messages": [bound.invoke(state["messages"])]}

        builder = StateGraph(llmState)
        builder.add_node("agent", agent_node)
        builder.add_node("tools", tools_node)
        builder.set_entry_point("agent")
        if schemas:
            builder.add_conditional_edges(
                "agent", tools_condition, {"tools": "tools", END: END}
            )
        else:
            builder.add_edge("agent", END)
        builder.add_edge("tools", "agent")
        app = builder.compile()

        messages = [{"role": "system", "content": ctx.system_prompt}]
        messages += [dict(m) for m in ctx.history]

        try:
            steps = app.stream(
                {"messages": messages},
                config={"configurable": {"thread_id": ctx.conversation_id}},
                stream_mode=["messages", "updates"],
            )
            for mode, payload in steps:
                if mode == "messages":
                    chunk, meta = payload or (None, None)
                    node = meta.get("langgraph_node") if isinstance(meta, dict) else ""
                    if node == "tools":
                        continue  # surfaced below via updates / tool_log
                    if isinstance(chunk, AIMessageChunk) and chunk.content:
                        yield {"type": "text_delta", "content": str(chunk.content)}
                elif mode == "updates":
                    for node_name, update in payload.items():
                        if node_name != "tools":
                            continue
                        for ev in tool_log:
                            yield ev
                        tool_log.clear()
            if tool_log:  # safety net if any events weren't drained
                for ev in tool_log:
                    yield ev
                tool_log.clear()
            yield {"type": "done", "usage": {}}
        except Exception as exc:
            yield {"type": "error", "message": f"langgraph :: {exc}"}


def sysframe() -> str:
    """Compatibility shim: modern langgraph exposes `MessagesState`."""
    try:
        from langgraph.graph import MessagesState  # noqa: F401

        return "messages"
    except Exception:
        return "typed"