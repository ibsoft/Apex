"""Thread-safe JSONL audit log with size-based rotation.

One writer per process appends one JSON object per line to ``apex.jsonl``. When
the active file would cross ``max_bytes`` it is rolled to ``apex.jsonl.1.gz``,
the older generations shift up, and the oldest beyond ``backup_count`` is
removed - the same contract as :class:`logging.handlers.RotatingFileHandler`,
but each line is an independently parseable JSON record so a torn line at a
restart only costs that one line. Backups are gzip-compressed (``compress``);
the active file stays plain text so it is cheap to append and to tail.

Logging must never break the thing it observes: every filesystem operation here
swallows its errors.
"""
from __future__ import annotations

import gzip
import json
import shutil
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from observability.events import (
    DEFAULT_MAX_CHARS,
    make_record,
    parse_since,
    redact,
)


class EventLog:
    """Append-only JSONL event log with rotation and a bounded reader."""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        enabled: bool = True,
        compress: bool = True,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_read: int = 5000,
    ) -> None:
        self.dir = Path(directory)
        self.max_bytes = int(max_bytes)
        self.backup_count = max(0, int(backup_count))
        self.enabled = bool(enabled)
        self.compress = bool(compress)
        self.max_chars = int(max_chars)
        self.max_read = max(1, int(max_read))
        self._lock = threading.Lock()

    # -- writing ----------------------------------------------------------
    @property
    def base(self) -> Path:
        return self.dir / "apex.jsonl"

    def _generation(self, index: int) -> Path:
        suffix = ".gz" if self.compress else ""
        return Path(f"{self.base}.{index}{suffix}")

    def files(self) -> list[Path]:
        """Existing files, newest (active) first."""
        candidates = [self.base] + [self._generation(i) for i in range(1, self.backup_count + 1)]
        return [p for p in candidates if p.exists()]

    def log(self, level: str, category: str, event: str, **fields: Any) -> dict:
        """Build, redact and append one record. Returns the stored record."""
        record = make_record(level, category, event, self.max_chars, **fields)
        self.append(record)
        return record

    def append(self, record: dict) -> None:
        if not self.enabled:
            return
        try:
            safe = redact(record, self.max_chars)
            line = json.dumps(safe, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        except Exception:
            return
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                base = self.base
                size = base.stat().st_size if base.exists() else 0
                encoded = len(line.encode("utf-8"))
                if self.max_bytes > 0 and size > 0 and size + encoded > self.max_bytes:
                    self._rotate()
                with open(base, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                pass

    def _rotate(self) -> None:
        try:
            if self.backup_count <= 0:
                self.base.unlink(missing_ok=True)
                return
            self._generation(self.backup_count).unlink(missing_ok=True)
            for index in range(self.backup_count - 1, 0, -1):
                src = self._generation(index)
                if src.exists():
                    src.replace(self._generation(index + 1))
            if self.base.exists():
                dst = self._generation(1)
                if self.compress:
                    self._gzip_to(self.base, dst)
                else:
                    self.base.replace(dst)
        except OSError:
            pass

    @staticmethod
    def _gzip_to(src: Path, dst: Path) -> None:
        """Compress ``src`` to ``dst`` and remove ``src`` only on success.

        A failure leaves the active file untouched (the caller swallows the
        error), so a rotation that cannot complete never loses a record.
        """
        tmp = dst.parent / f"{dst.name}.tmp"
        try:
            with open(src, "rb") as fin, gzip.open(tmp, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            tmp.replace(dst)
            src.unlink(missing_ok=True)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    # -- reading ----------------------------------------------------------
    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                    return fh.read().splitlines()
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, EOFError):
            return []

    # -- reading ----------------------------------------------------------
    @staticmethod
    def _matches(record: dict, filters: dict) -> bool:
        if not filters:
            return True
        since = filters.get("since")
        if since is not None and record.get("epoch", 0) < since:
            return False
        for key in ("level", "category", "event", "tool", "user_id", "status", "source"):
            wanted = filters.get(key)
            if wanted is None or wanted == "":
                continue
            got = record.get(key)
            if key == "status":
                try:
                    if int(got) != int(wanted):
                        return False
                except (TypeError, ValueError):
                    return False
            elif key == "user_id":
                # System/security events carry no owner and belong to everyone,
                # so an owner-scoped read still sees them.
                if got and str(got) != str(wanted):
                    return False
            elif str(got) != str(wanted):
                return False
        text = filters.get("text")
        if text:
            needle = str(text).lower()
            if needle not in json.dumps(record, ensure_ascii=False, default=str).lower():
                return False
        return True

    def read(self, limit: int = 200, **filters: Any) -> list[dict]:
        """Newest-first records matching ``filters`` (see :meth:`_matches`)."""
        if not self.enabled:
            return []
        filters = dict(filters)
        filters["since"] = parse_since(filters.get("since")) if filters.get("since") else None
        limit = max(1, min(int(limit or 200), self.max_read))
        results: list[dict] = []
        for path in self.files():
            for raw in reversed(self._read_lines(path)):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(record, dict) and self._matches(record, filters):
                    results.append(record)
                    if len(results) >= limit:
                        return results
        return results

    def iter_all(self) -> Iterable[dict]:
        """Oldest-first across every generation, for offline analysis."""
        for path in reversed(self.files()):
            for raw in self._read_lines(path):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(record, dict):
                    yield record

    def stats(self) -> dict:
        by_level: Counter = Counter()
        by_category: Counter = Counter()
        total = 0
        size = 0
        for path in self.files():
            try:
                size += path.stat().st_size
            except OSError:
                pass
            for raw in self._read_lines(path):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(record, dict):
                    continue
                total += 1
                by_level[str(record.get("level", "info"))] += 1
                by_category[str(record.get("category", "?"))] += 1
        return {
            "enabled": self.enabled,
            "dir": str(self.dir),
            "files": [p.name for p in self.files()],
            "total_events": total,
            "bytes": size,
            "by_level": dict(by_level.most_common()),
            "by_category": dict(by_category.most_common()),
        }


# -- process-wide singleton -------------------------------------------------
_LOG: EventLog | None = None
_LOG_LOCK = threading.Lock()


def get_log() -> EventLog:
    """The process-wide log, built from config on first use.

    Cached so the whole process shares one writer and one lock; tests replace it
    with :func:`set_log`.
    """
    global _LOG
    if _LOG is None:
        with _LOG_LOCK:
            if _LOG is None:
                from config import config

                _LOG = EventLog(
                    getattr(config, "OBSERVABILITY_LOG_DIR", Path("data/logs")),
                    max_bytes=getattr(config, "OBSERVABILITY_MAX_BYTES", 10 * 1024 * 1024),
                    backup_count=getattr(config, "OBSERVABILITY_BACKUP_COUNT", 5),
                    enabled=getattr(config, "OBSERVABILITY_ENABLED", True),
                    compress=getattr(config, "OBSERVABILITY_COMPRESS", True),
                    max_chars=getattr(config, "OBSERVABILITY_BODY_MAX_CHARS", DEFAULT_MAX_CHARS),
                )
    return _LOG


def set_log(log: EventLog | None) -> None:
    """Install a specific log (tests) or clear the cache (``None``)."""
    global _LOG
    with _LOG_LOCK:
        _LOG = log
