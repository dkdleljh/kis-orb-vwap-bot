#!/usr/bin/env python3
"""Check dynamic threshold signal-input health by date label."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FEATURES = ("prev_day", "news", "chart", "vix", "fg", "atr")


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _fmt_bool(v: bool) -> str:
    return "YES" if bool(v) else "NO"


def _component_row(name: str, payload: dict) -> str:
    present = bool(payload.get("present", False))
    sample_count = payload.get("sample_count")
    sample_txt = "-" if sample_count is None else str(sample_count)
    return f"  - {name:<8} present={_fmt_bool(present)} sample_count={sample_txt}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (label only)")
    ap.add_argument(
        "--path",
        default=str(ROOT / "logs" / "dynamic_thresholds.json"),
        help="dynamic thresholds json path",
    )
    args = ap.parse_args()

    try:
        target_day = dt.date.fromisoformat(str(args.date))
    except Exception:
        print(f"invalid_date={args.date}")
        return 2

    data = _read_json(Path(args.path))
    if not data:
        print(f"missing_or_invalid={args.path}")
        return 2

    ts = str(data.get("ts_kst") or "")
    ts_day = ""
    if ts:
        ts_day = ts.split("T")[0]

    print(f"target_date={target_day.isoformat()}")
    print(f"source={args.path}")
    print(f"snapshot_ts_kst={ts or '-'}")
    print(f"snapshot_date_matches_target={_fmt_bool(ts_day == target_day.isoformat())}")

    inputs = data.get("inputs") if isinstance(data.get("inputs"), dict) else {}
    for market in ("kr", "us"):
        block = inputs.get(market) if isinstance(inputs.get(market), dict) else {}
        print(f"{market.upper()}:")
        for feature in FEATURES:
            payload = block.get(feature) if isinstance(block.get(feature), dict) else {}
            print(_component_row(feature, payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
