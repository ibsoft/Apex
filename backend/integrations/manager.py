"""IntegrationManager: the facade the app and tools talk to.

* builds connectors from stored records (decrypting credentials);
* test / query / discover with per-connection limits and audit logging;
* health cache refreshed by a monitor thread;
* per-connection system-prompt blocks for chat turns;
* never exposes a secret: ``record_for_ui`` replaces credential values with a
  set-flags list and drops the discovery cache.
"""
from __future__ import annotations

import threading
import time

from config import config

from .connectors import build_sql_connector
from .errors import ConnectorError, McpError
from .http_connector import GraphqlConnector, HttpConnectorError, RestConnector
from .mcp_client import McpConnector
from .render import discovery_summary, render_rows
from .store import IntegrationStore

_FETCH_ROWS_CAP = 6_000


_DISCOVERY_TTL_SECONDS = 300


class IntegrationManager:
    def __init__(self, store: IntegrationStore):
        self.store = store
        self._health: dict[str, dict] = {}
        self._health_lock = threading.Lock()
        self._monitor_thread: threading.Thread | None = None
        self._discovery_cache: dict[str, dict] = {}
        self._cache_lock = threading.Lock()

    # ---- lifecycle ---------------------------------------------------------
    def start_monitor(self):
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        thread = threading.Thread(target=self._monitor_loop, daemon=True,
                                  name="apex-integrations-monitor")
        thread.start()
        self._monitor_thread = thread

    def _monitor_loop(self):
        interval = max(30, int(config.INTEGRATIONS_HEALTH_INTERVAL or 300))
        while True:
            time.sleep(interval)
            try:
                for record in self.store.all_enabled():
                    try:
                        self.probe(record)
                    except Exception:
                        continue
            except Exception:
                continue

    # ---- record helpers ---------------------------------------------------
    @staticmethod
    def _effective_config(record: dict) -> dict:
        """The record stores policy (readonly, allowed_operations, table
        allow/deny lists, limits) in dedicated columns; the connectors read it
        from the config dict. Merge so the operator's settings actually reach
        the validator and the per-query limits."""
        cfg = dict(record.get("config") or {})
        cfg["readonly"] = bool(record.get("readonly", True))
        cfg["allowed_operations"] = record.get("allowed_operations") or ["select"]
        cfg["allowed_tables"] = record.get("allowed_tables") or []
        cfg["denied_tables"] = record.get("denied_tables") or []
        cfg["max_rows"] = int(record.get("max_rows") or 500)
        cfg["timeout_seconds"] = int(record.get("timeout_seconds") or 10)
        return cfg

    def _connector(self, record: dict):
        kind = record["kind"]
        cfg = self._effective_config(record)
        creds = record.get("credentials_decrypted") or {}
        if kind in ("postgres", "mysql", "sqlserver", "oracle", "sqlite"):
            return build_sql_connector(kind, cfg, creds)
        if kind == "mcp":
            return McpConnector(
                cfg, creds,
                timeout=int(record.get("timeout_seconds")
                            or config.INTEGRATIONS_MCP_TIMEOUT or 15))
        if kind == "rest":
            return RestConnector(cfg, creds)
        if kind == "graphql":
            return GraphqlConnector(cfg, creds)
        raise ConnectorError(f"unknown connection kind `{kind}`")

    def probe(self, record: dict) -> dict:
        """Test a connection, persist status, return the health snapshot."""
        started = time.time()
        try:
            connector = self._connector(record)
            try:
                result = connector.test()
            finally:
                self._close(connector)
            status, error, latency = "ok", "", _ms(started)
            self.store.save_status(record["id"], status, "", latency)
        except Exception as exc:
            status = "error"
            error = str(exc) or exc.__class__.__name__
            latency = _ms(started)
            self.store.save_status(record["id"], status, error[:300], latency)
        snapshot = {
            "last_status": status,
            "last_error": error,
            "last_latency_ms": latency,
            "last_checked_at": time.time(),
        }
        with self._health_lock:
            self._health[record["id"]] = snapshot
        return snapshot

    def health(self, record: dict) -> dict:
        with self._health_lock:
            cached = self._health.get(record["id"])
        if cached:
            return cached
        return {
            "last_status": record.get("last_status"),
            "last_error": record.get("last_error"),
            "last_latency_ms": record.get("last_latency_ms"),
            "last_checked_at": record.get("last_checked_at"),
        }

    # ---- operations ---------------------------------------------------------
    def test(self, record: dict) -> dict:
        snapshot = self.probe(record)
        self._audit(record, "connection test",
                    ok=snapshot["last_status"] == "ok",
                    error=snapshot["last_error"], duration_ms=snapshot["last_latency_ms"])
        return snapshot

    def discover(self, record: dict, force: bool = False) -> dict:
        """Fetch (network) or reuse schemas/introspection. Reuses the persisted
        copy when the in-memory cache is empty so a prompt block never spawns an
        MCP process every turn; ``force`` refreshes from the live system."""
        now = time.time()
        with self._cache_lock:
            cached = self._discovery_cache.get(record["id"])
            cached_at = self._discovery_cache.get(f"{record['id']}:ts", 0)
        if cached and now - cached_at < _DISCOVERY_TTL_SECONDS and not force:
            return cached
        if not force:
            persisted = self.store.get_discovery(record["id"])
            if persisted:
                with self._cache_lock:
                    self._discovery_cache[record["id"]] = persisted
                return persisted
        connector = self._connector(record)
        try:
            discovered = connector.discover()
        finally:
            self._close(connector)
        with self._cache_lock:
            self._discovery_cache[record["id"]] = discovered
            self._discovery_cache[f"{record['id']}:ts"] = now
        self.store.save_status(record["id"],
                               record.get("last_status") or "unknown",
                               record.get("last_error") or "",
                               record.get("last_latency_ms"),
                               discovery=_compact(discovered))
        return discovered

    def query(self, record: dict, payload: dict) -> dict:
        """Run a kind-specific operation and audit it. Returns a JSON-safe dict."""
        kind = record["kind"]
        started = time.time()
        if kind in ("postgres", "mysql", "sqlserver", "oracle", "sqlite"):
            result = self._query_sql(record, payload)
            ok = not result.get("error")
            error = result.get("error") or ""
        elif kind == "mcp":
            result, ok, error = self._query_mcp(record, payload)
        elif kind == "rest":
            result, ok, error = self._query_rest(record, payload)
        elif kind == "graphql":
            result, ok, error = self._query_graphql(record, payload)
        else:
            return {"error": f"unsupported kind `{kind}`"}
        duration = _ms(started)
        rows = int(result.get("row_count") or 0) if isinstance(result, dict) else 0
        self.store.audit(
            owner_id=record["owner_id"], connection_id=record["id"], kind=kind,
            detail=_detail_for(payload, kind), rows=rows,
            duration_ms=duration, ok=ok, error=error[:300] if error else "")
        return result

    def _query_sql(self, record: dict, payload: dict) -> dict:
        sql = str(payload.get("sql") or "").strip()
        if not sql:
            return {"error": "no SQL provided"}
        connector = self._connector(record)
        try:
            result = connector.query(sql)
        except ConnectorError as exc:
            return {"error": str(exc) or exc.__class__.__name__}
        finally:
            self._close(connector)
        text = render_rows(result.columns, result.rows,
                           max_chars=int(config.INTEGRATIONS_MAX_RESULT_CHARS or 6000))
        return {
            "columns": result.columns,
            "rows": result.rows[: _FETCH_ROWS_CAP],
            "truncated": result.truncated or len(result.rows) > _FETCH_ROWS_CAP,
            "affected": result.affected,
            "operation": result.operation,
            "text": text,
            "row_count": len(result.rows),
        }

    def _query_mcp(self, record: dict, payload: dict) -> tuple[dict, bool, str]:
        try:
            connector = self._connector(record)
        except ConnectorError as exc:
            return {"error": str(exc)}, False, str(exc)
        try:
            if "resource" in payload:
                result = connector.read_resource(str(payload["resource"]))
                return {"resources": result.get("contents", []),
                        "text": _text_of_result(result),
                        "kind": "mcp_resource"}, True, ""
            if "prompt" in payload:
                result = connector.get_prompt(str(payload["prompt"]),
                                              payload.get("arguments"))
                messages = result.get("messages", [])
                text = _text_of_result(result)
                if not text:
                    text = "\n".join(
                        str(m.get("content")) for m in messages
                        if isinstance(m, dict) and m.get("content"))
                return {"messages": messages, "text": text,
                        "kind": "mcp_prompt"}, True, ""
            tool = str(payload.get("tool") or "").strip()
            if not tool:
                return {"error": "no MCP tool name provided"}, False, "no tool"
            result = connector.call_tool(tool, payload.get("arguments") or {})
            is_error = bool(result.get("is_error"))
            text = result.get("text") or ""
            out = {
                "tool": tool,
                "is_error": is_error,
                "text": text,
                "resources": result.get("resources") or [],
                "images": result.get("images") or [],
                "structured": result.get("structured") or {},
                "kind": "mcp_tool",
            }
            if is_error:
                out["error"] = text or f"MCP tool `{tool}` returned an error"
            return out, not is_error, (text if is_error else "")
        except McpError as exc:
            return {"error": str(exc)}, False, str(exc)
        except Exception as exc:
            return {"error": f"MCP call failed: {exc}"}, False, str(exc)
        finally:
            self._close(connector)

    def _query_rest(self, record: dict, payload: dict) -> tuple[dict, bool, str]:
        tool = payload.get("tool")
        if not tool:
            return {"error": "no REST endpoint requested"}, False, "no tool"
        cfg = self._effective_config(record)
        creds = record.get("credentials_decrypted") or {}
        connector = RestConnector(cfg, creds)
        discovery = self.discover(record)
        candidates = {t["name"]: t for t in discovery.get("tools", [])}
        handler = candidates.get(tool)
        if handler is None:
            return {"error": f"unknown REST endpoint `{tool}`"}, False, f"unknown endpoint {tool}"
        try:
            result = connector.request(handler, dict(payload.get("arguments") or {}))
            ok = int(result.get("status") or 0) < 400
            return {"status": result.get("status"), "body": result.get("body"),
                    "content_type": result.get("content_type"),
                    "kind": "rest", "text": result.get("body", "")}, ok, \
                "" if ok else f"http {result.get('status')}"
        except HttpConnectorError as exc:
            return {"error": str(exc)}, False, str(exc)

    def _query_graphql(self, record: dict, payload: dict) -> tuple[dict, bool, str]:
        cfg = self._effective_config(record)
        creds = record.get("credentials_decrypted") or {}
        gql = str(payload.get("query") or "").strip()
        if gql:
            connector = GraphqlConnector(cfg, creds)
            try:
                result = connector.query(gql, payload.get("variables"))
                ok = True
                return {"status": 200, "body": result["body"], "text": result["body"],
                        "kind": "graphql"}, ok, ""
            except HttpConnectorError as exc:
                return {"error": str(exc)}, False, str(exc)
        return {"error": "no GraphQL query provided (payload key `query`)"}, False, "empty query"

    # ---- skills / prompt blocks --------------------------------------------
    def block_for_record(self, record: dict, limit: int | None = None) -> str:
        """Compact, secret-free description the model can use to query."""
        max_chars = int(limit or config.INTEGRATIONS_PROMPT_MAX_CHARS or 8000)
        kind = record["kind"]
        name = record["name"]
        parts = [f"name={name} kind={kind}"]
        if record.get("description"):
            parts.append(record["description"])
        # Per-connection instructions the operator wrote for the model.
        if (record.get("system_prompt") or "").strip():
            parts.append("operator instructions: " + record["system_prompt"].strip())
        try:
            discovery = self.discover(record)
        except Exception as exc:
            parts.append(f"(unreachable: {exc})")
            discovery = None
        if discovery:
            if kind == "mcp":
                parts.append(_mcp_block(discovery))
            elif kind in ("postgres", "mysql", "sqlserver", "sqlite", "oracle"):
                parts.append("SQL connection - use integration_query with SQL "
                             "(SELECT only), tables:")
                parts.append(discovery_summary(discovery)[:max_chars])
            elif kind == "rest":
                parts.append("REST API endpoints (integration_query tool=name "
                             "arguments={...}):")
                names = [f"{t['method']} {t['path']} -> {t['name']}"
                         for t in discovery.get("endpoints", [])]
                parts.append("\n".join(names)[:max_chars])
            elif kind == "graphql":
                parts.append("GraphQL types (integration_query query = a read "
                             "query string, variables=...):")
                types = ["- " + t["name"] for t in discovery.get("types", [])]
                parts.append("\n".join(types)[:max_chars])
        return "\n".join(parts)[:max_chars]

    def blocks_for_skills(self, owner_id: str, skills: list[str]) -> str:
        """Prompt blocks for the chat turn: only connections enabled, owned by
        the user, and matching the active skills (empty skills = all)."""
        blocks = []
        active = {s for s in skills if s}
        for record in self.store.list_raw(owner_id):
            if not record.get("enabled"):
                continue
            conn_skills = record.get("skills") or []
            if active and conn_skills:
                if not (active & set(conn_skills)):
                    continue
            blocks.append(self.block_for_record(record))
        return "\n\n".join("----------" + b for b in blocks) if blocks else ""

    # ---- audit / record helpers ---------------------------------------------
    def audit_for_user(self, owner_id: str, connection_id: str | None = None,
                       limit: int = 50) -> list[dict]:
        return self.store.audit_list(owner_id, connection_id, limit)

    def _audit(self, record: dict, message: str, *, rows: int = 0, ok: bool = True,
               error: str = "", duration_ms: float | None = None):
        self.store.audit(owner_id=record["owner_id"], connection_id=record["id"],
                         kind=record["kind"], detail=message[:300],
                         rows=rows, ok=ok, error=error, duration_ms=duration_ms)

    @staticmethod
    def _close(connector):
        try:
            close = getattr(connector, "close", None)
            if close:
                close()
        except Exception:
            pass


