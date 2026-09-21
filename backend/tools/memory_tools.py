"""Memory tools: let the agent read/write the per-user vector store itself."""
from __future__ import annotations

import json

from tools.base import Tool, ToolContext


def build_memory_tools(memory) -> list[Tool]:
    """memory is a MemoryStore (see memory/store.py)."""

    def t_remember(args, ctx: ToolContext):
        text = (args.get("text") or "").strip()
        category = (args.get("category") or "general").strip() or "general"
        if not text:
            return "Please provide text to remember."
        mem_id = memory.remember(
            ctx.user_id,
            text,
            category=category,
            conversation_id=ctx.conversation_id,
        )
        if ctx.emit:
            ctx.emit({"type": "memory", "action": "saved",
                      "detail": {"id": mem_id, "text": text[:120]}})
        return f"remembered (id={mem_id})"

    def t_recall(args, ctx: ToolContext):
        query = (args.get("query") or "").strip()
        n = min(int(args.get("n", 5) or 5), 15)
        if not query:
            return "Please provide a query to recall."
        hits = memory.recall(ctx.user_id, query, n)
        if not hits:
            return "No memory found for that query."
        lines = []
        for h in hits:
            lines.append(f"- ({h['meta'].get('category','general')}) {h['text']}")
        if ctx.emit:
            ctx.emit({"type": "memory", "action": "recalled",
                      "detail": {"query": query, "count": len(hits)}})
        return "\n".join(lines)

    def t_memory_stats(args, ctx: ToolContext):
        return f"You have {memory.count(ctx.user_id)} memory entries."

    def t_forget(args, ctx: ToolContext):
        ids = args.get("ids") or []
        n = memory.forget(ctx.user_id, [str(i) for i in ids])
        return f"forgot {n} memory entr{'y' if n == 1 else 'ies'}"

    t_remember_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "A fact or memory to store permanently."},
            "category": {"type": "string", "description": "Optional category, e.g. preferences, project, task."},
        },
        "required": ["text"],
    }
    t_recall_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language query describing the memory to retrieve."},
            "n": {"type": "integer", "description": "Number of results (default 5)."},
        },
        "required": ["query"],
    }

    return [
        Tool("remember",
             "Store a fact or memory for the current user. Use for stable user information you want to persist.",
             t_remember_schema, t_remember),
        Tool("recall",
             "Search the user's long-term memory and return the most relevant stored facts.",
             t_recall_schema, t_recall),
        Tool("memory_stats",
             "Return the number of memory entries stored for the user.",
             {"type": "object", "properties": {}}, t_memory_stats),
        Tool("forget",
             "Delete memory entries by id.",
             {"type": "object",
              "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
              "required": ["ids"]}, t_forget),
    ]


def memory_prompt_block(user_id: str, memory, query: str, n: int = 5) -> str:
    """Build the injected memory context for the system prompt.

    Only a best-effort recall; tools `recall`/`remember` give the agent
    live access regardless.
    """
    if memory is None:
        return ""
    try:
        hits = memory.recall(user_id, query, n)
    except Exception:
        return ""
    if not hits:
        return ""
    lines = [str(h.get("text") or "").strip() for h in hits]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    return (
        "\n[Relevant long-term memory (auto-injected)]\n" + "\n- ".join(lines) + "\n"
    )