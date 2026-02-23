# Improvement Plan: KIS ORB/VWAP Bot (2026-02-23)

## 1) Root Cause Analysis (Today)

### A. API rate-limit failures (`EGW00201`) were caused by request bursts, not a single bad call
- Evidence in `logs/2026-02-23.log`:
  - `08:19:32` `get_positions failed ... EGW00201`
  - `08:51:52` `get_positions failed ... EGW00201`
  - `08:57:06` / `08:57:07` / `08:57:09` repeated rate-limit with exponential retries
  - `09:09:07` additional rate-limit on `get_positions`
  - `08:20:35` scanner fallback due rate-limit (`status=500 ... EGW00201`)
- Contributing behavior:
  - Multiple module/engine restarts in short intervals around `08:19~08:57` increased concurrent API pressure.
  - `KRSwingModule.monitor_loop()` polls `get_positions()` every 10s (`modules/kr_swing.py:356-361`), and similar loops run in parallel.
  - There is no global, shared request governor across clients/modules; retries are local per call (`kis_rest_orders.py:242-259`).

### B. Runtime failures beyond rate-limit
- `Server disconnected` quote errors appeared repeatedly (`get_quote(...) exception`) without structured retry/backoff (`kis_rest_orders.py:451-465`).
- `cancel_order` failures (`IGW00019 주문구분을 확인해주세요`) followed by repeated submit attempts:
  - Seen at `12:02:02`, `12:04:52`, `13:36:58` in `logs/2026-02-23.log`.
  - Cancel path is attempted but error class is not handled semantically (`kis_rest_orders.py:202-233`).
- Exit storm on symbol `001450`:
  - From `13:36:58` to `13:37:31`, repeated `sell_limit ... APBK0400 주문 가능한 수량을 초과했습니다.` (18 failures)
  - Exit logic keeps issuing new SELL intents/orders each cycle (`core/order_executor.py:423-499`) while position reconciliation is lagging.

### C. Resource-lifecycle issue
- `logs/nohup_kr_modules.log` ends with `Unclosed client session` / `Unclosed connector`.
- In module mode, module shutdown does not close REST client sessions (`modules/base.py` has no concrete `_on_shutdown` cleanup in modules).

---

## 2) Trade Execution Performance (fills vs intents)

### A. Latest report files are stale/incomplete for today
- `reports/trade_report_2026-02-23.md` generated at `2026-02-23T00:14:10+00:00` (KST 09:14), so it misses later intraday activity.
- It shows `intents=0, submitted=0, ack=0, fills=0`, which conflicts with later log/event activity.

### B. Actual 2026-02-23 event stream snapshot
- From `logs/events/20260223/events.jsonl`:
  - `OrderIntent=24`, `OrderSubmitted=26`, `OrderAck=17`, `Fill=14`
- Interpretation:
  - Fill/Intent ratio is low (~58%) for an intraday strategy under active signals.
  - Ack/Submitted mismatch is material (17 vs 26), indicating failed/unacknowledged order flow.
  - Repeated exit retries on `001450` inflated intents/submissions without proportional fills.

### C. Historical recent reports
- `reports/trade_report_2026-02-20.md`: intents=24, submitted=32, ack=27, fills=23 (better conversion than today).
- `reports/trade_report_2026-02-19.md`: intents=19, submitted=28, ack=17, fills=17.
- Trend implication: today’s reliability degraded due runtime instability/retry storms, not only strategy quality.

---

## 3) Code Audit Findings (logic gaps / weak handling / inefficiencies)

### Priority-1 defects
1. No global API throttle across modules/engine
- Current retries are per-call only (`kis_rest_orders.py`, `kis_rest_overseas.py`), which does not prevent aggregate burst limits.

2. Exit retry loop can spam SELL intents/orders under transient broker-state mismatch
- `core/order_executor.py:423-499` retries exits, re-checks positions, and can resubmit quickly.
- On broker-side latency or pending orders, this causes repeated SELL attempts and `APBK0400`.

