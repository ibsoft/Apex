"""Natural language -> validated query for a connection.

Uses the chat provider (same one the agent runs on) to turn an operator request
into either SQL, an MCP tool call, a REST endpoint invocation or a GraphQL
query, then validates / executes the result. The model output is treated as a
*proposal*: the strict validator and the discovery cache are the authority, and
a single repair iteration is attempted before giving up.

Only read-only operations are ever generated here; write grants for SQL must be
entered by the operator for the connection and even then the request must say so.
"""
from __future__ import annotations

import json

from .manager import IntegrationManager
from .render import discovery_summary

_MAX_DISCOVERY_IN_PROMPT = 6000
_MAX_OUTPUT_CHARS = 4000


def build_brief(manage: IntegrationManager, record: dict) -> str:
    """A data-only description of the connection the model can write against."""
    try:
        discovery = manage.discover(record)
    except Exception as exc:
        return f"connection {record['name']} currently unreachable ({exc})"
    kind = record["kind"]
    parts = [f"Connection `{record['name']}` is a {kind} integration."]
    if kind in ("postgres", "mysql", "sqlserver", "sqlite", "oracle"):
        parts.append("Answer with ONLY a single SQL SELECT statement (no "
                     "semicolons or extensions), matching this schema:")
        parts.append(discovery_summary(discovery)[:_MAX_DISCOVERY_IN_PROMPT])
        parts.append(f"Return your answer as a JSON object: "
                     f'{{"sql": "<one SELECT>"}}')
    elif kind == "mcp":
        tools = discovery.get("tools") or []
        parts.append("The MCP server exposes these tools; choose one and its "
                     "arguments from the schema:")
        for t in tools[:40]:
            parts.append(f"- {t.get('name')}: {t.get('description')} "
                         f"schema={json.dumps(t.get('schema', {}))[:300]}")
        parts.append(f'Return JSON {{"tool": "<name>", "arguments": {{...}}}}')
    elif kind == "rest":
        parts.append("The REST API exposes these endpoints (operationId, "
                     "method, path):")
        for e in discovery.get("endpoints", [])[:40]:
            parts.append(f"- {e.get('name')} [{e.get('method')} {e.get('path')}]"
                         + (f" - {e.get('description')}"
                            if e.get("description") else ""))
        parts.append(f'Return JSON {{"tool": "<operationId>", '
                     f'"arguments": {{query/path/body fields}}}}')
    elif kind == "graphql":
        parts.append("The GraphQL API exposes these object types (fields are "
                     "queryable):")
        types = discovery.get("types") or []
        for t in types[:30]:
            fields = ", ".join(f.get("name") for f in (t.get("fields") or [])[:20])
            parts.append(f"- {t.get('name')}: {fields}")
        parts.append('Return JSON {"query": "<read-only GraphQL query>", '
                     '"variables": {...}}')
    return "\n".join(parts)


def translate(manage: IntegrationManager, record: dict, request: str,
              provider, execute: bool = False) -> dict:
    """Turn ``request`` into a payload for ``manage.query``. Tries once, repairs
    once on validation/parse failure, then returns a comprehensible error."""
    brief = build_brief(manage, record)
    prompt = (
        "You translate a user request into a call against one integration.\n"
        "The request: {request}\n\n"
        "{brief}\n\n"
        "Rules:\n"
        "- Only data you see above is real; do not invent tables, tools, fields.\n"
        "- Never use system columns, information_schema, PRAGMA or metadata queries.\n"
        "- Reply with ONE JSON object and nothing else (no markdown fences)."
    ).format(request=request[:2000], brief=brief)

    first = _ask(provider, prompt)
    # A hard provider failure (non-retryable) is final; a parse/validation
    # failure is exactly what the repair iteration exists for.
    if first.get("error") and not first.get("retryable"):
        return first
    result = first if first.get("error") else _try_payload(manage, record, first, execute)
    if result.get("error") and result.get("retryable"):
        audit = f"repair after: {result['error'][:200]}"
        repair_prompt = (
            "Your previous answer was rejected:\n"
            f"{result['error']}\n\n"
            f"{brief}\n\nReply with ONE corrected JSON object and nothing else."
        )
        second = _ask(provider, repair_prompt)
        if second.get("error"):
            return second
        result = _try_payload(manage, record, second, execute)
    return result


