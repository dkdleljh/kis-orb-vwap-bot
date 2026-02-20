#!/usr/bin/env python3
"""Generate an end-of-day trading report from logs/events.

Reads
- logs/events/YYYYMMDD/events.jsonl

Writes
- reports/trade_report_YYYY-MM-DD.md

Design goals
- Safe for local + public sharing: never prints account numbers, API keys, tokens.
- Explainable: includes signal context (if logged), risk-block reasons, and
  execution slippage estimate (best-effort).

Notes / limitations
- Realized PnL is computed from matched BUY/SELL fills using FIFO.
- Unrealized PnL (if open) is estimated using the last Bar1mClosed close.
- Slippage is estimated by comparing Fill price to the most recent OrderIntent
  limit_price for the same idempotency_key. Market orders may show N/A.
- "모듈/소스별" 분류는 correlation_id prefix 기반(best-effort)입니다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FillRec:
    ts: dt.datetime
    symbol: str
    side: str
    qty: int
    price: float
    fee: float
    idempotency_key: str
    correlation_id: str
    module: str
    broker_order_id: str


@dataclass
class IntentRec:
    ts: dt.datetime
    symbol: str
    side: str
    qty: int
    order_type: str
    limit_price: Optional[float]
    idempotency_key: str
    correlation_id: str
    module: str


@dataclass
class SignalRec:
    ts: dt.datetime
    symbol: str
    side: str
    strength: float
    reason: str
    model: str
    context: Dict[str, Any]


@dataclass
class RiskDecisionRec:
    ts: dt.datetime
    symbol: str
    allowed: bool
    reason: str
    correlation_id: str
    module: str
    context: Dict[str, Any]


def _parse_ts(s: str) -> dt.datetime:
    # examples: 2026-02-19T02:07:02.763Z
    if not s:
        return dt.datetime.now(dt.timezone.utc)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s)


def _iter_events(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _kst(dtu: dt.datetime) -> dt.datetime:
    if dtu.tzinfo is None:
        return dtu
    return dtu.astimezone(dt.timezone(dt.timedelta(hours=9)))


def _fmt_money_krw(x: float) -> str:
    return f"{x:,.0f}원"


def _fmt_money_usd(x: float) -> str:
    return f"${x:,.2f}"


def _detect_market(symbol: str) -> str:
    return "KR" if symbol.isdigit() else "US"


def _source_from_corr(correlation_id: str) -> str:
    """Best-effort source tag from correlation_id.

    Examples:
    - eng_buy_xxx -> eng_buy
    - kr_entry_xxx -> kr_entry
    - us_exit_xxx -> us_exit

    NOTE: This is *not* the same as "module". We also try to record explicit
    payload.module in OrderIntent/RiskDecision/Fill (and Signal.context.module).
    """
    cid = (correlation_id or "").strip()
    if not cid:
        return "unknown"
    parts = cid.split("_")
    if len(parts) >= 3:
        return "_".join(parts[:2])
    return parts[0]


def _module_fallback(module: str, *, src: str, intent: Optional[IntentRec] = None) -> str:
    """Resolve a human-friendly module name.

    Preference order:
    1) explicit payload.module
    2) intent.module (if provided)
    3) correlation-id source heuristic (legacy logs)
    """
    m = (module or "").strip()
    if m and m != "unknown":
        return m
    if intent is not None:
        m2 = (intent.module or "").strip()
        if m2 and m2 != "unknown":
            return m2

    # Legacy fallback
    if src.startswith("eng_"):
        return "engine_orb_vwap"
    if src.startswith("kr_"):
        return "kr_module"
    if src.startswith("us_"):
        return "us_module"
    return "unknown"


def _fifo_realized_pnl(fills: List[FillRec]) -> Tuple[float, float, Dict[str, Any]]:
    lots: Deque[Tuple[int, float]] = deque()  # (qty, price)
    realized = 0.0
    fees = 0.0
    buy_qty = sell_qty = 0

    for f in sorted(fills, key=lambda x: x.ts):
        fees += float(f.fee or 0.0)
        if f.side.upper() == "BUY":
            lots.append((f.qty, f.price))
            buy_qty += f.qty
        elif f.side.upper() == "SELL":
            sell_qty += f.qty
            q = f.qty
            while q > 0 and lots:
                lq, lp = lots[0]
                take = min(q, lq)
                realized += (f.price - lp) * take
                lq -= take
                q -= take
                if lq <= 0:
                    lots.popleft()
                else:
                    lots[0] = (lq, lp)

    open_qty = sum(q for q, _ in lots)
    open_avg = (sum(q * p for q, p in lots) / open_qty) if open_qty else 0.0

    return realized, fees, {
        "buy_qty": buy_qty,
        "sell_qty": sell_qty,
        "open_qty": open_qty,
        "open_avg": open_avg,
    }


def _slippage(intent: Optional[IntentRec], fill: FillRec) -> Optional[float]:
    """Return signed slippage in price units (best-effort).

    Convention:
    - BUY: positive means worse (paid higher than intended)
    - SELL: positive means worse (sold lower than intended)
    """
    if intent is None:
        return None
    if intent.limit_price is None:
        return None
    if fill.side.upper() == "BUY":
        return float(fill.price) - float(intent.limit_price)
    if fill.side.upper() == "SELL":
        return float(intent.limit_price) - float(fill.price)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (KST). default=today", default=None)
    args = ap.parse_args()

    kst = dt.timezone(dt.timedelta(hours=9))
    if args.date:
        day = dt.date.fromisoformat(args.date)
    else:
        day = dt.datetime.now(tz=kst).date()

    ymd = day.strftime("%Y%m%d")
    events_path = ROOT / "logs" / "events" / ymd / "events.jsonl"

    fills_by_symbol: Dict[str, List[FillRec]] = defaultdict(list)
    fills_by_source: Dict[str, List[FillRec]] = defaultdict(list)
    intents_by_key: Dict[str, IntentRec] = {}
    intent_count_by_source: Counter[str] = Counter()

    last_close: Dict[str, float] = {}

    signals: List[SignalRec] = []

    order_submitted = 0
    order_acks = 0

    risk_allowed = 0
    risk_blocked = 0
    risk_block_reasons: Counter[str] = Counter()
    risk_block_reasons_by_source: Dict[str, Counter[str]] = defaultdict(Counter)
    risk_decisions: List[RiskDecisionRec] = []
    restored_symbols: set[str] = set()

    # --- parse stream ---
    for ev in _iter_events(events_path):
        typ = ev.get("type")
        sym = str(ev.get("symbol") or "").strip()
        payload = ev.get("payload") or {}
        ts = _parse_ts(ev.get("ts"))

        if typ == "Bar1mClosed":
            try:
                last_close[sym] = float(payload.get("close"))
            except Exception:
                pass

        elif typ == "Signal":
            try:
                signals.append(
                    SignalRec(
                        ts=ts,
                        symbol=sym,
                        side=str(payload.get("side") or ""),
                        strength=float(payload.get("strength") or 0.0),
                        reason=str(payload.get("reason") or ""),
                        model=str(payload.get("model") or ""),
                        context=dict(payload.get("context") or {}),
                    )
                )
            except Exception:
                pass

        elif typ == "OrderIntent":
            try:
                cid = str(payload.get("correlation_id") or "")
                src = _source_from_corr(cid)
                intent = IntentRec(
                    ts=ts,
                    symbol=sym,
                    side=str(payload.get("side") or ""),
                    qty=int(float(payload.get("qty") or 0)),
                    order_type=str(payload.get("order_type") or ""),
                    limit_price=(float(payload["limit_price"]) if "limit_price" in payload and payload.get("limit_price") is not None else None),
                    idempotency_key=str(payload.get("idempotency_key") or ""),
                    correlation_id=cid,
                    module=str(payload.get("module") or "") or "unknown",
                )
                if intent.idempotency_key:
                    intents_by_key[intent.idempotency_key] = intent
                intent_count_by_source[src] += 1
            except Exception:
                pass

        elif typ == "RiskDecision":
            try:
                allowed = bool(payload.get("allowed"))
                cid = str(payload.get("correlation_id") or "")
                src = _source_from_corr(cid)
                risk_decisions.append(
                    RiskDecisionRec(
                        ts=ts,
                        symbol=sym,
                        allowed=allowed,
                        reason=str(payload.get("reason") or "") or "(empty)",
                        correlation_id=cid,
                        module=str(payload.get("module") or "") or "unknown",
                        context=dict(payload.get("context") or {}),
                    )
                )
                if allowed:
                    risk_allowed += 1
                else:
                    risk_blocked += 1
                    reason = str(payload.get("reason") or "") or "(empty)"
                    risk_block_reasons[reason] += 1
                    risk_block_reasons_by_source[src][reason] += 1
            except Exception:
                pass

        elif typ == "OrderSubmitted":
            order_submitted += 1

        elif typ == "OrderAck":
            order_acks += 1

        elif typ == "Fill":
            try:
                cid = str(payload.get("correlation_id") or "")
                src = _source_from_corr(cid)
                fill = FillRec(
                    ts=ts,
                    symbol=sym,
                    side=str(payload.get("side") or ""),
                    qty=int(float(payload.get("qty") or 0)),
                    price=float(payload.get("price") or 0),
                    fee=float(payload.get("fee") or 0),
                    idempotency_key=str(payload.get("idempotency_key") or ""),
                    correlation_id=cid,
                    module=str(payload.get("module") or "") or "unknown",
                    broker_order_id=str(payload.get("broker_order_id") or ""),
                )
                fills_by_symbol[sym].append(fill)
                fills_by_source[src].append(fill)
            except Exception:
                pass

        elif typ == "PositionSnapshot":
            try:
                trigger = str(payload.get("trigger") or "").lower()
                qty = int(float(payload.get("qty") or 0))
                if trigger == "restore" and qty > 0 and sym:
                    restored_symbols.add(sym)
            except Exception:
                pass

        elif typ == "PositionRestored":
            if sym:
                restored_symbols.add(sym)

    # --- build report ---
    out_dir = ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"trade_report_{day.isoformat()}.md"

    traded_symbols = sorted([k for k, v in fills_by_symbol.items() if v])

    lines: List[str] = []
    lines.append(f"# 일일 거래 리포트 ({day.isoformat()} KST)\n")

    lines.append("## 요약\n")
    lines.append(f"- Fill(체결) 발생 종목 수: **{len(traded_symbols)}**")
    lines.append(f"- 주문 의도(OrderIntent): **{sum(intent_count_by_source.values())}**")
    lines.append(f"- 주문 제출(OrderSubmitted): **{order_submitted}**")
    lines.append(f"- 주문 접수(OrderAck): **{order_acks}**")
    lines.append(f"- 리스크 통과/차단: **{risk_allowed} / {risk_blocked}**\n")

    # (1) 소스/모듈별 성과
    lines.append("## 소스/모듈별 요약(상대 비교용, correlation_id 기반)\n")
    if not intent_count_by_source and not fills_by_source:
        lines.append("- (데이터 없음)\n")
    else:
        # realized pnl per source (FIFO per symbol inside source)
        for src in sorted(set(list(intent_count_by_source.keys()) + list(fills_by_source.keys()))):
            fills = fills_by_source.get(src, [])
            # group by symbol for fifo
            realized_total = 0.0
            fees_total = 0.0
            symbols = 0
            for sym in sorted({f.symbol for f in fills}):
                realized, fees, _ = _fifo_realized_pnl([x for x in fills if x.symbol == sym])
                realized_total += realized
                fees_total += fees
                symbols += 1
            lines.append(
                f"- **{src}**: intents={intent_count_by_source.get(src, 0)}, fills={len(fills)}, symbols={symbols}, "
                f"realized≈{realized_total:,.2f}, fees≈{fees_total:,.2f}"
            )
        lines.append("")

    # (2) 리스크 차단 사유 Top N
    lines.append("## 리스크 차단 사유 TOP (allowed=false)\n")
    if not risk_block_reasons:
        lines.append("- (차단 없음)\n")
    else:
        for reason, cnt in risk_block_reasons.most_common(10):
            lines.append(f"- {reason}: **{cnt}**")
        lines.append("")

    # (2-0) Cooldown blocks (new)
    cooldown_recs = [r for r in risk_decisions if (not r.allowed) and r.reason == "cooldown"]
    if cooldown_recs:
        lines.append("## 쿨다운 차단 요약(cooldown)\n")
        lines.append(f"- cooldown 차단 건수: **{len(cooldown_recs)}**")
        by_sym: Counter[str] = Counter([r.symbol for r in cooldown_recs if r.symbol])
        if by_sym:
            lines.append("- TOP 5 심볼:")
            for sym, cnt in by_sym.most_common(5):
                lines.append(f"  - {sym}: {cnt}")
        lines.append("")

    # (2-1) qty=0 numeric context breakdown
    lines.append("## qty=0 차단 원인 분해 (RiskDecision.context 기반)\n")
    qty0_recs = [r for r in risk_decisions if (not r.allowed) and r.reason == "qty=0"]
    if not qty0_recs:
        lines.append("- qty=0 차단이 없습니다.\n")
    else:
        cause_counter: Counter[str] = Counter()
        med_fields = [
            "cash",
            "equity_est",
            "exposure",
            "remaining_cap",
            "sym_remaining",
            "reserve_amt",
            "budget",
            "ask",
            "min_score",
        ]
        med_vals: Dict[str, List[float]] = {k: [] for k in med_fields}

        for rec in qty0_recs:
            c = rec.context or {}

            def _f(key: str) -> Optional[float]:
                try:
                    v = c.get(key)
                    if v is None:
                        return None
                    return float(v)
                except Exception:
                    return None

            remaining_cap = _f("remaining_cap")
            sym_remaining = _f("sym_remaining")
            cash = _f("cash")
            reserve_amt = _f("reserve_amt")
            budget = _f("budget")
            ask = _f("ask")

            if remaining_cap is not None and remaining_cap <= 0:
                cause_counter["remaining_cap<=0"] += 1
            elif sym_remaining is not None and sym_remaining <= 0:
                cause_counter["sym_remaining<=0"] += 1
            elif cash is not None and reserve_amt is not None and cash <= reserve_amt:
                cause_counter["cash<=reserve_amt"] += 1
            elif budget is not None and ask is not None and budget < ask:
                cause_counter["budget<ask"] += 1
            elif budget is not None and budget <= 0:
                cause_counter["budget<=0"] += 1
            elif not c:
                cause_counter["missing_context"] += 1
            else:
                cause_counter["other"] += 1

            for key in med_fields:
                x = _f(key)
                if x is not None:
                    med_vals[key].append(x)

        for k, v in cause_counter.most_common():
            lines.append(f"- {k}: **{v}**")
        lines.append("")

        lines.append("| metric | median |")
        lines.append("|---|---:|")
        for key in med_fields:
            vals = med_vals[key]
            if not vals:
                continue
            lines.append(f"| {key} | {statistics.median(vals):,.4f} |")
        lines.append("")

    # (3) 실행 품질(슬리피지) 요약
    lines.append("## 실행 품질(슬리피지) 요약 (Fill vs OrderIntent.limit_price, best-effort)\n")
    slip_values_kr: List[float] = []
    slip_values_us: List[float] = []
    for sym in traded_symbols:
        for f in fills_by_symbol[sym]:
            intent = intents_by_key.get(f.idempotency_key)
            s = _slippage(intent, f)
            if s is None:
                continue
            if _detect_market(sym) == "KR":
                slip_values_kr.append(float(s))
            else:
                slip_values_us.append(float(s))

    def _slip_summary(vals: List[float]) -> str:
        if not vals:
            return "N/A"
        avg = sum(vals) / len(vals)
        worst = max(vals)
        best = min(vals)
        return f"count={len(vals)}, avg={avg:.4f}, best={best:.4f}, worst={worst:.4f}"

    lines.append(f"- KR slippage: { _slip_summary(slip_values_kr) }")
    lines.append(f"- US slippage: { _slip_summary(slip_values_us) }\n")

    # signals section
    lines.append("## 시그널 로그(설명 가능한 경우)\n")
    if not signals:
        lines.append("- Signal 이벤트가 없습니다.\n")
    else:
        # group by symbol and show last few
        by_sym: Dict[str, List[SignalRec]] = defaultdict(list)
        for s in signals:
            by_sym[s.symbol].append(s)
        for sym in sorted(by_sym.keys()):
            ss = sorted(by_sym[sym], key=lambda x: x.ts)
            tail = ss[-5:]
            lines.append(f"### {sym}\n")
            for one in tail:
                t = _kst(one.ts).strftime("%H:%M:%S")
                ctx = one.context or {}
                # compact context
                ctx_keys = ["module", "score", "reason_short", "close", "vwap", "spread_pct", "rsi", "ma20", "ml_score", "atr_percent", "market_regime"]
                ctx2 = {k: ctx.get(k) for k in ctx_keys if k in ctx}
                lines.append(f"- {t} {one.side} strength={one.strength:.2f} model={one.model} ctx={ctx2}")
            lines.append("")

    # --- Reasons analysis (win/loss) ------------------------------------
    # Best-effort: join BUY fills to the most recent prior Signal (<=10min)
    # and bucket reasons by whether the symbol had positive/negative realized PnL.
    lines.append("## 시그널 사유 분석(승/패 기준, best-effort)\n")

    reason_win: Counter[str] = Counter()
    reason_lose: Counter[str] = Counter()

    # index signals by symbol (sorted)
    sig_by_sym: Dict[str, List[SignalRec]] = defaultdict(list)
    for s in signals:
        sig_by_sym[s.symbol].append(s)
    for sym in sig_by_sym:
        sig_by_sym[sym] = sorted(sig_by_sym[sym], key=lambda x: x.ts)

    def _nearest_signal(sym: str, t: dt.datetime, *, window_min: int = 10) -> Optional[SignalRec]:
        ss = sig_by_sym.get(sym) or []
        if not ss:
            return None
        best: Optional[SignalRec] = None
        for one in ss:
            if one.ts <= t:
                best = one
            else:
                break
        if best is None:
            return None
        delta = (t - best.ts).total_seconds()
        if delta < 0:
            return None
        if delta > window_min * 60:
            return None
        return best

    for sym in traded_symbols:
        fills = fills_by_symbol[sym]
        realized, _, _ = _fifo_realized_pnl(fills)
        bucket = reason_win if realized > 0 else reason_lose

        # take first BUY fill time as entry proxy
        buy_fills = [f for f in sorted(fills, key=lambda x: x.ts) if f.side.upper() == "BUY"]
        if not buy_fills:
            continue
        srec = _nearest_signal(sym, buy_fills[0].ts)
        if srec is None:
            continue
        ctx = srec.context or {}
        reasons = ctx.get("reasons")
        if isinstance(reasons, list):
            for r in reasons:
                if r:
                    bucket[str(r)] += 1
        else:
            rs = str(ctx.get("reason_short") or "").strip()
            if rs:
                for r in [x.strip() for x in rs.split(",") if x.strip()]:
                    bucket[r] += 1

    if not reason_win and not reason_lose:
        lines.append("- (분석에 필요한 시그널 컨텍스트가 부족합니다. 내일 로그부터 자동으로 채워집니다.)\n")
    else:
        if reason_win:
            lines.append("- 승리(실현손익>0) TOP:")
            for r, c in reason_win.most_common(10):
                lines.append(f"  - {r}: {c}")
        if reason_lose:
            lines.append("- 패배/기타(실현손익<=0) TOP:")
            for r, c in reason_lose.most_common(10):
                lines.append(f"  - {r}: {c}")
        lines.append("")

    # Per symbol detail
    if not traded_symbols:
        lines.append("## 체결 상세(종목별)\n")
        lines.append("체결이 없어 상세 내역이 없습니다.\n")
    else:
        lines.append("## 체결 상세(종목별)\n")
        grand_realized_krw = 0.0
        grand_fees_krw = 0.0
        grand_realized_usd = 0.0
        grand_fees_usd = 0.0

        for sym in traded_symbols:
            fills = fills_by_symbol[sym]
            market = _detect_market(sym)
            realized, fees, st = _fifo_realized_pnl(fills)

            open_qty = int(st["open_qty"])
            open_avg = float(st["open_avg"])

            unreal = 0.0
            last = last_close.get(sym)
            if open_qty and last is not None:
                unreal = (float(last) - open_avg) * open_qty

            if market == "KR":
                grand_realized_krw += realized
                grand_fees_krw += fees
            else:
                grand_realized_usd += realized
                grand_fees_usd += fees

            lines.append(f"### {sym} ({market})\n")
            lines.append(f"- 매수/매도 수량: {st['buy_qty']} / {st['sell_qty']}")
            lines.append(
                "- 실현손익(추정, FIFO): "
                + (_fmt_money_krw(realized) if market == "KR" else _fmt_money_usd(realized))
            )
            lines.append(
                "- 수수료 합계(로그 기준): "
                + (_fmt_money_krw(fees) if market == "KR" else _fmt_money_usd(fees))
            )

            if open_qty:
                if last is None:
                    lines.append(f"- 미청산 포지션: {open_qty}주(평균 {open_avg:.2f}) / 종가정보 없음")
                else:
                    lines.append(
                        f"- 미청산 포지션: {open_qty}주(평균 {open_avg:.2f}) / 마지막 종가 {last:.2f}"
                        + " / 미실현손익(추정): "
                        + (_fmt_money_krw(unreal) if market == "KR" else _fmt_money_usd(unreal))
                    )

            # slippage per symbol
            slip_sym: List[float] = []
            slip_na = 0
            for f in sorted(fills, key=lambda x: x.ts):
                intent = intents_by_key.get(f.idempotency_key)
                s = _slippage(intent, f)
                if s is None:
                    slip_na += 1
                else:
                    slip_sym.append(float(s))
            if slip_sym:
                lines.append(
                    f"- 슬리피지(추정): count={len(slip_sym)} avg={sum(slip_sym)/len(slip_sym):.4f} best={min(slip_sym):.4f} worst={max(slip_sym):.4f} (N/A {slip_na})"
                )
            else:
                lines.append(f"- 슬리피지(추정): N/A (N/A {slip_na})")

            lines.append("- 체결 타임라인:")
            for f in sorted(fills, key=lambda x: x.ts):
                t = _kst(f.ts).strftime("%H:%M:%S")
                intent = intents_by_key.get(f.idempotency_key)
                src = _source_from_corr(f.correlation_id)
                s = _slippage(intent, f)
                s_txt = "N/A" if s is None else f"{s:+.4f}"
                lp = None if intent is None else intent.limit_price
                lp_txt = "-" if lp is None else str(lp)
                mod = _module_fallback(f.module, src=src, intent=intent)
                lines.append(
                    f"  - {t} {f.side.upper()} {f.qty} @ {f.price} fee={f.fee} module={mod} src={src} intent_lp={lp_txt} slip={s_txt}"
                )
            lines.append("")

        # Compute KR unrealized total from open positions (best-effort)
        kr_unreal_total = 0.0
        for sym in traded_symbols:
            if _detect_market(sym) != "KR":
                continue
            fills = fills_by_symbol[sym]
            _, _, st = _fifo_realized_pnl(fills)
            open_qty = int(st["open_qty"])
            open_avg = float(st["open_avg"])
            last = last_close.get(sym)
            if open_qty and last is not None:
                kr_unreal_total += (float(last) - open_avg) * open_qty

        lines.append("## 합계\n")
        lines.append(
            f"- KR 실현손익(추정): **{_fmt_money_krw(grand_realized_krw)}** / 수수료 **{_fmt_money_krw(grand_fees_krw)}**"
        )
        lines.append(
            f"- KR 미실현손익(추정): **{_fmt_money_krw(kr_unreal_total)}**"
        )

        # Daily return percent (best-effort) from baseline snapshot
        baseline_path = ROOT / "logs" / f"daily_baseline_{ymd}.json"
        baseline_equity = None
        baseline_cash = None
        baseline_exposure = None
        try:
            if baseline_path.exists():
                b = json.loads(baseline_path.read_text(encoding="utf-8"))
                baseline_equity = float(b.get("equity"))
                baseline_cash = float(b.get("cash"))
                baseline_exposure = float(b.get("exposure"))
        except Exception:
            baseline_equity = None

        day_pnl_krw = float(grand_realized_krw) + float(kr_unreal_total) - float(grand_fees_krw)
        if baseline_equity and baseline_equity > 0:
            day_ret_pct = day_pnl_krw / float(baseline_equity)
            lines.append(
                f"- 당일 손익(추정, KRW): **{_fmt_money_krw(day_pnl_krw)}** / 당일 수익률(추정): **{day_ret_pct*100:.3f}%**"
            )
            lines.append(
                f"  - baseline(추정): cash={_fmt_money_krw(baseline_cash or 0)} exposure={_fmt_money_krw(baseline_exposure or 0)} equity={_fmt_money_krw(baseline_equity)}"
            )
        else:
            lines.append("- 당일 수익률(추정): baseline 파일이 없어 계산 불가 (logs/daily_baseline_YYYYMMDD.json)\n")

        lines.append(
            f"- US 실현손익(추정): **{_fmt_money_usd(grand_realized_usd)}** / 수수료 **{_fmt_money_usd(grand_fees_usd)}**\n"
        )

    # reconciliation
    lines.append("## 정합성 점검\n")
    total_fills = sum(len(v) for v in fills_by_symbol.values())
    total_intents = sum(intent_count_by_source.values())
    lines.append(
        f"- 주문/체결 카운트: intents={total_intents}, submitted={order_submitted}, ack={order_acks}, fills={total_fills}"
    )
    if order_submitted and order_acks < order_submitted:
        lines.append("- 경고: OrderAck 수가 OrderSubmitted보다 적습니다(수집 지연/누락 가능성).")
    if total_intents and total_fills == 0:
        lines.append("- 경고: OrderIntent는 있으나 Fill이 없습니다(미체결/로그 누락 가능성).")
    lines.append("")

    # quality flags
    lines.append("## 이상징후(체크)\n")
    flags: List[str] = []
    info_notes: List[str] = []

    # sell-only symbols
    for sym in traded_symbols:
        fills = fills_by_symbol[sym]
        b = sum(f.qty for f in fills if f.side.upper() == "BUY")
        s = sum(f.qty for f in fills if f.side.upper() == "SELL")
        if b == 0 and s > 0:
            if sym in restored_symbols:
                info_notes.append(
                    f"- {sym}: SELL만 존재하지만 장시작 포지션 복구 이벤트가 있어 정상 종료 가능성이 높음"
                )
            else:
                flags.append(f"- {sym}: SELL만 존재 (포지션 복구/전일 잔량/로그 누락 가능성 점검)")

    # fee==0 fills (paper vs live 분리)
    zero_fee = 0
    zero_fee_paper = 0
    zero_fee_live = 0
    for sym in traded_symbols:
        for f in fills_by_symbol[sym]:
            if float(f.fee or 0.0) != 0.0:
                continue
            zero_fee += 1
            if str(f.broker_order_id).upper() == "PAPER":
                zero_fee_paper += 1
            else:
                zero_fee_live += 1
    if zero_fee:
        info_notes.append(
            f"- fee=0 Fill: 총 {zero_fee}건 (paper={zero_fee_paper}, live/unknown={zero_fee_live})"
        )
    if zero_fee_live:
        flags.append(f"- live/unknown fee=0 Fill이 {zero_fee_live}건 있음 (수수료 계산/로그 소스 점검)")

    if flags:
        lines.extend(flags)
    else:
        lines.append("- (특이사항 없음)")
    if info_notes:
        lines.append("")
        lines.append("참고:")
        lines.extend(info_notes)
    lines.append("")

    # tomorrow action (template)
    lines.append("## 내일 장 대비 액션(템플릿)\n")
    lines.append("- 리스크 차단 TOP 사유가 'qty=0'이면: 주문가능현금/예산비율/호가 단위/최소수량 확인")
    lines.append("- 슬리피지가 나쁘면: 지정가/시장가 정책, 호가 스프레드 필터, 재시도 로직 점검")
    lines.append("- SELL-only 체결이 있으면: 전일 포지션 복구 로직/초기 상태 로딩 점검\n")

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
