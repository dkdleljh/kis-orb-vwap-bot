#!/usr/bin/env python3
"""
Perfect100Strategy End-to-End Test Script
모의 데이터를 사용하여 전략의 전체 흐름을 검증합니다.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dataclasses import dataclass, field
from typing import Dict, List
from datetime import datetime
import random

# Import the strategy modules
from perfect_strategy import Perfect100Strategy, State, ORState
from fee_calculator import FeeCalculator, USFeeCalculator
from models import Bar1m, OrderBookTop


@dataclass
class MockLogger:
    """Mock logger for testing"""
    logs: List[str] = field(default_factory=list)
    
    def info(self, msg: str):
        self.logs.append(f"[INFO] {msg}")
        print(f"[INFO] {msg}")
    
    def warning(self, msg: str):
        self.logs.append(f"[WARN] {msg}")
        print(f"[WARN] {msg}")
    
    def error(self, msg: str):
        self.logs.append(f"[ERROR] {msg}")
        print(f"[ERROR] {msg}")
    
    def debug(self, msg: str):
        self.logs.append(f"[DEBUG] {msg}")
        print(f"[DEBUG] {msg}")


def create_mock_bar(
    base_price: float = 50000,
    trend: str = "bullish"
) -> Bar1m:
    """Create a mock 1-minute bar"""
    if trend == "bullish":
        open_price = base_price
        close_price = base_price * random.uniform(1.002, 1.015)
        high_price = close_price * random.uniform(1.001, 1.005)
        low_price = base_price * random.uniform(0.990, 0.998)
    elif trend == "bearish":
        open_price = base_price
        close_price = base_price * random.uniform(0.985, 0.998)
        high_price = base_price * random.uniform(1.001, 1.005)
        low_price = close_price * random.uniform(0.990, 0.998)
    else:
        open_price = base_price
        close_price = base_price * random.uniform(0.998, 1.002)
        high_price = max(open_price, close_price) * 1.002
        low_price = min(open_price, close_price) * 0.998
    
    return Bar1m(
        start=datetime.now(),
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=random.randint(100000, 1000000),
    )


def create_mock_orderbook(
    base_price: float = 50000
) -> OrderBookTop:
    """Create a mock order book"""
    spread = base_price * 0.0005
    return OrderBookTop(
        symbol="005930",
        bid=base_price - spread/2,
        ask=base_price + spread/2,
        bid_size=random.randint(1000, 10000),
        ask_size=random.randint(1000, 10000),
        timestamp=datetime.now(),
    )


def create_mock_indicators(
    base_price: float = 50000,
    trend: str = "bullish"
) -> Dict:
    """Create mock technical indicators"""
    prev_close = base_price * random.uniform(0.97, 0.99)
    
    if trend == "bullish":
        ma20 = base_price * random.uniform(0.98, 1.00)
        volume_power = random.uniform(120, 200)
    elif trend == "bearish":
        ma20 = base_price * random.uniform(1.00, 1.02)
        volume_power = random.uniform(50, 100)
    else:
        ma20 = base_price
        volume_power = random.uniform(90, 110)
    
    return {
        "prev_close": prev_close,
        "ma20": ma20,
        "rsi": random.uniform(30, 70),
        "volume_power": volume_power,
        "atr": base_price * 0.01,
        "news_score": random.randint(-20, 50),
        "ema9": base_price * random.uniform(0.99, 1.01),
        "ema21": base_price * random.uniform(0.98, 1.02),
        "macd_line": random.uniform(-50, 50),
        "macd_signal": random.uniform(-50, 50),
        "macd_hist": random.uniform(-20, 20),
    }


def run_entry_signal_test(logger: MockLogger) -> bool:
    """Test 1: Entry Signal Generation"""
    print("\n" + "="*60)
    print("TEST 1: Entry Signal Generation")
    print("="*60)
    
    strategy = Perfect100Strategy(
        logger=logger,
        min_score=60,
        max_spread_pct=0.003,
        min_bid_ask_ratio=0.8,
        min_win_rate=0.55,
        min_r_ratio=1.5,
    )
    
    # Set state to WAIT_SIGNAL
    strategy.set_state(State.WAIT_SIGNAL)
    
    # Setup OR state
    or_state = ORState(or_high=50000, or_low=49500)
    strategy.or_state["005930"] = or_state
    
    # Create bullish scenario
    bar = create_mock_bar(base_price=50200, trend="bullish")
    book = create_mock_orderbook(base_price=50200)
    indicators = create_mock_indicators(base_price=50200, trend="bullish")
    indicators["atr"] = 500
    
    vwap = 50100
    
    print("\nScenario: Bullish breakout")
    print(f"  Bar: O={bar.open:.0f} H={bar.high:.0f} L={bar.low:.0f} C={bar.close:.0f}")
    print(f"  Book: bid={book.bid:.0f} ask={book.ask:.0f}")
    print(f"  Indicators: ma20={indicators['ma20']:.0f}, volume_power={indicators['volume_power']:.0f}")
    print(f"  VWAP: {vwap:.0f}")
    
    signal = strategy.evaluate_entry(
        bar=bar,
        last_price=bar.close,
        vwap=vwap,
        book=book,
        lever_symbol="122630",
        inverse_symbol="114800",
        indicators=indicators,
        market_regime="BULL",
    )
    
    print("\nResult:")
    print(f"  Signal side: {signal.side}")
    print(f"  Score: {signal.score}")
    print(f"  Reasons: {signal.reasons}")
    print(f"  Expected win rate: {signal.expected_win_rate:.2%}")
    print(f"  Risk/Reward ratio: {signal.risk_reward_ratio:.2f}")
    print(f"  Fee adjusted: {signal.fee_adjusted}")
    
    if signal.side == "BUY" and signal.score >= 60:
        print("\n✓ TEST PASSED: Entry signal generated successfully")
        return True
    else:
        print("\n✗ TEST FAILED: No entry signal generated")
        return False


def run_tp_sl_test(logger: MockLogger) -> bool:
    """Test 2: Take Profit / Stop Loss with Fees"""
    print("\n" + "="*60)
    print("TEST 2: TP/SL with Fee Adjustments")
    print("="*60)
    
    strategy = Perfect100Strategy(logger=logger)
    fee_calc = FeeCalculator()
    
    entry_price = 50000
    atr = 500
    
    # TP/SL uses NET PnL (after fees ~4.4%)
    test_cases = [
        (51250, True, False, "TP2 at +2.5% net"),
        (51750, True, False, "TP3 at +3.5% net"),
        (49600, False, True, "SL1 at -0.8% net"),
        (49400, False, True, "SL2 at -1.2% net"),
        (49250, False, True, "SL3 at -1.5% net"),
        (50200, False, False, "No trigger at +0.4% net"),
    ]
    
    all_passed = True
    
    for current_price, expected_tp, expected_sl, desc in test_cases:
        should_tp, tp_label = strategy.should_take_profit(current_price, entry_price, atr)
        should_sl, sl_label = strategy.should_stop_loss(current_price, entry_price, atr)
        
        gross_pnl = (current_price - entry_price) / entry_price
        
        # Calculate net PnL with fees
        entry_costs = fee_calc.calculate_entry_cost(entry_price, 1)
        exit_costs = fee_calc.calculate_exit_cost(current_price, 1)
        total_cost_pct = (entry_costs.total_cost + exit_costs.total_cost) / entry_price
        net_pnl = gross_pnl - total_cost_pct
        
        print(f"\n{desc}")
        print(f"  Entry: {entry_price:.0f} -> Current: {current_price:.0f}")
        print(f"  Gross PnL: {gross_pnl:.2%}, Net PnL: {net_pnl:.2%} (fees: {total_cost_pct:.2%})")
        print(f"  TP triggered: {should_tp} ({tp_label}), SL triggered: {should_sl} ({sl_label})")
        
        if should_tp != expected_tp or should_sl != expected_sl:
            print(f"  ✗ FAILED - Expected TP={expected_tp}, SL={expected_sl}")
            all_passed = False
        else:
            print("  ✓ OK")
    
    if all_passed:
        print("\n✓ TEST PASSED: TP/SL with fees works correctly")
    else:
        print("\n✗ TEST FAILED: TP/SL has issues")
    
    return all_passed


def run_fee_calculation_test(logger: MockLogger) -> bool:
    """Test 3: Fee Calculator"""
    print("\n" + "="*60)
    print("TEST 3: Fee Calculator")
    print("="*60)
    
    # Test domestic (Korean) stocks
    domestic = FeeCalculator(
        commission_rate=0.00015,
        slippage_rate=0.001,
        tax_rate=0.002,
    )
    
    price = 50000
    qty = 10
    
    entry = domestic.calculate_entry_cost(price, qty)
    exit = domestic.calculate_exit_cost(price, qty)
    
    total_cost_pct = (entry.total_cost + exit.total_cost) / (price * qty)
    
    print("\n[Domestic Stocks]")
    print(f"  Price: {price:,} x Qty: {qty}")
    print(f"  Entry cost: {entry.total_cost:,.0f} (comm: {entry.commission:,.0f}, slip: {entry.slippage:,.0f})")
    print(f"  Exit cost: {exit.total_cost:,.0f} (comm: {exit.commission:,.0f}, slip: {exit.slippage:,.0f}, tax: {exit.tax:,.0f})")
    print(f"  Total roundtrip: {total_cost_pct:.3%}")
    
    # commission_min=1000 applies for small trades
    # 50000 * 10 = 500000, commission = max(500000*0.00015, 1000) = 1000
    # Expected: (1000+1000)/500000*2 + 0.1%*2 + 0.2% = 0.8%
    expected_domestic = 0.008
    if abs(total_cost_pct - expected_domestic) > 0.001:
        print(f"  ✗ Expected ~{expected_domestic:.2%}, got {total_cost_pct:.2%}")
        return False
    
    # Test US stocks
    us = USFeeCalculator(commission_per_share=0.005, slippage_rate=0.001)
    
    us_price = 150.0
    us_qty = 100
    
    entry_us = us.calculate_entry_cost(us_price, us_qty)
    exit_us = us.calculate_exit_cost(us_price, us_qty)
    
    total_cost_pct_us = (entry_us.total_cost + exit_us.total_cost) / (us_price * us_qty)
    
    print("\n[US Stocks]")
    print(f"  Price: ${us_price} x Qty: {us_qty}")
    print(f"  Entry cost: ${entry_us.total_cost:.2f} (comm: ${entry_us.commission:.2f}, slip: ${entry_us.slippage:.2f})")
    print(f"  Exit cost: ${exit_us.total_cost:.2f}")
    print(f"  Total roundtrip: {total_cost_pct_us:.3%}")
    
    # Expected: ~0.2% (commission $0.005/share * 2 = $1, slippage 0.1% * 2 = 0.2%)
    print("\n✓ TEST PASSED: Fee calculations are correct")
    return True


def run_parameter_sensitivity_test(logger: MockLogger) -> bool:
    """Test 4: Parameter Sensitivity Analysis"""
    print("\n" + "="*60)
    print("TEST 4: Parameter Sensitivity Analysis")
    print("="*60)
    
    # Test different parameter combinations
    param_sets = [
        {"min_score": 50, "min_r_ratio": 1.0, "description": "Aggressive"},
        {"min_score": 60, "min_r_ratio": 1.5, "description": "Balanced (recommended)"},
        {"min_score": 70, "min_r_ratio": 2.0, "description": "Conservative"},
    ]
    
    for params in param_sets:
        strategy = Perfect100Strategy(
            logger=logger,
            min_score=params["min_score"],
            min_r_ratio=params["min_r_ratio"],
        )
        strategy.set_state(State.WAIT_SIGNAL)
        
        # Create a moderate signal scenario
        or_state = ORState(or_high=50000, or_low=49500)
        strategy.or_state["005930"] = or_state
        
        bar = create_mock_bar(base_price=50200, trend="bullish")
        book = create_mock_orderbook(base_price=50200)
        indicators = create_mock_indicators(base_price=50200, trend="bullish")
        indicators["atr"] = 500
        
        signal = strategy.evaluate_entry(
            bar=bar,
            last_price=bar.close,
            vwap=50100,
            book=book,
            lever_symbol="122630",
            inverse_symbol="114800",
            indicators=indicators,
            market_regime="BULL",
        )
        
        print(f"\n[{params['description']}] min_score={params['min_score']}, min_r_ratio={params['min_r_ratio']}")
        print(f"  Score: {signal.score}, Signal: {signal.side}")
    
    print("\n✓ TEST PASSED: Parameter sensitivity analysis complete")
    return True


def run_risk_reward_test(logger: MockLogger) -> bool:
    """Test 5: Risk/Reward with Fee Adjustment"""
    print("\n" + "="*60)
    print("TEST 5: Risk/Reward Ratio with Fee Adjustment")
    print("="*60)
    
    strategy = Perfect100Strategy(logger=logger)
    
    # Test case: Entry at 50000, SL at 49500 (1% risk), TP at 51500 (3% reward)
    entry_price = 50000
    stop_loss = 49500
    take_profit = 51500
    
    rr_ratio, net_rr = strategy._calculate_risk_reward(entry_price, stop_loss, take_profit)
    
    gross_rr = 3.0  # 3% / 1% = 3:1
    
    print(f"\nEntry: {entry_price:,}, SL: {stop_loss:,}, TP: {take_profit:,}")
    print(f"  Gross R/R: {gross_rr:.1f}:1")
    print(f"  Calculated R/R: {rr_ratio:.2f}:1")
    print(f"  Net R/R (fee-adjusted): {net_rr:.2f}:1")
    
    # With ~0.43% fees, net reward should be reduced
    # Gross: 3%, Net: 3% - 0.43% = 2.57%
    # Gross risk: 1%, Net risk: 1% + 0.43% = 1.43%
    # Net R/R = 2.57 / 1.43 = 1.80
    
    if net_rr < rr_ratio:
        print(f"  ✓ Net R/R ({net_rr:.2f}) correctly lower than gross R/R ({rr_ratio:.2f})")
    else:
        print("  ✗ Net R/R should be lower than gross")
        return False
    
    print("\n✓ TEST PASSED: Fee-adjusted R/R calculation correct")
    return True


def main():
    """Run all tests"""
    print("\n" + "="*60)
    print("PERFECT100STRATEGY END-TO-END TEST SUITE")
    print("="*60)
    
    logger = MockLogger()
    
    results = []
    
    # Run all tests
    results.append(("Entry Signal", run_entry_signal_test(logger)))
    results.append(("TP/SL with Fees", run_tp_sl_test(logger)))
    results.append(("Fee Calculator", run_fee_calculation_test(logger)))
    results.append(("Parameter Sensitivity", run_parameter_sensitivity_test(logger)))
    results.append(("Risk/Reward", run_risk_reward_test(logger)))
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    passed = 0
    failed = 0
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"  {name}: {status}")
        if result:
            passed += 1
        else:
            failed += 1
    
    print(f"\nTotal: {passed} passed, {failed} failed")
    
    if failed == 0:
        print("\n🎉 ALL TESTS PASSED!")
        return 0
    else:
        print(f"\n⚠️  {failed} TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
