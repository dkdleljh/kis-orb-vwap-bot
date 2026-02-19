"""Tests for dynamic threshold risk scoring pipeline."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dynamic_thresholds import threshold_from_score
from risk_scoring import compute_market_score, score_to_adjustment


def test_weights_redistribute_when_news_missing():
    res = compute_market_score(
        market="KR",
        feature_inputs={
            "prev_day": {"return_pct": 0.8, "range_pct": 1.2, "sample_count": 80},
            "news": {"sentiment_avg": None, "sample_count": 0},
            "chart": {"trend": "UP", "sample_count": 120},
            "vix": {"vix_last": 19.0, "vix_ma50": 18.0, "vix_gap": 1.0},
            "fg": {"fg_score": 58.0},
            "atr": {"atr_pct": 1.5, "sample_count": 80},
        },
    )
    w = res["weights"]
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["news"] == 0.0
    assert w["chart"] > 0.0


def test_score_is_bounded_0_to_100():
    res = compute_market_score(
        market="US",
        feature_inputs={
            "prev_day": {"return_pct": -9.0, "range_pct": 12.0, "sample_count": 120},
            "news": {"sentiment_avg": -120.0, "sample_count": 15, "success_rate": 1.0, "std": 90.0},
            "chart": {"trend": "DOWN", "strength": -1.0, "sample_count": 120},
            "vix": {"vix_last": 45.0, "vix_ma50": 22.0, "vix_gap": 23.0},
            "fg": {"fg_score": 4.0},
            "atr": {"atr_pct": 7.0, "sample_count": 120},
        },
    )
    assert 0.0 <= float(res["score"]) <= 100.0


def test_adjustment_and_threshold_clamp_ranges():
    for score in (0.0, 25.0, 50.0, 75.0, 100.0):
        adj = score_to_adjustment(score)
        assert -8 <= adj <= 8

    th_hi, adj_hi = threshold_from_score(base=88, score=0.0)
    th_lo, adj_lo = threshold_from_score(base=52, score=100.0)
    assert -8 <= adj_hi <= 8
    assert -8 <= adj_lo <= 8
    assert 50 <= th_hi <= 90
    assert 50 <= th_lo <= 90

