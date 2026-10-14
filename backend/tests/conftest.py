"""Test-suite isolation for the audit log.

The observability feature is on by default, so without this every test request
and tool call would append to the real ``backend/data/logs`` alongside the live
service, and the autonomous scanner thread would start. Point the log at a
throwaway directory and disable the scanner for the whole session; individual
tests that need a log build their own ``EventLog`` in a ``TemporaryDirectory``.
"""
from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import config  # noqa: E402

_log_dir = tempfile.mkdtemp(prefix="apex-test-logs-")
config.OBSERVABILITY_LOG_DIR = Path(_log_dir)
config.OBSERVABILITY_AUTO_SCAN = False

# Rebuild the process log from the patched config if it was already created.
try:
    from observability import store as _store

    _store.set_log(None)
except Exception:  # pragma: no cover - import best effort
    pass


@atexit.register
def _cleanup() -> None:
    shutil.rmtree(_log_dir, ignore_errors=True)
