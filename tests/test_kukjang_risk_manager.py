from modules.kukjang import RiskManager


def test_kukjang_risk_manager_reset_daily_clears_counters() -> None:
    risk = RiskManager(max_entries=2, daily_loss_limit=-0.02, max_consecutive_stop=2)

    risk.record_entry()
    risk.record_entry()
    risk.record_exit(-0.03, is_stop=True)

    assert risk.can_enter() is False

    risk.reset_daily()

    assert risk.entries_today == 0
    assert risk.daily_pnl == 0.0
    assert risk.consecutive_stops == 0
    assert risk.can_enter() is True
