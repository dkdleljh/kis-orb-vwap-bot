#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE"

# Apply yesterday's recommendations (KST date). Run this after EOD report/reco generation.
YDAY=$(TZ=Asia/Seoul date -d 'yesterday' +%F)

./venv/bin/python scripts/apply_next_day_reco.py --date "$YDAY"
