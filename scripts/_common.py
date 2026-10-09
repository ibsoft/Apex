"""Shared bootstrap for the skill-tooling CLIs.

These thin wrappers expose ``backend/skills/tooling.py`` at the paths the
SKILL_CREATOR spec names (``scripts/validate_skill.py`` and friends). They
re-exec under the project virtualenv when they are started with a Python that
cannot import the backend's dependencies (notably PyYAML).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"


def _reexec_in_venv() -> None:
    venv = ROOT / ".venv" / "bin" / "python"
    if venv.exists() and Path(sys.executable).resolve() != venv.resolve():
        os.execv(str(venv), [str(venv), *sys.argv])


def run(subcommand: str) -> int:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    try:
        from skills.tooling import main
    except ModuleNotFoundError:
        _reexec_in_venv()
        raise
    return main([subcommand, *sys.argv[1:]])
