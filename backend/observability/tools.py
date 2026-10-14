"""Agent-facing tools for reading and auditing the event log.

Two tools, both force-included for every skill (like ``terminal_command``) so
the operator never has to set anything up to ask "what happened?" or "was that
suspicious?":

* ``log_query`` - filtered read of the JSONL audit log;
* ``security_audit`` - run the suspicious/malicious analyzer over a window.

They return JSON the model can read. A failure is a plain string reason, never
an exception - the same contract every tool follows.
"""
from __future__ import annotations

import json
from typing import Any

from config import config
from observability.analyzer import analyze, summarize
from observability.store import get_log
from tools.base import Tool, ToolContext

_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "level": {"type": "string", "description": "debug, info, warning, error, critical or security."},
        "category": {
            "type": "string",
            "description": "request, error, log, chat, tool, skill, agent, task, system or security.",
        },
        "event": {"type": "string", "description": "Exact event name, e.g. http_request, tool_call, chat_message."},
        "tool": {"type": "string", "description": "Tool name, e.g. terminal_command."},
        "text": {"type": "string", "description": "Case-insensitive substring search across the whole record."},
        "user_id": {"type": "string", "description": "Only needed with scope=all to filter to one user."},
        "since": {"type": "string", "description": "ISO-8601 timestamp or epoch seconds; only newer events."},
        "limit": {"type": "integer", "description": "Max records to return (1-5000). Default 100."},
        "scope": {
            "type": "string",
            "enum": ["self", "all"],
            "description": "'self' (default) shows only your own and system events; 'all' shows every user's.",
        },
        "count_only": {"type": "boolean", "description": "Return log statistics instead of records."},
    },
}
_QUERY_SCHEMA["additionalProperties"] = False

_AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "since": {"type": "string", "description": "ISO-8601 or epoch seconds; start of the window."},
        "lookback_seconds": {
            "type": "integer",
            "description": "Window length in seconds when `since` is omitted. Default 86400 (24h).",
        },
        "limit": {"type": "integer", "description": "Max events to scan (default 5000)."},
        "scope": {
            "type": "string",
            "enum": ["self", "all"],
            "description": "'all' (default for an audit) scans every user's activity; 'self' limits to yours.",
        },
    },
}
_AUDIT_SCHEMA["additionalProperties"] = False


def _resolve_scope(args: dict, ctx: ToolContext, *, default: str) -> str:
    scope = str(args.get("scope") or default).lower()
    return "all" if scope == "all" else "self"


def t_log_query(args: dict, ctx: ToolContext) -> str:
    log = get_log()
    if not log.enabled:
        return "The audit log is disabled (OBSERVABILITY_ENABLED=false)."
    if args.get("count_only"):
        return json.dumps(log.stats(), ensure_ascii=False, indent=2)
    scope = _resolve_scope(args, ctx, default="self")
    user_id = args.get("user_id") if scope == "all" else ctx.user_id
    try:
        limit = int(args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    records = log.read(
        limit=limit,
        level=args.get("level"),
        category=args.get("category"),
        event=args.get("event"),
        tool=args.get("tool"),
        text=args.get("text"),
        user_id=user_id or None,
        since=args.get("since"),
    )
    payload: dict[str, Any] = {"count": len(records), "scope": scope, "records": records}
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def t_security_audit(args: dict, ctx: ToolContext) -> str:
    log = get_log()
    if not log.enabled:
        return "The audit log is disabled (OBSERVABILITY_ENABLED=false)."
    scope = _resolve_scope(args, ctx, default="all")
    since = args.get("since")
    if not since:
        import time

        try:
            lookback = int(args.get("lookback_seconds") or 86400)
        except (TypeError, ValueError):
            lookback = 86400
        since = time.time() - max(1, lookback)
    try:
        limit = int(args.get("limit") or 5000)
    except (TypeError, ValueError):
        limit = 5000
    records = log.read(limit=limit, since=since, user_id=(ctx.user_id if scope == "self" else None) or None)
    findings = analyze(records)
    payload = {
        "scope": scope,
        "events_scanned": len(records),
        "since": since,
        "findings": findings,
        "summary": summarize(findings),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def build_log_tools(cfg: Any = config) -> list[Tool]:
    if not getattr(cfg, "OBSERVABILITY_ENABLED", True):
        return []
    return [
        Tool(
            name="log_query",
            description=(
                "Read APEX's own audit log (JSONL events for requests, responses, errors, "
                "debug, chats, tool calls and skills). Use it to answer 'what happened?', to "
                "find a specific request or tool call, or to inspect recent errors. Filters "
                "combine (AND). Defaults to the caller's own events; pass scope='all' to read "
                "every user's audit trail. Set count_only=true for log statistics."
            ),
            parameters=_QUERY_SCHEMA,
            handler=t_log_query,
        ),
        Tool(
            name="security_audit",
            description=(
                "Scan the audit log for suspicious or malicious activity (reverse shells, "
                "remote-exec pipes, credential access, privilege escalation, destructive or "
                "exfiltration commands, brute-force login or CSRF bursts). Returns findings "
                "with severity and evidence. Use when the operator asks to check for anything "
                "suspicious, or when investigating an incident. Defaults to scanning every "
                "user's activity over the last 24h."
            ),
            parameters=_AUDIT_SCHEMA,
            handler=t_security_audit,
        ),
    ]
