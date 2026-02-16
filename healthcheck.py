"""Simple operational health helpers.

The trading engine periodically writes a JSON health file to:
    logs/health_status.json

This module provides a lightweight CLI-friendly reader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_health_status(base_dir: str) -> dict[str, Any]:
    path = Path(base_dir) / "logs" / "health_status.json"
    if not path.exists():
        return {
            "ok": False,
            "reason": "health_status_file_missing",
            "path": str(path),
        }

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("ok", True)
            data.setdefault("path", str(path))
            return data
    except Exception as e:
        return {
            "ok": False,
            "reason": "health_status_file_unreadable",
            "path": str(path),
            "error": str(e),
        }

    return {
        "ok": False,
        "reason": "health_status_invalid_format",
        "path": str(path),
    }