def _ask(provider, prompt: str) -> dict:
    try:
        reply = provider.chat_stream(
            messages=[
                {"role": "system",
                 "content": "You output JSON only, no prose, no markdown."},
                {"role": "user", "content": prompt},
            ],
            tools=None,
        )
        text = _collect(reply)
    except Exception as exc:
        return {"error": f"NL translation failed: {exc}", "retryable": True}
    if not text:
        return {"error": "NL translation returned nothing", "retryable": True}
    return _parse_json(text)


def _collect(stream) -> str:
    """Consume the provider stream the way every engine does: chunks are dicts
    tagged ``type`` (``text`` carries ``content``), an ``error`` chunk aborts.
    Also tolerates a callable returning the stream and the ``delta`` shape used
    by OpenAI-style providers."""
    if callable(stream):
        stream = stream()
    parts: list[str] = []
    total = 0
    for chunk in stream:
        if not isinstance(chunk, dict):
            continue
        ctype = chunk.get("type")
        if ctype == "error":
            raise RuntimeError(chunk.get("message") or chunk.get("error")
                               or "provider error")
        if ctype and ctype != "text":
            continue
        piece = chunk.get("content")
        if piece is None:
            delta = chunk.get("delta")
            if isinstance(delta, dict):
                piece = delta.get("content") or delta.get("text")
            elif isinstance(delta, str):
                piece = delta
        if not piece:
            continue
        total += len(piece)
        if total > _MAX_OUTPUT_CHARS:
            break
        parts.append(piece)
    return "".join(parts).strip()


def _parse_json(text: str) -> dict:
    """Parse a model JSON answer, tolerating markdown fences and trailing prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.lstrip("`").lstrip("\n")
        cleaned = cleaned.split("```")[0]
    cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
    except (ValueError, TypeError) as exc:
        # try to salvage a {...} substring
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if 0 <= start < end:
            try:
                parsed = json.loads(cleaned[start:end + 1])
            except (ValueError, TypeError):
                return {"error": f"NL answer was not JSON: {exc}", "retryable": True}
        else:
            return {"error": f"NL answer was not JSON: {exc}", "retryable": True}
    if not isinstance(parsed, dict):
        return {"error": "NL answer was not a JSON object", "retryable": True}
    return parsed


def _try_payload(manage: IntegrationManager, record: dict, payload: dict,
                 execute: bool) -> dict:
    """Validate the proposal; execute only against filters/validator."""
    kind = record["kind"]
    if kind in ("postgres", "mysql", "sqlserver", "sqlite", "oracle"):
        sql = str(payload.get("sql") or "").strip()
        if not sql:
            return {"error": "no SQL in the model answer", "retryable": True}
        if not execute:
            return {"payload": {"sql": sql}, "execute_first": False,
                    "kind": kind}
        return manage.query(record, {"sql": sql})
    if kind == "mcp":
        tool = str(payload.get("tool") or "").strip()
        if not tool:
            return {"error": "no MCP tool in the model answer", "retryable": True}
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        return manage.query(record, {"tool": tool, "arguments": arguments})
    if kind == "rest":
        tool = str(payload.get("tool") or "").strip()
        if not tool:
            return {"error": "no REST endpoint in the model answer", "retryable": True}
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        return manage.query(record, {"tool": tool, "arguments": arguments})
    if kind == "graphql":
        query = str(payload.get("query") or "").strip()
        if not query:
            return {"error": "no GraphQL query in the model answer", "retryable": True}
        variables = payload.get("variables") if isinstance(payload.get("variables"), dict) else {}
        return manage.query(record, {"query": query, "variables": variables})
    return {"error": f"unsupported kind {kind}", "retryable": False}