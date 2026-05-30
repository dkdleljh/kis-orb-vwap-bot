#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Pull latest
if git remote >/dev/null 2>&1 && [ -n "$(git remote)" ]; then
  git pull --rebase --autostash || true
fi

# Basic secret scan (best-effort). Scan only files Git would actually sync,
# and avoid matching this script's own regex or placeholder documentation.
if command -v rg >/dev/null 2>&1; then
  scan_files="$(mktemp)"
  trap 'rm -f "$scan_files"' EXIT
  git ls-files -co --exclude-standard | rg -v '^(scripts/auto_sync\.sh|.*\.env\.example|\.env\.recommended)$' >"$scan_files" || true
  if [ -s "$scan_files" ] && xargs -r -d '\n' rg -n "(BEGIN PRIVATE KEY|KIS_APP_SECRET\s*=\s*['\"]?[A-Za-z0-9_./+=-]{20,}|gho_[A-Za-z0-9]{20,})" -S --hidden <"$scan_files" >/dev/null 2>&1; then
    echo "[auto_sync] ERROR: potential secret detected" >&2
    exit 3
  fi
fi

# Compile sanity
if [ -x ./venv/bin/python ]; then
  ./venv/bin/python -m py_compile main.py strategy_state_machine.py >/dev/null 2>&1 || true
fi

if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -m "chore(auto): sync $(date +%F)" || true
  git push || true
  echo "OK: $ROOT_DIR pushed" >&2
else
  echo "[auto_sync] clean" >&2
fi
