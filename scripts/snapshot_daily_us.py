"""Snapshot US daily bars to local files (yfinance best-effort).

추천값:
- 대표 심볼: QQQ (성장주/나스닥 레짐), 보조: SPY
- 저장: data/daily_us/<symbol>_YYYYMMDD.json

이 파일은 미장 임계값 산정(레짐: ATR%, 추세)에 사용합니다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import csv
import urllib.request


def _download_stooq(symbol: str) -> list[dict]:
    # Stooq provides free daily CSV. Symbols: voo.us, spy.us, qqq.us etc.
    s = symbol.lower()
    stooq_sym = f"{s}.us"
    url = f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d"

    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            text = r.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    rows = list(csv.DictReader(text.splitlines()))
    out: list[dict] = []
    for row in rows:
        try:
            c = float(row.get("Close") or 0)
            if c <= 0:
                continue
            out.append(
                {
                    "date": row.get("Date"),
                    "open": float(row.get("Open") or 0),
                    "high": float(row.get("High") or 0),
                    "low": float(row.get("Low") or 0),
                    "close": c,
                    "volume": float(row.get("Volume") or 0),
                }
            )
        except Exception:
            continue
    return out


def main() -> int:
    base_dir = Path(__file__).resolve().parents[1]
    data_dir = base_dir / "data" / "daily_us"
    data_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y%m%d")
    # Recommended US representatives: VOO (S&P 500), backup: SPY
    reps = ["VOO", "SPY"]

    for sym in reps:
        bars = _download_stooq(sym)
        if not bars:
            print(f"no daily bars for {sym}")
            continue
        path = data_dir / f"{sym}_{today}.json"
        path.write_text(json.dumps(bars, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"saved {sym} bars={len(bars)} -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
