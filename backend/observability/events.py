"""Event schema and secret redaction for the APEX audit log.

Every record is one JSON object on its own line. A small set of core fields is
shared by every record (``ts``, ``epoch``, ``level``, ``category``, ``event``);
the rest are optional and omitted when empty so a record stays cheap to filter.

The log is a security artifact *and* a debugging aid, so it must never store a
secret. :func:`redact` masks sensitive keys and truncates long values before a
record reaches the writer - that is the one rule callers must not skip. It is
applied in :class:`observability.store.EventLog.append`, so no caller can leak a
value by forgetting it.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

# Levels are ordered least to most severe. ``security`` is a marker for events
# the analyzer raises (a finding), not a Python logging level.
LEVELS = ("debug", "info", "warning", "error", "critical", "security")

# Categories partition the log so a reader can ask for one kind of activity.
CATEGORIES = (
    "request",     # an inbound HTTP request and its response
    "error",       # an unhandled route error or a Python log record at ERROR+
    "log",         # a forwarded Python logging record
    "chat",        # a user message or assistant reply
    "tool",        # a tool invocation and its result
    "skill",       # skill selection / routing
    "agent",       # a turn started, finished or a steer applied
    "task",        # a scheduled task run
    "system",      # the log system itself (rotation, scanner start, ...)
    "security",    # a suspicious/malicious finding
)

# Kinds of value that must never be written to disk verbatim. Matched against a
# *key* name (dict) - dropping the bare word "auth" is deliberate: it would also
# match "author", and a false positive here suppresses real content.
_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd|phrase)?|secret|token|api[_-]?key|authorization|cookie|"
    r"csrf|bearer|credential|private[_-]?key|session[_-]?id|sip_password|"
    r"bot_token|access[_-]?token|refresh[_-]?token|client[_-]?secret)",
    re.IGNORECASE,
)

_REDACTED = "[redacted]"
_MAX_DEPTH = 6
DEFAULT_MAX_CHARS = 2000


def now_iso() -> str:
    """UTC timestamp with millisecond precision and a trailing ``Z``."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _truncate(value: str, max_chars: int) -> str:
    if max_chars and len(value) > max_chars:
        return value[:max_chars] + f"...(+{len(value) - max_chars})"
    return value


def redact(value: Any, max_chars: int = DEFAULT_MAX_CHARS, _depth: int = 0) -> Any:
    """Return a copy of ``value`` safe to persist.

    Sensitive dict keys are replaced by ``"[redacted]"`` and every string is
    truncated to ``max_chars``. Recursion is bounded so a self-referential or
    absurdly nested structure cannot hang the writer.
    """
    if _depth > _MAX_DEPTH:
        return "[depth-limit]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            k = str(key)
            if _SENSITIVE_KEY.search(k):
                out[k] = _REDACTED
            else:
                out[k] = redact(item, max_chars, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [redact(item, max_chars, _depth + 1) for item in value]
    if isinstance(value, (str, bytes, bytearray)):
        if isinstance(value, (bytes, bytearray)):
            try:
                value = value.decode("utf-8", "replace")
            except Exception:
                return "[bytes]"
        return _truncate(str(value), max_chars)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        return _truncate(str(value), max_chars)
    except Exception:
        return "[unserialisable]"


def make_record(
    level: str,
    category: str,
    event: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    **fields: Any,
) -> dict:
    """Build a redacted record ready for :meth:`EventLog.append`.

    Empty/``None`` fields are dropped so a line never carries noise, and any
    mapping passed via ``extra=`` is flattened one level (its keys become fields
    only if they do not collide with the core ones).
    """
    record: dict[str, Any] = {
        "ts": now_iso(),
        "epoch": round(time.time(), 3),
        "level": level if level in LEVELS else "info",
        "category": category,
        "event": event,
    }
    for key, val in fields.items():
        if val is None or val == "" or val == [] or val == {}:
            continue
        record[key] = redact(val, max_chars)
    return record


def parse_since(value: Any) -> float | None:
    """Coerce a ``since`` filter (epoch seconds or ISO-8601) to epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        iso = text.replace("Z", "+00:00")
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None
