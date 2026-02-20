#!/usr/bin/env python3
"""Simple secret/log leak scanner for tracked files.

Scans git-tracked files with regex patterns for common KIS credentials/account IDs.
Intended for CI pre-merge checks (fast, low dependency).
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_FILE_HINTS = (
    ".example",
    "config.example.json",
    ".env.example",
    ".env.recommended",
    "package-lock.json",
)

PLACEHOLDER_VALUES = {
    "YOUR_APP_KEY",
    "YOUR_APP_SECRET",
    "YOUR_ACCOUNT_NO",
    "YOUR_ACCOUNT_PRODUCT_CODE",
    "REPLACE_ME",
    "CHANGEME",
    "APP_KEY",
    "APP_SECRET",
    "12345678",
    "01",
    "00",
}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "app_key",
        re.compile(r"(?i)\\b(?:KIS_APP_KEY|APP_KEY)\\b\\s*[:=]\\s*[\"']?([A-Za-z0-9_\-]{16,})"),
    ),
    (
        "app_secret",
        re.compile(r"(?i)\\b(?:KIS_APP_SECRET|APP_SECRET)\\b\\s*[:=]\\s*[\"']?([A-Za-z0-9_\-]{20,})"),
    ),
    (
        "cano",
        re.compile(r"(?i)\\b(?:KIS_ACCOUNT_NO|CANO|account_no)\\b\\s*[:=]\\s*[\"']?([0-9]{8,12})"),
    ),
    (
        "acnt_prdt_cd",
        re.compile(
            r"(?i)\\b(?:KIS_ACCOUNT_PRODUCT_CODE|ACNT_PRDT_CD|account_product_code)\\b\\s*[:=]\\s*[\"']?([0-9]{2,4})"
        ),
    ),
]


def _git_tracked_files() -> list[Path]:
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=str(ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return []
    out: list[Path] = []
    for raw in (proc.stdout or "").splitlines():
        p = ROOT / raw.strip()
        if p.is_file():
            out.append(p)
    return out


def _is_excluded(path: Path) -> bool:
    s = str(path.relative_to(ROOT))
    if "/venv/" in s or s.startswith("venv/"):
        return True
    if "/.git/" in s or s.startswith(".git/"):
        return True
    if "/node_modules/" in s or s.startswith("node_modules/"):
        return True
    return any(hint in s for hint in EXCLUDED_FILE_HINTS)


def _looks_placeholder(value: str) -> bool:
    v = value.strip().strip("\"'")
    if not v:
        return True
    if v in PLACEHOLDER_VALUES:
        return True
    if v.startswith("${") and v.endswith("}"):
        return True
    if "YOUR_" in v or "<" in v or "example" in v.lower():
        return True
    return False


def scan(files: list[Path]) -> list[tuple[Path, int, str, str]]:
    findings: list[tuple[Path, int, str, str]] = []
    for path in files:
        if _is_excluded(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for lineno, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            for name, pattern in PATTERNS:
                m = pattern.search(line)
                if not m:
                    continue
                value = (m.group(1) or "").strip()
                if _looks_placeholder(value):
                    continue
                findings.append((path, lineno, name, value[:6] + "..."))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="Optional paths to scan (default: git tracked files)")
    args = ap.parse_args()

    if args.paths:
        files = [Path(p).resolve() for p in args.paths if Path(p).is_file()]
    else:
        files = _git_tracked_files()

    findings = scan(files)
    if not findings:
        print("secret_scan=PASS")
        return 0

    print("secret_scan=FAIL")
    for path, lineno, name, masked in findings:
        rel = path.relative_to(ROOT)
        print(f"{rel}:{lineno}: {name} suspected ({masked})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
