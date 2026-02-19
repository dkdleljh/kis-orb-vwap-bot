"""Correlation-id utilities.

Goal:
- Use a single, consistent correlation id format across engine/modules/scripts.
- Enable joining: Decision <-> OrderIntent/RiskDecision <-> OrderSubmitted/Fill.

Design:
- Correlation id is a UUID4 hex with a short prefix for readability.
- Callers may add lightweight context fields into Decision.extra, but the corr id
  itself stays opaque and stable.
"""

from __future__ import annotations

import uuid


def new_corr(prefix: str = "corr") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
