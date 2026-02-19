#!/usr/bin/env python3
"""Recommend next-day tuning values (best-effort, safe).

Inputs:
- reports/trade_report_YYYY-MM-DD.md (optional)
- logs/events/YYYYMMDD/events.jsonl

Outputs:
- reports/next_day_reco_YYYY-MM-DD.json
- reports/next_day_reco_YYYY-MM-DD.md

Principles:
- NEVER touches secrets or account identifiers.
- NEVER applies config changes automatically.
- Recommendations are conservative; user reviews & applies manually.

Current heuristics (v1):
- If RiskDecision blocked reasons are dominated by qty=0:
  suggest increasing entry_budget_pct slightly OR filtering out high-priced symbols.
- If fee==0 fills are frequent:
  suggest verifying fee calculator / fill fee logging sources.
- If slippage worst is high:
  suggest tightening max_spread_pct or switching entry limit policy.
- If realized PnL is flat but fees are high:
  suggest reducing churn: higher min_score, higher min ATR, or fewer symbols.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable

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

    risk_block_reasons: Counter[str] = Counter()
    fee_zero = 0
    fee_total = 0.0
    fill_count = 0

    # crude realized pnl estimate per symbol (FIFO) is in daily_trade_report; here keep lightweight:
    # if we see lots of fills but fees significant, we recommend churn reduction.

    for ev in _iter_events(events_path):
        typ = ev.get("type")
        payload = ev.get("payload") or {}

        if typ == "RiskDecision":
            try:
                if payload.get("allowed") is False:
                    reason = str(payload.get("reason") or "") or "(empty)"
                    risk_block_reasons[reason] += 1
            except Exception:
                pass

        if typ == "Fill":
            try:
                fee = float(payload.get("fee") or 0.0)
                fill_count += 1
                fee_total += fee
                if fee == 0.0:
                    fee_zero += 1
            except Exception:
                pass

    recos: Dict[str, Any] = {
        "date": day.isoformat(),
        "inputs": {
            "events_path": str(events_path),
            "fill_count": fill_count,
            "fee_total": fee_total,
            "fee_zero": fee_zero,
            "risk_block_reasons": dict(risk_block_reasons),
        },
        "recommendations": [],
    }

    def add(title: str, why: str, action: str, value: Any | None = None) -> None:
        recos["recommendations"].append({"title": title, "why": why, "action": action, "value": value})

    total_blocks = sum(risk_block_reasons.values())
    qty0 = risk_block_reasons.get("qty=0", 0)
    if total_blocks >= 5 and qty0 / max(1, total_blocks) >= 0.6:
        add(
            title="qty=0 차단이 과다 → 진입 예산/필터 재조정",
            why=f"RiskDecision 차단 {total_blocks}건 중 qty=0이 {qty0}건",
            action="entry_budget_pct를 +0.02p(예: 0.15→0.17)로 소폭 상향하거나, ask가 큰 종목은 유니버스에서 제외(상한가/대형주 등)",
            value={"suggest_entry_budget_pct_delta": 0.02},
        )

    if fill_count >= 5 and fee_zero / max(1, fill_count) >= 0.5:
        add(
            title="fee=0 Fill 비율이 높음 → 수수료 로깅 점검",
            why=f"Fill {fill_count}건 중 fee=0이 {fee_zero}건",
            action="실거래 Fill 이벤트에서 fee 계산 경로(commission+tax) 누락 여부 점검. (리포트 정확도 향상)",
        )

    if fill_count >= 10 and fee_total > 0:
        # very rough: lots of fills implies churn
        add(
            title="체결이 잦으면(회전율↑) → min_score 상향 검토",
            why=f"Fill {fill_count}건(수수료 합계 {fee_total:.2f})",
            action="kr_scalp_entry_threshold(기본 72)를 +3~+5 상향해 불필요한 진입 감소(추천: +3)",
            value={"suggest_kr_scalp_entry_threshold_delta": 3},
        )

    if not recos["recommendations"]:
        add(
            title="유의미한 이상징후 없음",
            why="이벤트/체결 데이터 기준으로 큰 문제 신호가 없음",
            action="기본값 유지. 내일도 동일 조건으로 관찰",
        )

    out_dir = ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"next_day_reco_{day.isoformat()}.json"
    out_md = out_dir / f"next_day_reco_{day.isoformat()}.md"

    out_json.write_text(json.dumps(recos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # simple md
    lines = [f"# 내일 장 대비 추천 (v1) - {day.isoformat()}\n", "## 추천 요약\n"]
    for r in recos["recommendations"]:
        lines.append(f"### {r['title']}\n")
        lines.append(f"- 왜: {r['why']}")
        lines.append(f"- 액션: {r['action']}")
        if r.get("value") is not None:
            lines.append(f"- 값: `{json.dumps(r['value'], ensure_ascii=False)}`")
        lines.append("")

    out_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
