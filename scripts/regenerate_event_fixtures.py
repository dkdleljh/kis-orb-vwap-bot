#!/usr/bin/env python3
"""Regenerate golden event fixtures for entry/exit snapshot tests."""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    env = dict(os.environ)
    env["UPDATE_GOLDEN_FIXTURES"] = "1"
    cmd = [sys.executable, "-m", "pytest", "-q", "tests/test_event_golden_fixtures.py"]
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
