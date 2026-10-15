"""Sub-agent tools: the model creating, managing and delegating to its own
autonomous workers.

These are force-included for every skill while SUBAGENTS_ENABLED is on (see
``AgentContext.active_tools``), for the same reason ``task_schedule`` is: being
able to hand a long job to a named agent is a first-class capability, not a
specialist skill the operator should have to select first.

``subagent_delegate`` is the reporting contract in code: it runs the agent
synchronously and returns the agent's report as the tool result, so the model
holds the result and answers the operator with it - chat or spoken - in the same
turn. A Run Now from the AGENTS tab is the same call with ``background=True``; its
report surfaces through ``subagent_context_block`` on the next turn.
"""
from __future__ import annotations

from tools.base import Tool, ToolContext

from subagents.manager import SubAgentError, get_manager


def _db():
    from db import get_db

    return get_db()


def _require_user(ctx: ToolContext) -> str:
    return "" if ctx.user_id else "Sign in to manage sub-agents."


def _find(rows: list[dict], ref: str) -> tuple[dict | None, str]:
    """Resolve a sub-agent by id, unique id prefix, or its number in the list
    (the number the AGENTS tab and ``subagent_list`` show)."""
    wanted = (ref or "").strip()
    if wanted.isdigit():
        index = int(wanted)
        if 1 <= index <= len(rows):
            return rows[index - 1], ""
        return None, f"There is no sub-agent #{index}. You have {len(rows)}."
    for row in rows:
        if row["id"] == wanted:
            return row, ""
    matches = [row for row in rows if str(row["id"]).startswith(wanted)]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, f"{wanted!r} matches {len(matches)} sub-agents; use more of the id."
    return None, f"No sub-agent matches {ref!r}."


