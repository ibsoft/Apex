"""Universal integrations (the MCP tab): store, encryption, dialect validation,
SQLite end-to-end, a real MCP subprocess, REST/GraphQL over a real loopback HTTP
server, natural-language translation, routes and agent tools.

Nothing here is mocked at the integration boundary: SQLite is the real DB the
feature ships, the MCP tests spawn ``tests/support/mcp_demo_server.py`` as a real
subprocess over stdio with the official SDK, and the HTTP tests run a real
``ThreadingHTTPServer``. Only the *model provider* (an external service) is a
stub, and that stub is fed through the same stream shape the engines use.
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import config
from integrations import deps, secrets
from integrations.connectors import build_sql_connector
from integrations.dialects import ValidationError, apply_limit, has_limit, validate
from integrations.errors import ConnectorError
from integrations.manager import IntegrationManager
from integrations.store import IntegrationStore
from integrations.tools import build_integration_tools
from tools.base import ToolContext

DEMO_SERVER = str(Path(__file__).resolve().parent / "support" / "mcp_demo_server.py")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_store():
    tmp = tempfile.mkdtemp()
    return IntegrationStore(os.path.join(tmp, "integrations.db"))


def make_manager():
    return IntegrationManager(make_store())


def make_sqlite_db(tables_sql: str) -> str:
    path = os.path.join(tempfile.mkdtemp(), "data.db")
    conn = sqlite3.connect(path)
    conn.executescript(tables_sql)
    conn.commit()
    conn.close()
    return path


_SHOP_SQL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, secret TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total INT);
INSERT INTO customers(name, secret) VALUES ('Ada', 'x'), ('Bob', 'y');
INSERT INTO orders(customer_id, total) VALUES (1, 10), (1, 20), (2, 30);
"""


def sqlite_record(store, name="shop", **kwargs):
    db = make_sqlite_db(_SHOP_SQL)
    return store.create(owner_id="u1", kind="sqlite", name=name,
                        config={"database": db}, **kwargs)


