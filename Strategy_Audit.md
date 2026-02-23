# Strategy Audit

## Current Logic Summary

### 1) Entry Signal Quality
- `modules/kr_swing.py` uses 15m aggregated bars from 1m data and only evaluates at 15m close (`modules/kr_swing.py:172`, `modules/kr_swing.py:203`).
- Entry scoring is delegated to `Perfect100Strategy.evaluate_entry()` (`modules/kr_swing.py:252`, `perfect_strategy.py:138`).
- Signal factors are a weighted additive score: RSI, EMA(9/21), MACD histogram, day return vs previous close, MA20 relation, VWAP relation, OR breakout/pullback, orderbook imbalance, wick penalty, overextension penalty, and simple news score (`perfect_strategy.py:167`, `perfect_strategy.py:183`, `perfect_strategy.py:190`, `perfect_strategy.py:197`, `perfect_strategy.py:203`, `perfect_strategy.py:214`, `perfect_strategy.py:218`, `perfect_strategy.py:229`, `perfect_strategy.py:233`, `perfect_strategy.py:243`, `perfect_strategy.py:249`).
- `kr_swing` currently feeds simplified indicators: fixed `volume_power=120`, fixed `news_score=0`, and always passes `market_regime="NEUTRAL"` (`modules/kr_swing.py:231`, `modules/kr_swing.py:232`, `modules/kr_swing.py:259`).

### 2) Risk Management
- `core/entry_gates.py` enforces pre-entry controls: rate limit, microstructure filters (spread/depth/staleness), optional slippage blocklist, news sentiment block, cooldown, cash reserve, exposure caps, symbol caps, max trades/day, max trades/symbol, max concurrent positions (`core/entry_gates.py:135`, `core/entry_gates.py:182`, `core/entry_gates.py:225`, `core/entry_gates.py:247`, `core/entry_gates.py:315`, `core/entry_gates.py:364`, `core/entry_gates.py:452`, `core/entry_gates.py:503`, `core/entry_gates.py:551`).
- In bear regime, entry budget is halved (`core/entry_gates.py:331`).
- In `perfect_strategy.py`, stop/profit checks exist as helper functions with fixed % thresholds plus ATR fallback (`perfect_strategy.py:212`, `perfect_strategy.py:238`, `perfect_strategy.py:498`).
- In `modules/kr_swing.py`, live exits are not handled inside module; comment says exit is delegated elsewhere (`modules/kr_swing.py:352`).

### 3) Market Regime Adaptation
- `Perfect100Strategy` can block long entries in `BEAR` (`perfect_strategy.py:252`), and sell-entry logic exists only for `BEAR` (`perfect_strategy.py:94` in the sell evaluator path).
- `EntryGateEvaluator` reduces budget in bear regime (`core/entry_gates.py:331`).
- `kr_swing` does not pass dynamic regime; it hardcodes `NEUTRAL`, effectively disabling regime-aware entry behavior from strategy layer (`modules/kr_swing.py:259`).
- `strategy_profiles.py` only manages static/dynamic threshold retrieval (`strategy_profiles.py:47`).
- `scripts/recommend_next_day.py` adapts mostly KR scalp controls (`kr_scalp_entry_threshold`, cooldown, entry budget, cash reserve, ATR min percent) and does not optimize KR swing threshold directly (`scripts/recommend_next_day.py:84`, `scripts/recommend_next_day.py:141`, `scripts/recommend_next_day.py:152`, `scripts/recommend_next_day.py:162`, `scripts/recommend_next_day.py:185`).

### 4) Profit Taking
- Profit taking in `perfect_strategy.py` is fixed net PnL ladder (`1.5%`, `2.5%`, `3.5%`) (`perfect_strategy.py:226`).
- Partial TP levels are fixed and BUY-oriented (`entry*1.015`, `1.025`, `1.035`) (`perfect_strategy.py:27` in this section; function at `perfect_strategy.py:19`).
- Trailing stop uses `2.5 * ATR` distance from peak (`perfect_strategy.py:8`, `perfect_strategy.py:13`).

## Identified Weaknesses

### 1) Entry Signal Quality
- Signal inputs are partially synthetic/constant in swing module.
  - `volume_power` is hardcoded to `120`, so volume quality filter is not real (`modules/kr_swing.py:231`).
  - `news_score` is hardcoded to `0`, disabling positive/negative news discrimination in scoring (`modules/kr_swing.py:232`).
- Breakout validation is weak for false-breakout control.
  - Breakout only checks `bar.close > or_high`; no confirmation by breakout distance, retest hold, relative volume, or multi-bar hold (`perfect_strategy.py:218`).
- OR reference may be diluted for swing usage.
  - OR high/low is updated continuously from all bars, not a fixed opening window, so "breakout" can become less meaningful as session progresses (`perfect_strategy.py:27`, `perfect_strategy.py:32`).
- Regime input mismatch weakens score relevance.
  - Strategy supports regime-aware behavior, but swing module always calls with `NEUTRAL` (`modules/kr_swing.py:259`).
- Score output in `kr_swing` can misrepresent weak/no-signal as high score.
  - If no valid signal score exists, fallback uses static breakdown total, which is 100 (`modules/kr_swing.py:264`, `modules/kr_swing.py:271`). This can create audit/log inconsistency versus actual signal side.

### 2) Risk Management
- Swing module-defined risk parameters are unused in execution path.
  - `stop_loss_pct` and `take_profit_pct` are configured but never applied in `modules/kr_swing.py` (`modules/kr_swing.py:58`, `modules/kr_swing.py:59`).
- Exit responsibility is unclear and fragmented.
  - Module explicitly delegates exits; if engine-level logic diverges from strategy assumptions, realized risk may differ from expected RR (`modules/kr_swing.py:352`).
