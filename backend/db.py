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
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, meta, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (conv_id, role, content, json.dumps(meta or {}), time.time()),
            )
        self.update_conversation(conv_id)
        return cur.lastrowid

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


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        from config import config
        _db = Database(config.DB_PATH)
    return _db