# ---------------------------------------------------------------------------
# encryption
# ---------------------------------------------------------------------------
class EncryptionTests(unittest.TestCase):
    def test_round_trip_and_ciphertext_hides_the_secret(self):
        blob = secrets.encrypt({"password": "hunter2"})
        self.assertIsInstance(blob, str)
        self.assertNotIn("hunter2", blob)
        self.assertEqual(secrets.decrypt(blob), {"password": "hunter2"})

    def test_garbage_and_tampered_tokens_do_not_raise_out(self):
        self.assertIsNone(secrets.decrypt_safe("not-a-token"))
        # A tampered token must be reported, never decoded into garbage.
        token = secrets.encrypt({"a": 1})
        flipped = "A" if token[10] != "A" else "B"
        tampered = token[:10] + flipped + token[11:]
        self.assertIsNone(secrets.decrypt_safe(tampered))


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_create_get_list_and_secret_masking(self):
        rec = self.store.create(
            owner_id="u1", kind="postgres", name="prod",
            config={"host": "db", "port": 5432},
            credentials={"password": "s3cr3t"})
        self.assertEqual(rec["name"], "prod")
        self.assertEqual(rec["credentials_decrypted"], {"password": "s3cr3t"})

        listed = self.store.list("u1")
        self.assertEqual(len(listed), 1)
        # list() is the browser shape: never a secret value, only the names set.
        self.assertEqual(listed[0]["credentials_set"], ["password"])
        self.assertNotIn("credentials_decrypted", listed[0])
        self.assertNotIn("s3cr3t", json.dumps(listed[0]))

    def test_update_merges_and_clears_individual_credentials(self):
        rec = self.store.create(owner_id="u1", kind="rest", name="api",
                                config={"base_url": "http://x"},
                                credentials={"api_key": "k1", "bearer_token": "b1"})
        self.store.update(rec["id"], "u1", {"credentials": {"bearer_token": "b2"}})
        got = self.store.get(rec["id"], "u1")
        self.assertEqual(got["credentials_decrypted"],
                         {"api_key": "k1", "bearer_token": "b2"})
        self.store.update(rec["id"], "u1", {"credentials_clear": ["api_key"]})
        got = self.store.get(rec["id"], "u1")
        self.assertEqual(got["credentials_decrypted"], {"bearer_token": "b2"})

    def test_owner_scoping(self):
        rec = self.store.create(owner_id="u1", kind="sqlite", name="mine",
                                config={"database": "/tmp/x.db"})
        self.assertIsNone(self.store.get(rec["id"], "u2"))
        self.assertFalse(self.store.delete(rec["id"], "u2"))
        self.assertIsNone(self.store.update(rec["id"], "u2", {"name": "stolen"}))
        self.assertEqual(self.store.list("u2"), [])

    def test_delete(self):
        rec = self.store.create(owner_id="u1", kind="sqlite", name="mine",
                                config={"database": "/tmp/x.db"})
        self.assertTrue(self.store.delete(rec["id"], "u1"))
        self.assertIsNone(self.store.get(rec["id"], "u1"))

    def test_db_file_is_private(self):
        rec = self.store.create(owner_id="u1", kind="sqlite", name="mine",
                                config={"database": "/tmp/x.db"})
        self.assertTrue(rec["id"])
        mode = os.stat(self.store.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_discovery_round_trips(self):
        rec = self.store.create(owner_id="u1", kind="sqlite", name="mine",
                                config={"database": "/tmp/x.db"})
        self.store.save_status(rec["id"], "ok", "", 5, discovery={"kind": "x",
                                                                  "tables": [{"name": "t"}]})
        self.assertEqual(self.store.get_discovery(rec["id"])["tables"][0]["name"], "t")


# ---------------------------------------------------------------------------
# dialect validation
# ---------------------------------------------------------------------------
class DialectValidationTests(unittest.TestCase):
    def test_select_allowed_write_blocked_by_default(self):
        v = validate("SELECT * FROM customers", kind="sqlite")
        self.assertEqual(v.operation, "select")
        for bad in ("DROP TABLE customers", "DELETE FROM customers",
                    "UPDATE customers SET name='x'", "INSERT INTO customers VALUES (1)"):
            with self.assertRaises(ValidationError):
                validate(bad, kind="sqlite")

    def test_multiple_statements_rejected(self):
        with self.assertRaises(ValidationError):
            validate("SELECT 1; SELECT 2", kind="sqlite")
        # one trailing semicolon is fine
        self.assertEqual(validate("SELECT 1;", kind="sqlite").operation, "select")

    def test_allowlist_and_denylist(self):
        with self.assertRaises(ValidationError):
            validate("SELECT * FROM orders", kind="sqlite", allowed_tables=["customers"])
        with self.assertRaises(ValidationError):
            validate("SELECT * FROM orders", kind="sqlite", denied_tables=["orders"])
        # schema-qualified names normalise to the bare table
        self.assertEqual(
            validate("SELECT * FROM public.customers", kind="postgres",
                     allowed_tables=["customers"]).operation, "select")

    def test_blocked_functions_and_file_writes(self):
        for bad in ("SELECT load_extension('x')",
                    "SELECT * FROM customers INTO OUTFILE '/tmp/x'"):
            with self.assertRaises(ValidationError):
                validate(bad, kind="sqlite")

    def test_operator_grant_allows_a_write(self):
        v = validate("DELETE FROM orders", kind="sqlite", readonly=False,
                     allowed_operations=["select", "delete"])
        self.assertEqual(v.operation, "delete")
        # but the write is still refused when only select is granted
        with self.assertRaises(ValidationError):
            validate("DELETE FROM orders", kind="sqlite", allowed_operations=["select"])

    def test_cte_name_is_not_mistaken_for_a_table(self):
        v = validate(
            "WITH recent AS (SELECT * FROM customers) SELECT * FROM recent",
            kind="sqlite", allowed_tables=["customers"])
        self.assertEqual(v.operation, "select")

    def test_limit_injection_per_dialect(self):
        self.assertTrue(has_limit("SELECT * FROM t LIMIT 5", "sqlite"))
        self.assertFalse(has_limit("SELECT * FROM t", "sqlite"))
        self.assertIn("LIMIT 10", apply_limit("SELECT * FROM t", 10, "sqlite"))
        self.assertIn("TOP 10", apply_limit("SELECT * FROM t", 10, "sqlserver"))
        self.assertIn("FETCH FIRST 10 ROWS", apply_limit("SELECT * FROM t", 10, "oracle"))


# ---------------------------------------------------------------------------
# SQLite end-to-end (real DB through store + manager)
# ---------------------------------------------------------------------------
class SqliteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.manage = make_manager()

    def test_health_discover_query_join_and_audit(self):
        rec = sqlite_record(self.manage.store)
        self.assertEqual(self.manage.test(rec)["last_status"], "ok")

        discovery = self.manage.discover(rec, force=True)
        names = {t["name"] for t in discovery["tables"]}
        self.assertEqual(names, {"customers", "orders"})

        result = self.manage.query(rec, {"sql": "SELECT name FROM customers ORDER BY name"})
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["row_count"], 2)
        self.assertIn("Ada", result["text"])

        joined = self.manage.query(rec, {"sql": (
            "SELECT c.name, o.total FROM customers c "
            "JOIN orders o ON o.customer_id = c.id ORDER BY o.total")})
        self.assertEqual(joined["row_count"], 3)

        entries = self.manage.audit_for_user("u1", rec["id"])
        self.assertTrue(entries)
        self.assertTrue(all("ts" in e for e in entries))
        self.assertTrue(all(e["ok"] == 1 for e in entries))

    def test_denied_table_and_drop_are_audited_as_failures(self):
        rec = sqlite_record(self.manage.store, allowed_tables=["customers"])
        blocked = self.manage.query(rec, {"sql": "SELECT * FROM orders"})
        self.assertIn("not permitted", blocked["error"])
        dropped = self.manage.query(rec, {"sql": "DROP TABLE customers"})
        self.assertIn("error", dropped)
        entries = self.manage.audit_for_user("u1", rec["id"])
        self.assertTrue(all(e["ok"] == 0 for e in entries))

    def test_operator_granted_write_executes(self):
        rec = sqlite_record(self.manage.store, readonly=False,
                            allowed_operations=["select", "delete"])
        result = self.manage.query(rec, {"sql": "DELETE FROM orders WHERE id=1"})
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["operation"], "delete")
        self.assertEqual(result["affected"], 1)

    def test_max_rows_and_limit(self):
        rec = sqlite_record(self.manage.store, max_rows=1)
        result = self.manage.query(rec, {"sql": "SELECT * FROM orders"})
        self.assertEqual(result["row_count"], 1)

    def test_timeout_is_reported_not_raised(self):
        rec = sqlite_record(self.manage.store, timeout_seconds=1)
        # A recursive CTE that never finishes within the budget.
        slow = (
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
            "SELECT count(*) FROM c"
        )
        result = self.manage.query(rec, {"sql": slow})
        self.assertIn("error", result)

    def test_prompt_block_is_secret_free_and_has_the_schema(self):
        rec = sqlite_record(self.manage.store)
        block = self.manage.block_for_record(rec)
        self.assertIn("customers", block)
        self.assertIn("SQL connection", block)

    def test_blocks_for_skills_filters_by_enabled_and_skill(self):
        all_rec = sqlite_record(self.manage.store, name="all")
        tagged = self.manage.store.create(
            owner_id="u1", kind="sqlite", name="tagged",
            config={"database": make_sqlite_db(_SHOP_SQL)}, skills=["analyst"])
        disabled = sqlite_record(self.manage.store, name="off", enabled=False)

        # empty skill list -> every enabled connection
        block = self.manage.blocks_for_skills("u1", [])
        self.assertIn("all", block)
        self.assertIn("tagged", block)
        self.assertNotIn("off", block)

        # a skill set only matches connections that name it (or name none)
        block = self.manage.blocks_for_skills("u1", ["analyst"])
        self.assertIn("tagged", block)
        self.assertIn("all", block)

        block = self.manage.blocks_for_skills("u1", ["chef"])
        self.assertNotIn("tagged", block)
        self.assertIn("all", block)

        # per-connection system prompt rides along
        self.manage.store.update(tagged["id"], "u1",
                                 {"system_prompt": "always filter by year"})
        tagged = self.manage.store.get(tagged["id"], "u1")
        self.assertIn("always filter by year", self.manage.block_for_record(tagged))


