#!/usr/bin/env python3
"""
Pro Trader Phase 3 - Paper Trading Simulator
실제 거래 없이 수익 검증
"""

import random
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum

from perfect_strategy import Perfect100Strategy, State
from fee_calculator import FeeCalculator


class SimulationMode(Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass
class SimPosition:
    symbol: str
    qty: int
    entry_price: float
    entry_time: datetime
    side: str


@dataclass
class SimOrder:
    order_id: str
    symbol: str
    side: str
    qty: int
    price: float
    status: str
    filled_price: Optional[float] = None
    filled_time: Optional[datetime] = None


class PaperTradingSimulator:
    def __init__(
        self,
        logger,
        initial_capital: float = 1000000.0,
        slippage_factor: float = 1.0,
        mode: SimulationMode = SimulationMode.PAPER,
    ):
        self.logger = logger
        self.mode = mode
        self.initial_capital = initial_capital
        self.slippage_factor = slippage_factor

        self.capital = initial_capital
        self.positions: Dict[str, SimPosition] = {}
        self.orders: List[SimOrder] = []
        self.trade_history: List[Dict] = []

        self.fee_calc = FeeCalculator(min_commission_check=False)

        self.order_id_counter = 1000

    def reset(self) -> None:
        self.capital = self.initial_capital
        self.positions = {}
        self.orders = []
        self.trade_history = []

    def get_orderbook(self, symbol: str, base_price: float) -> Dict:
        spread = base_price * 0.0005
        return {
            "bid": base_price - spread,
            "ask": base_price + spread,
            "bid_size": random.randint(1000, 10000),
            "ask_size": random.randint(1000, 10000),
        }

    def simulate_market_tick(self, symbol: str, base_price: float) -> float:
        if self.mode == SimulationMode.PAPER:
            change = base_price * random.uniform(-0.001, 0.001) * self.slippage_factor
            return base_price + change
        return base_price

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        order_type: str = "limit",
    ) -> SimOrder:
        order_id = f"sim_{self.order_id_counter}"
        self.order_id_counter += 1

        slippage = price * 0.0005 * self.slippage_factor
        if side == "BUY":
            exec_price = price + slippage
        else:
            exec_price = price - slippage

        order = SimOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            status="PENDING",
            filled_price=exec_price,
            filled_time=datetime.now(),
        )

        self.orders.append(order)

        if order_type == "market" or random.random() < 0.8:
            self._fill_order(order)

        return order

    def _fill_order(self, order: SimOrder) -> None:
        order.status = "FILLED"

        if order.side == "BUY":
            cost = order.filled_price * order.qty
            fees = self.fee_calc.calculate_entry_cost(order.filled_price, order.qty)

            if cost > self.capital:
                order.status = "REJECTED"
                return

            self.capital -= cost + fees.total_cost

            self.positions[order.symbol] = SimPosition(
                symbol=order.symbol,
                qty=order.qty,
                entry_price=order.filled_price,
                entry_time=datetime.now(),
                side="LONG",
            )

        else:
            if order.symbol not in self.positions:
                order.status = "REJECTED"
                return

            pos = self.positions[order.symbol]
            revenue = order.filled_price * order.qty
            fees = self.fee_calc.calculate_exit_cost(order.filled_price, order.qty)

            pnl = (order.filled_price - pos.entry_price) * pos.qty - fees.total_cost

            self.capital += revenue - fees.total_cost

            self.trade_history.append(
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "qty": order.qty,
                    "entry_price": pos.entry_price,
                    "exit_price": order.filled_price,
                    "pnl": pnl,
                    "pnl_pct": pnl / (pos.entry_price * pos.qty) * 100,
                    "fees": fees.total_cost,
                    "entry_time": pos.entry_time,
                    "exit_time": datetime.now(),
                }
            )

            del self.positions[order.symbol]

        self.logger.info(
            f"Order filled: {order.side} {order.symbol} {order.qty} @ {order.filled_price:,.0f}"
        )

    def check_tp_sl(
        self,
        symbol: str,
        current_price: float,
        tp_pct: float = 0.03,
        sl_pct: float = -0.015,
    ) -> Optional[str]:
        if symbol not in self.positions:
            return None

        pos = self.positions[symbol]
        pnl_pct = (current_price - pos.entry_price) / pos.entry_price

        if pnl_pct >= tp_pct:
            return "TP"
        if pnl_pct <= sl_pct:
            return "SL"

        return None

    def run_backtest(
        self,
        strategy: Perfect100Strategy,
        market_data: List[Dict],
        initial_capital: Optional[float] = None,
    ) -> Dict[str, Any]:
        if initial_capital:
            self.capital = initial_capital

        equity_curve = [self.capital]

        current_symbol = None
        or_high = None
        or_low = None

        for i, data in enumerate(market_data):
            current_price = data.get("current_price")
            symbol = data.get("symbol", "TEST")

            if current_symbol != symbol:
                current_symbol = symbol
                or_high = current_price
                or_low = current_price

            if i < 10:
                or_high = max(or_high or current_price, current_price)
                or_low = min(or_low or current_price, current_price)
            else:
                strategy.or_state[symbol] = type(
                    "ORState", (), {"or_high": or_high, "or_low": or_low}
                )()
                strategy.set_state(State.WAIT_SIGNAL)

            book_data = self.get_orderbook(symbol, current_price)

            from models import Bar1m, OrderBookTop

            bar = data.get("bar") or Bar1m(
                start=datetime.now(),
                open=current_price,
                high=current_price * 1.005,
                low=current_price * 0.995,
                close=current_price,
                volume=1000000,
            )

            book = OrderBookTop(
                symbol=symbol,
                bid=book_data["bid"],
                ask=book_data["ask"],
                bid_size=book_data["bid_size"],
                ask_size=book_data["ask_size"],
                timestamp=datetime.now(),
            )

            signal = strategy.evaluate_entry(
                bar=bar,
                last_price=current_price,
                vwap=current_price * 0.9995,
                book=book,
                lever_symbol=data.get("symbol", "TEST"),
                inverse_symbol="INVERSE",
                indicators=data.get("indicators", {}),
                market_regime=data.get("market_regime", "NEUTRAL"),
            )

            if signal.side and signal.score >= strategy.min_score:
                if data.get("symbol") not in self.positions:
                    self.place_order(
                        symbol=data.get("symbol", "TEST"),
                        side=signal.side,
                        qty=10,
                        price=current_price,
                    )

            trigger = self.check_tp_sl(data.get("symbol", "TEST"), current_price)
            if trigger and data.get("symbol") in self.positions:
                self.place_order(
                    symbol=data.get("symbol", "TEST"),
                    side="SELL",
                    qty=self.positions[data.get("symbol")].qty,
                    price=current_price,
                )

            unrealized = 0
            for sym, pos in self.positions.items():
                unrealized += (current_price - pos.entry_price) * pos.qty

            equity_curve.append(self.capital + unrealized)

        return self._calculate_metrics(equity_curve, self.capital)

    def _calculate_metrics(
        self, equity_curve: List[float], final_capital: float
    ) -> Dict[str, Any]:
        if not self.trade_history:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "max_drawdown": 0,
                "sharpe_ratio": 0,
                "final_capital": final_capital,
                "profit_pct": (final_capital - self.initial_capital)
                / self.initial_capital
                * 100,
            }

        wins = [t for t in self.trade_history if t["pnl"] > 0]
        losses = [t for t in self.trade_history if t["pnl"] <= 0]

        total_pnl = sum(t["pnl"] for t in self.trade_history)

        peak = equity_curve[0]
        max_dd = 0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

        returns = []
        for i in range(1, len(equity_curve)):
            ret = (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            returns.append(ret)

        avg_ret = sum(returns) / len(returns) if returns else 0
        std_ret = (
            (sum((r - avg_ret) ** 2 for r in returns) / len(returns)) ** 0.5
            if returns
            else 1
        )
        sharpe = (avg_ret / std_ret * (252**0.5)) if std_ret > 0 else 0

        return {
            "total_trades": len(self.trade_history),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": len(wins) / len(self.trade_history) * 100,
            "total_pnl": total_pnl,
            "avg_win": sum(t["pnl"] for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t["pnl"] for t in losses) / len(losses) if losses else 0,
            "max_drawdown": max_dd * 100,
            "sharpe_ratio": sharpe,
            "final_capital": self.capital,
            "profit_pct": (self.capital - self.initial_capital)
            / self.initial_capital
            * 100,
        }

    def print_results(self) -> None:
        metrics = self._calculate_metrics([self.capital], self.capital)

        print(f"""
╔══════════════════════════════════════════════════════════════╗
║              PAPER TRADING RESULTS                          ║
╠══════════════════════════════════════════════════════════════╣
║ Initial Capital: ₩{self.initial_capital:>15,.0f}                       ║
║ Final Capital:   ₩{self.capital:>15,.0f}                       ║
║ Profit:         ₩{metrics["total_pnl"]:>15,.0f}                       ║
║ Profit %:       {metrics["profit_pct"]:>14.1f}%                       ║
╠══════════════════════════════════════════════════════════════╣
║ Total Trades:   {metrics["total_trades"]:>15}                             ║
║ Winning:        {metrics["winning_trades"]:>15}                             ║
║ Losing:         {metrics["losing_trades"]:>15}                             ║
║ Win Rate:       {metrics["win_rate"]:>14.1f}%                       ║
╠══════════════════════════════════════════════════════════════╣
║ Max Drawdown:   {metrics["max_drawdown"]:>14.1f}%                       ║
║ Sharpe Ratio:   {metrics["sharpe_ratio"]:>14.2f}                          ║
╚══════════════════════════════════════════════════════════════╝
""")
