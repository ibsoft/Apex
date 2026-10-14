"""SQLite persistence for integration connections and the audit log.

The database lives in ``DATA_DIR/integrations.db`` (chmod 0600, gitignored with
the rest of ``backend/data``). Connections embed an encrypted credentials JSON
blob; ``config`` holds non-secret fields (host, port, urls, limits...). Schema
migrations are idempotent ``CREATE TABLE IF NOT EXISTS`` + a ``user_version``
stamp, the same approach the main ``db.py`` uses.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import secrets

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id                 TEXT PRIMARY KEY,
    owner_id           TEXT NOT NULL,
    kind               TEXT NOT NULL,
    name               TEXT NOT NULL,
    description        TEXT DEFAULT '',
    config             TEXT NOT NULL,
    credentials        TEXT NOT NULL,
    system_prompt      TEXT DEFAULT '',
    enabled            INTEGER DEFAULT 1,
    readonly           INTEGER DEFAULT 1,
    allowed_tables     TEXT DEFAULT '',
    denied_tables      TEXT DEFAULT '',
    allowed_operations TEXT DEFAULT '["select"]',
    max_rows           INTEGER DEFAULT 500,
    timeout_seconds    INTEGER DEFAULT 10,
    skills             TEXT DEFAULT '',
    last_status        TEXT,
    last_error         TEXT,
    last_latency_ms    INTEGER,
    last_checked_at    REAL,
    discovery_cache    TEXT,
    created_at         REAL,
    updated_at         REAL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    owner_id      TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    kind          TEXT NOT NULL,
    detail        TEXT DEFAULT '',
    rows          INTEGER DEFAULT 0,
    duration_ms   REAL,
    ok            INTEGER DEFAULT 1,
    error         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_owner ON audit_log(owner_id, ts);
"""

_KIND_SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "postgres": ("password",),
    "mysql": ("password",),
    "sqlserver": ("password",),
    "oracle": ("password",),
    "sqlite": (),
    "mcp": ("bearer_token", "api_key"),
    "rest": ("api_key", "bearer_token", "basic_password"),
    "graphql": ("api_key", "bearer_token", "basic_password"),
}

KINDS = {
    "mcp",
    "postgres",
    "mysql",
    "sqlserver",
    "sqlite",
    "oracle",
    "rest",
    "graphql",
}


def kind_secret_fields(kind: str) -> tuple[str, ...]:
    return _KIND_SECRET_FIELDS.get(kind, ())


class IntegrationStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock:
            self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            os.chmod(self.path, 0o600)
            for side in ("-wal", "-shm"):
                p = f"{self.path}{side}"
                if os.path.exists(p):
                    os.chmod(p, 0o600)
        except OSError:  # pragma: no cover
            pass
        return conn

    def _migrate(self):
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < _SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    # ---- connections ------------------------------------------------------
    def list(self, owner_id: str) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM connections WHERE owner_id=? ORDER BY name",
                (owner_id,),
            ).fetchall()
        return [self._public(self._row(r)) for r in rows]

    def list_raw(self, owner_id: str) -> list[dict]:
        """Internal use: includes ``credentials_decrypted`` so a connector can be
        built. Never hand these to a route or the model."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM connections WHERE owner_id=? ORDER BY name",
                (owner_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def all_enabled(self) -> list[dict]:
        """Every enabled connection across owners (health monitor)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM connections WHERE enabled=1", ()
            ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, connection_id: str, owner_id: str | None = None) -> dict | None:
        sql = "SELECT * FROM connections WHERE id=?"
        args = [connection_id]
        if owner_id is not None:
            sql += " AND owner_id=?"
            args.append(owner_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, args).fetchone()
        return self._row(row) if row else None

    def create(
        self,
        *,
        owner_id: str,
        kind: str,
        name: str,
        description: str = "",
        config: dict | None = None,
        credentials: dict | None = None,
        system_prompt: str = "",
        enabled: bool = True,
        readonly: bool = True,
        allowed_tables: list[str] | None = None,
        denied_tables: list[str] | None = None,
        allowed_operations: list[str] | None = None,
        max_rows: int = 500,
        timeout_seconds: int = 10,
        skills: list[str] | None = None,
    ) -> dict:
        now = time.time()
        record = {
            "id": uuid.uuid4().hex,
            "owner_id": owner_id,
            "kind": kind,
            "name": name,
            "description": description,
            "config": json.dumps(config or {}, ensure_ascii=False),
            "credentials": secrets.encrypt(credentials or {}),
            "system_prompt": system_prompt,
            "enabled": 1 if enabled else 0,
            "readonly": 1 if readonly else 0,
            "allowed_tables": json.dumps(allowed_tables or []),
            "denied_tables": json.dumps(denied_tables or []),
            "allowed_operations": json.dumps(allowed_operations or ["select"]),
            "max_rows": int(max_rows or 500),
            "timeout_seconds": int(timeout_seconds or 10),
            "skills": json.dumps(skills or []),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO connections (id, owner_id, kind, name, description, "
                "config, credentials, system_prompt, enabled, readonly, "
                "allowed_tables, denied_tables, allowed_operations, max_rows, "
                "timeout_seconds, skills, created_at, updated_at) "
                "VALUES (:id, :owner_id, :kind, :name, :description, :config, "
                ":credentials, :system_prompt, :enabled, :readonly, "
                ":allowed_tables, :denied_tables, :allowed_operations, :max_rows, "
                ":timeout_seconds, :skills, :created_at, :updated_at)",
                record,
            )
        return self.get(record["id"], owner_id)

    def update(self, connection_id: str, owner_id: str, patch: dict) -> dict | None:
        """Partial update. ``credentials`` keys replace individual secrets,
        ``credentials_clear`` deletes named secrets; everything else is merged
        into the matching column by field name."""
        current = self.get(connection_id, owner_id)
        if current is None:
            return None
        fields = {
            "name", "description", "config", "system_prompt", "enabled",
            "readonly", "allowed_tables", "denied_tables", "allowed_operations",
            "max_rows", "timeout_seconds", "skills",
        }
        changes = {
            "updated_at": time.time(),
        }
        creds = dict(current["credentials_decrypted"] or {})
        for key, value in patch.items():
            if key == "credentials" and isinstance(value, dict):
                for k, v in value.items():
                    if v not in ("", None):
                        creds[k] = str(v)
            elif key == "credentials_clear" and isinstance(value, (list, tuple)):
                for k in value:
                    creds.pop(k, None)
            elif key == "credentials_decrypted":
                continue
            elif key == "id":
                continue
            elif key in fields:
                if key in ("enabled", "readonly") and isinstance(value, bool):
                    changes[key] = 1 if value else 0
                elif key in ("allowed_tables", "denied_tables", "allowed_operations", "skills"):
                    changes[key] = json.dumps(value or [])
                elif key in ("max_rows", "timeout_seconds") and value is not None:
                    changes[key] = int(value)
                elif key == "config" and isinstance(value, dict):
                    merged = dict(current["config"] or {})
                    merged.update(value)
                    changes[key] = json.dumps(merged, ensure_ascii=False)
                elif key == "system_prompt":
                    changes[key] = value
                elif key == "name":
                    changes[key] = str(value).strip() or current["name"]
                elif key == "description":
                    changes[key] = value
        if creds != (current["credentials_decrypted"] or {}):
            changes["credentials"] = secrets.encrypt(creds)
        assignments = ", ".join(f"{k}=?" for k in changes)
        values = list(changes.values()) + [connection_id, owner_id]
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE connections SET {assignments} WHERE id=? AND owner_id=?",
                values,
            )
        return self.get(connection_id, owner_id)

    def delete(self, connection_id: str, owner_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM connections WHERE id=? AND owner_id=?",
                (connection_id, owner_id),
            )
            return cur.rowcount > 0

    def save_status(
        self,
        connection_id: str,
        status: str,
        error: str = "",
        latency_ms: int | None = None,
        discovery: dict | None = None,
    ) -> None:
        discovery_json = json.dumps(discovery, ensure_ascii=False)[:200_000] if discovery else None
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE connections SET last_status=?, last_error=?, "
                "last_latency_ms=?, last_checked_at=?, discovery_cache=?, "
                "updated_at=? WHERE id=?",
                (status, error, latency_ms, time.time(), discovery_json,
                 time.time(), connection_id),
            )

    def get_discovery(self, connection_id: str) -> dict | None:
        """Persisted compact discovery (survives restarts, no connector call)."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT discovery_cache FROM connections WHERE id=?",
                (connection_id,),
            ).fetchone()
        if row is None or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return None

    # ---- audit ------------------------------------------------------------
    def audit(
        self,
        *,
        owner_id: str,
        connection_id: str,
        kind: str,
        detail: str = "",
        rows: int = 0,
        duration_ms: float | None = None,
        ok: bool = True,
        error: str = "",
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, owner_id, connection_id, kind, detail, "
                "rows, duration_ms, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), owner_id, connection_id, kind, detail, rows,
                 duration_ms, 1 if ok else 0, error),
            )

    def audit_list(self, owner_id: str, connection_id: str | None = None,
                   limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM audit_log WHERE owner_id=?"
        args = [owner_id]
        if connection_id:
            sql += " AND connection_id=?"
            args.append(connection_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(min(int(limit or 50), 200))
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        d = dict(row)
        secrets_blob = d.pop("credentials")
        try:
            decrypted = secrets.decrypt(secrets_blob)
        except Exception:
            decrypted = None
        d["config"] = _json_or(d.get("config"))
        d["credentials_decrypted"] = decrypted
        for field in ("allowed_tables", "denied_tables", "allowed_operations", "skills"):
            d[field] = _json_or(d.get(field), [])
        d["discovery_cache"] = _json_or(d.get("discovery_cache"), None)
        d["system_prompt_masked"] = bool(d.get("system_prompt"))
        return d

    @staticmethod
    def _public(record: dict) -> dict:
        """The shape sent to the browser: secrets replaced with a set-flags list,
        internal encrypted blob dropped, discovery cache dropped."""
        out = dict(record)
        decrypted = out.pop("credentials_decrypted", None) or {}
        out["credentials_set"] = [k for k in sorted(decrypted) if str(decrypted.get(k, "")).strip()]
        out.pop("discovery_cache", None)
        return out


def _json_or(raw, default=None):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default