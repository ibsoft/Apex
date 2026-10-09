#!/usr/bin/env python3
"""Completeness report for an APEX skill. Usage: skill_report.py <name|path> [--json]"""
import sys
from _common import run

if __name__ == "__main__":
    sys.exit(run("report"))
