#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE"

#!/usr/bin/env bash
# NOTE: This script used to revert temporary intraday overrides.
# We now use it as a "next-day apply" hook for stable defaults.
set -euo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE"

# Apply next-day recommendations (guarded + audited)
./scripts/apply_next_day_reco.sh

