#!/usr/bin/env python3
"""Validate an APEX skill. Usage: validate_skill.py <name|path> [--json]"""
import sys
from _common import run

if __name__ == "__main__":
    sys.exit(run("validate"))
