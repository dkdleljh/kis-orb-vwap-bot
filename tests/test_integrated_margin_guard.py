from modules.us_buy_guard import evaluate_us_buy_guard, set_symbol_cooldown


def test_integrated_margin_guard_allows_buy_attempt_when_ord_psbl_qty_zero_and_estimate_enough() -> None:
    d = evaluate_us_buy_guard(
        integrated_margin_mode=True,
        ord_psbl_qty=0,
        integrated_margin_estimate_usd=250.0,
        min_usd=100.0,
    )
    assert d.buy_attempt_allowed is True
    assert d.used_integrated_margin_fallback is True


def test_integrated_margin_guard_blocks_when_ord_psbl_qty_zero_and_estimate_too_small() -> None:
    d = evaluate_us_buy_guard(
        integrated_margin_mode=True,
        ord_psbl_qty=0,
        integrated_margin_estimate_usd=30.0,
        min_usd=100.0,
    )
    assert d.buy_attempt_allowed is False


def test_order_failure_sets_symbol_cooldown() -> None:
    cooldown_map: dict[str, float] = {}
    now_ts = 1000.0
    until = set_symbol_cooldown(cooldown_map=cooldown_map, symbol="AAPL", now_ts=now_ts, cooldown_sec=120.0)
    assert until == 1120.0
    assert cooldown_map["AAPL"] == 1120.0
