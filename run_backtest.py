#!/usr/bin/env python3
"""
Pro Trader - Backtest Runner
100점 트레이더 백테스트 실행기
"""

import asyncio
import logging
from datetime import datetime, timedelta
import random
import sys

sys.path.insert(0, "/home/zenith/Desktop/kis_orb_vwap_bot")

from integrated_trader import IntegratedTrader, TraderConfig
from models import Bar1m, OrderBookTop


def generate_realistic_market_data(
    symbol: str = "005930",
    days: int = 30,
    start_price: int = 80000,
    trend: str = "bull",
) -> list:
    """현실적인 시장 데이터 생성 - 트렌드 기반"""
    data = []
    current_price = start_price
    base_time = datetime.now() - timedelta(days=days)

    trend_factor = {"bull": 0.003, "neutral": 0.0, "bear": -0.003}.get(trend, 0.0)

    for day in range(days):
        for hour in range(9, 16):
            if hour == 12:
                continue

            for minute in range(0, 60, 10):
                is_or_period = hour == 9 and minute < 10

                if is_or_period:
                    change = current_price * random.uniform(-0.003, 0.003)
                else:
                    if random.random() < 0.6 + trend_factor * 100:
                        change = current_price * random.uniform(0.003, 0.025)
                    else:
                        change = current_price * random.uniform(-0.015, 0.003)

                current_price = max(1000, current_price + change)

                open_price = current_price * random.uniform(0.997, 1.003)
                high_price = max(open_price, current_price) * random.uniform(1.0, 1.02)
                low_price = min(open_price, current_price) * random.uniform(0.98, 1.0)
                close_price = current_price

                prev_close = start_price * random.uniform(0.97, 1.03)

                bar = Bar1m(
                    start=base_time + timedelta(days=day, hours=hour, minutes=minute),
                    open=open_price,
                    high=high_price,
                    low=low_price,
                    close=close_price,
                    volume=random.randint(100000, 1000000),
                )

                book = OrderBookTop(
                    symbol=symbol,
                    bid=current_price - 50,
                    ask=current_price + 50,
                    bid_size=random.randint(5000, 15000),
                    ask_size=random.randint(5000, 15000),
                    timestamp=bar.start,
                )

                rsi = (
                    random.uniform(30, 75)
                    if trend == "bull"
                    else random.uniform(25, 70)
                )
                atr = current_price * 0.02
                volume_ratio = random.uniform(0.8, 1.8)
                ma20 = current_price * random.uniform(0.97, 1.03)

                indicators = {
                    "rsi": rsi,
                    "macd": random.uniform(-100, 100),
                    "atr": atr,
                    "volume_ratio": volume_ratio,
                    "ma20": ma20,
                    "ma5": current_price * random.uniform(0.98, 1.02),
                    "vol_ma20": 500000,
                    "volume_power": volume_ratio * 100,
                    "prev_close": prev_close,
                    "news_score": random.randint(-10, 50),
                }

                regime = "BULL" if current_price > start_price * 1.02 else "NEUTRAL"

                data.append(
                    {
                        "symbol": symbol,
                        "bar": bar,
                        "book": book,
                        "current_price": current_price,
                        "vwap": current_price * random.uniform(0.995, 1.005),
                        "indicators": indicators,
                        "market_regime": regime,
                    }
                )

    return data


def generate_mock_market_data(
    symbol: str = "005930",
    days: int = 30,
    start_price: int = 80000,
) -> list:
    return generate_realistic_market_data(symbol, days, start_price, trend="bull")


