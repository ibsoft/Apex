"""Run one sub-agent turn.

The sub-agent gets its own :class:`AgentContext`: its own model/provider, its own
conversation (so a run can see what the previous ones found), its own workspace
as the shell/terminal cwd, and its own tool list. The engine is one of the same
three APEX engines, so a sub-agent is not a second agent framework - it is the
same loop pointed at a different configuration.

Reporting back to APEX is the return value: the caller (the delegate tool, or the
manager for a background run) receives ``(report, error)``. Progress is emitted
through ``emit`` as ``subagent_progress`` / ``subagent_tool`` events, which the
delegating turn relays to the browser in causal order (the engine drains a
tool's queued events right after its result).
"""
from __future__ import annotations

import threading
import time

from config import config as default_config

_progress_min_interval = 1.5

#: Tools a sub-agent never gets, whatever its list says:
#:  * the terminal tools open an interactive browser window, which makes no sense
#:    for an unattended worker and would litter the operator's desktop;
#:  * the sub-agent management tools would let an agent recursively delegate.
_SUBAGENT_EXCLUDED = frozenset({
    "terminal_command",
    "terminal_sessions",
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


def _audit(level: str, category: str, event: str, **fields) -> None:
    """Append one audit-log event for an autonomous run. Never raises."""
    try:
        from observability import record

        record(level, category, event, **fields)
    except Exception:
        pass


def _load_list(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    import json

    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x).strip() for x in value if str(x).strip()] if isinstance(value, list) else []


def build_subagent_prompt(agent: dict, *, workspace: str = "") -> str:
    """The system prompt for one run. Operator instruction first, then the
    unattended contract and the workspace, then the secrets rule."""
    name = agent.get("name") or "Agent"
    lines = [f'You are "{name}", an autonomous sub-agent working for APEX.']
    if (agent.get("description") or "").strip():
        lines.append(agent["description"].strip())
    if (agent.get("role") or "").strip():
        lines.append(agent["role"].strip())
    if workspace:
        lines.append(
            f"Your workspace directory is {workspace}. Shell commands (run_shell) "
            "start there; keep any files you produce inside it."
        )
    lines.append(
        "You are running unattended: there is no operator to answer a question, so "
        "decide and act with your tools instead of asking. When you are done, end "
        "with a concise report of what you did and what you found - that report is "
        "handed back to APEX, which answers the operator, so put the actual result "
        "in it. If you could not do it, say exactly what failed and why."
    )
    lines.append(
        "Never read, print, quote or return the contents of any .env file or other "
        "secret store; name the variable and leave editing to the operator."
    )
    return "\n\n".join(lines)


def run_subagent_turn(agent: dict, prompt: str, *, user_id: str, run_id: str = "",
                      conversation_id: str = "", emit=None,
                      timeout: int | None = None) -> tuple[str, str]:
    """Run one turn for ``agent``. Returns ``(report, error)``. Never raises."""
    from auth import bearer_for_api, is_subscription_access
    from agent.base import AgentContext
    from agent.factory import build_engine, make_registry
    from db import get_db
    from models.providers import ProviderError, ProviderManager

    db = get_db()
    runtime = db.all_settings()
    provider_name = (
        agent.get("provider") or runtime.get("provider")
        or getattr(default_config, "PROVIDER_DEFAULT", "openai")
    ).lower()
    engine_name = (
        runtime.get("engine") or getattr(default_config, "AGENT_ENGINE", "responses")
    ).lower()
    model = (agent.get("model") or runtime.get("model") or "").strip()
    # The codex provider only speaks the Responses API, exactly as in /api/chat.
    if provider_name == "codex":
        engine_name = "responses"

    try:
        provider = ProviderManager(
            bearer=bearer_for_api(user_id),
            use_oauth_access=is_subscription_access(user_id),
            runtime=runtime,
        ).build(provider_name, model or None)
    except ProviderError as exc:
        return "", f"no model provider available: {exc}"

    memory = None
    if getattr(default_config, "MEMORY_ENABLED", False):
        try:
            from memory.store import get_memory

            memory = get_memory()
        except Exception:
            memory = None

    # The agent's own thread, reused across runs so a run has the previous ones
    # for context. Created on first use and remembered on the agent row. Only the
    # agent's *own* conversation may be reused: a caller that passes some other
    # thread (the delegate tool used to pass the operator's chat conversation)
    # must not have the agent's private turn written into it.
    own_conversation = agent.get("conversation_id") or ""
    conv = db.get_conversation(conversation_id) if conversation_id and conversation_id == own_conversation else None
    if conv is None or conv.get("user_id") != user_id:
        conv = db.create_conversation(user_id, title=f"Agent: {agent.get('name') or 'sub-agent'}")
        db.update_subagent(agent["id"], conversation_id=conv["id"])
    conversation_id = conv["id"]

    workspace = agent.get("workspace") or ""
    tool_names = _load_list(agent.get("tools"))
    system_prompt = build_subagent_prompt(agent, workspace=workspace)

    window = int(getattr(default_config, "SUBAGENT_HISTORY_WINDOW", 20) or 20)
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in db.list_messages(conversation_id)[-window:]
        if m["role"] in ("user", "assistant") and m.get("content")
    ]
    db.add_message(conversation_id, "user", prompt,
                   {"subagent_id": agent["id"], "run_id": run_id, "autonomous": True})
    # The engine consumes ctx.history verbatim: this run's prompt must be the last
    # entry or the model answers the previous run instead of this one.
    history.append({"role": "user", "content": prompt})

    _audit("info", "chat", "chat_message", role="user", source="subagent",
           user_id=user_id, agent_id=agent["id"], run_id=run_id,
           conversation_id=conversation_id, text=prompt)

    user_row = db.get_user(user_id) or {}
    ctx = AgentContext(
        user_id=user_id,
        conversation_id=conversation_id,
        system_prompt=system_prompt,
        history=history,
        provider=provider,
        provider_kind=provider_name,
        engine_name=engine_name,
        skill_name="",
        tools=make_registry(memory=memory),
        skill_tools=tool_names,
        strict_tools=bool(tool_names),
        exclude_tools=sorted(_SUBAGENT_EXCLUDED),
        memory=memory,
        runtime=runtime,
        voice_mode=False,
        user_name=user_row.get("name") or "",
        workspace=workspace,
    )
    report, error = _collect_stream(
        build_engine(engine_name, ctx), timeout, emit, agent, run_id,
    )
    db.add_message(conversation_id, "assistant", report or "",
                   {"subagent_id": agent["id"], "run_id": run_id, "autonomous": True,
                    **({"error": error} if error else {})})
    if error:
        _audit("error", "error", "chat_error", user_id=user_id, agent_id=agent["id"],
               run_id=run_id, source="subagent", message=str(error))
    _audit("info", "chat", "chat_reply", role="assistant", source="subagent",
           user_id=user_id, agent_id=agent["id"], run_id=run_id,
           conversation_id=conversation_id, error=bool(error), text=report or "")
    return report, error


