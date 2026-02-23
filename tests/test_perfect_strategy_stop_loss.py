from perfect_strategy import Perfect100Strategy


def test_should_stop_loss_sell_side_with_numeric_minus_one_uses_rising_stop():
    strategy = Perfect100Strategy()

    should_sl, label = strategy.should_stop_loss(
        current_price=102.5,
        entry_price=100.0,
        atr=1.0,
        side=-1,  # legacy short-side marker
    )

    assert should_sl is True
    assert label == "ATR_SL"
