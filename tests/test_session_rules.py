from __future__ import annotations

from datetime import datetime

from core.session_rules import build_time_rules, exit_phase, resolve_entry_risk_bounds


class _DummyLogger:
    def __init__(self) -> None:
        self.infos: list[tuple] = []
        self.warnings: list[tuple] = []

    def info(self, *args, **kwargs):
        self.infos.append(args)
        return None

    def warning(self, *args, **kwargs):
        self.warnings.append(args)
        return None


def test_build_time_rules_defaults_and_invalid_fallback():
    logger = _DummyLogger()
    rules, early_exit = build_time_rules(
        {
            "observe_start": "bad-time",
            "or_start": "",
            "or_end": "09:06:00",
            "entry_start": None,
            "force_exit": "15:30:00",
        },
        logger,
        log_prefix="[test]",
    )

    assert str(rules.observe_start) == "08:59:00"
    assert str(rules.or_start) == "09:00:00"
    assert str(rules.or_end) == "09:06:00"
    assert str(rules.entry_start) == "09:05:05"
    assert str(rules.force_exit) == "15:30:00"
    assert str(early_exit) == "15:00:00"
    assert any("invalid time_rules.%s" in str(x[0]) for x in logger.warnings)
    assert any("defaults used for:" in str(x[0]) for x in logger.warnings)


def test_exit_phase_cutoffs():
    early = datetime(2026, 2, 20, 15, 0, 0).time()
    force = datetime(2026, 2, 20, 15, 15, 0).time()

    p, t = exit_phase(datetime(2026, 2, 20, 14, 59, 59), early, force)
    assert p is None
    assert str(t) == "15:18:00"

    p, _ = exit_phase(datetime(2026, 2, 20, 15, 0, 0), early, force)
    assert p == "early"

    p, _ = exit_phase(datetime(2026, 2, 20, 15, 15, 0), early, force)
    assert p == "force"

    p, _ = exit_phase(datetime(2026, 2, 20, 15, 18, 0), early, force)
    assert p == "emergency"


def test_resolve_entry_risk_bounds_clamps():
    b = resolve_entry_risk_bounds(
        {
            "max_total_position_pct": 9.0,
            "max_symbol_position_pct": -1.0,
        }
    )
    assert b.max_total_position_pct == 0.95
    assert b.max_symbol_position_pct == 0.01