async def run_backtest():
    """백테스트 실행"""
    print("=" * 70)
    print("Pro Trader - Backtest Runner")
    print("100점 트레이더 백테스트")
    print("=" * 70)

    # Setup logging
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger("backtest")

    # Create trader
    config = TraderConfig(
        initial_capital=10_000_000,
        mode=TraderMode.PAPER,
        min_score=40,
        min_r_ratio=1.2,
    )

    trader = IntegratedTrader(config=config, logger=logger)

    print(f"\nInitial Capital: {config.initial_capital:,.0f} KRW")
    print(f"Min Score: {config.min_score}")
    print(f"Min R-Ratio: {config.min_r_ratio}")

    # Start trader
    await trader.start()

    # Generate mock data
    print("\nGenerating mock market data...")
    market_data = generate_mock_market_data(
        symbol="005930",
        days=10,
        start_price=80000,
    )
    print(f"Generated {len(market_data)} data points")

    # Run backtest
    print("\nRunning backtest...")
    strategy = trader.strategy

    # ORB 초기화: 처음 10개 bars로 OR 상태 구축
    print("\nInitializing ORB state...")
    or_bars_count = 10
    for i, data in enumerate(market_data[:or_bars_count]):
        bar = data["bar"]
        strategy.update_or(data["symbol"], bar)

    # OR 구축 후 신호 대기 상태로 전환
    from perfect_strategy import State

    strategy.set_state(State.WAIT_SIGNAL)
    print(f"ORB initialized with {or_bars_count} bars, state: {strategy.state}")

    trade_count = 0
    wins = 0
    losses = 0

    # 실제 백테스트는 OR 구축 후부터
    for i, data in enumerate(market_data[or_bars_count:]):
        bar = data["bar"]
        book = data["book"]

        signal = strategy.evaluate_entry(
            bar=bar,
            last_price=data["current_price"],
            vwap=data["vwap"],
            book=book,
            lever_symbol=data["symbol"],
            inverse_symbol="INVERSE",
            indicators=data["indicators"],
            market_regime=data["market_regime"],
        )

        # Set market regime
        trader.set_market_regime(data["market_regime"])

        # Check if we can enter
        can_trade, reason = trader.can_trade(data["symbol"])

        if (
            signal.side
            and signal.score >= config.min_score
            and signal.risk_reward_ratio >= config.min_r_ratio
            and can_trade
            and data["symbol"]
            not in [t.symbol for t in trader.session_manager.open_trades]
        ):
            # Calculate stop loss
            stop_loss_pct = 0.03
            atr = data["indicators"].get("atr", data["current_price"] * 0.02)

            # Execute entry
            result = await trader.execute_entry(
                symbol=data["symbol"],
                side=signal.side,
                price=data["current_price"],
                stop_loss_pct=stop_loss_pct,
                signal_strength=signal.score / 100,
                atr=atr,
            )

            if result:
                trade_count += 1
                print(
                    f"  Entry #{trade_count}: {signal.side} {data['symbol']} @ {data['current_price']:,.0f} (score={signal.score}, r={signal.risk_reward_ratio:.1f})"
                )

        # Check exits (TP/SL)
        for trade in list(trader.session_manager.open_trades):
            current_price = data["current_price"]
            entry_price = trade.entry_price

            pnl_pct = (current_price - entry_price) / entry_price

            # TP at 4.5%
            if pnl_pct >= 0.045:
                exit_result = await trader.execute_exit(
                    symbol=trade.symbol,
                    price=current_price,
                    reason="TP",
                )
                if exit_result:
                    pnl = exit_result["pnl"]
                    if pnl > 0:
                        wins += 1
                    else:
                        losses += 1
                    print(
                        f"  Exit (TP): {trade.symbol} @ {current_price:,.0f} PnL={pnl:,.0f}"
                    )

            # SL at -2%
            elif pnl_pct <= -0.02:
                exit_result = await trader.execute_exit(
                    symbol=trade.symbol,
                    price=current_price,
                    reason="SL",
                )
                if exit_result:
                    pnl = exit_result["pnl"]
                    if pnl > 0:
                        wins += 1
                    else:
                        losses += 1
                    print(
                        f"  Exit (SL): {trade.symbol} @ {current_price:,.0f} PnL={pnl:,.0f}"
                    )

        # Progress
        if (i + 1) % 50 == 0:
            print(f"  Progress: {i + 1}/{len(market_data)}")

    # Close remaining positions
    print("\nClosing remaining positions...")
    for trade in list(trader.session_manager.open_trades):
        last_price = market_data[-1]["current_price"]
        exit_result = await trader.execute_exit(
            symbol=trade.symbol,
            price=last_price,
            reason="END",
        )
        if exit_result:
            pnl = exit_result["pnl"]
            if pnl > 0:
                wins += 1
            else:
                losses += 1

    # Results
    print("\n" + "=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)

    status = trader.get_status()
    risk = status["risk"]
    session = status["session"]

    print("\n[Capital]")
    print(f"  Initial: {config.initial_capital:,.0f} KRW")
    print(f"  Final: {risk['current_capital']:,.0f} KRW")
    total_pnl = risk["current_capital"] - config.initial_capital
    total_pnl_pct = total_pnl / config.initial_capital * 100
    print(f"  Total PnL: {total_pnl:,.0f} KRW ({total_pnl_pct:+.2f}%)")

    print("\n[Trades]")
    print(f"  Total: {risk['total_trades']}")

    print("\n[Risk]")
    print(f"  Max Drawdown: {risk['max_drawdown']:.2f}%")
    print(f"  Kelly: {risk['kelly']:.2%}")
    print(f"  Profit Factor: {risk['profit_factor']:.2f}")

    print("\n[Daily]")
    print(f"  Date: {session['daily']['date']}")
    print(f"  Trades: {session['daily']['total_trades']}")
    print(f"  PnL: {session['daily']['total_pnl']:,.0f} KRW")

    print("\n" + trader.get_risk_report())

    await trader.stop()

    print("\nBacktest Complete!")

    return risk["total_pnl_pct"] > 0


if __name__ == "__main__":
    from integrated_trader import TraderMode

    success = asyncio.run(run_backtest())
    sys.exit(0 if success else 1)
