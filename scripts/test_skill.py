#!/usr/bin/env python3
"""Validate and test one APEX skill. Usage: test_skill.py <name|path> [--json]"""
import sys
from _common import run

if __name__ == "__main__":
    sys.exit(run("test"))
