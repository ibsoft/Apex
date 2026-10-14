"""Agent-facing tools for the integration manager.

Three tools, force-included for every skill when ``INTEGRATIONS_ENABLED``
(the same floor pattern as the browser/terminal/task tools):

* ``integration_query``   - run an operation on a named connection (SQL always
                            passes the strict validator first; MCP/REST/GraphQL
                            go through their executors). An ``nl`` request is
                            translated by the provider into the right payload.
* ``integration_discover``- refresh and show the schema / tools / endpoints.
* ``integration_status``  - overall status + which connections are reachable.

The tools read the caller's ``user_id`` from the context, never from arguments.
"""
from __future__ import annotations

import threading
import time

from tools.base import Tool, ToolContext

from .errors import ConnectorError
from .manager import IntegrationManager
from .render import discovery_summary
from .store import IntegrationStore


def _ensure(ctx: ToolContext, manage: IntegrationManager) -> IntegrationManager:
    if not ctx.user_id:
        raise ConnectorError("integration tools need a logged-in user")
    return manage


def _pick(manage: IntegrationManager, user_id: str, name: str) -> dict:
    for record in manage.store.list_raw(user_id):
        if record["name"] == name or record["id"] == name:
            if not record.get("enabled"):
                raise ConnectorError(f"connection `{name}` is disabled")
            return record
    raise ConnectorError(
        f"no connection named `{name}`; list them with integration_status")


def t_integration_query(args: dict, ctx: ToolContext):
    manage = _wired(ctx)
    _ensure(ctx, manage)
    name = str(args.get("connection") or "").strip()
    if not name:
        return "provide `connection` (list them with integration_status)"
    record = _pick(manage, ctx.user_id, name)
    nl = str(args.get("nl") or "").strip()
    if nl:
        from .nl_query import translate

        if not ctx.provider:
            return "NL→query needs a provider; pass explicit SQL instead"
        return _format_result(translate(manage, record, nl, ctx.provider, execute=True))

    kind = record["kind"]
    if kind in ("postgres", "mysql", "sqlserver", "sqlite", "oracle"):
        sql = str(args.get("sql") or "").strip()
        if not sql:
            return "give `sql` for SQL connections (or `nl` for a natural request)"
        return _format_result(manage.query(record, {"sql": sql}))
    if kind == "mcp":
        tool = str(args.get("tool") or "").strip()
        payload = {"tool": tool, "arguments": args.get("arguments") or {}}
        if not tool:
            return "give `tool` (see integration_discover) for MCP connections"
        return _format_result(manage.query(record, payload))
    if kind == "rest":
        tool = str(args.get("tool") or "").strip()
        if not tool:
            return "give `tool` (an operationId from integration_discover)"
        return _format_result(
            manage.query(record, {"tool": tool, "arguments": args.get("arguments") or {}}))
    if kind == "graphql":
        query = str(args.get("query") or "").strip()
        if not query:
            return "give `query` (a read-only GraphQL query) for GraphQL connections"
        return _format_result(
            manage.query(record, {"query": query, "variables": args.get("variables") or {}}))
    return f"unsupported connection kind `{kind}`"


def t_integration_discover(args: dict, ctx: ToolContext):
    manage = _wired(ctx)
    _ensure(ctx, manage)
    name = str(args.get("connection") or "").strip()
    force = bool(args.get("force"))
    if name:
        record = _pick(manage, ctx.user_id, name)
        try:
            discovered = manage.discover(record, force=force)
        except ConnectorError as exc:
            return f"discovery failed: {exc}"
        if record["kind"] in ("postgres", "mysql", "sqlserver", "sqlite", "oracle"):
            summary = discovery_summary(discovered)
            return f"{record['name']} ({record['kind']}):\n{summary}"
        if record["kind"] == "mcp":
            lines = [f"{record['name']} (mcp):"]
            for tool in discovered.get("tools") or []:
                lines.append(f"- {tool.get('name')}: {tool.get('description')}")
            return "\n".join(lines) or f"{record['name']}: (no tools advertised)"
        if record["kind"] == "rest":
            lines = [f"{record['name']} (rest):"]
            for e in discovered.get("endpoints") or []:
                lines.append(f"- {e.get('name')} [{e.get('method')} {e.get('path')}]")
            return "\n".join(lines) or f"{record['name']}: (no endpoints)"
        if record["kind"] == "graphql":
            lines = [f"{record['name']} (graphql) query type "
                     f"{discovered.get('query_type')}:"]
            for t in discovered.get("types") or []:
                fields = ", ".join(f.get("name") for f in (t.get("fields") or [])[:20])
                lines.append(f"- {t.get('name')}: {fields}")
            return "\n".join(lines)
        return f"discovered {record['name']}"
    # no name -> everything discoverable for this user
    lines = []
    for record in manage.store.list_raw(ctx.user_id):
        if not record.get("enabled"):
            continue
        try:
            discovered = manage.discover(record)
        except ConnectorError as exc:
            lines.append(f"- {record['name']}: unreachable ({exc})")
            continue
        items = (discovered.get("tables") or discovered.get("tools")
                 or discovered.get("endpoints") or discovered.get("types") or [])
        lines.append(f"- {record['name']} ({record['kind']}): {len(items)} items")
    return "\n".join(lines) if lines else "no integrations configured"


