"""Dynamic threshold computation and loading.

- 매일 전일/당일 뉴스(간이 감성 점수) 기반으로
  시장 리스크 레짐에 따라 진입 임계값을 자동 보정합니다.

원칙(추천값):
- 기본 임계값은 strategy_profiles.py / config.json에 있는 값
- 뉴스/리스크가 나쁘면 임계값을 올려서(더 엄격) 진입 감소
- 뉴스/리스크가 좋으면 임계값을 내려서(덜 엄격) 진입 증가
- 보정폭은 작게(최대 +/- 8) 유지

출력 파일(SSOT): logs/dynamic_thresholds.json
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict


DEFAULT_PATH = "logs/dynamic_thresholds.json"


@dataclass(frozen=True)
class Thresholds:
    kr_scalp: int
    kr_swing: int
    us_scalp: int
    us_swing: int


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def _adjust(base: int, sentiment_avg: float) -> int:
    """Small adjustment based on sentiment.

    sentiment_avg: -100..+100

    Rules:
    - Very negative: +5~+8 (stricter)
    - Neutral: 0
    - Positive: -2~-5 (looser)
    """
    adj = 0
    if sentiment_avg <= -30:
        adj = +8
    elif sentiment_avg <= -15:
        adj = +5
    elif sentiment_avg <= -8:
        adj = +3
    elif sentiment_avg >= 30:
        adj = -5
    elif sentiment_avg >= 15:
        adj = -3
    elif sentiment_avg >= 8:
        adj = -2
    return _clamp(base + adj, 50, 90)


def load_thresholds(path: str = DEFAULT_PATH) -> Dict[str, int] | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(j, dict):
            return None
        vals = j.get("thresholds")
        if not isinstance(vals, dict):
            return None
        out = {}
        for k in ("kr_scalp", "kr_swing", "us_scalp", "us_swing"):
            if k in vals:
                out[k] = int(vals[k])
        return out
    except Exception:
        return None


def write_thresholds(
    *,
    path: str,
    base: Thresholds,
    kr_sentiment_avg: float,
    us_sentiment_avg: float,
    extras: dict | None = None,
) -> Thresholds:
    th = Thresholds(
        kr_scalp=_adjust(base.kr_scalp, kr_sentiment_avg),
        kr_swing=_adjust(base.kr_swing, kr_sentiment_avg),
        us_scalp=_adjust(base.us_scalp, us_sentiment_avg),
        us_swing=_adjust(base.us_swing, us_sentiment_avg),
    )

    payload = {
        "ts_kst": datetime.now().astimezone().isoformat(),
        "inputs": {
            "kr_sentiment_avg": float(kr_sentiment_avg),
            "us_sentiment_avg": float(us_sentiment_avg),
            **(extras or {}),
        },
        "base": {
            "kr_scalp": base.kr_scalp,
            "kr_swing": base.kr_swing,
            "us_scalp": base.us_scalp,
            "us_swing": base.us_swing,
        },
        "thresholds": {
            "kr_scalp": th.kr_scalp,
            "kr_swing": th.kr_swing,
            "us_scalp": th.us_scalp,
            "us_swing": th.us_swing,
        },
    }

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return th
