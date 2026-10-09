#!/usr/bin/env python3
"""Scaffold a new APEX skill. Usage: create_skill.py --name N --description D [--output DIR] [--force]"""
import sys
from _common import run

if __name__ == "__main__":
    sys.exit(run("create"))
