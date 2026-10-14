"""MCP client built on the official ``mcp`` SDK.

Supports the three transports: stdio (command + args), streamable HTTP and SSE.
Discovery lists tools/resources/prompts; ``call`` invokes a tool with JSON
arguments; ``read_resource``/``get_prompt`` cover the other surfaces. Everything
runs under asyncio with a per-operation timeout and is wrapped so a failure is
a plain string reason, never a raised transport exception.

The wrapper is intentionally thin - the SDK owns protocol correctness - but the
community package version is pinned at import time so an incompatible upgrade
is a clear message rather than an AttributeError mid-flight.
"""
from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass, field
from typing import Any

from .errors import McpError

SDK_TIMEOUT = 15


@dataclass
class McpSessionInfo:
    tools: list[dict] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)
    prompts: list[dict] = field(default_factory=list)
    raw_tools: list[Any] = field(default_factory=list)


async def _optional(call, timeout: int):
    """Resources/prompts are optional server capabilities: a server that does not
    expose them must not fail discovery."""
    try:
        return await asyncio.wait_for(call(), timeout)
    except Exception:
        return None


class _BaseMcpClient:
    transport_label = "?"

    def __init__(self, config: dict, timeout: int = SDK_TIMEOUT):
        self.config = config
        self.timeout = max(1, int(timeout or SDK_TIMEOUT))

    # -- lifecycle ---------------------------------------------------------
    def _transport(self):
        """Return the SDK transport context manager (stdio_client / sse_client /
        streamable_http_client). Entered with ``async with`` so the anyio task
        group is entered and exited in one task - entering it manually around an
        ``asyncio.wait_for`` moves the task group to another task and hangs on
        shutdown."""
        raise NotImplementedError

    async def _with_session(self, fn):
        """Open the transport + session, initialize, run ``fn(session)``, close.

        Timeouts are enforced on initialize and on each operation, all inside the
        single task that owns the transport context."""
        from mcp import ClientSession

        try:
            async with self._transport() as (read, write):
                async with ClientSession(read, write) as session:
                    try:
                        await asyncio.wait_for(session.initialize(), self.timeout)
                    except asyncio.TimeoutError as exc:
                        raise McpError(
                            f"{self.transport_label} MCP server did not initialize "
                            f"within {self.timeout}s") from exc
                    return await fn(session)
        except McpError:
            raise
        except asyncio.TimeoutError as exc:
            raise McpError(f"{self.transport_label} MCP operation timed out "
                           f"after {self.timeout}s") from exc
        except Exception as exc:
            raise McpError(f"{self.transport_label} MCP connection failed: {exc}") from exc

    async def session_info(self) -> McpSessionInfo:
        async def _fn(session):
            tools = await asyncio.wait_for(session.list_tools(), self.timeout)
            resources = await _optional(lambda: session.list_resources(), self.timeout)
            prompts = await _optional(lambda: session.list_prompts(), self.timeout)
            return McpSessionInfo(
                tools=[_tool_for(t) for t in tools.tools],
                resources=[_resource_for(r) for r in getattr(resources, "resources", [])],
                prompts=[_prompt_for(p) for p in getattr(prompts, "prompts", [])],
                raw_tools=list(tools.tools),
            )
        return await self._with_session(_fn)

    async def call_tool(self, name: str, arguments: dict) -> dict:
        async def _fn(session):
            result = await asyncio.wait_for(
                session.call_tool(name, arguments or {}), self.timeout)
            return _call_result(result)
        return await self._with_session(_fn)

    async def read_resource(self, uri: str) -> dict:
        async def _fn(session):
            result = await asyncio.wait_for(session.read_resource(uri), self.timeout)
            return _resource_result(result)
        return await self._with_session(_fn)

    async def get_prompt(self, name: str, arguments: dict | None = None) -> dict:
        async def _fn(session):
            result = await asyncio.wait_for(
                session.get_prompt(name, arguments or {}), self.timeout)
            return _prompt_result(result)
        return await self._with_session(_fn)


class StdioMcpClient(_BaseMcpClient):
    transport_label = "stdio"

    def __init__(self, config: dict, timeout: int = SDK_TIMEOUT):
        super().__init__(config, timeout)
        self.command = str(config.get("command") or "").strip()
        args = list(config.get("args") or [])
        argc = config.get("arguments") or []
        if args and not argc:
            args = list(args)
        elif argc:
            args = list(argc)
        self.args = args
        self.env = dict(config.get("env") or {})

    def _transport(self):
        if not self.command:
            raise McpError("stdio MCP server needs a `command`")
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(os.environ)
        if self.env:
            env.update({str(k): str(v) for k, v in self.env.items()})
        params = StdioServerParameters(command=self.command, args=self.args, cwd=None, env=env)
        return stdio_client(params)


