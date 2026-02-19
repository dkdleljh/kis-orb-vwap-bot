#!/usr/bin/env python3
"""Recommend next-day tuning values (best-effort, safe).

Inputs:
- reports/trade_report_YYYY-MM-DD.md (optional)
- logs/events/YYYYMMDD/events.jsonl
- config.json

Outputs:
- reports/next_day_reco_YYYY-MM-DD.json
- reports/next_day_reco_YYYY-MM-DD.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]


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


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _conf(level: str, score: float) -> Dict[str, Any]:
    return {"level": level, "score": round(float(score), 2)}


def _load_current_cfg() -> Dict[str, Any]:
    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _med(vals: List[float]) -> float | None:
    if not vals:
        return None
    try:
        return float(statistics.median(vals))
    except Exception:
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
    trade_report_path = ROOT / "reports" / f"trade_report_{day.isoformat()}.md"

    cfg = _load_current_cfg()
    trading = dict(cfg.get("trading") or {})
    scoring = dict(trading.get("scoring") or {})

    cur_entry_budget_pct = float(trading.get("entry_budget_pct", 0.20) or 0.20)
    cur_cash_reserve_pct = float(trading.get("cash_reserve_pct", 0.20) or 0.20)
    cur_entries_per_min = int(trading.get("max_new_entries_per_minute", 2) or 2)
    cur_min_score = float(scoring.get("kr_scalp_entry_threshold", 50) or 50)

    risk_block_reasons: Counter[str] = Counter()
    qty0_contexts: List[Dict[str, Any]] = []
    signal_ctx_count = 0
    signal_ctx_min_score_count = 0
    signal_ctx_reasons_count = 0

    fee_zero = 0
    fee_zero_paper = 0
    fee_zero_live = 0
    fee_total = 0.0
    fill_count = 0

    for ev in _iter_events(events_path):
        typ = ev.get("type")
        payload = ev.get("payload") or {}

        if typ == "RiskDecision":
            try:
                if payload.get("allowed") is False:
                    reason = str(payload.get("reason") or "") or "(empty)"
                    risk_block_reasons[reason] += 1
                    if reason == "qty=0":
                        qty0_contexts.append(dict(payload.get("context") or {}))
            except Exception:
                pass

        elif typ == "Signal":
            try:
                ctx = dict(payload.get("context") or {})
                if ctx:
                    signal_ctx_count += 1
                if ctx.get("min_score") is not None:
                    signal_ctx_min_score_count += 1
                reasons = ctx.get("reasons")
                if isinstance(reasons, list) and len(reasons) > 0:
                    signal_ctx_reasons_count += 1
            except Exception:
                pass

        elif typ == "Fill":
            try:
                fee = float(payload.get("fee") or 0.0)
                fill_count += 1
                fee_total += fee
                if fee == 0.0:
                    fee_zero += 1
                    if str(payload.get("broker_order_id") or "").upper() == "PAPER":
                        fee_zero_paper += 1
                    else:
                        fee_zero_live += 1
            except Exception:
                pass

    qty0_budget_lt_ask = 0
    qty0_remaining_cap_le0 = 0
    qty0_sym_remaining_le0 = 0
    qty0_cash_reserve_hit = 0
    qty0_missing_context = 0

    qty0_budget_vals: List[float] = []
    qty0_ask_vals: List[float] = []
    qty0_remaining_vals: List[float] = []

    for ctx in qty0_contexts:
        if not ctx:
            qty0_missing_context += 1
            continue

        def _f(key: str) -> float | None:
            try:
                v = ctx.get(key)
                return None if v is None else float(v)
            except Exception:
                return None

        budget = _f("budget")
        ask = _f("ask")
        remaining_cap = _f("remaining_cap")
        sym_remaining = _f("sym_remaining")
        cash = _f("cash")
        reserve_amt = _f("reserve_amt")

        if budget is not None:
            qty0_budget_vals.append(budget)
        if ask is not None and ask > 0:
            qty0_ask_vals.append(ask)
        if remaining_cap is not None:
            qty0_remaining_vals.append(remaining_cap)

        if budget is not None and ask is not None and budget < ask:
            qty0_budget_lt_ask += 1
        if remaining_cap is not None and remaining_cap <= 0:
            qty0_remaining_cap_le0 += 1
        if sym_remaining is not None and sym_remaining <= 0:
            qty0_sym_remaining_le0 += 1
        if cash is not None and reserve_amt is not None and cash <= reserve_amt:
            qty0_cash_reserve_hit += 1

    recos: Dict[str, Any] = {
        "date": day.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "current": {
            "trading.entry_budget_pct": cur_entry_budget_pct,
            "trading.cash_reserve_pct": cur_cash_reserve_pct,
            "trading.max_new_entries_per_minute": cur_entries_per_min,
            "trading.scoring.kr_scalp_entry_threshold": cur_min_score,
        },
        "inputs": {
            "events_path": str(events_path),
            "trade_report_path": str(trade_report_path),
            "trade_report_exists": trade_report_path.exists(),
            "fill_count": fill_count,
            "fee_total": fee_total,
            "fee_zero": fee_zero,
            "fee_zero_paper": fee_zero_paper,
            "fee_zero_live": fee_zero_live,
            "risk_block_reasons": dict(risk_block_reasons),
            "qty0_context_count": len(qty0_contexts),
            "qty0_missing_context": qty0_missing_context,
            "signal_context": {
                "total": signal_ctx_count,
                "with_min_score": signal_ctx_min_score_count,
                "with_reasons": signal_ctx_reasons_count,
            },
        },
        "recommendations": [],
    }

    def add(
        *,
        key: str,
        path: str,
        current: float | int,
        recommended: float | int,
        confidence: Dict[str, Any],
        rationale: str,
        evidence: Dict[str, Any],
    ) -> None:
        recos["recommendations"].append(
            {
                "key": key,
                "path": path,
                "current": current,
                "recommended": recommended,
                "delta": round(float(recommended) - float(current), 6),
                "confidence": confidence,
                "rationale": rationale,
                "evidence": evidence,
            }
        )

    total_blocks = sum(risk_block_reasons.values())
    qty0 = int(risk_block_reasons.get("qty=0", 0) or 0)
    qty0_ratio = qty0 / max(1, total_blocks)

    if qty0 >= 5 and qty0_ratio >= 0.40:
        median_budget = _med(qty0_budget_vals)
        median_ask = _med(qty0_ask_vals)
        budget_vs_ask = (
            (median_budget / median_ask)
            if (median_budget is not None and median_ask is not None and median_ask > 0)
            else None
        )
        step = 0.01
        if qty0_budget_lt_ask >= max(3, int(qty0 * 0.5)):
            step = 0.03
        elif qty0_budget_lt_ask >= max(2, int(qty0 * 0.3)):
            step = 0.02

        reco_budget = round(_clamp(cur_entry_budget_pct + step, 0.05, 0.35), 3)
        add(
            key="entry_budget_pct",
            path="trading.entry_budget_pct",
            current=cur_entry_budget_pct,
            recommended=reco_budget,
            confidence=_conf("medium", 0.68 if qty0 < 12 else 0.76),
            rationale="qty=0 차단 비중이 높고 budget<ask 패턴이 많아 진입예산을 소폭 상향합니다.",
            evidence={
                "qty0": qty0,
                "total_blocks": total_blocks,
                "qty0_ratio": round(qty0_ratio, 4),
                "qty0_budget_lt_ask": qty0_budget_lt_ask,
                "median_budget": median_budget,
                "median_ask": median_ask,
                "median_budget_vs_ask": budget_vs_ask,
            },
        )

    cash_reserve_blocks = int(risk_block_reasons.get("cash_reserve", 0) or 0)
    if cash_reserve_blocks >= 3:
        reco_reserve = round(_clamp(cur_cash_reserve_pct - 0.02, 0.05, 0.80), 3)
        add(
            key="cash_reserve_pct",
            path="trading.cash_reserve_pct",
            current=cur_cash_reserve_pct,
            recommended=reco_reserve,
            confidence=_conf("medium", 0.63),
            rationale="cash_reserve 차단이 반복되어 현금보유 하한을 소폭 완화합니다.",
            evidence={"cash_reserve_blocks": cash_reserve_blocks},
        )

    entry_rate_blocks = int(risk_block_reasons.get("entry_rate_limit", 0) or 0)
    if entry_rate_blocks >= 2:
        reco_rate = int(max(1, min(10, cur_entries_per_min + 1)))
        add(
            key="max_new_entries_per_minute",
            path="trading.max_new_entries_per_minute",
            current=cur_entries_per_min,
            recommended=reco_rate,
            confidence=_conf("medium", 0.60),
            rationale="entry_rate_limit 차단이 있어 신규진입 속도 제한을 1단계 완화합니다.",
            evidence={"entry_rate_limit_blocks": entry_rate_blocks},
        )

    if fill_count >= 10 and fee_total > 0:
        bump = 3 if fill_count >= 20 else 2
        reco_min_score = round(_clamp(cur_min_score + bump, 45, 90), 1)
        add(
            key="kr_scalp_entry_threshold",
            path="trading.scoring.kr_scalp_entry_threshold",
            current=cur_min_score,
            recommended=reco_min_score,
            confidence=_conf("low", 0.55),
            rationale="체결 수가 많아 회전율/수수료 부담 가능성이 있어 진입 점수 하한을 상향합니다.",
            evidence={"fill_count": fill_count, "fee_total": round(fee_total, 4)},
        )

    if fee_zero_live >= 1:
        recos["recommendations"].append(
            {
                "key": "fee_logging_check",
                "path": "ops.fill_fee_pipeline",
                "current": "unknown",
                "recommended": "verify_live_fee_path",
                "delta": None,
                "confidence": _conf("high", 0.9),
                "rationale": "live/unknown fee=0 Fill이 확인되어 수수료 로깅 정합성 점검이 필요합니다.",
                "evidence": {"fee_zero_live": fee_zero_live, "fee_zero_paper": fee_zero_paper},
            }
        )

    if not recos["recommendations"]:
        recos["recommendations"].append(
            {
                "key": "keep_defaults",
                "path": "trading.*",
                "current": "as-is",
                "recommended": "as-is",
                "delta": 0,
                "confidence": _conf("medium", 0.7),
                "rationale": "이벤트 기반 이상징후가 크지 않아 기본값 유지가 합리적입니다.",
                "evidence": {"total_blocks": total_blocks, "fill_count": fill_count},
            }
        )

    out_dir = ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"next_day_reco_{day.isoformat()}.json"
    out_md = out_dir / f"next_day_reco_{day.isoformat()}.md"

    out_json.write_text(json.dumps(recos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [f"# 내일 장 대비 추천 - {day.isoformat()}\n", "## 자동 체크리스트(권장 기본값)\n"]
    lines.append(f"- [x] trade_report 확인: `{trade_report_path}`")
    lines.append(f"- [x] next_day_reco 생성: `{out_json}`")
    lines.append(f"- [ ] 검증 실행: `python scripts/verify_next_day_prep.py --date {day.isoformat()}`\n")
    lines.append("## 추천 항목\n")

    for r in recos["recommendations"]:
        lines.append(f"### {r['key']} ({r['path']})\n")
        lines.append(f"- 현재값: `{r['current']}`")
        lines.append(f"- 추천값: `{r['recommended']}`")
        if r.get("delta") is not None:
            lines.append(f"- 변경량: `{r['delta']}`")
        conf = r.get("confidence") or {}
        lines.append(f"- 신뢰도: `{conf.get('level', 'n/a')}` ({conf.get('score', 'n/a')})")
        lines.append(f"- 근거: {r['rationale']}")
        lines.append(f"- 증거: `{json.dumps(r.get('evidence') or {}, ensure_ascii=False)}`")
        lines.append("")

    out_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
