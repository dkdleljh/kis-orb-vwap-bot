# Changelog

All notable changes to this project will be documented in this file.

This repository is an automated trading system. **Behavior changes can affect live orders.**
Always review changes and run `scripts/smoke_test.sh` before enabling live trading.

## Unreleased

## v2026.02.20-1622 (2026-02-20)

### What changed (high-level)
- Added a **daily learning → next-day recommendation → guarded auto-apply** pipeline.
- Reduced “buy high / sell low” churn by adding **symbol cooldown** and an **ATR% minimum filter**.
- Improved resilience of KIS integrations (fills parsing, websocket subscribe-limit handling, and cron scripts).

### Strategy / risk controls
- **Symbol cooldown** (`trading.symbol_cooldown_sec`, default: 900s): prevents repeated re-entry attempts on the same symbol.
- **ATR minimum filter** (`trading.atr_min_percent`, default: 0.35): skips low-volatility names where fees dominate.
- **Minimum hold time** (`trading.min_hold_sec`, default: 120s): avoids immediate stop-outs from microstructure noise.
- **ATR stop tuning**: widened ATR stop behavior by increasing `trading.atr_multiplier` (2.0 → 2.7).
- **Cash reserve** tuned (0.20 → 0.18) to reduce `cash_reserve` blocks.
- **Entry threshold** tuned (example baseline: 78) to reduce churn/fees.

### Learning / automation
- New:
  - `scripts/apply_next_day_reco.py` (guarded apply with whitelist, clamps, confidence gating, backups, audit logs)
  - `scripts/apply_next_day_reco.sh`
- `scripts/recommend_next_day.py`
  - Now reads **`config.kr.json` first** (then `config.json`).
  - Adds fee-efficiency signals: `fee_per_fill`, `daily_pnl_est`.
  - Can recommend: `cash_reserve_pct`, `entry_budget_pct`, `kr_scalp_entry_threshold`, `symbol_cooldown_sec`, `atr_min_percent`.
- Auto-apply safety:
  - Runs `scripts/smoke_test.sh` + `scripts/healthcheck_kis.py` after apply.
  - On failure: **auto-rollback to backup + restart**.
  - If reco file is missing: prints `OK: missing reco file ...` and exits 0 (unattended-safe).

### Reporting
- `scripts/daily_trade_report.py` now includes **cooldown block summary** when present (`RiskDecision.reason == cooldown`).

### Operational fixes
- Fixed entry evaluation crash (`OrderBookTop.spread_pct` missing) by computing spread from bid/ask.
- Improved `get_cash_available()` variants to avoid rejected KIS fields.
- Improved fills parsing (`get_fills`) to handle inconsistent broker fields and occasional non-JSON responses.
- Websocket: detect `MAX SUBSCRIBE OVER (OPSP0008)` and temporarily block new subscriptions to reduce error storms.
- Cron stability: `scripts/prune_us_reserved_orders.py` now retries transient network/broker errors and degrades to OK when the broker is unavailable.

### Notes / migration
- Generated audit logs are written under `reports/applied/` and ignored by git.
- Local backup files `*.bak.*` are ignored by git.
