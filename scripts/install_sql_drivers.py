#!/usr/bin/env python3
"""Ensure the optional SQL dependencies are installed.

The Python drivers come from ``backend/requirements.txt``; this script covers
the one thing pip cannot - Microsoft's "ODBC Driver 18 for SQL Server", which
is a system library. It is provisioned into ``INTEGRATIONS_ODBC_DIR`` (default
``DATA_DIR/odbc``) without root, so the MCP tab's SQL Server kind works with no
manual step.

Usage: install_sql_drivers.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

try:
    from integrations.deps import main
except ModuleNotFoundError:
    venv = ROOT / ".venv" / "bin" / "python"
    if venv.exists() and Path(sys.executable).resolve() != venv.resolve():
        os.execv(str(venv), [str(venv), *sys.argv])
    raise

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
