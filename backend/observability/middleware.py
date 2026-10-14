"""Flask and :mod:`logging` bridges into the audit log.

The request hook records one ``request`` event per inbound call - method, path,
status, duration, the resolved user, and redacted request/response bodies when
they are small enough to be useful. The error hook records the traceback of an
unhandled route error so a failure is diagnosable from the log alone. The
logging handler forwards Python ``logging`` records (which is how ``app.logger``
and every module logger already report) so existing error/debug calls land in
the same stream.

Nothing here may raise: a broken audit hook would take the API down with it, so
every handler is defensive and the writer itself swallows filesystem errors.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from flask import g, request, session

from observability.events import DEFAULT_MAX_CHARS

# Paths whose own logging would be noise: the log API is polled by the UI, and
# the terminal drain is a 160ms non-consuming byte-cursor poll (see
# frontend/lib/pwa.ts). Recording it would churn the rotation and bury real
# security events; the terminal_command tool call that produced the output is
# still logged at the tool choke point, so no forensic signal is lost.
# An SSE reply must never be buffered to inspect its body (handled in _body).
_SKIP_PREFIXES = ("/api/logs",)
_SKIP_SUFFIXES = ("/drain",)
_TEXTUAL = ("application/json", "text/", "application/x-www-form-urlencoded")


def _skip(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _SKIP_PREFIXES) or any(
        path.endswith(suffix) for suffix in _SKIP_SUFFIXES
    )


def _client_ip() -> str:
    try:
        if getattr(_client_ip, "trust_proxy", False):
            fwd = request.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return request.remote_addr or ""
    except Exception:
        return ""


def _body(resp: Any, limit: int) -> Any:
    try:
        if getattr(resp, "is_streamed", False) or getattr(resp, "direct_passthrough", False):
            return None
        length = resp.content_length
        if length is None or length > limit:
            return None
        ctype = (resp.mimetype or "").lower()
        if ctype and not any(ctype.startswith(t) for t in ("application/json", "text/")):
            return None
        data = resp.get_data(as_text=True)
        if not data:
            return None
        if ctype.startswith("application/json"):
            try:
                return json.loads(data)
            except ValueError:
                return data
        return data
    except Exception:
        return None


def _request_body(limit: int) -> Any:
    try:
        length = request.content_length
        if not length or length > limit:
            return None
        ctype = (request.mimetype or "").lower()
        if ctype and "json" not in ctype and "text" not in ctype:
            return None
        raw = request.get_data(cache=True, as_text=True)
        if not raw:
            return None
        if "json" in ctype:
            try:
                return json.loads(raw)
            except ValueError:
                return raw
        return raw
    except Exception:
        return None


def install(app, log, config) -> None:
    """Register the request/error hooks and the logging bridge on ``app``."""
    enabled = bool(getattr(config, "OBSERVABILITY_ENABLED", True))
    if not enabled or log is None:
        return

    body_chars = int(getattr(config, "OBSERVABILITY_BODY_MAX_CHARS", DEFAULT_MAX_CHARS))
    body_limit = max(body_chars * 2, 4096)
    debug = bool(getattr(config, "OBSERVABILITY_DEBUG", False))
    _client_ip.trust_proxy = bool(getattr(config, "TRUST_PROXY", False))  # type: ignore[attr-defined]

    @app.before_request
    def _obs_begin():
        if _skip(request.path or ""):
            return None
        g.apex_obs_start = time.perf_counter()
        g.apex_obs_body = _request_body(body_limit) if debug else None
        return None

    @app.after_request
    def _obs_end(resp):
        path = request.path or ""
        if _skip(path):
            return resp
        started = getattr(g, "apex_obs_start", None)
        if started is None:
            return resp
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        sid = session.get("sid", "") if session else ""
        fields: dict[str, Any] = {
            "method": request.method,
            "path": path,
            "status": resp.status_code,
            "duration_ms": duration_ms,
            "ip": _client_ip(),
            "user_id": (session.get("user_id") if session else "") or "",
            "session": _sid_tag(sid),
            "query": request.args.to_dict(flat=True) if request.args else None,
            "request_body": getattr(g, "apex_obs_body", None),
            "response_body": _body(resp, body_limit),
        }
        level = "info"
        if resp.status_code >= 500:
            level = "error"
        elif resp.status_code >= 400:
            level = "warning"
        log.log(level, "request", "http_request", **fields)
        return resp

    # Unhandled errors are captured from Flask's own signal rather than an
    # `errorhandler(Exception)`: a registered handler would intercept the
    # exception before Flask's testing/debug propagation, silently changing
    # error semantics. The signal fires in both the propagating and the
    # 500-response paths, so nothing is missed and nothing is altered.
    def _on_exception(sender, exception, **extra):
        log.log(
            "error",
            "error",
            "unhandled_exception",
            method=request.method,
            path=request.path,
            error=f"{type(exception).__name__}: {exception}",
            traceback=_format_traceback(exception),
            user_id=(session.get("user_id") if session else "") or "",
            ip=_client_ip(),
        )

    try:
        from flask import got_request_exception

        got_request_exception.connect(_on_exception, app, weak=False)
    except Exception:
        pass

    _install_logging_bridge(log, debug)


def _format_traceback(exc: BaseException) -> str:
    import traceback

    try:
        return "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )[-4000:]
    except Exception:
        return ""


def _sid_tag(sid: str) -> str:
    """A short, non-reversible tag for correlating a login without storing the
    session id itself (the CSRF guard keys off it, so it is a secret)."""
    if not sid:
        return ""
    import hashlib

    return hashlib.sha256(sid.encode("utf-8", "replace")).hexdigest()[:12]


class _EventLogHandler(logging.Handler):
    """Forward standard log records into the JSONL event log."""

    def __init__(self, log) -> None:
        super().__init__()
        self._log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = record.levelname.lower()
            if level == "fatal":
                level = "critical"
            if level not in ("debug", "info", "warning", "error", "critical"):
                level = "info"
            self._log.log(
                level,
                "log",
                "log_record",
                logger=record.name,
                message=record.getMessage(),
                path=record.pathname,
                line=record.lineno,
                exc_info=bool(record.exc_info),
            )
        except Exception:
            pass


_bridge_handler: "_EventLogHandler | None" = None


def _install_logging_bridge(log, debug: bool) -> None:
    """Attach one :class:`_EventLogHandler` to the root logger.

    A single handler is reused and re-pointed at the current log, so a test that
    swaps the process log does not end up with two handlers double-writing, nor
    with records still going to a discarded log.
    """
    global _bridge_handler
    if _bridge_handler is not None:
        _bridge_handler._log = log
        _bridge_handler.setLevel(logging.DEBUG if debug else logging.INFO)
        return

    root = logging.getLogger()
    handler = _EventLogHandler(log)
    handler.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(handler)
    if root.level > (logging.DEBUG if debug else logging.INFO):
        root.setLevel(logging.DEBUG if debug else logging.INFO)
    _bridge_handler = handler

    # Werkzeug logs every request itself; the request hook already records them,
    # so forwarding these too would double the volume with less information.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def reset_logging_bridge() -> None:
    """Detach the root-logging bridge.

    Exists for tests that install the bridge against a throwaway log: without
    this the global handler would keep writing to a directory the test has
    already removed. ``create_app`` re-installs it on the next call.
    """
    global _bridge_handler
    if _bridge_handler is not None:
        try:
            logging.getLogger().removeHandler(_bridge_handler)
        except Exception:
            pass
        _bridge_handler = None