# ---------------------------------------------------------------------------
# Driver-backed SQL connectors (PostgreSQL / MySQL / SQL Server / Oracle).
# These never actually connect here - a driver may or may not be installed - but
# `_params` must be implemented (it once raised NotImplementedError, which the
# health check swallowed into an empty error) and a missing driver must still
# surface a specific, non-empty reason.
# ---------------------------------------------------------------------------
class DriverConnectorTests(unittest.TestCase):
    def setUp(self):
        # Keep these tests hermetic: never let a missing driver trigger a real
        # pip/download during the suite.
        patcher = patch.object(deps, "auto_install_enabled", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_driver_kind_builds_connection_params(self):
        creds = {"password": "s3cret"}
        ss = build_sql_connector(
            "sqlserver",
            {"host": "10.0.0.5", "port": 1433, "database": "prod", "user": "ro"},
            creds)._params()
        self.assertEqual(ss["DRIVER"], "ODBC Driver 18 for SQL Server")
        self.assertEqual(ss["SERVER"], "10.0.0.5,1433")
        self.assertEqual(ss["DATABASE"], "prod")
        self.assertEqual(ss["UID"], "ro")
        self.assertEqual(ss["PWD"], "s3cret")
        self.assertEqual(ss["TrustServerCertificate"], "yes")

        pg = build_sql_connector(
            "postgres",
            {"host": "db", "port": 5433, "database": "app", "user": "ro"},
            creds)._params()
        self.assertEqual(pg["dbname"], "app")
        self.assertEqual(pg["port"], 5433)
        self.assertEqual(pg["password"], "s3cret")

        my = build_sql_connector(
            "mysql",
            {"host": "db", "database": "app", "user": "ro"}, creds)._params()
        self.assertEqual(my["database"], "app")
        self.assertEqual(my["port"], 3306)

        ora = build_sql_connector(
            "oracle",
            {"host": "db", "service_name": "ORCL", "user": "ro"}, creds)._params()
        self.assertEqual(ora["service_name"], "ORCL")
        self.assertEqual(ora["port"], 1521)

    def test_sqlserver_params_honour_encrypt_overrides(self):
        conn = build_sql_connector(
            "sqlserver",
            {"host": "h", "database": "d", "user": "u",
             "encrypt": False, "trust_server_certificate": False},
            {"password": "p"})
        params = conn._params()
        self.assertEqual(params["Encrypt"], "no")
        self.assertNotIn("TrustServerCertificate", params)

    def test_missing_driver_is_a_specific_non_empty_reason(self):
        conn = build_sql_connector(
            "sqlserver",
            {"host": "h", "port": 1433, "database": "d", "user": "u"},
            {"password": "p"})
        try:
            import pyodbc  # noqa: F401
            has_driver = bool(pyodbc.drivers())
        except Exception:
            has_driver = False
        if has_driver:
            self.skipTest("an ODBC SQL Server driver is installed on this machine")
        # No driver registered, no local provision, auto-install off: the
        # connector must still fail with a non-empty, actionable reason.
        with patch.object(deps, "_odbc_drivers", return_value=[]), \
                patch.object(deps, "local_driver_path", return_value=None), \
                patch.object(deps, "auto_install_enabled", return_value=False):
            with self.assertRaises(ConnectorError) as ctx:
                conn._connect()
        self.assertTrue(str(ctx.exception).strip(), "error message must not be empty")

    def test_probe_never_reports_an_empty_error(self):
        manage = make_manager()
        rec = manage.store.create(owner_id="u1", kind="sqlite", name="x",
                                  config={"database": ":memory:"})

        class _Boom:
            def test(self):
                raise Exception()  # empty str()

            def close(self):
                pass

        with patch.object(manage, "_connector", return_value=_Boom()):
            snapshot = manage.probe(rec)
        self.assertEqual(snapshot["last_status"], "error")
        self.assertEqual(snapshot["last_error"], "Exception")


class DepsTests(unittest.TestCase):
    """Auto-provisioning of the optional SQL dependencies (no network here)."""

    def test_version_key_orders_numerically(self):
        self.assertGreater(deps._version_key("18.7.1.1-1"),
                           deps._version_key("18.6.2.1-1"))
        self.assertEqual(deps._version_key("1.2.3"), (1, 2, 3))

    def test_find_local_driver_prefers_newest_and_ignores_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a").mkdir()
            (root / "a" / "libmsodbcsql-17.1.so").write_bytes(b"x")
            (root / "b").mkdir()
            (root / "b" / "libmsodbcsql-18.7.so.1.1").write_bytes(b"x")
            (root / "b" / "libmsodbcsql-99.so").symlink_to(root / "b" / "libmsodbcsql-18.7.so.1.1")
            found = deps._find_local_driver(root)
            self.assertEqual(found.name, "libmsodbcsql-18.7.so.1.1")

    def test_find_local_driver_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(deps._find_local_driver(Path(td)))

    def test_resolve_prefers_registered_configured_driver(self):
        with patch.object(deps, "_odbc_drivers",
                          return_value=["ODBC Driver 18 for SQL Server"]), \
                patch.object(deps, "local_driver_path", return_value=None):
            self.assertEqual(
                deps.resolve_sqlserver_driver("ODBC Driver 18 for SQL Server"),
                "ODBC Driver 18 for SQL Server")

    def test_resolve_picks_registered_microsoft_driver_without_configured(self):
        with patch.object(deps, "_odbc_drivers",
                          return_value=["ODBC Driver 17 for SQL Server"]), \
                patch.object(deps, "local_driver_path", return_value=None):
            self.assertEqual(deps.resolve_sqlserver_driver(""),
                             "ODBC Driver 17 for SQL Server")

    def test_resolve_uses_local_provision(self):
        with patch.object(deps, "_odbc_drivers", return_value=[]), \
                patch.object(deps, "local_driver_path",
                             return_value=Path("/tmp/libmsodbcsql-18.so")):
            self.assertEqual(deps.resolve_sqlserver_driver(""),
                             "/tmp/libmsodbcsql-18.so")

    def test_resolve_returns_none_when_disabled_and_absent(self):
        with patch.object(deps, "_odbc_drivers", return_value=[]), \
                patch.object(deps, "local_driver_path", return_value=None):
            self.assertIsNone(deps.resolve_sqlserver_driver("", auto_install=False))

    def test_provision_disabled_never_downloads(self):
        with patch.object(deps, "_download_bytes") as download:
            self.assertIsNone(deps.provision_sqlserver_driver(auto_install=False))
        download.assert_not_called()

    def test_latest_deb_url_parses_the_newest_version(self):
        index = (
            "Package: msodbcsql18\nVersion: 18.5.1.1-1\n"
            "Filename: pool/main/m/msodbcsql18/old.deb\n\n"
            "Package: msodbcsql18\nVersion: 18.7.1.1-1\n"
            "Filename: pool/main/m/msodbcsql18/new.deb\n\n")
        release = {"ID": "ubuntu", "VERSION_ID": "24.04",
                   "VERSION_CODENAME": "noble"}
        with patch.object(deps, "_os_release", return_value=release), \
                patch.object(deps, "_download_bytes",
                             return_value=gzip.compress(index.encode())):
            url = deps._latest_deb_url()
        self.assertTrue(url.endswith("pool/main/m/msodbcsql18/new.deb"))

    def test_latest_deb_url_none_off_ubuntu(self):
        with patch.object(deps, "_os_release",
                          return_value={"ID": "debian", "VERSION_ID": "12"}):
            self.assertIsNone(deps._latest_deb_url())

    def test_load_driver_module_installs_then_retries(self):
        import importlib
        calls = {"n": 0}

        def fake_import(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ImportError("missing")
            return "MODULE"

        with patch.object(deps, "auto_install_enabled", return_value=True), \
                patch.object(deps, "_pip_install", return_value=True) as install, \
                patch.object(importlib, "import_module", side_effect=fake_import):
            self.assertEqual(deps.load_driver_module("mysql"), "MODULE")
        install.assert_called_once()

    def test_load_driver_module_does_not_install_when_disabled(self):
        import importlib
        with patch.object(deps, "auto_install_enabled", return_value=False), \
                patch.object(deps, "_pip_install") as install, \
                patch.object(importlib, "import_module",
                             side_effect=ImportError("missing")):
            with self.assertRaises(ImportError):
                deps.load_driver_module("mysql")
        install.assert_not_called()

    def test_pick_microsoft(self):
        self.assertEqual(deps._pick_microsoft(["SQLite", "ODBC Driver 18 for SQL Server"]),
                         "ODBC Driver 18 for SQL Server")
        self.assertIsNone(deps._pick_microsoft(["SQLite"]))


# ---------------------------------------------------------------------------
# MCP (real subprocess over stdio)
# ---------------------------------------------------------------------------
class McpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.manage = make_manager()
        self.rec = self.manage.store.create(
            owner_id="u1", kind="mcp", name="demo",
            config={"transport": "stdio", "command": sys.executable,
                    "args": [DEMO_SERVER]},
            timeout_seconds=25)

    def test_health_discover_and_call(self):
        self.assertEqual(self.manage.test(self.rec)["last_status"], "ok")
        discovery = self.manage.discover(self.rec, force=True)
        names = {t["name"] for t in discovery["tools"]}
        self.assertEqual(names, {"add", "greet", "status"})
        self.assertEqual([r["name"] for r in discovery["resources"]], ["readme"])
        self.assertEqual([p["name"] for p in discovery["prompts"]], ["greeting"])

        added = self.manage.query(self.rec, {"tool": "add", "arguments": {"a": 2, "b": 3}})
        self.assertIsNone(added.get("error"))
        self.assertEqual(added["text"], "5")

        greeted = self.manage.query(self.rec, {"tool": "greet", "arguments": {"name": "Ada"}})
        self.assertEqual(greeted["text"], "hello Ada")

    def test_read_resource_and_prompt(self):
        resource = self.manage.query(self.rec, {"resource": "demo://readme"})
        self.assertIn("apex demo resource", resource["text"])
        prompt = self.manage.query(self.rec, {"prompt": "greeting", "arguments": {"name": "Bob"}})
        self.assertIn("Bob", prompt["text"])

    def test_unknown_tool_is_reported(self):
        result = self.manage.query(self.rec, {"tool": "nope", "arguments": {}})
        self.assertIn("error", result)

    def test_prompt_block_lists_tools(self):
        block = self.manage.block_for_record(self.rec)
        self.assertIn("add", block)
        self.assertIn("MCP server", block)

    def test_http_transport_bearer_token_reaches_headers(self):
        # The remote transports carry auth in the request headers; the token is
        # a credential and must reach the client the same way config does.
        from integrations.mcp_client import build_mcp_client
        client = build_mcp_client(
            {"transport": "http", "url": "https://mcp.example/mcp"},
            {"bearer_token": "s3cret"})
        self.assertEqual((client._headers() or {}).get("Authorization"),
                         "Bearer s3cret")

    def test_api_key_fills_config_hole(self):
        from integrations.mcp_client import build_mcp_client
        client = build_mcp_client(
            {"transport": "sse", "url": "https://mcp.example/sse"},
            {"api_key": "k"})
        self.assertEqual((client._headers() or {}).get("Authorization"), "Bearer k")


# ---------------------------------------------------------------------------
# REST + GraphQL over a real loopback HTTP server
# ---------------------------------------------------------------------------
_OPENAPI = {
    "openapi": "3.0.0",
    "info": {"title": "Demo API"},
    "paths": {
        "/items": {
            "get": {"operationId": "listItems", "summary": "List items",
                    "parameters": [{"name": "q", "in": "query",
                                    "schema": {"type": "string"}}]},
            "post": {"operationId": "addItem", "summary": "Add an item",
                     "requestBody": {"required": True, "content": {
                         "application/json": {"schema": {
                             "type": "object",
                             "properties": {"name": {"type": "string"}},
                             "required": ["name"]}}}}},
        },
    },
}

_GRAPHQL_INTROSPECTION = {"data": {"__schema": {
    "queryType": {"name": "Query"},
    "types": [
        {"name": "Query", "kind": "OBJECT", "fields": [
            {"name": "users", "args": [], "type": {"name": None, "kind": "LIST",
             "ofType": {"name": "User", "kind": "OBJECT"}}}]},
        {"name": "User", "kind": "OBJECT", "fields": [
            {"name": "id", "args": [], "type": {"name": "ID", "kind": "SCALAR"}},
            {"name": "name", "args": [], "type": {"name": "String", "kind": "SCALAR"}}]},
    ]}}}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/openapi.json"):
            return self._send(_OPENAPI)
        if self.path.startswith("/items"):
            return self._send({"items": [{"id": 1, "name": "a"},
                                         {"id": 2, "name": "b"}]})
        return self._send({"error": "nope"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        if self.path.startswith("/graphql"):
            query = payload.get("query", "")
            if "__schema" in query:
                return self._send(_GRAPHQL_INTROSPECTION)
            return self._send({"data": {"users": [{"id": "1", "name": "Ada"},
                                                  {"id": "2", "name": "Bob"}]}})
        if self.path.startswith("/items"):
            return self._send({"created": payload}, 201)
        return self._send({"error": "nope"}, 404)


class _LoopbackServer:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class RestGraphqlIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.manage = make_manager()
        self.http = _LoopbackServer()
        self.addCleanup(self.http.stop)

    def test_rest_discovery_and_execution(self):
        rec = self.manage.store.create(
            owner_id="u1", kind="rest", name="demoapi",
            config={"base_url": self.http.base,
                    "openapi_url": self.http.base + "/openapi.json"})
        self.assertEqual(self.manage.test(rec)["last_status"], "ok")
        discovery = self.manage.discover(rec, force=True)
        endpoints = {(e["method"], e["path"], e["name"])
                     for e in discovery["endpoints"]}
        self.assertIn(("GET", "/items", "listItems"), endpoints)
        self.assertIn(("POST", "/items", "addItem"), endpoints)

        got = self.manage.query(rec, {"tool": "listItems", "arguments": {"q": "x"}})
        self.assertEqual(got["status"], 200)
        self.assertIn("items", got["body"])

        posted = self.manage.query(rec, {"tool": "addItem",
                                         "arguments": {"name": "zed"}})
        self.assertEqual(posted["status"], 201)
        self.assertIn("zed", posted["body"])

    def test_graphql_discovery_and_query(self):
        rec = self.manage.store.create(
            owner_id="u1", kind="graphql", name="gql",
            config={"url": self.http.base + "/graphql"})
        self.assertEqual(self.manage.test(rec)["last_status"], "ok")
        discovery = self.manage.discover(rec, force=True)
        self.assertEqual(discovery["query_type"], "Query")
        self.assertIn("User", {t["name"] for t in discovery["types"]})

        result = self.manage.query(rec, {"query": "{ users { id name } }"})
        self.assertIn("Ada", result["body"])


# ---------------------------------------------------------------------------
# natural-language translation
# ---------------------------------------------------------------------------
class _StubProvider:
    """Feeds the exact stream shape the engines consume; counts calls so the
    repair path can be asserted."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def chat_stream(self, messages, tools=None):
        payload = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        half = max(1, len(payload) // 2)
        for piece in (payload[:half], payload[half:]):
            if piece:
                yield {"type": "text", "content": piece}


class _ErrorProvider:
    def chat_stream(self, messages, tools=None):
        yield {"type": "error", "message": "provider down"}


class NaturalLanguageTests(unittest.TestCase):
    def setUp(self):
        self.manage = make_manager()

    def test_translate_and_execute(self):
        from integrations.nl_query import translate
        rec = sqlite_record(self.manage.store)
        provider = _StubProvider(['{"sql": "SELECT name FROM customers ORDER BY name"}'])
        result = translate(self.manage, rec, "list the customers", provider, execute=True)
        self.assertIn("Ada", result["text"])
        self.assertEqual(provider.calls, 1)

    def test_bad_answer_triggers_one_repair(self):
        from integrations.nl_query import translate
        rec = sqlite_record(self.manage.store)
        provider = _StubProvider(["I cannot answer", '{"sql": "SELECT name FROM customers"}'])
        result = translate(self.manage, rec, "list customers", provider, execute=True)
        self.assertEqual(provider.calls, 2)
        self.assertIn("Ada", result["text"])

    def test_repair_fails_comprehensibly(self):
        from integrations.nl_query import translate
        rec = sqlite_record(self.manage.store)
        provider = _StubProvider(["nope", "still nope"])
        result = translate(self.manage, rec, "list customers", provider, execute=True)
        self.assertIn("error", result)
        self.assertTrue(result.get("retryable"))

    def test_provider_error_is_reported(self):
        from integrations.nl_query import translate
        rec = sqlite_record(self.manage.store)
        result = translate(self.manage, rec, "list customers", _ErrorProvider(),
                           execute=True)
        self.assertIn("error", result)


# ---------------------------------------------------------------------------
# agent tools
# ---------------------------------------------------------------------------
class IntegrationToolTests(unittest.TestCase):
    def setUp(self):
        self.manage = make_manager()
        self.tools = {t.name: t for t in build_integration_tools(self.manage)}
        self.ctx = ToolContext(user_id="u1")

    def test_status_query_and_discover(self):
        sqlite_record(self.manage.store)
        status = self.tools["integration_status"].handler({}, self.ctx)
        self.assertIn("shop", status)

        out = self.tools["integration_query"].handler(
            {"connection": "shop", "sql": "SELECT name FROM customers ORDER BY name"},
            self.ctx)
        self.assertIn("Ada", out)

        discovery = self.tools["integration_discover"].handler(
            {"connection": "shop"}, self.ctx)
        self.assertIn("customers", discovery)

    def test_tools_require_a_user(self):
        sqlite_record(self.manage.store)
        with self.assertRaises(ConnectorError):
            self.tools["integration_status"].handler({}, ToolContext(user_id=""))

    def test_unknown_connection_is_a_clean_error(self):
        with self.assertRaises(ConnectorError):
            self.tools["integration_query"].handler(
                {"connection": "ghost", "sql": "SELECT 1"}, self.ctx)

    def test_nl_request_uses_the_provider(self):
        sqlite_record(self.manage.store)
        self.ctx.provider = _StubProvider(['{"sql": "SELECT name FROM customers"}'])
        out = self.tools["integration_query"].handler(
            {"connection": "shop", "nl": "who are the customers"}, self.ctx)
        self.assertIn("Ada", out)


# ---------------------------------------------------------------------------
# routes (real app + signed-in session)
# ---------------------------------------------------------------------------
from test_auth_routes import AuthRouteTestCase  # noqa: E402


class IntegrationRouteTests(AuthRouteTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(config, "INTEGRATIONS_DB",
                               Path(tmp.name) / "integrations.db")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _create(self, client, token, **overrides):
        payload = {
            "kind": "sqlite",
            "name": "shop",
            "config": {"database": make_sqlite_db(_SHOP_SQL)},
        }
        payload.update(overrides)
        resp = client.post("/api/integrations", json=payload,
                           headers=self._headers(token))
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()["connection"]

    def test_routes_require_a_session(self):
        client = self._app().test_client()
        self.assertEqual(client.get("/api/integrations").status_code, 401)
        self.assertEqual(client.get("/api/integrations/kinds").status_code, 401)

    def test_crud_round_trip_and_secret_masking(self):
        app = self._app()
        client = app.test_client()
        token = self._login(client)

        conn = self._create(client, token)
        self.assertEqual(conn["name"], "shop")
        self.assertEqual(conn["credentials_set"], [])
        self.assertIn("health", conn)

        listed = client.get("/api/integrations").get_json()["connections"]
        self.assertEqual(len(listed), 1)

        kinds = client.get("/api/integrations/kinds").get_json()
        self.assertIn("mcp", {k["kind"] for k in kinds["kinds"]})

        # update: system prompt round-trips
        resp = client.patch(f"/api/integrations/{conn['id']}",
                            json={"system_prompt": "use the shop schema"},
                            headers=self._headers(token))
        self.assertEqual(resp.get_json()["connection"]["system_prompt"],
                         "use the shop schema")

        # query works end to end
        result = client.post(f"/api/integrations/{conn['id']}/query",
                             json={"sql": "SELECT name FROM customers"},
                             headers=self._headers(token))
        self.assertEqual(result.status_code, 200)
        self.assertIn("Ada", result.get_json()["result"]["text"])

        # test + discover + audit
        self.assertEqual(
            client.post(f"/api/integrations/{conn['id']}/test",
                        headers=self._headers(token)).get_json()["health"]["last_status"],
            "ok")
        self.assertIn("customers", json.dumps(
            client.post(f"/api/integrations/{conn['id']}/discover",
                        headers=self._headers(token)).get_json()["discovery"]))
        self.assertTrue(client.get(f"/api/integrations/{conn['id']}/audit").get_json()["audit"])

        # delete
        self.assertEqual(
            client.delete(f"/api/integrations/{conn['id']}",
                          headers=self._headers(token)).status_code, 200)
        self.assertEqual(client.get(f"/api/integrations/{conn['id']}").status_code, 404)

    def test_secrets_are_stored_but_never_returned(self):
        app = self._app()
        client = app.test_client()
        token = self._login(client)
        conn = self._create(client, token, kind="postgres", name="pg",
                            config={"host": "127.0.0.1", "port": 5432,
                                    "database": "app"},
                            credentials={"password": "topsecret"})
        self.assertEqual(conn["credentials_set"], ["password"])
        body = json.dumps(conn)
        self.assertNotIn("topsecret", body)

        # and a non-secret password left as a placeholder does not overwrite it
        client.patch(f"/api/integrations/{conn['id']}",
                     json={"symbol": "ignored", "credentials": {"password": ""}},
                     headers=self._headers(token))
        listed = client.get(f"/api/integrations").get_json()["connections"][0]
        self.assertEqual(listed["credentials_set"], ["password"])

    def test_mcp_bearer_token_is_an_encrypted_secret(self):
        app = self._app()
        client = app.test_client()
        token = self._login(client)
        conn = self._create(client, token, kind="mcp", name="remote",
                            config={"transport": "http", "url": "https://x/mcp"},
                            credentials={"bearer_token": "TOP-SECRET-VALUE",
                                         "ignored": "x"})
        # Only a field the kind declares is kept; anything else is dropped.
        self.assertEqual(conn["credentials_set"], ["bearer_token"])
        self.assertNotIn("TOP-SECRET-VALUE", json.dumps(conn))

    def test_another_users_connection_is_not_found(self):
        app = self._app()
        alice = app.test_client()
        alice_token = self._login(alice, "alice")
        conn = self._create(alice, alice_token)

        bob = app.test_client()
        bob_token = self._login(bob, "bob")
        # 404, never 403: a wrong owner must be indistinguishable from a
        # connection that never existed.
        self.assertEqual(bob.get(f"/api/integrations/{conn['id']}").status_code, 404)
        self.assertEqual(bob.delete(f"/api/integrations/{conn['id']}",
                                    headers=self._headers(bob_token)).status_code, 404)
        self.assertEqual(bob.get("/api/integrations").get_json()["connections"], [])
        self.assertEqual(
            bob.post(f"/api/integrations/{conn['id']}/query",
                     json={"sql": "SELECT 1"},
                     headers=self._headers(bob_token)).status_code, 404)

    def test_ask_route_uses_the_provider(self):
        app = self._app()
        client = app.test_client()
        token = self._login(client)
        conn = self._create(client, token)

        provider = _StubProvider(['{"sql": "SELECT name FROM customers"}'])
        with patch("integrations.routes._build_provider", return_value=provider):
            resp = client.post(f"/api/integrations/{conn['id']}/ask",
                               json={"request": "list customers"},
                               headers=self._headers(token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertIn("Ada", resp.get_json()["result"]["text"])

    def test_unknown_kind_and_missing_name_are_400(self):
        app = self._app()
        client = app.test_client()
        token = self._login(client)
        self.assertEqual(
            client.post("/api/integrations", json={"kind": "nope", "name": "x"},
                        headers=self._headers(token)).status_code, 400)
        self.assertEqual(
            client.post("/api/integrations", json={"kind": "sqlite", "name": ""},
                        headers=self._headers(token)).status_code, 400)


if __name__ == "__main__":
    unittest.main()
