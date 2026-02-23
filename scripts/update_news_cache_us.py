"""Update US news cache for dynamic thresholds.

Writes: data/news_cache_us/YYYYMMDD.json
Best-effort: if fetch fails, still writes file with whatever is available.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import sys

KST = ZoneInfo("Asia/Seoul")
BASE_DIR = Path(__file__).resolve().parents[1]

# ensure project root on sys.path
sys.path.insert(0, str(BASE_DIR))

from news import NewsSentimentAnalyzer


def main() -> int:
    ymd = datetime.now(KST).strftime("%Y%m%d")

    # Default US symbols (broad + mega-cap) to ensure enough coverage.
    symbols = ["VOO", "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "TSLA"]

    an = NewsSentimentAnalyzer(max_news_age_hours=72)

    scores: dict[str, float] = {}
    updated_at_epoch: dict[str, float] = {}

    # async methods; use asyncio
    import asyncio

    async def run():
        for sym in symbols:
            try:
                r = await an.get_us_sentiment_score(sym)
                s = float(r.get("score") or 0)
                scores[sym] = s
                ts = r.get("ts") or r.get("timestamp")
                if ts:
                    try:
                        # store ts as iso (keep as str) but also epoch approx
                        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                        updated_at_epoch[sym] = dt.timestamp()
                    except Exception:
                        pass
            except Exception:
                continue

    asyncio.run(run())

    out_dir = BASE_DIR / "data" / "news_cache_us"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(KST).isoformat(),
        "scores": scores,
        "updated_at_epoch": updated_at_epoch,
    }
    (out_dir / f"{ymd}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"ymd": ymd, "count": len(scores), "symbols": list(scores.keys())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
