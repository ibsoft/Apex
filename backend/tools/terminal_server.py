"""Live host terminal windows (PTY-backed).

Creates per-user pseudo-terminal sessions running the user's shell and exposes
them over small REST routes the frontend's xterm window polls:

    POST   /api/terminal/session                     create a session -> {terminal_id, rows, cols, shell}
    POST   /api/terminal/session/<id>/input          write bytes into the pty   {data: <base64>}
    POST   /api/terminal/session/<id>/resize         set the pty window size    {cols, rows}
    GET    /api/terminal/session/<id>/drain?from=N   -> {from, data(base64), closed}
    DELETE /api/terminal/session/<id>                terminate + free the session

Full interactive programs (vim, htop, ncurses, ssh) work because input and
output go through a real pty: the child process gets a controlling terminal.
Each session is owned by the signed-in user, capped per user, and idle sessions
are reaped after TERMINAL_IDLE_SECONDS so a closed browser tab cannot leak
processes forever.

A terminal is a shell on the host, so it is only available when ``run_shell``
itself is enabled (``ENABLE_RUN_SHELL=true``, see backend/config.py).

Sessions live in THIS process's memory and own a real pty fd: every request
(create/drain/input, plus the model's terminal_command/terminal_sessions) must
be served by the same process. Run the backend as a single gunicorn worker
(see deploy/systemd/apex-backend.service); multiple *processes* would silently
split the registry so a terminal "opens and closes instantly" or Apex reports
no terminal is open.
"""
from __future__ import annotations

import base64
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from flask import jsonify, request

_TERMINALS: dict[str, "TerminalSession"] = {}
_TERMINALS_LOCK = threading.Lock()

_IDLE_SECONDS = 300  # sessions not polled/touched for this long are reaped
_MAX_PER_USER = 8  # a terminal window is capped like MAX_WINDOWS
_ENABLE_ATTR = "ENABLE_RUN_SHELL"