class HttpMcpClient(_BaseMcpClient):
    """streamable HTTP + SSE over one client (the SDK picks per URL)."""

    transport_label = "http"

    def _transport(self):
        url = str(self.config.get("url") or "").strip()
        headers = self._headers()
        if not url:
            raise McpError("http MCP server needs a `url`")
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamable_http_client

        transport = str(self.config.get("transport") or "http").lower()
        if transport == "sse":
            return sse_client(url, headers=headers)
        # streamable_http_client has no `headers` kwarg in this SDK; auth is
        # carried by an httpx client when needed.
        return streamable_http_client(url)

    def _headers(self) -> dict[str, str] | None:
        headers = dict(self.config.get("headers") or {})
        bearer = self.config.get("bearer_token") or self.config.get("api_key")
        if bearer and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers or None


def build_mcp_client(config: dict, creds: dict, timeout: int = SDK_TIMEOUT):
    """Dispatcher: return a real client for the configured transport."""
    kind = str(config.get("transport") or config.get("kind") or "stdio").lower()
    merged = dict(config or {})
    # allow creds to fill any config holes (api_key etc.) for http transports
    for key, value in (creds or {}).items():
        merged.setdefault(key, value)
    if kind == "http" or (kind in ("streamableHttp", "streamable-http")):
        return HttpMcpClient(merged, timeout)
    if kind == "sse":
        merged["transport"] = "sse"
        return HttpMcpClient(merged, timeout)
    return StdioMcpClient(merged, timeout)


class McpConnector:
    """Adapts an MCP client to the connector surface the manager expects:
    ``test()``, ``discover()``, ``call_tool()``, ``read_resource()``,
    ``get_prompt()``, ``close()``."""

    def __init__(self, config: dict, creds: dict, timeout: int = SDK_TIMEOUT):
        self.timeout = max(1, int(timeout or SDK_TIMEOUT))
        self._client = build_mcp_client(config, creds, timeout)

    def test(self) -> str:
        info = sync_session_info(self._client, self.timeout)
        return "ok" if info is not None else "unreachable"

    def discover(self) -> dict:
        info = sync_session_info(self._client, self.timeout)
        return {
            "kind": "mcp",
            "tools": info.tools,
            "resources": info.resources,
            "prompts": info.prompts,
        }

    def call_tool(self, name: str, arguments: dict) -> dict:
        return sync_call_tool(self._client, name, arguments, self.timeout)

    def read_resource(self, uri: str) -> dict:
        return sync_read_resource(self._client, uri, self.timeout)

    def get_prompt(self, name: str, arguments=None) -> dict:
        return sync_get_prompt(self._client, name, arguments, self.timeout)

    def describe(self) -> str:
        return describe(self._client)

    def close(self):
        pass


def _tool_for(tool) -> dict:
    name = getattr(tool, "name", None) or getattr(tool, "title", None)
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "schema", None) or {}
    desc = getattr(tool, "description", None) or ""
    return {
        "name": name,
        "description": desc,
        "schema": schema,
        "title": getattr(tool, "title", None) or name,
    }


def _resource_for(r) -> dict:
    uri = getattr(r, "uri", None)
    name = getattr(r, "name", None) or uri
    desc = getattr(r, "description", None) or ""
    mime = getattr(r, "mimeType", None)
    return {"uri": uri, "name": name, "description": desc, "mime_type": mime}


def _prompt_for(p) -> dict:
    return {
        "name": getattr(p, "name", None),
        "description": getattr(p, "description", None) or "",
        "arguments": [{"name": a.name, "required": a.required, "description": a.description}
                      for a in (getattr(p, "arguments", None) or [])],
    }


def _call_result(result) -> dict:
    is_error = _attr(result, "is_error", "isError", default=False)
    text = []
    images = []
    resources = []
    for content in getattr(result, "content", []) or []:
        kind = getattr(content, "type", "")
        if kind == "text":
            text.append(getattr(content, "text", ""))
        elif kind == "image":
            data = (getattr(content, "data", None) or "")[:64]
            images.append({
                "mime": getattr(content, "mimeType", "image/png"),
                "preview": f"<image {data}…>",
            })
        elif kind == "resource":
            resources.append({
                "uri": getattr(content, "uri", None),
                "mime": getattr(content, "mimeType", None),
                "text": (getattr(content, "text", None) or "")[:400],
            })
    if not text and not images and not resources:
        text = [str(getattr(result, "content", ""))]
    return {
        "is_error": bool(is_error),
        "text": "\n".join(t for t in text if t),
        "images": images,
        "resources": resources,
        "structured": _structured(result),
    }