def _compact(discovered: dict) -> dict:
    """Discovery cache stored alongside the record: cap table/tool lists but
    keep enough shape for the REST executor to call an endpoint later."""
    out = dict(discovered)
    tables = out.get("tables") or []
    if tables:
        out["tables"] = [{"name": t.get("name"), "kind": t.get("kind"),
                          "schema": t.get("schema"),
                          "columns": t.get("columns")} for t in tables[:60]]
    tools = out.get("tools")
    if isinstance(tools, list) and tools:
        out["tools"] = [
            {k: t.get(k) for k in
             ("name", "title", "description", "method", "path", "body_fields",
              "parameters", "body_required")}
            for t in tools[:80]
        ]
    endpoints = out.get("endpoints")
    if isinstance(endpoints, list) and endpoints:
        out["endpoints"] = endpoints[:80]
    types = out.get("types")
    if isinstance(types, list) and types:
        out["types"] = types[:80]
    return out


def _mcp_block(discovery: dict) -> str:
    tools = discovery.get("tools") or []
    if tools:
        lines = ["MCP server - use integration_query tool=<name> arguments={...}:"]
        lines += [f"- {t['name']}: {t.get('description')}" for t in tools[:50]]
        return "\n".join(lines)
    return "MCP server (no tools advertised yet)"


def _ms(started: float) -> int:
    return int((time.time() - started) * 1000)


def _detail_for(payload: dict, kind: str) -> str:
    if kind in ("postgres", "mysql", "sqlserver", "sqlite", "oracle"):
        return str(payload.get("sql") or "")[:200]
    if kind == "mcp":
        return f"tool:{payload.get('tool') or payload.get('resource') or payload.get('prompt') or '?'}"
    if kind == "rest":
        return f"tool:{payload.get('tool') or '?'}"
    if kind == "graphql":
        return str(payload.get("query") or "")[:200]
    return ""


def _text_of_result(result: dict) -> str:
    text = result.get("text") or ""
    if text:
        return text
    contents = result.get("contents") or []
    out = []
    for item in contents:
        t = item.get("text") if isinstance(item, dict) else str(item)
        if t:
            out.append(t)
    return "\n".join(out)


def store_for(config_override=None) -> IntegrationStore:
    """Construct the app-wide store from config (for app.py wiring)."""
    path = config.INTEGRATIONS_DB
    return IntegrationStore(path)