def t_integration_status(args: dict, ctx: ToolContext):
    manage = _wired(ctx)
    _ensure(ctx, manage)
    records = manage.store.list_raw(ctx.user_id)
    if not records:
        return ("no integrations configured - the operator adds them in the "
                "MCP tab of the UI")
    lines = [f"{len(records)} integration(s):"]
    for record in records:
        health = manage.health(record) or {}
        status = health.get("last_status") or "unknown"
        mark = "✓" if status == "ok" else "✗"
        when = ""
        if health.get("last_checked_at"):
            when = f" (checked {int(time.time() - health['last_checked_at'])}s ago)"
        extra = ""
        if not record.get("enabled"):
            extra = " [disabled]"
        name = record["name"]
        lines.append(f"- {mark} {name} [{record['kind']}]{extra}: {status}{when}")
        err = health.get("last_error")
        if err:
            lines.append(f"    {err[:160]}")
    return "\n".join(lines)


_MANAGER_CACHE: dict[str, IntegrationManager] = {}
_MANAGER_LOCK: object = threading.Lock()


def _global_manager() -> IntegrationManager:
    """One manager per store path; app.py constructs the same one (same path)."""
    from config import config as cfg

    path = str(cfg.INTEGRATIONS_DB)
    with _MANAGER_LOCK:
        manage = _MANAGER_CACHE.get(path)
        if manage is None:
            manage = IntegrationManager(IntegrationStore(path))
            _MANAGER_CACHE[path] = manage
        return manage


def _wired(ctx: ToolContext) -> IntegrationManager:
    manage = getattr(ctx, "_integr_manage", None)
    if manage is not None:
        return manage
    return _global_manager()


def _format_result(result: dict) -> str:
    if not isinstance(result, dict):
        return str(result)
    if result.get("error"):
        return f"query failed: {result['error']}"
    text = result.get("text")
    if text:
        if result.get("truncated"):
            text += "\n…(rows truncated to the connection limit)"
        return text
    if "rows" in result:
        return f"{result.get('row_count', 0)} rows"
    if result.get("status") is not None:
        return f"http {result['status']}:\n{result.get('body', '')}"
    body = result.get("body")
    if body:
        return body
    return "ok"


def build_integration_tools(manage: IntegrationManager | None = None) -> list[Tool]:
    def _with_manage(t_handler):
        def wrapper(args: dict, ctx: ToolContext):
            if manage is not None:
                ctx._integr_manage = manage
            return t_handler(args, ctx)
        return wrapper

    return [
        Tool(
            name="integration_query",
            description=(
                "Run a read operation on a configured integration (MCP tool, "
                "SQL SELECT, REST endpoint or GraphQL query). Pass `nl` as a "
                "natural-language request to have it translated and run "
                "automatically, or pass the exact payload: `sql` for SQL "
                "connections, `tool`+`arguments` for MCP/REST, `query` for "
                "GraphQL. List connections with integration_status and their "
                "surfaces with integration_discover."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "connection": {"type": "string",
                                   "description": "connection name or id"},
                    "nl": {"type": "string",
                           "description": "natural-language request, translated "
                                          "and executed (choose this for plain "
                                          "English requests)"},
                    "sql": {"type": "string",
                            "description": "exact SQL for SQL connections"},
                    "tool": {"type": "string",
                             "description": "MCP tool name or REST operationId"},
                    "arguments": {"type": "object",
                                  "description": "tool arguments (MCP/REST)"},
                    "query": {"type": "string",
                              "description": "read-only GraphQL query string"},
                    "variables": {"type": "object"},
                },
                "required": ["connection"],
            },
            handler=_with_manage(t_integration_query),
            dangerous=True,
        ),
        Tool(
            name="integration_discover",
            description=(
                "Show what an integration exposes: SQL tables/columns, MCP "
                "tools, REST endpoints or GraphQL types. Optional `force` "
                "refreshes from the live system instead of the cache."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "connection": {"type": "string",
                                   "description": "empty = all connections"},
                    "force": {"type": "boolean", "default": False},
                },
            },
            handler=_with_manage(t_integration_discover),
        ),
        Tool(
            name="integration_status",
            description=(
                "List the configured integrations for the current user with "
                "their health status and any connectivity error."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_with_manage(t_integration_status),
        ),
    ]


__all__ = ["build_integration_tools", "t_integration_query",
           "t_integration_discover", "t_integration_status"]