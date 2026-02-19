#!/usr/bin/env bash
set -euo pipefail

# Usage: set_us_live_confirm.sh YES|NO
VAL="${1:-}"
if [[ "$VAL" != "YES" && "$VAL" != "NO" ]]; then
  echo "Usage: $0 YES|NO" >&2
  exit 2
fi

ROOT="/home/zenith/Desktop/kis_orb_vwap_bot"
ENV_FILE="$ROOT/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE" >&2
  exit 2
fi

# Replace existing line, or append if missing.
if rg -n "^KIS_US_LIVE_CONFIRM=" "$ENV_FILE" >/dev/null 2>&1; then
  sed -i "s/^KIS_US_LIVE_CONFIRM=.*/KIS_US_LIVE_CONFIRM=$VAL/" "$ENV_FILE"
else
  printf "\n# US live trading confirm (US module)\nKIS_US_LIVE_CONFIRM=%s\n" "$VAL" >> "$ENV_FILE"
fi

echo "KIS_US_LIVE_CONFIRM set to $VAL"