class TerminalSession:
    """One host shell behind a pty with an append-only output buffer."""

    def __init__(self, shell: str, rows: int, cols: int):
        self.id = uuid.uuid4().hex
        self.user_id: str | None = None
        self.shell = shell
        self.rows = rows
        self.cols = cols
        self.master = -1
        self.proc: subprocess.Popen | None = None
        self.closed = False
        self.base = 0
        self.data = bytearray()
        self.lock = threading.Lock()
        self.last_touch = time.time()
        self.reader: threading.Thread | None = None
        self._closing = False
        # Independent output stream for the model (terminal_command capture).
        # The browser consumes+trims `data`; the model consumes `model_q` with
        # its own cursor, so neither consumer can lose bytes to the other.
        self.model_base = 0
        self.model_q: deque[bytes] = deque()
        self._model_len = 0
        self._model_max = 262144
        # Exact-once guard for terminal_command run mode: last payload sent and
        # when, so the model's retry loop can never re-execute a running command.
        self.last_run_payload: bytes | None = None
        self.last_run_at = 0.0
        # The most recent mode="type" write (verbatim, no Enter) still sitting
        # on the prompt. Running the same command again must only press Enter —
        # the text is already visible — so a confirm step never doubles it.
        self.last_typed: bytes = b""

    @property
    def cursor(self) -> int:
        with self.lock:
            return self.base + len(self.data)

    @property
    def model_cursor(self) -> int:
        with self.lock:
            return self.model_base + sum(len(c) for c in self.model_q)

    def _set_winsize(self, fd: int, rows: int, cols: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def start(self) -> "TerminalSession":
        master = slave = None
        try:
            master, slave = pty.openpty()
            self._set_winsize(master, self.rows, self.cols)
            env = {**os.environ, "TERM": "xterm-256color"}
            self.proc = subprocess.Popen(
                [self.shell],
                stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, close_fds=True,
                cwd=str(Path.home()),
                env=env,
            )
            self.master = master
        except Exception:
            self.closed = True
            if master is not None:
                try:
                    os.close(master)
                except OSError:
                    pass
            raise
        finally:
            if slave is not None:
                try:
                    os.close(slave)
                except OSError:
                    pass
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        return self

    def _read_loop(self) -> None:
        # Blocking reads are used instead of select(): select() only works on
        # fds < 1024, and in a busy server the pty master can land higher,
        # which would kill the loop instantly and look like "terminal opens but
        # no prompt". Closing the master (close/delete/reap) unblocks the read
        # with EBADF/EIO, so the thread still exits promptly.
        while not self.closed:
            try:
                chunk = os.read(self.master, 8192)
            except OSError:
                break
            if not chunk:
                break
            with self.lock:
                self.data += chunk
                self.model_q.append(chunk)
                self._model_len += len(chunk)
                while self._model_len > self._model_max:
                    dropped = self.model_q.popleft()
                    self._model_len -= len(dropped)
                    self.model_base += len(dropped)
            self.touch()
        with self.lock:
            self.closed = True

    def touch(self) -> None:
        with self.lock:
            self.last_touch = time.time()

    def write(self, data: bytes) -> None:
        if self.closed:
            raise RuntimeError("terminal closed")
        try:
            os.write(self.master, data)
        except OSError:
            self.close()
            raise
        self.touch()

    def resize(self, rows: int, cols: int) -> None:
        self.rows = max(2, min(200, int(rows)))
        self.cols = max(2, min(500, int(cols)))
        if self.master >= 0 and not self.closed:
            try:
                self._set_winsize(self.master, self.rows, self.cols)
            except OSError:
                pass
        self.touch()

    def drain(self, since: int, limit: int = 131072) -> tuple[int, bytes, bool]:
        """Return (new_from, new_bytes, closed) for output after ``since``.

        Consumed bytes are trimmed so a long-running buffer cannot grow without
        bound; ``from`` cursor semantics match the client's poll position.
        """
        with self.lock:
            self.last_touch = time.time()
            if since is None or since < self.base:
                since = self.base
            rel = since - self.base
            if self.closed:
                chunk = bytes(self.data[rel:])
                self.base += len(self.data) - rel
                del self.data[:]
                return self.base, chunk, True
            chunk = bytes(self.data[rel: rel + limit])
            self.base += rel + len(chunk)
            del self.data[:rel + len(chunk)]
            return self.base, chunk, False

    def model_read(self, since: int, limit: int = 131072) -> tuple[int, bytes, bool]:
        """Return (new_from, bytes, closed) for the *model* output stream.

        Independent of the browser's ``drain`` buffer: the model consumes its
        own queue with its own cursor, so both can read the full stream without
        ever stealing bytes from the other. Bytes handed out are trimmed from
        the queue; ``model_base`` always tracks the queue head.
        """
        with self.lock:
            self.last_touch = time.time()
            since = max(since, self.model_base)
            rel = since - self.model_base
            parts: list[bytes] = []
            taken = 0
            pending = rel
            for part in self.model_q:
                if pending >= len(part):
                    pending -= len(part)
                    continue
                piece = part[pending:]
                pending = 0
                parts.append(piece)
                taken += len(piece)
                if taken >= limit:
                    break
            chunk = b"".join(parts)
            if self.closed:
                self.model_q.clear()
                self._model_len = 0
                return since + taken, chunk, True
            self._drop_model(rel + taken)
            return self.model_base, chunk, False

    def _drop_model(self, consumed: int) -> None:
        todo = consumed
        dropped = 0
        while todo > 0 and self.model_q:
            head = self.model_q[0]
            if len(head) <= todo:
                self.model_q.popleft()
                self._model_len -= len(head)
                dropped += len(head)
                todo -= len(head)
            else:
                self.model_q[0] = head[todo:]
                self._model_len -= todo
                dropped += todo
                todo = 0
        self.model_base += dropped

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.closed = True
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            except OSError:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        if self.master >= 0:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = -1


def _get_session(user_id: str, terminal_id: str) -> TerminalSession | None:
    with _TERMINALS_LOCK:
        sess = _TERMINALS.get(terminal_id)
        if sess is None or sess.user_id != user_id:
            return None
        return sess


def _drop_closed(user_id: str) -> None:
    """Remove sessions that already terminated/closed for this user.

    A dead session is useless (input fails, no output arrives) and would
    otherwise linger and confuse the model's terminal listings, so they are
    cleaned up eagerly whenever a new session is opened."""
    victims: list[TerminalSession] = []
    with _TERMINALS_LOCK:
        for tid, sess in list(_TERMINALS.items()):
            if sess.user_id == user_id and sess.closed:
                del _TERMINALS[tid]
                victims.append(sess)
    for sess in victims:
        sess.close()


def _reap_idle(user_id: str, idle_seconds: int) -> None:
    now = time.time()
    victims: list[TerminalSession] = []
    with _TERMINALS_LOCK:
        for tid, sess in list(_TERMINALS.items()):
            if sess.user_id == user_id and now - sess.last_touch > idle_seconds:
                del _TERMINALS[tid]
                victims.append(sess)
    for sess in victims:
        sess.close()


def _evict_oldest(user_id: str, keep: int = _MAX_PER_USER) -> None:
    victims: list[TerminalSession] = []
    with _TERMINALS_LOCK:
        owned = [(s.last_touch, tid, s) for tid, s in _TERMINALS.items() if s.user_id == user_id]
        owned.sort(key=lambda row: row[0])
        for _touched, tid, sess in owned[: max(0, len(owned) - keep)]:
            del _TERMINALS[tid]
            victims.append(sess)
    for sess in victims:
        sess.close()


def register_terminal_routes(app, require_user, config):
    def enabled() -> bool:
        return bool(getattr(config, _ENABLE_ATTR, False))

    @app.post("/api/terminal/session")
    def terminal_create():
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to open a terminal."}), 401
        if not enabled():
            return jsonify({
                "error": "Terminal is disabled. Set ENABLE_RUN_SHELL=true to allow host terminals."
            }), 403
        payload = request.get_json(silent=True) or {}
        try:
            rows = int(payload.get("rows", 24))
            cols = int(payload.get("cols", 80))
        except (TypeError, ValueError):
            rows, cols = 24, 80
        shell = os.environ.get("SHELL", "/bin/bash")
        sess = TerminalSession(shell, rows, cols)
        sess.user_id = str(user["id"])
        try:
            sess.start()
        except Exception as err:
            return jsonify({"error": f"Could not start terminal: {err}"}), 500
        _drop_closed(sess.user_id)
        _reap_idle(sess.user_id, int(getattr(config, "TERMINAL_IDLE_SECONDS", _IDLE_SECONDS)))
        _evict_oldest(sess.user_id)
        with _TERMINALS_LOCK:
            _TERMINALS[sess.id] = sess
        return jsonify({
            "terminal_id": sess.id,
            "shell": shell,
            "rows": sess.rows,
            "cols": sess.cols,
        })

    @app.post("/api/terminal/session/<terminal_id>/input")
    def terminal_input(terminal_id):
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to use the terminal."}), 401
        sess = _get_session(str(user["id"]), terminal_id)
        if sess is None:
            return jsonify({"error": "Terminal not found."}), 404
        payload = request.get_json(silent=True) or {}
        raw = base64.b64decode(str(payload.get("data", "")))
        if not raw:
            return jsonify({"ok": True})
        try:
            sess.write(raw)
        except RuntimeError:
            return jsonify({"error": "Terminal closed."}), 410
        return jsonify({"ok": True})

    @app.post("/api/terminal/session/<terminal_id>/resize")
    def terminal_resize(terminal_id):
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to use the terminal."}), 401
        sess = _get_session(str(user["id"]), terminal_id)
        if sess is None:
            return jsonify({"error": "Terminal not found."}), 404
        payload = request.get_json(silent=True) or {}
        try:
            rows = int(payload.get("rows", sess.rows))
            cols = int(payload.get("cols", sess.cols))
        except (TypeError, ValueError):
            rows, cols = sess.rows, sess.cols
        sess.resize(rows, cols)
        return jsonify({"ok": True})

    @app.get("/api/terminal/session/<terminal_id>/drain")
    def terminal_drain(terminal_id):
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to use the terminal."}), 401
        sess = _get_session(str(user["id"]), terminal_id)
        if sess is None:
            return jsonify({"error": "Terminal not found."}), 404
        try:
            since = int(request.args.get("from", 0))
        except (TypeError, ValueError):
            since = 0
        cursor, chunk, closed = sess.drain(since)
        return jsonify({
            "from": cursor,
            "data": base64.b64encode(chunk).decode("ascii"),
            "closed": closed,
        })

    @app.delete("/api/terminal/session/<terminal_id>")
    def terminal_delete(terminal_id):
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to close the terminal."}), 401
        with _TERMINALS_LOCK:
            sess = _TERMINALS.get(terminal_id)
            if sess is None or sess.user_id != str(user["id"]):
                return jsonify({"error": "Terminal not found."}), 404
            del _TERMINALS[terminal_id]
        sess.close()
        return jsonify({"ok": True})

    return app