def build_subagent_tools(config) -> list[Tool]:
    if not bool(getattr(config, "SUBAGENTS_ENABLED", True)):
        return []

    manager = get_manager(config)

    def t_subagent_list(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        rows = _db().list_subagents(ctx.user_id)
        if not rows:
            return "No sub-agents yet. Create one with subagent_create when the operator wants a named worker."
        out = []
        for index, row in enumerate(rows, start=1):
            public = manager.public(row)
            out.append({
                "n": index,
                "id": row["id"],
                "name": row["name"],
                "description": row.get("description") or "",
                "model": row.get("model") or "",
                "tools": public["tools"] or "all",
                "enabled": public["enabled"],
                "unread": public["unread"],
            })
        return _json(out)

    def t_subagent_create(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        data = {
            "name": args.get("name"),
            "description": args.get("description") or "",
            "role": args.get("role") or args.get("description") or "",
            "provider": args.get("provider") or "",
            "model": args.get("model") or "",
        }
        if args.get("tools"):
            data["tools"] = args["tools"]
        if args.get("workspace"):
            data["workspace"] = args["workspace"]
        try:
            agent = manager.create(ctx.user_id, data)
        except SubAgentError as exc:
            return f"Could not create the sub-agent: {exc}"
        if ctx.emit:
            ctx.emit({"type": "subagents_changed", "action": "created",
                      "agent_id": agent["id"], "name": agent["name"]})
        scope = f"{len(agent['tools'])} tool(s)" if agent["tools"] else "all tools"
        return (
            f'Created sub-agent "{agent["name"]}" (id={agent["id"]}, {scope}, '
            f'model {agent.get("model") or "the default"}), workspace '
            f'{agent["workspace"]}. Delegate to it with subagent_delegate.'
        )

    def t_subagent_update(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        rows = _db().list_subagents(ctx.user_id)
        row, error = _find(rows, str(args.get("agent") or ""))
        if row is None:
            return error
        data: dict = {}
        for key in ("name", "description", "role", "provider", "model", "workspace"):
            if args.get(key) is not None and str(args.get(key)).strip() != "":
                data[key] = str(args[key]).strip()
        if args.get("tools") is not None:
            data["tools"] = args["tools"]
        if args.get("enabled") is not None:
            data["enabled"] = bool(args["enabled"])
        if not data:
            return "Nothing to change. Pass at least one of name, description, role, model, tools, workspace, enabled."
        try:
            agent = manager.update(row["id"], ctx.user_id, data)
        except SubAgentError as exc:
            return f"Could not update the sub-agent: {exc}"
        if agent is None:
            return "That sub-agent no longer exists."
        if ctx.emit:
            ctx.emit({"type": "subagents_changed", "action": "updated",
                      "agent_id": agent["id"], "name": agent["name"]})
        return f'Updated sub-agent "{agent["name"]}".'

    def t_subagent_delete(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        rows = _db().list_subagents(ctx.user_id)
        row, error = _find(rows, str(args.get("agent") or ""))
        if row is None:
            return error
        manager.delete(row["id"], ctx.user_id)
        if ctx.emit:
            ctx.emit({"type": "subagents_changed", "action": "deleted",
                      "agent_id": row["id"], "name": row["name"]})
        return f'Deleted sub-agent "{row["name"]}". Its workspace files were left on disk.'

    def t_subagent_status(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        rows = _db().list_subagents(ctx.user_id)
        row, error = _find(rows, str(args.get("agent") or ""))
        if row is None:
            return error
        runs = _db().list_subagent_runs(row["id"], 5)
        if not runs:
            return f'Sub-agent "{row["name"]}" has not run yet.'
        lines = [f'Last runs of "{row["name"]}":']
        for run in runs:
            text = (run.get("output") or run.get("error") or "").strip()
            lines.append(
                f'- [{run.get("status")}] {run.get("prompt", "")[:80]!r}: {text[:400]}'
            )
        return "\n".join(lines)

    def t_subagent_delegate(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        rows = _db().list_subagents(ctx.user_id)
        row, error = _find(rows, str(args.get("agent") or ""))
        if row is None:
            return error
        prompt = str(args.get("task") or args.get("prompt") or "").strip()
        if not prompt:
            return "Tell the sub-agent what to do (the `task` argument)."
        # Run it synchronously: the agent's report is the tool result, so this
        # turn can answer the operator with it. Progress events flow to the
        # browser through ctx.emit while it works.
        #
        # Deliberately no ``conversation_id``: the runner gives the agent its own
        # thread and stores it on the agent row, so a delegated run must not be
        # written into the operator's chat conversation (passing
        # ``ctx.conversation_id`` would do exactly that, and the operator would
        # see the sub-agent's private turn in their own history).
        run, err = manager.run_now(
            row["id"], ctx.user_id, prompt,
            background=False, emit=ctx.emit,
        )
        if run is None:
            return err or "Could not start the sub-agent."
        # This run's report is the tool result: the model holds it now and will
        # relay it in this turn. Mark it seen so subagent_context_block does not
        # repeat the same report on the next turn.
        try:
            _db().mark_subagent_runs_read(ctx.user_id, [run["id"]])
        except Exception:
            pass
        if run.get("status") == "error":
            return (
                f'Sub-agent "{row["name"]}" failed: {run.get("error") or "unknown error"}. '
                f'Report it honestly to the operator.'
            )
        report = (run.get("output") or "").strip()
        return (
            f'Sub-agent "{row["name"]}" finished. Its report:\n\n{report}\n\n'
            "Relay this to the operator in your own words."
        )

    def t_subagent_tools(args: dict, ctx: ToolContext):
        """The tool names a sub-agent can be given (for subagent_create/update)."""
        return _json(manager.known_tool_names())

    def t_subagent_fanout(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        jobs_arg = args.get("jobs")
        if not isinstance(jobs_arg, list) or not jobs_arg:
            return ("Give a `jobs` list of {agent, task} entries - one per part of "
                    "the job - and I will run them in parallel.")
        rows = _db().list_subagents(ctx.user_id)
        if not rows:
            return ("There are no sub-agents yet. Create the ones you need with "
                    "subagent_create, then call subagent_fanout.")
        resolved: list[dict] = []
        for index, item in enumerate(jobs_arg, start=1):
            item = item if isinstance(item, dict) else {}
            row, error = _find(rows, str(item.get("agent") or ""))
            if row is None:
                return f"Job {index}: {error}"
            task = str(item.get("task") or item.get("prompt") or "").strip()
            if not task:
                return f"Job {index}: give the agent a `task`."
            resolved.append({"agent": row, "prompt": task})
        cap = max(1, int(getattr(config, "SUBAGENT_FANOUT_MAX", 8) or 8))
        if len(resolved) > cap:
            return (f"That is {len(resolved)} jobs; at most {cap} run in one "
                    f"fan-out. Split it or raise SUBAGENT_FANOUT_MAX.")
        # Blocks until every job is done; progress reaches the browser through
        # ctx.emit (drained right after this result). The reports are the tool
        # result, so this turn composes the operator's answer from them.
        results = manager.run_many(resolved, ctx.user_id, emit=ctx.emit)
        lines: list[str] = []
        finished_ids: list[str] = []
        for job, run, err in results:
            name = job["agent"].get("name") or "agent"
            if run is None:
                lines.append(f'- "{name}": could not run - {err}')
                continue
            finished_ids.append(run["id"])
            if run.get("status") == "error":
                lines.append(f'- "{name}" FAILED: {run.get("error") or "unknown error"}')
            else:
                lines.append(f'- "{name}":\n{(run.get("output") or "").strip()}')
        if finished_ids:
            try:
                _db().mark_subagent_runs_read(ctx.user_id, finished_ids)
            except Exception:
                pass
        return (
            f"Fan-out complete ({len(results)} job(s)). Reports:\n\n"
            + "\n".join(lines)
            + "\n\nCompose the operator's answer from these reports in your own words."
        )

    def t_subagent_cleanup(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        days = args.get("older_than_days")
        try:
            days = float(days) if days not in (None, "") else float(
                getattr(config, "SUBAGENT_STALE_DAYS", 30) or 30
            )
        except (TypeError, ValueError):
            return "older_than_days must be a number of days."
        if days < 0:
            return "older_than_days must not be negative."
        removed = manager.cleanup_stale(ctx.user_id, days)
        if ctx.emit and removed:
            ctx.emit({"type": "subagents_changed", "action": "cleanup"})
        if not removed:
            return f"No sub-agents have been unused for {days:g} days or more."
        names = ", ".join(f'"{r["name"]}"' for r in removed)
        return (
            f"Removed {len(removed)} sub-agent(s) unused for {days:g}+ days: {names}. "
            "Their workspace files were left on disk."
        )

    agent_ref = {
        "type": "object",
        "properties": {
            "agent": {"type": "string",
                      "description": "Sub-agent id, the start of one, or its number in subagent_list."},
        },
        "required": ["agent"],
    }

    return [
        Tool(
            "subagent_list",
            "List the sub-agents you own, with their model, tool scope and any unseen report.",
            {"type": "object", "properties": {}}, t_subagent_list,
        ),
        Tool(
            "subagent_create",
            "Create a named autonomous sub-agent with its own model, workspace and tool set. "
            "Empty `tools` means it may use every tool you can; a non-empty list scopes it exactly. "
            "Call subagent_tools first for valid tool names.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short display name, e.g. 'Log watcher'."},
                    "description": {"type": "string", "description": "One line on what it is for."},
                    "role": {"type": "string", "description": "Optional standing instructions appended to its prompt."},
                    "model": {"type": "string", "description": "Model override; empty uses the default."},
                    "provider": {"type": "string", "description": "Provider override (openai, ollama, torch, kimi...); empty uses the default."},
                    "tools": {"type": "array", "items": {"type": "string"},
                              "description": "Exact tool names to allow. Omit or [] for all tools. A sub-agent never gets the terminal or sub-agent-management tools."},
                    "workspace": {"type": "string", "description": "Working directory for its shell commands. Empty uses a private directory under the sub-agents data dir."},
                },
                "required": ["name"],
            }, t_subagent_create,
        ),
        Tool(
            "subagent_update",
            "Edit a sub-agent: rename it, change its model/role/tools/workspace, or pause/resume it.",
            {
                "type": "object",
                "properties": {
                    **agent_ref["properties"],
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "role": {"type": "string"},
                    "model": {"type": "string"},
                    "provider": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "workspace": {"type": "string"},
                    "enabled": {"type": "boolean", "description": "false pauses it, true resumes it."},
                },
                "required": ["agent"],
            }, t_subagent_update,
        ),
        Tool(
            "subagent_delete", "Delete a sub-agent for good. Its workspace files are left on disk.",
            agent_ref, t_subagent_delete,
        ),
        Tool(
            "subagent_status", "The last few runs of one sub-agent, with their reports.",
            agent_ref, t_subagent_status,
        ),
        Tool(
            "subagent_delegate",
            "Hand ONE task to a sub-agent and wait for its report, then relay that report to the "
            "operator. For a complex job with independent parts, prefer subagent_fanout.",
            {
                "type": "object",
                "properties": {
                    **agent_ref["properties"],
                    "task": {"type": "string", "description": "The instruction for the sub-agent. Be specific and self-contained; it cannot ask you questions."},
                },
                "required": ["agent", "task"],
            }, t_subagent_delegate,
        ),
        Tool(
            "subagent_fanout",
            "Run several sub-agent tasks in PARALLEL and wait for every report at once. Split a "
            "complex request into independent parts and give one job per part. Use the agents "
            "already listed to you; create new ones first only if none fits. The reports come "
            "back together, so compose the operator's final answer from them.",
            {
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "array",
                        "description": "One entry per part of the job.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": {"type": "string", "description": "Sub-agent id, the start of one, or its number."},
                                "task": {"type": "string", "description": "Self-contained instruction for this part."},
                            },
                            "required": ["agent", "task"],
                        },
                    },
                },
                "required": ["jobs"],
            }, t_subagent_fanout,
        ),
        Tool(
            "subagent_cleanup",
            "Delete sub-agents that have not been used for a long time. Use it to keep the roster "
            "tidy; workspaces are left on disk.",
            {
                "type": "object",
                "properties": {
                    "older_than_days": {"type": "number",
                                        "description": "Unused for at least this many days. Defaults to the configured threshold."},
                },
            }, t_subagent_cleanup,
        ),
        Tool(
            "subagent_tools",
            "The tool names a sub-agent may be given, for subagent_create/subagent_update.",
            {"type": "object", "properties": {}}, t_subagent_tools,
        ),
    ]


def _json(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
