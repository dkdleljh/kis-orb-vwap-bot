"""Unified scoring primitives for KR/US + Scalp/Swing strategies.

목표:
- 모든 전략이 동일한 형태로 '점수화' 결과를 내도록 표준화
- 로그/리포팅/알림에서 일관된 포맷으로 표현

NOTE: 본 파일은 '표현/계약' 레이어입니다.
실제 점수 계산 로직은 각 Strategy/Module에서 구성해 사용합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ScoreBreakdown:
    """Category -> points mapping."""

    categories: Dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return float(sum(self.categories.values()))

    def top(self, n: int = 5) -> List[str]:
        items = sorted(self.categories.items(), key=lambda kv: kv[1], reverse=True)
        return [f"{k} {v:.0f}" for k, v in items[:n]]


@dataclass
class SignalScore:
    """Standardized scoring output.

    side:
      - "BUY" | "SELL" | None

    market/style are labels used for reporting.
    """

    symbol: str
    side: Optional[str] = None
    score: float = 0.0
    threshold: float = 0.0
    market: str = "KR"  # KR|US
    style: str = "SCALP"  # SCALP|SWING
    risk_grade: str = "C"  # A/B/C/D
    breakdown: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    reasons: List[str] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        return bool(self.side) and (self.score >= self.threshold)

    def to_line(self) -> str:
        side = self.side or "NONE"
        top = ",".join(self.breakdown.top(5)) if self.breakdown.categories else ""
        reasons = ",".join(self.reasons[:5]) if self.reasons else ""
        return (
            f"[{self.market}-{self.style}] {self.symbol} {side} score={self.score:.0f}/100 "
            f"(th={self.threshold:.0f}, risk={self.risk_grade}) "
            f"breakdown=[{top}] reasons=[{reasons}]"
        )
