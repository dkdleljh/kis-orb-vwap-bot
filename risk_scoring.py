"""Risk/condition scoring with auto-weights for KR/US shared pipeline."""

from __future__ import annotations

import math
from typing import Any


FEATURES = ("prev_day", "news", "chart", "vix", "fg", "atr")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _to_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def _normalize_weights(raw: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, raw.get(k, 0.0)) for k in FEATURES)
    if total <= 0:
        return {k: (1.0 / len(FEATURES)) for k in FEATURES}
    return {k: max(0.0, raw.get(k, 0.0)) / total for k in FEATURES}


def _enforce_floor(weights: dict[str, float], key: str, floor: float) -> dict[str, float]:
    if weights.get(key, 0.0) >= floor:
        return weights
    deficit = floor - weights.get(key, 0.0)
    donors = [k for k in FEATURES if k != key and weights.get(k, 0.0) > 0]
    donor_total = sum(weights[k] for k in donors)
    if donor_total <= 0:
        return weights
    for k in donors:
        weights[k] = max(0.0, weights[k] - (weights[k] / donor_total) * deficit)
    weights[key] = floor
    return _normalize_weights(weights)


def _limit_weight_change(weights: dict[str, float], prev: dict[str, Any] | None, max_delta: float = 0.20) -> dict[str, float]:
    if not prev:
        return weights
    clipped: dict[str, float] = {}
    for k in FEATURES:
        p = _to_float(prev.get(k) if isinstance(prev, dict) else None)
        if p is None:
            clipped[k] = weights.get(k, 0.0)
            continue
        lo = max(0.0, p - max_delta)
        hi = min(1.0, p + max_delta)
        clipped[k] = _clamp(weights.get(k, 0.0), lo, hi)
    return _normalize_weights(clipped)


def _weight_quality(*, present: bool, reliability: float, coverage: float, variability: float) -> float:
    if not present:
        return 0.0
    rel = _clamp(reliability, 0.0, 1.0)
    cov = _clamp(coverage, 0.0, 1.0)
    var = _clamp(variability, 0.0, 1.0)
    quality = (0.10 + 0.55 * rel + 0.35 * cov) * (1.0 - 0.5 * var)
    return _clamp(quality, 0.0, 1.0)


def _prev_day_component(v: dict[str, Any]) -> tuple[float, float, float, float, dict[str, Any]]:
    ret = _to_float(v.get("return_pct"))
    rng = _to_float(v.get("range_pct"))
    samples = _to_float(v.get("sample_count")) or 0.0
    if ret is None and rng is None:
        return 50.0, 0.0, 0.0, 1.0, {"present": False}

    score = 50.0
    if ret is not None:
        score += _clamp(ret * 4.0, -25.0, 25.0)
    if rng is not None:
        score -= _clamp(max(0.0, rng - 1.8) * 6.0, 0.0, 30.0)
    score = _clamp(score, 0.0, 100.0)

    coverage = _clamp(samples / 60.0, 0.0, 1.0)
    reliability = 1.0 if (ret is not None and rng is not None) else 0.6
    variability = _clamp((rng or 0.0) / 8.0, 0.0, 1.0)
    return score, reliability, coverage, variability, {
        "present": True,
        "return_pct": ret,
        "range_pct": rng,
        "sample_count": int(samples),
    }


def _news_component(v: dict[str, Any]) -> tuple[float, float, float, float, dict[str, Any]]:
    sentiment = _to_float(v.get("sentiment_avg"))
    count = _to_float(v.get("sample_count")) or 0.0
    success_rate = _to_float(v.get("success_rate"))
    std = _to_float(v.get("std"))
    if sentiment is None:
        return 50.0, 0.0, 0.0, 1.0, {"present": False, "sample_count": int(count)}

    score = _clamp((sentiment + 100.0) / 2.0, 0.0, 100.0)
    coverage = _clamp(count / 20.0, 0.0, 1.0)
    reliability = _clamp(success_rate if success_rate is not None else (1.0 if count > 0 else 0.0), 0.0, 1.0)
    variability = _clamp((std / 60.0) if std is not None else 0.4, 0.0, 1.0)
    return score, reliability, coverage, variability, {
        "present": True,
        "sentiment_avg": sentiment,
        "sample_count": int(count),
        "success_rate": reliability,
        "std": std,
    }


def _chart_component(v: dict[str, Any]) -> tuple[float, float, float, float, dict[str, Any]]:
    trend = str(v.get("trend") or "").upper()
    strength = _to_float(v.get("strength"))
    samples = _to_float(v.get("sample_count")) or 0.0
    if trend not in {"UP", "DOWN", "FLAT"}:
        return 50.0, 0.0, 0.0, 1.0, {"present": False, "trend": None}

    base = {"UP": 72.0, "FLAT": 55.0, "DOWN": 35.0}[trend]
    if strength is not None:
        base += _clamp(strength, -1.0, 1.0) * 10.0
    score = _clamp(base, 0.0, 100.0)

    coverage = _clamp(samples / 120.0, 0.0, 1.0)
    reliability = 1.0
    variability = _clamp(1.0 - coverage, 0.0, 1.0)
    return score, reliability, coverage, variability, {
        "present": True,
        "trend": trend,
        "strength": strength,
        "sample_count": int(samples),
    }


