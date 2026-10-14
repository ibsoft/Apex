"""APEX observability: a JSONL audit log with rotation, a suspicious-activity
analyzer, agent tools to read it, an autonomous scanner and read-only REST.

Everything the app does funnels through a small number of choke points:

* ``observability.middleware`` - every HTTP request/response and unhandled error;
* the ``logging`` bridge in the same module - any ``logger.error/debug`` call;
* ``tools.base.ToolRegistry.invoke`` - every tool call (where the agent *acts*);
* the agent engines - turn start/finish, skill selection and chat messages.

The log is written by :class:`observability.store.EventLog`, which redacts
secrets and rotates files. :func:`init_app` wires the Flask pieces and starts
the scanner; :func:`record` is the one-liner other modules use.
"""
from __future__ import annotations

from observability.analyzer import analyze, summarize
from observability.events import make_record, redact
from observability.middleware import install as install_middleware
from observability.routes import register_log_routes
from observability.scanner import SecurityScanner, start_scanner
from observability.store import EventLog, get_log, set_log
from observability.tools import build_log_tools

__all__ = [
    "EventLog",
    "SecurityScanner",
    "analyze",
    "build_log_tools",
    "get_log",
    "init_app",
    "make_record",
    "record",
    "redact",
    "set_log",
    "summarize",
]


def record(level: str, category: str, event: str, **fields):
    """Append one event to the audit log. Safe to call from anywhere."""
    return get_log().log(level, category, event, **fields)


def init_app(app, require_user, config):
    """Install request logging, REST routes and the autonomous scanner.

    Defensive: observability must never be the reason the API fails to boot, so
    a failure here is logged and ignored rather than raised.
    """
    log = get_log()
    try:
        install_middleware(app, log, config)
        register_log_routes(app, require_user, config, log)
        scanner = start_scanner(log, config)
        app.extensions["apex_observability"] = {"log": log, "scanner": scanner}
        log.log("info", "system", "observability_started", auto_scan=scanner is not None)
    except Exception:
        try:
            import logging

            logging.getLogger("observability").exception("failed to install observability")
        except Exception:
            pass
    return log
