"""Database connectors for the integration manager.

Every connector exposes the same surface:

* ``test()``            - reach the database and run ``SELECT 1`` (or equivalent);
* ``query(sql)``        - run a query, return ``{"columns", "rows", "truncated"}``;
* ``discover()``        - schemas/tables/columns/relationships;
* ``close()``           - release the connection.

Queries run on a worker thread supervised by :class:`_Executor`; when the
per-connection timeout fires the thread is abandoned and the underlying
connection is closed, so the driver wakes up and a stale handle is never reused.

Drivers for PostgreSQL / MySQL / SQL Server / Oracle are imported lazily and a
missing one yields a plain string reason instead of a traceback, because local
installs rarely carry every driver. SQLite uses the stdlib and is the testable
end-to-end path.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field

from . import deps, dialects
from .errors import ConnectorError


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    truncated: bool = False
    affected: int = 0
    operation: str = "select"


class _Executor(threading.Thread):
    """Run a callable on a worker thread; abandon it after ``timeout`` seconds."""

    def __init__(self, fn, timeout: int):
        super().__init__(daemon=True)
        self._fn = fn
        self._timeout = max(1, int(timeout))
        self.result: QueryResult | None = None
        self.error: BaseException | None = None
        self._done = threading.Event()

    def run(self):
        try:
            self.result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - reported back, not raised
            self.error = exc
        finally:
            self._done.set()

    def wait(self) -> QueryResult:
        if not self._done.wait(self._timeout):
            raise ConnectorError(f"query timed out after {self._timeout}s")
        if self.error is not None:
            if isinstance(self.error, ConnectorError):
                raise self.error
            raise ConnectorError(str(self.error)) from self.error
        return self.result


def prepare(sql: str, config: dict, kind: str) -> tuple[str, dialects.ValidatedQuery]:
    """Validate sql -> optionally add a LIMIT for the dialect -> return text."""
    validated = dialects.validate(
        sql,
        kind=kind,
        readonly=bool(config.get("readonly", True)),
        allowed_operations=config.get("allowed_operations") or ["select"],
        allowed_tables=config.get("allowed_tables") or [],
        denied_tables=config.get("denied_tables") or [],
    )
    text = sql
    if validated.operation == "select":
        text = dialects.apply_limit(
            text, int(config.get("max_rows") or 500), kind=kind
        )
    return text, validated


class SqliteConnector:
    def __init__(self, config: dict, creds: dict):
        self.config = config
        self.creds = creds
        path = str(config.get("database") or config.get("path") or "")
        if not path:
            raise ConnectorError("no database path configured for SQLite connection")
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._open_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        with self._open_lock:
            if self._conn is None:
                try:
                    self._conn = sqlite3.connect(
                        self.path, timeout=5, check_same_thread=False,
                        isolation_level=None,
                    )
                except sqlite3.Error as exc:
                    raise ConnectorError(f"cannot open SQLite database: {exc}") from exc
        return self._conn

    def test(self) -> str:
        self._connect().execute("SELECT 1").fetchone()
        return "ok"

    def query(self, sql: str) -> QueryResult:
        text, validated = prepare(sql, self.config, "sqlite")
        if validated.operation == "select":
            return self._query_fetch(text)
        # A write that passed the validator (operator enabled it): run it.
        base = int(self.config.get("timeout_seconds") or 10)
        with self._connect() as conn:
            def _run():
                cur = conn.execute(text)
                affected = cur.rowcount
                return QueryResult(affected=affected, operation=validated.operation)
            runner = _Executor(_run, base)
            runner.start()
            result = runner.wait()
            if runner.error is not None:
                raise ConnectorError(f"SQLite error: {runner.error}") from runner.error
            return result

    def _query_fetch(self, text: str) -> QueryResult:
        conn = self._connect()
        base = int(self.config.get("timeout_seconds") or 10)
        limit = int(self.config.get("max_rows") or 500)
        watchdog = threading.Timer(base, _sqlite_interrupt, args=(conn,))
        watchdog.daemon = True
        watchdog.start()
        try:
            def _run():
                cur = conn.execute(text)
                try:
                    columns = [d[0] for d in (cur.description or [])]
                    rows: list[list] = []
                    truncated = False
                    for index, row in enumerate(cur.fetchall()):
                        if index >= limit:
                            truncated = True
                            break
                        rows.append(_clean(list(row)))
                    return QueryResult(columns=columns, rows=rows, truncated=truncated)
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
            runner = _Executor(_run, base)
            runner.start()
            result = runner.wait()
            if runner.error is not None:
                raise ConnectorError(f"SQLite error: {runner.error}") from runner.error
            return result
        finally:
            watchdog.cancel()
            if not watchdog.finished:
                # the watchdog may have fired; if so, drop the poisoned handle
                _close_conn(conn)
    def discover(self) -> dict:
        conn = self._connect()
        tables = []
        cur = conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        for name, kind in cur.fetchall():
            cols = []
            try:
                prow = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            except sqlite3.Error:
                prow = []
            cols = [{"name": r[1], "type": r[2], "nullable": not r[3], "pk": bool(r[5])}
                    for r in prow]
            tables.append({"name": name, "kind": kind.lower(), "columns": cols})
        return {"kind": "sqlite", "tables": tables,
                "relationships": self._relationships(conn)}

    @staticmethod
    def _relationships(conn) -> list[dict]:
        out = []
        for (table,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchall():
            for fr in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
                out.append({"table": table, "column": fr[3],
                            "references_table": fr[2],
                            "references_column": fr[4]})
        return out

    def close(self):
        self._close()


class DriverConnector:
    """PostgreSQL / MySQL / SQL Server / Oracle over a lazy-imported driver."""

    def __init__(self, kind: str, config: dict, creds: dict):
        self.kind = kind
        self.config = config
        self.creds = creds
        self._conn = None
        self._module = None
        import_error = None
        try:
            if kind in ("postgres", "mysql", "sqlserver", "oracle"):
                self._module = deps.load_driver_module(kind)
            else:
                raise ConnectorError(f"unsupported SQL kind `{kind}`")
        except ImportError as exc:
            import_error = (
                f"the `{kind}` driver is not installed on this server ({exc.name}); "
                f"connect from the MCP tab and the raw query will also explain it"
            )
        self._import_error = import_error

    def _connect(self):
        if self._import_error:
            raise ConnectorError(self._import_error)
        if self._conn is None:
            self._precheck()
            params = self._params()
            try:
                self._conn = self._module.connect(**params, **self._module_extra())
            except Exception as exc:
                raise ConnectorError(f"{self.kind}: cannot connect: {exc}") from exc
            try:
                self._conn.autocommit = True
            except Exception:
                pass
        return self._conn

    def _precheck(self):
        """Resolve a usable SQL Server ODBC driver before the driver's own (often
        opaque) error. pyodbc can be installed while no ODBC driver is; rather
        than stopping there we auto-provision Microsoft's driver into a
        user-owned directory (no root) unless the operator disabled it."""
        if self.kind == "sqlserver":
            driver = deps.resolve_sqlserver_driver(self.config.get("driver") or "")
            if driver:
                self._driver = driver
                return
            have = ", ".join(deps._odbc_drivers()) or "none"
            wanted = str(self.config.get("driver") or "ODBC Driver 18 for SQL Server").strip()
            raise ConnectorError(
                f"SQL Server ODBC driver `{wanted}` is not installed and could not "
                f"be provisioned automatically (pyodbc sees: {have}). Install "
                f"`msodbcsql18`, allow auto-install (INTEGRATIONS_AUTO_INSTALL), or "
                f"connect to SQL Server through an MCP server in the MCP tab "
                f"instead - an MCP connection needs no ODBC driver.")

    def _module_extra(self) -> dict:
        if self.kind == "sqlserver":
            return {"autocommit": True,
                    "timeout": int(self.config.get("timeout_seconds") or 10)}
        return {}

    def _params(self) -> dict:
        c = self.config
        creds = self.creds
        user = c.get("user") or creds.get("user") or ""
        password = creds.get("password") or ""
        if self.kind == "postgres":
            params = {
                "host": c.get("host") or "127.0.0.1",
                "port": int(c.get("port") or 5432),
                "dbname": c.get("database") or "",
                "user": user,
                "password": password,
            }
            if c.get("sslmode"):
                params["sslmode"] = str(c["sslmode"])
            return params
        if self.kind == "mysql":
            return {
                "host": c.get("host") or "127.0.0.1",
                "port": int(c.get("port") or 3306),
                "database": c.get("database") or None,
                "user": user,
                "password": password,
                "charset": "utf8mb4",
            }
        if self.kind == "sqlserver":
            params = {
                "DRIVER": (getattr(self, "_driver", None)
                           or str(c.get("driver") or "ODBC Driver 18 for SQL Server")),
                "SERVER": f"{c.get('host') or '127.0.0.1'},{int(c.get('port') or 1433)}",
                "DATABASE": c.get("database") or "",
                "UID": user,
                "PWD": password,
            }
            # Driver 18 encrypts by default and rejects a self-signed cert;
            # default to trusting the server certificate so an internal
            # instance works without a CA round-trip.
            params["Encrypt"] = "yes" if c.get("encrypt", True) else "no"
            if c.get("trust_server_certificate", True):
                params["TrustServerCertificate"] = "yes"
            return params
        if self.kind == "oracle":
            return {
                "user": user,
                "password": password,
                "host": c.get("host") or "127.0.0.1",
                "port": int(c.get("port") or 1521),
                "service_name": c.get("service_name") or c.get("database") or "",
            }
        raise ConnectorError(f"unsupported SQL kind `{self.kind}`")

    def test(self) -> str:
        self._connect()
        return "ok"

    def query(self, sql: str) -> QueryResult:
        text, validated = prepare(sql, self.config, self.kind)
        timeout = int(self.config.get("timeout_seconds") or 10)
        conn = self._connect()
        runner = _Executor(
            lambda: self._query_on(conn, text, validated.operation), timeout)
        runner.start()
        try:
            result = runner.wait()
        except ConnectorError:
            # The worker may still hold a statement mid-flight on this handle.
            self._poison()
            raise
        if runner.error is not None:
            self._poison()
            raise ConnectorError(f"{self.kind} query failed: {runner.error}") from runner.error
        return result

    def _query_on(self, conn, text: str, operation: str) -> QueryResult:
        cur = conn.cursor()
        try:
            cur.execute(text)
            if operation != "select":
                conn.commit()
                return QueryResult(affected=max(cur.rowcount or 0, 0), operation=operation)
            columns = [d[0] for d in (cur.description or [])]
            limit = int(self.config.get("max_rows") or 500)
            rows = []
            truncated = False
            for index, row in enumerate(cur.fetchall()):
                if index >= limit:
                    truncated = True
                    break
                rows.append(_clean(row))
            return QueryResult(columns=columns, rows=rows, truncated=truncated)
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def discover(self) -> dict:
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                if self.kind == "postgres":
                    tables, rels = self._discover_postgres(cur)
                elif self.kind == "mysql":
                    tables = self._discover_mysql(cur)
                    rels = []
                elif self.kind == "sqlserver":
                    tables = self._discover_sqlserver(cur)
                    rels = []
                else:
                    tables = self._discover_oracle(cur)
                    rels = []
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(f"{self.kind} discovery failed: {exc}") from exc
        return {"kind": self.kind, "tables": tables, "relationships": rels}

    def _discover_postgres(self, cur):
        cur.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
            "ORDER BY table_schema, table_name"
        )
        tables = []
        for schema, name, typ in cur.fetchall():
            cur2 = cur.connection.cursor()
            cur2.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (schema, name))
            cols = [{"name": c[0], "type": c[1], "nullable": c[2] == "YES"}
                    for c in cur2.fetchall()]
            cur2.close()
            tables.append({"name": name, "schema": schema,
                           "kind": "view" if typ == "VIEW" else "table",
                           "columns": cols})
        rels = []
        try:
            cur.execute(
                "SELECT kcu.table_schema||'.'||kcu.table_name, kcu.column_name, "
                "ccu.table_schema||'.'||ccu.table_name, ccu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name=kcu.constraint_name "
                " AND tc.table_schema=kcu.table_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name=tc.constraint_name "
                " AND ccu.table_schema=tc.table_schema "
                "WHERE tc.constraint_type='FOREIGN KEY'")
            rels = [{"table": r[0], "column": r[1],
                     "references_table": r[2], "references_column": r[3]}
                    for r in cur.fetchall()]
        except Exception:
            rels = []
        return tables, rels

    def _discover_mysql(self, cur):
        db = self.config.get("database")
        # MySQL connectors scope discovery to the configured database.
        tables = []
        sql = (
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type IN ('BASE TABLE','VIEW') "
            "ORDER BY table_name")
        cur.execute(sql, (db,))
        for name, typ in cur.fetchall():
            cur2 = cur.connection.cursor()
            cur2.execute(
                "SELECT column_name, data_type, is_nullable, column_key "
                "FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (db, name))
            cols = [{"name": c[0], "type": c[1], "nullable": c[2] == "YES",
                     "pk": c[3] == "PRI"} for c in cur2.fetchall()]
            cur2.close()
            tables.append({"name": name,
                           "kind": "view" if typ == "VIEW" else "table",
                           "columns": cols})
        return tables

    def _discover_sqlserver(self, cur):
        cur.execute(
            "SELECT SCHEMA_NAME(t.schema_id), t.name, t.type_desc "
            "FROM sys.tables t ORDER BY t.name")
        tables = []
        for schema, name, type_desc in cur.fetchall():
            cur2 = cur.connection.cursor()
            cur2.execute(
                "SELECT c.name, ty.name, c.is_nullable "
                "FROM sys.columns c JOIN sys.types ty ON c.user_type_id=ty.user_type_id "
                "WHERE c.object_id=OBJECT_ID(?) ORDER BY c.column_id",
                (f"{schema}.{name}",))
            cols = [{"name": col[0], "type": col[1], "nullable": bool(col[2])}
                    for col in cur2.fetchall()]
            cur2.close()
            tables.append({"name": name, "schema": schema,
                           "kind": "view" if "VIEW" in type_desc else "table",
                           "columns": cols})
        return tables

    def _discover_oracle(self, cur):
        cur.execute(
            "SELECT table_name, 'TABLE' FROM user_tables "
            "UNION SELECT view_name, 'VIEW' FROM user_views ORDER BY 1")
        tables = []
        for name, typ in cur.fetchall():
            cur2 = cur.connection.cursor()
            cur2.execute(
                "SELECT column_name, data_type, nullable FROM user_tab_columns "
                "WHERE table_name=:1 ORDER BY column_id",
                (name,))
            cols = [{"name": c[0], "type": c[1], "nullable": c[2] == "Y"}
                    for c in cur2.fetchall()]
            cur2.close()
            tables.append({"name": name,
                           "kind": "view" if typ == "VIEW" else "table",
                           "columns": cols})
        return tables

    def _poison(self):
        """Drop a connection whose worker thread may still hold a statement."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        self._poison()


def build_sql_connector(kind: str, config: dict, creds: dict):
    if kind == "sqlite":
        return SqliteConnector(config, creds)
    if kind in ("postgres", "mysql", "sqlserver", "oracle"):
        return DriverConnector(kind, config, creds)
    raise ConnectorError(f"unsupported SQL kind `{kind}`")


def _sqlite_interrupt(conn: sqlite3.Connection) -> None:
    try:
        conn.interrupt()
    except Exception:
        pass


def _close_conn(conn):
    try:
        conn.close()
    except Exception:
        pass


def _clean(row):
    import datetime

    out = []
    for cell in row:
        if isinstance(cell, (datetime.datetime, datetime.date, datetime.time)):
            out.append(cell.isoformat())
        elif isinstance(cell, (bytes, bytearray, memoryview)):
            out.append(f"<{len(cell)} bytes>")
        elif cell is None:
            out.append(None)
        else:
            try:
                out.append(cell)
            except Exception:
                out.append(str(cell))
    return out