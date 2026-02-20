#!/usr/bin/env python3
"""Run walk-forward backtest for next-day recommendation model."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import scripts.recommend_next_day as reco

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (KST). default=today", default=None)
    ap.add_argument("--lookback-days", type=int, default=30)
    ap.add_argument("--min-train-rows", type=int, default=10)
    ap.add_argument("--walk-forward-k", type=int, default=7)
    ap.add_argument("--model", choices=["ridge", "tree"], default="ridge")
    ap.add_argument("--hit-threshold", type=float, default=500.0)
    args = ap.parse_args()

    kst = dt.timezone(dt.timedelta(hours=9))
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=kst).date()

    x_train, y_train, days = reco._discover_training_rows(day, max(7, int(args.lookback_days)))
    wf = reco._walk_forward_validate(
        x_train,
        y_train,
        days,
        min_train_rows=max(1, int(args.min_train_rows)),
        eval_last_k=max(1, int(args.walk_forward_k)),
        model_name=args.model,
        hit_threshold=float(args.hit_threshold),
    )

    out_dir = ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_md = out_dir / f"ml_backtest_next_day_{day.isoformat()}.md"

    lines = [
        f"# ML Backtest Next Day - {day.isoformat()}",
        "",
        "## Inputs",
        "",
        f"- model: `{args.model}`",
        f"- lookback_days: `{int(args.lookback_days)}`",
        f"- min_train_rows: `{int(args.min_train_rows)}`",
        f"- walk_forward_k: `{int(args.walk_forward_k)}`",
        f"- hit_threshold: `{float(args.hit_threshold)}`",
        f"- discovered_rows: `{len(x_train)}`",
        "",
        "## Metrics",
        "",
        f"- n_eval: `{int(wf.get('n_eval') or 0)}`",
        f"- mae: `{wf.get('mae')}`",
        f"- directional_accuracy: `{wf.get('directional_accuracy')}`",
        f"- hit_rate_above_threshold: `{wf.get('hit_rate_above_threshold')}`",
        f"- calibration_error: `{wf.get('calibration_error')}`",
        "",
        "## Evaluated Days",
        "",
        f"- days: `{json.dumps(list(wf.get('evaluated_days') or []), ensure_ascii=False)}`",
    ]

    out_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(str(out_md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