def _vix_component(v: dict[str, Any]) -> tuple[float, float, float, float, dict[str, Any]]:
    last = _to_float(v.get("vix_last"))
    ma50 = _to_float(v.get("vix_ma50"))
    gap = _to_float(v.get("vix_gap"))
    if last is None:
        return 50.0, 0.0, 0.0, 1.0, {"present": False}

    gap_eff = gap if gap is not None else (last - ma50 if ma50 is not None else 0.0)
    score = 78.0 - _clamp(max(0.0, last - 18.0) * 2.2, 0.0, 55.0) - _clamp(max(0.0, gap_eff) * 4.0, 0.0, 30.0)
    if gap_eff < 0:
        score += _clamp(abs(gap_eff) * 1.2, 0.0, 8.0)
    score = _clamp(score, 0.0, 100.0)

    coverage = 1.0
    reliability = 1.0 if ma50 is not None else 0.8
    variability = _clamp(abs(gap_eff) / 10.0, 0.0, 1.0)
    return score, reliability, coverage, variability, {
        "present": True,
        "vix_last": last,
        "vix_ma50": ma50,
        "vix_gap": gap_eff,
    }


def _fg_component(v: dict[str, Any]) -> tuple[float, float, float, float, dict[str, Any]]:
    fg_score = _to_float(v.get("fg_score"))
    if fg_score is None:
        return 50.0, 0.0, 0.0, 1.0, {"present": False}
    score = _clamp(fg_score, 0.0, 100.0)
    coverage = 1.0
    reliability = 0.9
    variability = _clamp(abs(fg_score - 50.0) / 50.0, 0.0, 1.0)
    return score, reliability, coverage, variability, {"present": True, "fg_score": fg_score}


def _atr_component(v: dict[str, Any]) -> tuple[float, float, float, float, dict[str, Any]]:
    atr_pct = _to_float(v.get("atr_pct"))
    samples = _to_float(v.get("sample_count")) or 0.0
    if atr_pct is None:
        return 50.0, 0.0, 0.0, 1.0, {"present": False}

    target = 1.6
    score = 82.0 - _clamp(abs(atr_pct - target) * 24.0, 0.0, 70.0)
    score = _clamp(score, 0.0, 100.0)
    coverage = _clamp(samples / 60.0, 0.0, 1.0)
    reliability = 1.0 if samples >= 30 else _clamp(samples / 30.0, 0.0, 1.0)
    variability = _clamp(atr_pct / 6.0, 0.0, 1.0)
    return score, reliability, coverage, variability, {
        "present": True,
        "atr_pct": atr_pct,
        "sample_count": int(samples),
    }


_SCORERS = {
    "prev_day": _prev_day_component,
    "news": _news_component,
    "chart": _chart_component,
    "vix": _vix_component,
    "fg": _fg_component,
    "atr": _atr_component,
}


def score_to_adjustment(score: float, max_abs: int = 8) -> int:
    """Convert score(0..100) to threshold adjustment.

    Low score -> positive adjustment (stricter threshold).
    High score -> negative adjustment (looser threshold).
    """
    raw = ((50.0 - _clamp(score, 0.0, 100.0)) / 50.0) * float(max_abs)
    return int(_clamp(round(raw), -max_abs, max_abs))


def compute_market_score(
    *,
    market: str,
    feature_inputs: dict[str, Any],
    prev_weights: dict[str, Any] | None = None,
    prev_score: float | None = None,
) -> dict[str, Any]:
    market_u = str(market or "").upper()
    components: dict[str, float] = {}
    raw_weights: dict[str, float] = {k: 0.0 for k in FEATURES}
    input_summary: dict[str, Any] = {}

    for key in FEATURES:
        scorer = _SCORERS[key]
        block = feature_inputs.get(key) if isinstance(feature_inputs, dict) else {}
        if not isinstance(block, dict):
            block = {}
        score, reliability, coverage, variability, summary = scorer(block)
        components[key] = float(score)
        input_summary[key] = summary
        present = bool(summary.get("present", False))
        raw_weights[key] = _weight_quality(
            present=present,
            reliability=reliability,
            coverage=coverage,
            variability=variability,
        )

    weights = _normalize_weights(raw_weights)
    if input_summary.get("vix", {}).get("present"):
        weights = _enforce_floor(weights, "vix", 0.08)
    if input_summary.get("fg", {}).get("present"):
        weights = _enforce_floor(weights, "fg", 0.08)
    weights = _limit_weight_change(weights, prev_weights, max_delta=0.20)

    score = sum(weights[k] * components[k] for k in FEATURES)
    score = _clamp(score, 0.0, 100.0)
    if prev_score is not None:
        score = _clamp(score, float(prev_score) - 20.0, float(prev_score) + 20.0)

    return {
        "market": market_u,
        "score": float(round(score, 2)),
        "weights": {k: float(round(weights[k], 6)) for k in FEATURES},
        "components": {k: float(round(components[k], 2)) for k in FEATURES},
        "input_summary": input_summary,
    }