def _collect_stream(engine, timeout, emit, agent: dict,
                    run_id: str) -> tuple[str, str]:
    """Drain an engine on a worker thread so ``timeout`` is enforced, emitting
    throttled progress events."""
    parts: list[str] = []
    failure: list[str] = []
    last_emit = [0.0]
    name = agent.get("name") or "agent"
    agent_id = agent.get("id") or ""

    def _progress(force: bool = False):
        if not emit:
            return
        now = time.time()
        if force or now - last_emit[0] >= _progress_min_interval:
            last_emit[0] = now
            emit({"type": "subagent_progress", "agent": name, "agent_id": agent_id,
                  "run_id": run_id, "text": "".join(parts)[-1000:]})

    def drain():
        try:
            for event in engine.stream():
                kind = event.get("type")
                if kind == "text_delta":
                    parts.append(str(event.get("content") or ""))
                    _progress()
                elif kind == "tool_call" and emit:
                    emit({"type": "subagent_tool", "agent": name, "agent_id": agent_id,
                          "run_id": run_id, "tool": event.get("name")})
                elif kind == "error":
                    failure.append(str(event.get("message") or "agent error"))
                    return
        except Exception as exc:
            failure.append(f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=drain, daemon=True, name="apex-subagent-turn")
    worker.start()
    worker.join(timeout if timeout and timeout > 0 else None)
    if worker.is_alive():
        return "".join(parts).strip(), f"the sub-agent did not finish within {timeout}s"
    _progress(force=True)
    return "".join(parts).strip(), (failure[0] if failure else "")
