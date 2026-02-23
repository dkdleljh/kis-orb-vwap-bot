from perfect_strategy import Perfect100Strategy


def test_get_atr_targets_buy_and_sell():
    strategy = Perfect100Strategy()

    buy_targets = strategy.get_atr_targets(entry_price=100.0, atr=2.0, side="BUY")
    sell_targets = strategy.get_atr_targets(entry_price=100.0, atr=2.0, side="SELL")

    assert buy_targets == [("TP1", 104.0), ("TP2", 108.0), ("TP3", 112.0)]
    assert sell_targets == [("TP1", 96.0), ("TP2", 92.0), ("TP3", 88.0)]


def test_get_targets_uses_r_multiples_when_stop_distance_is_known():
    strategy = Perfect100Strategy()
    targets = strategy.get_atr_targets(
        entry_price=100.0, atr=2.0, side="BUY", stop_distance=1.5
    )
    assert targets == [("TP1", 103.0), ("TP2", 106.0), ("TP3", 109.0)]


def test_should_take_profit_uses_atr_targets_and_returns_highest_hit():
    strategy = Perfect100Strategy()

    should_tp, label = strategy.should_take_profit(
        current_price=112.0, entry_price=100.0, atr=2.0, side="BUY"
    )
    assert should_tp is True
    assert label == "TP3"

    should_tp, label = strategy.should_take_profit(
        current_price=92.0, entry_price=100.0, atr=2.0, side="SELL"
    )
    assert should_tp is True
    assert label == "TP2"


def test_get_partial_tp_levels_supports_sell_symmetrically():
    strategy = Perfect100Strategy()

    buy_levels = strategy.get_partial_tp_levels(
        current_price=104.0, entry_price=100.0, atr=2.0, side="BUY"
    )
    sell_levels = strategy.get_partial_tp_levels(
        current_price=92.0, entry_price=100.0, atr=2.0, side="SELL"
    )

    assert buy_levels == [(104.0, 0.30, 30)]
    assert sell_levels == [(96.0, 0.30, 30), (92.0, 0.30, 30)]


def test_should_trailing_stop_uses_3atr_and_tightens_to_1_5atr_after_tp1():
    strategy = Perfect100Strategy()

    should_trail, _ = strategy.should_trailing_stop(
        current_price=106.1,
        peak_price=109.0,
        entry_price=100.0,
        atr=1.0,
        side="BUY",
    )
    assert should_trail is False

    should_trail, _ = strategy.should_trailing_stop(
        current_price=106.0,
        peak_price=109.0,
        entry_price=100.0,
        atr=1.0,
        side="BUY",
    )
    assert should_trail is True

    should_trail, _ = strategy.should_trailing_stop(
        current_price=103.8,
        peak_price=105.4,
        entry_price=100.0,
        atr=1.0,
        side="BUY",
        tp1_hit=True,
    )
    assert should_trail is True


def test_should_trailing_stop_supports_breakeven_after_tp1():
    strategy = Perfect100Strategy()
    should_trail, label = strategy.should_trailing_stop(
        current_price=99.9,
        peak_price=101.0,
        entry_price=100.0,
        atr=1.5,
        side="BUY",
        tp1_hit=True,
    )
    assert should_trail is True
    assert label == "TRAIL_BE"


def test_should_time_stop_when_bars_held_and_pnl_are_weak():
    strategy = Perfect100Strategy()
    should_exit, label = strategy.should_time_stop(bars_held=13, pnl=0.004)
    assert should_exit is True
    assert label == "TIME_STOP"
