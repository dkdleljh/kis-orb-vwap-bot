"""Dynamic threshold computation and loading.

SSOT output: logs/dynamic_thresholds.json

- KR/US 공통 risk_scoring 파이프라인으로 score(0..100) 산출
- score -> threshold adjustment(max +/-8)
- 입력 결측 시 feature 가중치 자동 재분배
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from risk_scoring import compute_market_score, score_to_adjustment


DEFAULT_PATH = "logs/dynamic_thresholds.json"


@dataclass(frozen=True)
class Thresholds:
    kr_scalp: int
    kr_swing: int
    us_scalp: int
    us_swing: int


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def threshold_from_score(*, base: int, score: float) -> tuple[int, int]:
    adj = score_to_adjustment(score, max_abs=8)
    return _clamp(base + adj, 50, 90), int(adj)


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
    kr_inputs: dict[str, Any],
    us_inputs: dict[str, Any],
    extras: dict | None = None,
) -> Thresholds:
    p = Path(path)
    prev: dict[str, Any] = {}
    if p.exists():
        try:
            prev = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(prev, dict):
                prev = {}
        except Exception:
            prev = {}

    prev_scoring = prev.get("scoring") if isinstance(prev, dict) else {}
    if not isinstance(prev_scoring, dict):
        prev_scoring = {}

    kr_prev = prev_scoring.get("kr") if isinstance(prev_scoring.get("kr"), dict) else {}
    us_prev = prev_scoring.get("us") if isinstance(prev_scoring.get("us"), dict) else {}

    kr_calc = compute_market_score(
        market="KR",
        feature_inputs=kr_inputs or {},
        prev_weights=(kr_prev.get("weights") if isinstance(kr_prev, dict) else None),
        prev_score=(kr_prev.get("score") if isinstance(kr_prev, dict) else None),
    )
    us_calc = compute_market_score(
        market="US",
        feature_inputs=us_inputs or {},
        prev_weights=(us_prev.get("weights") if isinstance(us_prev, dict) else None),
        prev_score=(us_prev.get("score") if isinstance(us_prev, dict) else None),
    )

    kr_scalp, kr_adj = threshold_from_score(base=base.kr_scalp, score=float(kr_calc["score"]))
    kr_swing, _ = threshold_from_score(base=base.kr_swing, score=float(kr_calc["score"]))
    us_scalp, us_adj = threshold_from_score(base=base.us_scalp, score=float(us_calc["score"]))
    us_swing, _ = threshold_from_score(base=base.us_swing, score=float(us_calc["score"]))

    th = Thresholds(kr_scalp=kr_scalp, kr_swing=kr_swing, us_scalp=us_scalp, us_swing=us_swing)

    payload = {
        "version": 2,
        "ts_kst": datetime.now().astimezone().isoformat(),
        "inputs": {"kr": kr_calc["input_summary"], "us": us_calc["input_summary"], **(extras or {})},
        "base": {
            "kr_scalp": base.kr_scalp,
            "kr_swing": base.kr_swing,
            "us_scalp": base.us_scalp,
            "us_swing": base.us_swing,
        },
        "scoring": {
            "kr": {
                "score": kr_calc["score"],
                "weights": kr_calc["weights"],
                "components": kr_calc["components"],
                "adjustment": kr_adj,
            },
            "us": {
                "score": us_calc["score"],
                "weights": us_calc["weights"],
                "components": us_calc["components"],
                "adjustment": us_adj,
            },
        },
        "thresholds": {
            "kr_scalp": th.kr_scalp,
            "kr_swing": th.kr_swing,
            "us_scalp": th.us_scalp,
            "us_swing": th.us_swing,
        },
    }

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return th