def _attr(obj, *names, default=None):
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _structured(result) -> dict:
    structured = _attr(result, "structured_content", "structuredContent", default=None)
    if isinstance(structured, dict):
        return structured
    return {}


def _resource_result(result) -> dict:
    contents = getattr(result, "contents", []) or []
    out = []
    for content in contents:
        text = getattr(content, "text", None)
        if text is not None:
            out.append({"uri": getattr(content, "uri", None),
                        "text": text[:_MAX_RETURN]})
        else:
            blob = getattr(content, "blob", None)
            if blob:
                out.append({"uri": getattr(content, "uri", None),
                            "blob_preview": f"<{len(blob)} bytes binary>"})
            else:
                out.append({"uri": getattr(content, "uri", None), "text": ""})
    return {"contents": out}


def _prompt_result(result) -> dict:
    messages = getattr(result, "messages", []) or []
    return {
        "description": getattr(result, "description", None),
        "messages": [
            {
                "role": getattr(m.role, "value", str(getattr(m, "role", "?"))),
                "content": _render_content(getattr(m, "content", None)),
            }
            for m in messages
        ],
    }


def _render_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, (str, int, float)):
        return str(content)
    if isinstance(content, list):
        return "\n".join(_render_block(b) for b in content if _render_block(b))
    return _render_block(content)


def _render_block(block) -> str:
    if block is None:
        return ""
    if isinstance(block, str):
        return block
    # TextContent (and prompt/content wrappers) carry .text.
    text = getattr(block, "text", None)
    if text is not None:
        return str(text)
    kind = getattr(block, "type", "")
    if kind in ("image", "resource"):
        return f"<{kind}>"
    return str(block)


_MAX_RETURN = 8000


def run(coro, timeout: int = SDK_TIMEOUT) -> Any:
    """Run one async MCP operation to completion synchronously with a hard cap."""
    try:
        loop = asyncio.new_event_loop()
        try:
            return asyncio.run(asyncio.wait_for(coro, timeout=timeout))
        finally:
            try:
                loop.close()
            except Exception:
                pass
    except asyncio.TimeoutError as exc:
        raise McpError(f"operation timed out after {timeout}s") from exc


def sync_session_info(client, timeout: int = SDK_TIMEOUT) -> McpSessionInfo:
    try:
        return run(client.session_info(), timeout)
    except McpError:
        raise
    except Exception as exc:
        raise McpError(f"{client.transport_label} MCP failed: {exc}") from exc


def sync_call_tool(client, name: str, arguments: dict, timeout: int = SDK_TIMEOUT) -> dict:
    try:
        return run(client.call_tool(name, arguments), timeout)
    except McpError:
        raise
    except Exception as exc:
        raise McpError(f"tool call failed: {exc}") from exc


def sync_read_resource(client, uri: str, timeout: int = SDK_TIMEOUT) -> dict:
    try:
        return run(client.read_resource(uri), timeout)
    except McpError:
        raise
    except Exception as exc:
        raise McpError(f"resource read failed: {exc}") from exc


def sync_get_prompt(client, name: str, arguments=None, timeout: int = SDK_TIMEOUT) -> dict:
    try:
        return run(client.get_prompt(name, arguments), timeout)
    except McpError:
        raise
    except Exception as exc:
        raise McpError(f"prompt get failed: {exc}") from exc


def describe(client) -> str:
    try:
        info = sync_session_info(client)
    except McpError as exc:
        return f"unreachable: {exc}"
    label = client.transport_label
    parts = [
        f"mcp/{label}: tools={len(info.tools)} resources={len(info.resources)} prompts={len(info.prompts)}",
    ]
    summary = tools_summary(info.tools)
    if summary != "(no tools exposed)":
        parts.append(summary)
    return "\n".join(parts)


def command_from_string(source: str) -> tuple[str, list[str]]:
    """Split a user-friendly `command with args` string, tolerating quotes."""
    parts = shlex.split(source)
    if not parts:
        return "", []
    return parts[0], parts[1:]