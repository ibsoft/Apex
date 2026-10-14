"""Registry-facing builder for the audit-log tools.

The tool bodies live in :mod:`observability.tools` (next to the log they read);
this module exists so tool wiring follows the same ``build_<x>_tools`` shape as
every other feature and ``tools/base.py`` stays uniform.
"""
from __future__ import annotations

from typing import Any

from config import config
from observability.tools import build_log_tools


def build_observability_tools(cfg: Any = config):
    return build_log_tools(cfg)
