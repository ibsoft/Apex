"""SQLite persistence: users, OAuth tokens, conversations, messages."""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class Database:
    """Small thread-safe SQLite wrapper.

    Tables:
      users          -> identity + serialised OAuth token bundle
      conversations  -> one per chat session
      messages       -> roles/content per conversation
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock:
            self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # The settings table holds secrets (SIP password, Telegram bot token),
        # and sqlite creates the file world-readable under a normal umask, so a
        # bot token in the settings row would be readable by every local user.
        # Enforced on every open, not just at creation - a chmod elsewhere has
        # no reason to exist, and idempotent chmod is cheap.
        try:
            import os

            os.chmod(self.path, 0o600)
            for side in ("-wal", "-shm"):
                path = f"{self.path}{side}"
                if os.path.exists(path):
                    os.chmod(path, 0o600)
        except OSError:
            pass
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            TEXT PRIMARY KEY,
                    name          TEXT,
                    email         TEXT,
                    picture       TEXT,
                    tokens        TEXT,          -- JSON OAuth token bundle
                    token_scopes  TEXT,
                    created_at    REAL,
                    updated_at    REAL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id           TEXT PRIMARY KEY,
                    user_id      TEXT NOT NULL,
                    title        TEXT,
                    skill        TEXT,
                    engine       TEXT,
                    created_at   REAL,
                    updated_at   REAL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role            TEXT NOT NULL,
                    content         TEXT,
                    meta            TEXT,        -- JSON (tool events, memory badge, tokens)
                    created_at      REAL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
                CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);

                CREATE TABLE IF NOT EXISTS settings (
                    key          TEXT PRIMARY KEY,
                    value        TEXT,
                    updated_at   REAL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id              TEXT PRIMARY KEY,
                    user_id         TEXT NOT NULL,
                    title           TEXT NOT NULL,
                    prompt          TEXT NOT NULL,
                    plan            TEXT,      -- commands the model agreed to run
                    skill           TEXT,
                    schedule        TEXT NOT NULL,  -- 'cron' | 'once'
                    cron            TEXT,
                    run_at          REAL,      -- for 'once'
                    next_run        REAL,
                    last_run        REAL,
                    last_status     TEXT,      -- running | ok | error | NULL (never ran)
                    last_output     TEXT,
                    last_error      TEXT,
                    runs            INTEGER DEFAULT 0,
                    failures        INTEGER DEFAULT 0,
                    enabled         INTEGER DEFAULT 1,
                    unread          INTEGER DEFAULT 0,  -- a finished run the user has not seen
                    conversation_id TEXT,      -- where the task's own turns are stored
                    created_at      REAL,
                    updated_at      REAL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(enabled, next_run);

                CREATE TABLE IF NOT EXISTS subagents (
                    id          TEXT PRIMARY KEY,
                    user_id     TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    role        TEXT DEFAULT '',   -- system prompt / instructions
                    provider    TEXT DEFAULT '',   -- optional provider override
                    model       TEXT DEFAULT '',   -- optional model override
                    tools       TEXT DEFAULT '',   -- JSON tool-name list; empty = all
                    workspace   TEXT DEFAULT '',   -- absolute dir; created on save
                    enabled     INTEGER DEFAULT 1,
                    conversation_id TEXT DEFAULT '', -- the agent's own thread
                    created_at  REAL,
                    updated_at  REAL
                );
                CREATE INDEX IF NOT EXISTS idx_subagents_user ON subagents(user_id);

                CREATE TABLE IF NOT EXISTS subagent_runs (
                    id              TEXT PRIMARY KEY,
                    agent_id        TEXT NOT NULL,
                    user_id         TEXT NOT NULL,
                    prompt          TEXT NOT NULL,
                    status          TEXT,          -- running | ok | error
                    output          TEXT DEFAULT '',
                    error           TEXT DEFAULT '',
                    conversation_id TEXT DEFAULT '',
                    started_at      REAL,
                    finished_at     REAL,
                    unread          INTEGER DEFAULT 0,
                    created_at      REAL
                );
                CREATE INDEX IF NOT EXISTS idx_subagent_runs_agent
                    ON subagent_runs(agent_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_subagent_runs_user
                    ON subagent_runs(user_id, unread);
                """
            )

    # ---- users ------------------------------------------------------------
    def upsert_user(
        self,
        user_id: str,
        name: str,
        email: str,
        picture: str,
        tokens: dict,
        token_scopes: str,
    ) -> dict:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (id, name, email, picture, tokens, token_scopes,
                                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, email=excluded.email, picture=excluded.picture,
                    tokens=excluded.tokens, token_scopes=excluded.token_scopes,
                    updated_at=excluded.updated_at
                """,
                (
                    user_id, name, email, picture,
                    json.dumps(tokens), token_scopes, now, now,
                ),
            )
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        if isinstance(d.get("tokens"), str):
            try:
                d["tokens"] = json.loads(d["tokens"])
            except (TypeError, ValueError):
                d["tokens"] = None
        return d

    def update_user_tokens(self, user_id: str, tokens: dict, scopes: str):
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET tokens = ?, token_scopes = ?, updated_at = ? WHERE id = ?",
                (json.dumps(tokens), scopes, now, user_id),
            )

    def get_user_tokens(self, user_id: str) -> dict | None:
        row = self.get_user(user_id)
        if not row or not row.get("tokens"):
            return None
        return json.loads(row["tokens"])

    # ---- conversations ----------------------------------------------------
    def create_conversation(self, user_id: str, title: str = "", skill="", engine="") -> dict:
        conv_id = uuid.uuid4().hex
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, user_id, title, skill, engine, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conv_id, user_id, title or "New conversation", skill, engine, now, now),
            )
        return self.get_conversation(conv_id)

    def get_conversation(self, conv_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_conversations(self, user_id: str) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_conversation(self, conv_id: str, **fields):
        if not fields:
            return
        allowed = {
            "title": "title", "skill": "skill", "engine": "engine",
        }
        cols, vals = [], []
        for key, col in allowed.items():
            if key in fields:
                cols.append(f"{col} = ?")
                vals.append(fields[key])
        cols.append("updated_at = ?")
        vals.append(time.time())
        vals.append(conv_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE conversations SET {', '.join(cols)} WHERE id = ?", vals
            )

    def delete_conversation(self, conv_id: str):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))

    # ---- messages ---------------------------------------------------------
    def add_message(self, conv_id: str, role: str, content: str, meta: dict | None = None) -> int:
        serialized = self._dumps(meta)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, meta, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (conv_id, role, content, serialized, time.time()),
            )
        self.update_conversation(conv_id)
        return cur.lastrowid

    @staticmethod
    def _dumps(meta: dict | None) -> str:
        """Serialize message meta without ever dropping the message: provider
        usage payloads occasionally contain objects json can't encode, which
        would otherwise raise mid-stream and lose the saved turn."""
        try:
            return json.dumps(meta or {})
        except (TypeError, ValueError):
            return json.dumps(meta or {}, default=str)

    def list_messages(self, conv_id: str) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
                (conv_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d.get("meta") or "{}")
            out.append(d)
        return out

    # ---- tasks -------------------------------------------------------------
    # Scheduled jobs the agent runs on its own (see tools/tasks.py). `next_run`
    # is indexed because the runner's whole job is "which rows are due", and
    # `unread` is what lets the browser be told about a finished run exactly
    # once, however many tabs are open.
    TASK_COLUMNS = (
        "title", "prompt", "plan", "skill", "schedule", "cron", "run_at",
        "next_run", "last_run", "last_status", "last_output", "last_error",
        "runs", "failures", "enabled", "unread", "conversation_id",
    )

    def create_task(self, user_id: str, **fields) -> dict:
        task_id = uuid.uuid4().hex
        now = time.time()
        row = {
            "id": task_id,
            "user_id": user_id,
            "title": (fields.get("title") or "Task").strip() or "Task",
            "prompt": (fields.get("prompt") or "").strip(),
            "plan": fields.get("plan") or "",
            "skill": fields.get("skill") or "",
            "schedule": fields.get("schedule") or "cron",
            "cron": fields.get("cron") or "",
            "run_at": fields.get("run_at"),
            "next_run": fields.get("next_run"),
            "last_run": None,
            "last_status": None,
            "last_output": "",
            "last_error": "",
            "runs": 0,
            "failures": 0,
            "enabled": 1 if fields.get("enabled", True) else 0,
            "unread": 0,
            "conversation_id": fields.get("conversation_id") or "",
            "created_at": now,
            "updated_at": now,
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock, self._connect() as conn:
            conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})", tuple(row.values()))
        return self.get_task(task_id)

    def get_task(self, task_id: str, user_id: str | None = None) -> dict | None:
        with self._lock, self._connect() as conn:
            if user_id is None:
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            else:
                # Ownership is a filter, not a check the caller does afterwards:
                # a task owned by someone else must be indistinguishable from an
                # id that does not exist (404, never 403).
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
                ).fetchone()
        return dict(row) if row else None

    def list_tasks(self, user_id: str) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? ORDER BY enabled DESC, next_run ASC, created_at ASC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_all_tasks(self) -> list[dict]:
        """Every task regardless of owner. Only the scheduler thread uses this,
        to re-arm rows that came due while the process was not running."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY next_run ASC").fetchall()
        return [dict(r) for r in rows]

    def due_tasks(self, now: float, limit: int = 20) -> list[dict]:
        """Enabled tasks whose next_run has passed. Disabled and never-scheduled
        rows can never come out of here."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ? "
                "ORDER BY next_run ASC LIMIT ?",
                (now, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_task(self, task_id: str, **fields) -> dict | None:
        if not fields:
            return self.get_task(task_id)
        cols, vals = [], []
        for key in self.TASK_COLUMNS:
            if key in fields:
                cols.append(f"{key} = ?")
                vals.append(fields[key])
        if not cols:
            return self.get_task(task_id)
        cols.append("updated_at = ?")
        vals.append(time.time())
        vals.append(task_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(cols)} WHERE id = ?", vals)
        return self.get_task(task_id)

    def delete_task(self, task_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return bool(cur.rowcount)

    # ---- sub-agents --------------------------------------------------------
    # Named workers with their own model/workspace/tools (see subagents/). Runs
    # are a separate table so an agent's history survives edits, and `unread`
    # lets the tab be told about a finished background run exactly once.
    SUBAGENT_COLUMNS = (
        "name", "description", "role", "provider", "model", "tools",
        "workspace", "enabled", "conversation_id",
    )
    SUBAGENT_RUN_COLUMNS = (
        "status", "output", "error", "conversation_id", "started_at",
        "finished_at", "unread",
    )

    def create_subagent(self, user_id: str, **fields) -> dict:
        agent_id = uuid.uuid4().hex
        now = time.time()
        row = {
            "id": agent_id,
            "user_id": user_id,
            "name": (fields.get("name") or "Agent").strip() or "Agent",
            "description": fields.get("description") or "",
            "role": fields.get("role") or "",
            "provider": (fields.get("provider") or "").strip(),
            "model": (fields.get("model") or "").strip(),
            "tools": fields.get("tools") or "",
            "workspace": fields.get("workspace") or "",
            "enabled": 1 if fields.get("enabled", True) else 0,
            "created_at": now,
            "updated_at": now,
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock, self._connect() as conn:
            conn.execute(f"INSERT INTO subagents ({cols}) VALUES ({marks})", tuple(row.values()))
        return self.get_subagent(agent_id)

    def get_subagent(self, agent_id: str, user_id: str | None = None) -> dict | None:
        with self._lock, self._connect() as conn:
            if user_id is None:
                row = conn.execute(
                    "SELECT * FROM subagents WHERE id = ?", (agent_id,)
                ).fetchone()
            else:
                # Ownership is a filter, not a check afterwards: another user's
                # agent must be indistinguishable from an id that does not exist.
                row = conn.execute(
                    "SELECT * FROM subagents WHERE id = ? AND user_id = ?",
                    (agent_id, user_id),
                ).fetchone()
        return dict(row) if row else None

    def list_subagents(self, user_id: str) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subagents WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_subagent(self, agent_id: str, **fields) -> dict | None:
        if not fields:
            return self.get_subagent(agent_id)
        cols, vals = [], []
        for key in self.SUBAGENT_COLUMNS:
            if key in fields:
                value = fields[key]
                if key == "enabled":
                    value = 1 if value else 0
                cols.append(f"{key} = ?")
                vals.append(value)
        if not cols:
            return self.get_subagent(agent_id)
        cols.append("updated_at = ?")
        vals.append(time.time())
        vals.append(agent_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE subagents SET {', '.join(cols)} WHERE id = ?", vals)
        return self.get_subagent(agent_id)

    def delete_subagent(self, agent_id: str) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM subagent_runs WHERE agent_id = ?", (agent_id,))
            cur = conn.execute("DELETE FROM subagents WHERE id = ?", (agent_id,))
        return bool(cur.rowcount)

    def create_subagent_run(self, agent_id: str, user_id: str, prompt: str,
                            conversation_id: str = "") -> dict:
        run_id = uuid.uuid4().hex
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO subagent_runs (id, agent_id, user_id, prompt, status, "
                "output, error, conversation_id, started_at, finished_at, unread, created_at) "
                "VALUES (?, ?, ?, ?, 'running', '', '', ?, ?, NULL, 0, ?)",
                (run_id, agent_id, user_id, prompt, conversation_id, now, now),
            )
        return self.get_subagent_run(run_id)

    def get_subagent_run(self, run_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subagent_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_subagent_run(self, run_id: str, **fields) -> dict | None:
        if not fields:
            return self.get_subagent_run(run_id)
        cols, vals = [], []
        for key in self.SUBAGENT_RUN_COLUMNS:
            if key in fields:
                value = fields[key]
                if key == "unread":
                    value = 1 if value else 0
                cols.append(f"{key} = ?")
                vals.append(value)
        if not cols:
            return self.get_subagent_run(run_id)
        vals.append(run_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE subagent_runs SET {', '.join(cols)} WHERE id = ?", vals)
        return self.get_subagent_run(run_id)

    def list_subagent_runs(self, agent_id: str, limit: int = 20) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subagent_runs WHERE agent_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (agent_id, min(int(limit or 20), 200)),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_unread_runs(self, user_id: str, limit: int = 10) -> list[dict]:
        """Finished runs the operator has not seen yet (for the chat context
        block and the tab's unread badge)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subagent_runs WHERE user_id = ? AND unread = 1 "
                "ORDER BY finished_at DESC LIMIT ?",
                (user_id, min(int(limit or 10), 100)),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_subagent_runs_read(self, user_id: str, run_ids: list[str] | None = None) -> int:
        with self._lock, self._connect() as conn:
            if run_ids:
                marks = ", ".join("?" for _ in run_ids)
                cur = conn.execute(
                    f"UPDATE subagent_runs SET unread = 0 WHERE user_id = ? AND id IN ({marks})",
                    (user_id, *run_ids),
                )
            else:
                cur = conn.execute(
                    "UPDATE subagent_runs SET unread = 0 WHERE user_id = ? AND unread = 1",
                    (user_id,),
                )
        return cur.rowcount


# ---- settings --------------------------------------------------------
    def set_setting(self, key: str, value):
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), time.time()),
            )

    def get_setting(self, key: str):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def all_settings(self) -> dict:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def clear_settings(self, *keys: str):
        if not keys:
            return
        with self._lock, self._connect() as conn:
            conn.executemany("DELETE FROM settings WHERE key = ?", ((key,) for key in keys))


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        from config import config
        _db = Database(config.DB_PATH)
    return _db