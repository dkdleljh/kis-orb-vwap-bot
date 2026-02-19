"""Recommended strategy profiles (thresholds + scoring policy).

주인님 결정:
- 스윙도 실주문
- 점수 임계값은 추천값 사용

이 파일은 '추천 기본값'을 코드로 고정하여,
config.json에서 누락 시에도 예측 가능한 동작을 하도록 합니다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyProfile:
    market: str  # KR|US
    style: str  # SCALP|SWING
    entry_threshold: int


# 추천 임계값 (보수적 기준)
# - KR은 체결/호가 품질이 비교적 안정적이라 SCALP를 약간 낮게
# - US는 스프레드/슬리피지 리스크가 커서 SCALP는 더 엄격
RECOMMENDED_PROFILES = {
    ("KR", "SCALP"): StrategyProfile("KR", "SCALP", entry_threshold=72),
    ("KR", "SWING"): StrategyProfile("KR", "SWING", entry_threshold=65),
    ("US", "SCALP"): StrategyProfile("US", "SCALP", entry_threshold=75),
    ("US", "SWING"): StrategyProfile("US", "SWING", entry_threshold=68),
}


def get_base_threshold(market: str, style: str) -> int:
    """Return the hardcoded baseline threshold (ignores dynamic file).

    Use this when computing a new dynamic recommendation to avoid compounding.
    """
    m = market.upper()
    s = style.upper()
    prof = RECOMMENDED_PROFILES.get((m, s))
    if prof is None:
        return 70
    return int(prof.entry_threshold)


def get_recommended_threshold(market: str, style: str) -> int:
    """Return recommended entry threshold.

    Order of precedence:
    1) logs/dynamic_thresholds.json (daily recommender)
    2) hardcoded RECOMMENDED_PROFILES
    """
    from dynamic_thresholds import load_thresholds

    dyn = load_thresholds("logs/dynamic_thresholds.json") or {}
    m = market.upper()
    s = style.upper()

    if m == "KR" and s == "SCALP" and "kr_scalp" in dyn:
        return int(dyn["kr_scalp"])
    if m == "KR" and s == "SWING" and "kr_swing" in dyn:
        return int(dyn["kr_swing"])
    if m == "US" and s == "SCALP" and "us_scalp" in dyn:
        return int(dyn["us_scalp"])
    if m == "US" and s == "SWING" and "us_swing" in dyn:
        return int(dyn["us_swing"])

    return get_base_threshold(m, s)
