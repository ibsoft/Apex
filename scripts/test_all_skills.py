#!/usr/bin/env python3
"""Validate and test every installed APEX skill. Usage: test_all_skills.py [root] [--json]"""
import sys
from _common import run

if __name__ == "__main__":
    sys.exit(run("test-all"))