3. Cancel error handling is not semantic
- `cancel_order()` treats `IGW00019` as hard error but caller keeps retrying.
- No branch for benign/terminal cancel conditions (already filled, invalid cancel state).

4. Session lifecycle leak in module mode
- Unclosed `aiohttp` sessions from module-run clients.

### Priority-2 defects
5. `get_quote()` lacks retry/backoff and typed exception handling (`kis_rest_orders.py:451-465`).
6. `get_all_positions()` has no rate-limit retry path (unlike `get_positions`), yet is called frequently by order execution (`core/order_executor.py:265,485,500`).
7. `engine/core_engine.py:1744` calls `await self.rest.close()` (wrong method name; client exposes `aclose()`), hiding cleanup in exception.
8. Position polling cadence is aggressive and duplicated across runtime components (`modules/kr_swing.py:356-361`, engine loops).

---

## 4) Specific Code Fixes

## A. Rate limiting / traffic shaping
1. Add global async rate limiter (token bucket) shared by all KIS REST clients.
- Apply before each REST call (quotes, balances, cash, order submit/cancel).
- Separate buckets by API class if needed (`orders`, `account`, `quotes`).

2. Add jittered backoff policy utility and use consistently.
- Replace ad-hoc `1,2,4` only; include cap and random jitter.

3. Add short TTL cache for read-heavy calls.
- `get_positions`/`get_all_positions` cache 300-700ms to collapse duplicate reads from concurrent loops.

## B. Retry/error classification
4. In `cancel_order`, classify `IGW00019` as terminal-nonfatal.
- Return structured status (`already_done`/`invalid_cancel_state`) and stop retry cascades.

5. In `get_quote`, add bounded retry for `aiohttp.ClientError`/disconnect with jitter.
- Prevent one-shot failure propagation during transient transport glitches.

6. Add rate-limit handling to `get_all_positions`.
- Mirror `EGW00201` handling used in `get_positions`.

## C. Exit logic hardening
7. Add per-symbol `exit_cooldown_sec` after failed exit.
- Prevent 1-second monitor loop from reissuing order storms.

8. Before re-submitting SELL, query open orders for same symbol and wait/cancel once.
- Do not place new sell if existing working sell exists.

9. Use broker-reconciled available quantity for reattempts.
- Avoid fixed `pos.qty` resubmissions when available is lower (cause of `APBK0400`).

## D. Lifecycle and cleanup
10. Implement `_on_shutdown()` in modules to close owned REST clients.
- `await self.rest.aclose()` where applicable.

11. Fix `await self.rest.close()` -> `await self.rest.aclose()` in baseline path.

---

## 5) Logic Enhancements for Better Execution

1. Introduce order state machine (`NEW -> ACK -> PARTIAL -> FILLED/CANCELED/REJECTED`).
- Base reattempts on broker state, not only local timing.

2. Add "single active exit order per symbol" invariant.
- Block additional exit intents until previous exit reaches terminal state or timeout.

3. Improve report freshness control.
- Include cutoff timestamp and run_id in report.
- Regenerate EOD report after market close; mark intraday reports as `partial`.

4. Add health counters for operational quality.
- `rate_limit_count`, `cancel_reject_count`, `exit_retry_count`, `disconnect_count`, `ack_gap`.

---

## 6) Refactoring Recommendations

1. Unify request policy in one module.
- Centralize throttle/retry/error classification (remove duplicated logic in order clients).

2. Separate read API and order API adapters.
- Different QoS and quotas; easier to control burst behavior.

3. Consolidate duplicate execution logic.
- Keep `core/order_executor.py` as single authoritative execution path.

4. Reduce log noise with deduping/rate-limited logging.
- Especially repeated `position for reconcile` and repeated identical sell failures.

---

## 7) Suggested Implementation Order (fastest risk reduction)

1. Global limiter + `get_all_positions` retry + `get_quote` retry.
2. Exit cooldown + single-active-exit guard + cancel/error classification.
3. Module shutdown `aclose()` and cleanup method fix in engine.
4. Report pipeline freshness improvements + operational metrics.