- `should_stop_loss()` has a SELL-side ATR condition bug.
  - For SELL, stop is `entry + 2*ATR`, but condition still checks `current_price <= atr_stop`; it should be `>=` for short stop-out (`perfect_strategy.py:65`, `perfect_strategy.py:67`).
- Entry gate can silently weaken protection.
  - `KIS_DISABLE_MICROSTRUCTURE_FILTER` can fully disable microstructure checks by env var (`core/entry_gates.py:176`).
- News filter fallback can pass entries on analyzer errors.
  - On exception, it logs and proceeds with technical-only entry (`core/entry_gates.py:10` in second half snippet).

### 3) Market Regime Adaptation
- Swing layer does not propagate actual regime into entry strategy.
  - Hardcoded `NEUTRAL` prevents BEAR blocking and side adaptation logic in `Perfect100Strategy` (`modules/kr_swing.py:259`, `perfect_strategy.py:252`).
- Adaptation is mostly one-dimensional (budget scaling).
  - Entry gate only halves budget in BEAR, but does not dynamically tighten spread/depth, min score, ATR multiple, or cooldown by volatility regime (`core/entry_gates.py:331`).
- Daily recommender is scalp-centric, not swing-aware.
  - It recommends `kr_scalp_entry_threshold` and not `kr_swing_entry_threshold`, so swing threshold can remain stale (`scripts/recommend_next_day.py:84`, `scripts/recommend_next_day.py:141`).
- Static baseline thresholds in profiles are coarse and not volatility-conditioned.
  - Thresholds are fixed constants by market/style (`strategy_profiles.py:26`).

### 4) Profit Taking
- TP ladder is fixed-percentage and does not adapt to volatility, trend strength, or instrument behavior.
  - `TP1/2/3` fixed at 1.5/2.5/3.5% net (`perfect_strategy.py:226`).
- Partial TP function is long-only biased.
  - `get_partial_tp_levels()` only checks `current_price >= tp_price`; no symmetric short logic (`perfect_strategy.py:33`).
- Trailing stop logic may be inactive/too loose in low ATR regimes.
  - Distance is `2.5*ATR`; if ATR estimate is stale/noisy, trailing may trigger too late or never (`perfect_strategy.py:8`, `perfect_strategy.py:13`).
- Profit-taking helpers are not clearly wired into `kr_swing` runtime path.
  - Module has no local exit loop using these helpers (`modules/kr_swing.py:352`).

## Upgrade Proposals

### Entry Signal Quality Upgrades
- Add breakout quality filter:
  - Require `close > or_high + k*ATR` (e.g., `k=0.2~0.5`) and/or a 1-bar retest hold above breakout level.
  - Require breakout bar relative volume (`vol / SMA(vol,20) >= 1.5`).
- Add trend-strength filter:
  - Add `ADX(14) >= threshold` as gate for breakout entries.
- Replace placeholder features:
  - Compute real `volume_power` from 15m volume z-score / RVOL.
  - Pass real `news_score` (or disable scoring component entirely if unavailable).
- Stabilize OR semantics:
  - Freeze OR after opening window instead of continuously extending high/low.
- Fix score reporting in `kr_swing`:
  - If `sig.side is None`, set score to `0` (or actual computed) instead of static fallback 100.

### Risk Management Upgrades
- Unify exit model ownership:
  - Implement explicit swing exit manager in `modules/kr_swing.py` or enforce one canonical engine exit contract with test coverage.
- Apply ATR-anchored initial stop at entry:
  - Persist initial stop (`entry - n*ATR`) and trail only after favorable move.
- Fix SELL ATR stop bug in `should_stop_loss()`:
  - Use `current_price >= atr_stop` for SELL.
- Add volatility-aware position sizing:
  - Size by risk-per-trade: `qty = risk_cash / stop_distance` rather than budget-only sizing.
- Harden safety overrides:
  - Restrict/expire `KIS_DISABLE_MICROSTRUCTURE_FILTER` (e.g., forced auto-reset per session + audit alert).

### Market Regime Adaptation Upgrades
- Feed real regime into swing evaluator:
  - Pass detected regime (trend + vol state) instead of hardcoded `NEUTRAL`.
- Add regime matrix for parameters:
  - Tune `min_score`, `max_spread_pct`, ATR stop multiple, and cooldown by regime (`TREND_UP`, `RANGE`, `VOLATILE`, `BEAR`).
- Extend daily recommender to swing controls:
  - Include `trading.scoring.kr_swing_entry_threshold` and swing-specific ATR/trailing parameters.
- Add cross-asset/sector risk overlay:
  - Implement sector correlation check and cap simultaneous entries in highly correlated names.

### Profit Taking Upgrades
- Replace fixed TP ladder with ATR/structure targets:
  - Example: TP1 at `1R`, TP2 at `2R`, runner with Chandelier/ATR trail.
- Make partial TP symmetric for SELL.
- Add adaptive trailing stop:
  - Use `max(ATR*k, percent_floor)` and tighten trailing after TP1 hit.
- Add time-stop and failure-stop:
  - Exit if breakout fails to extend within N bars or closes back below breakout/retest level.

### Priority Implementation Order
1. Fix critical stop bug (`perfect_strategy.py` SELL ATR condition).
2. Pass real market regime in `kr_swing` and remove placeholder `NEUTRAL`.
3. Replace synthetic `volume_power/news_score` with real features.
4. Add breakout confirmation (ATR distance + RVOL + retest).
5. Move from fixed TP/SL percentages to R-multiple + ATR trailing.
6. Extend `recommend_next_day.py` to include swing-specific parameter recommendations.
