import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.recommend_thresholds_daily import _apply_cached_overrides


def test_recommend_thresholds_uses_premarket_metrics_and_news_cache(tmp_path: Path):
    ymd = "20260219"
    metrics_dir = tmp_path / "data" / "premarket_metrics_us"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / f"{ymd}.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-02-19T08:30:00+09:00",
                "metrics": {
                    "VOO": {
                        "premarket_change_pct": 1.25,
                        "premarket_atr_pct_est": 0.87,
                        "premarket_trend": "up",
                        "sample_count": 12,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    news_dir = tmp_path / "data" / "news_cache_us"
    news_dir.mkdir(parents=True, exist_ok=True)
    (news_dir / f"{ymd}.json").write_text(
        json.dumps({"scores": {"AAPL": 20, "MSFT": 10}}), encoding="utf-8"
    )

    base_inputs = {
        "prev_day": {"return_pct": -0.5, "range_pct": 1.0, "sample_count": 120},
        "news": {"sentiment_avg": -30.0, "sample_count": 2, "coverage": 0.2, "success_rate": 0.2, "std": 5.0},
        "chart": {"trend": "DOWN", "strength": -0.1, "sample_count": 120},
        "atr": {"atr_pct": 2.5, "sample_count": 120},
    }

    out = _apply_cached_overrides(
        base_dir=tmp_path,
        market="US",
        ymd=ymd,
        inputs=base_inputs,
        symbols=["AAPL", "MSFT"],
        rep_symbol="VOO",
        news_cap=10,
    )

    assert out["prev_day"]["return_pct"] == 1.25
    assert out["atr"]["atr_pct"] == 0.87
    assert out["chart"]["trend"] == "UP"
    assert out["chart"]["sample_count"] == 12
    assert out["news"]["sample_count"] == 2
    assert out["news"]["sentiment_avg"] == 15.0
