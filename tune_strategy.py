#!/usr/bin/env python3
"""
Perfect100Strategy Parameter Tuning Framework
전략 파라미터를 최적화하기 위한 튜닝 도구입니다.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import random
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from perfect_strategy import Perfect100Strategy, State
from fee_calculator import FeeCalculator
from models import Bar1m, OrderBookTop


@dataclass
class TradeResult:
    entry_price: float
    exit_price: float
    qty: int
    pnl_gross: float
    pnl_net: float
    fees: float
    duration_bars: int
    tp_triggered: bool = False
    sl_triggered: bool = False


@dataclass
class BacktestResult:
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl_gross: float
    total_pnl_net: float
    max_drawdown: float
    avg_win: float
    avg_loss: float
    win_rate: float
    profit_factor: float
    sharpe_ratio: float


class ParameterOptimizer:
    def __init__(
        self,
        fee_calc: FeeCalculator,
        initial_params: Optional[Dict] = None,
    ):
        self.fee_calc = fee_calc
        self.default_params = initial_params or {
            "min_score": 60,
            "max_spread_pct": 0.003,
            "min_bid_ask_ratio": 0.8,
            "min_win_rate": 0.55,
            "min_r_ratio": 1.5,
        }
        self.best_params = None
        self.best_score = float('-inf')
        self.history: List[Dict] = []
    
    def generate_params_grid(self, param_ranges: Dict) -> List[Dict]:
        """Generate parameter combinations from ranges"""
        keys = list(param_ranges.keys())
        values = list(param_ranges.values())
        
        combinations = [dict(zip(keys, v)) for v in self._cartesian_product(values)]
        return combinations
    
    def _cartesian_product(self, lists: List[List]) -> List[Tuple]:
        """Generate cartesian product of lists"""
        if not lists:
            return [()]
        return [(x,) + y for x in lists[0] for y in self._cartesian_product(lists[1:])]
    
    def evaluate_params(
        self,
        params: Dict,
        market_data: List[Dict],
    ) -> BacktestResult:
        """Evaluate parameter set with market data"""
        strategy = Perfect100Strategy(
            logger=None,
            min_score=params.get("min_score", 60),
            max_spread_pct=params.get("max_spread_pct", 0.003),
            min_bid_ask_ratio=params.get("min_bid_ask_ratio", 0.8),
            min_win_rate=params.get("min_win_rate", 0.55),
            min_r_ratio=params.get("min_r_ratio", 1.5),
        )
        
        trades: List[TradeResult] = []
        equity_curve = [1000000]  # Start with 1M
        
        position = None
        
        for i, data in enumerate(market_data):
            bar = data.get("bar")
            book = data.get("book")
            indicators = data.get("indicators", {})
            vwap = data.get("vwap")
            
            if bar is None:
                continue
            
            # Check for entry signal
            if position is None and strategy.state == State.WAIT_SIGNAL:
                signal = strategy.evaluate_entry(
                    bar=bar,
                    last_price=bar.close,
                    vwap=vwap,
                    book=book,
                    lever_symbol="122630",
                    inverse_symbol="114800",
                    indicators=indicators,
                    market_regime=data.get("market_regime", "NEUTRAL"),
                )
                
                if signal.side == "BUY":
                    entry_price = bar.close
                    position = {
                        "entry_price": entry_price,
                        "entry_bar_idx": i,
                        "atr": indicators.get("atr", entry_price * 0.01),
                    }
                    strategy.set_state(State.IN_POSITION)
            
            # Check for exit
            elif position is not None:
                should_tp, _ = strategy.should_take_profit(
                    bar.close, position["entry_price"], position["atr"]
                )
                should_sl, _ = strategy.should_stop_loss(
                    bar.close, position["entry_price"], position["atr"]
                )
                
                if should_tp or should_sl or i - position["entry_bar_idx"] > 60:
                    exit_price = bar.close
                    
                    gross_pnl = (exit_price - position["entry_price"]) * 10
                    entry_cost = self.fee_calc.calculate_entry_cost(position["entry_price"], 10)
                    exit_cost = self.fee_calc.calculate_exit_cost(exit_price, 10)
                    net_pnl = gross_pnl - entry_cost.total_cost - exit_cost.total_cost
                    
                    trades.append(TradeResult(
                        entry_price=position["entry_price"],
                        exit_price=exit_price,
                        qty=10,
                        pnl_gross=gross_pnl,
                        pnl_net=net_pnl,
                        fees=entry_cost.total_cost + exit_cost.total_cost,
                        duration_bars=i - position["entry_bar_idx"],
                        tp_triggered=should_tp,
                        sl_triggered=should_sl,
                    ))
                    
                    equity_curve.append(equity_curve[-1] + net_pnl)
                    position = None
                    strategy.set_state(State.WAIT_SIGNAL)
        
        return self._calculate_metrics(trades, equity_curve)
    
    def _calculate_metrics(
        self,
        trades: List[TradeResult],
        equity_curve: List[float],
    ) -> BacktestResult:
        if not trades:
            return BacktestResult(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        
        winning = [t for t in trades if t.pnl_net > 0]
        losing = [t for t in trades if t.pnl_net <= 0]
        
        total_pnl_gross = sum(t.pnl_gross for t in trades)
        total_pnl_net = sum(t.pnl_net for t in trades)
        
        max_dd = 0
        peak = equity_curve[0]
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
        
        avg_win = sum(t.pnl_net for t in winning) / len(winning) if winning else 0
        avg_loss = sum(t.pnl_net for t in losing) / len(losing) if losing else 0
        
        win_rate = len(winning) / len(trades)
        
        total_wins = sum(t.pnl_net for t in winning) if winning else 0
        total_losses = abs(sum(t.pnl_net for t in losing)) if losing else 1
        profit_factor = total_wins / total_losses if total_losses > 0 else 0
        
        returns = []
        for i in range(1, len(equity_curve)):
            ret = (equity_curve[i] - equity_curve[i-1]) / equity_curve[i-1]
            returns.append(ret)
        
        if returns and len(returns) > 1:
            avg_ret = sum(returns) / len(returns)
            std_ret = (sum((r - avg_ret) ** 2 for r in returns) / len(returns)) ** 0.5
            sharpe = (avg_ret / std_ret * (252 ** 0.5)) if std_ret > 0 else 0
        else:
            sharpe = 0
        
        return BacktestResult(
            total_trades=len(trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            total_pnl_gross=total_pnl_gross,
            total_pnl_net=total_pnl_net,
            max_drawdown=max_dd,
            avg_win=avg_win,
            avg_loss=avg_loss,
            win_rate=win_rate,
            profit_factor=profit_factor,
            sharpe_ratio=sharpe,
        )
    
    def grid_search(
        self,
        param_ranges: Dict,
        market_data: List[Dict],
        metric: str = "profit_factor",
    ) -> Dict:
        """Perform grid search over parameter ranges"""
        print(f"\n{'='*60}")
        print(f"GRID SEARCH: {len(param_ranges)} params")
        print(f"{'='*60}")
        
        combinations = self.generate_params_grid(param_ranges)
        print(f"Testing {len(combinations)} combinations...")
        
        best_score = float('-inf')
        best_params = None
        best_result = None
        
        for i, params in enumerate(combinations):
            params = {**self.default_params, **params}
            result = self.evaluate_params(params, market_data)
            
            score = getattr(result, metric, 0)
            
            if score > best_score:
                best_score = score
                best_params = params
                best_result = result
                print(f"  [{i+1}/{len(combinations)}] NEW BEST: {metric}={score:.2f}")
            
            self.history.append({
                "params": params,
                "result": result.__dict__,
                "score": score,
            })
        
        self.best_params = best_params
        self.best_score = best_score
        
        print("\nBest Parameters:")
        for k, v in best_params.items():
            print(f"  {k}: {v}")
        
        print(f"\nBest Result ({metric}={best_score:.2f}):")
        print(f"  Trades: {best_result.total_trades}")
        print(f"  Win Rate: {best_result.win_rate:.1%}")
        print(f"  Net PnL: ₩{best_result.total_pnl_net:,.0f}")
        print(f"  Profit Factor: {best_result.profit_factor:.2f}")
        print(f"  Max Drawdown: {best_result.max_drawdown:.1%}")
        
        return best_params
    
    def random_search(
        self,
        param_ranges: Dict,
        market_data: List[Dict],
        n_iterations: int = 50,
        metric: str = "profit_factor",
    ) -> Dict:
        """Perform random search over parameter ranges"""
        print(f"\n{'='*60}")
        print(f"RANDOM SEARCH: {n_iterations} iterations")
        print(f"{'='*60}")
        
        best_score = float('-inf')
        best_params = None
        best_result = None
        
        for i in range(n_iterations):
            params = {**self.default_params}
            for key, values in param_ranges.items():
                params[key] = random.choice(values)
            
            result = self.evaluate_params(params, market_data)
            score = getattr(result, metric, 0)
            
            if score > best_score:
                best_score = score
                best_params = params
                best_result = result
                print(f"  [{i+1}/{n_iterations}] NEW BEST: {metric}={score:.2f}")
            
            self.history.append({
                "params": params,
                "result": result.__dict__,
                "score": score,
            })
        
        self.best_params = best_params
        self.best_score = best_score
        
        print("\nBest Parameters:")
        for k, v in best_params.items():
            print(f"  {k}: {v}")
        
        print(f"\nBest Result ({metric}={best_score:.2f}):")
        print(f"  Trades: {best_result.total_trades}")
        print(f"  Win Rate: {best_result.win_rate:.1%}")
        print(f"  Net PnL: ₩{best_result.total_pnl_net:,.0f}")
        print(f"  Profit Factor: {best_result.profit_factor:.2f}")
        
        return best_params
    
    def save_results(self, filepath: str):
        """Save tuning results to file"""
        with open(filepath, 'w') as f:
            json.dump({
                "best_params": self.best_params,
                "best_score": self.best_score,
                "history": self.history,
            }, f, indent=2)
        print(f"Results saved to {filepath}")


def generate_synthetic_market_data(n_bars: int = 500) -> List[Dict]:
    """Generate synthetic market data for testing"""
    data = []
    base_price = 50000
    trend = 1
    
    for i in range(n_bars):
        trend += random.uniform(-0.1, 0.1)
        trend = max(-2, min(2, trend))
        
        base_price *= (1 + trend * 0.001 + random.uniform(-0.002, 0.002))
        
        spread = base_price * 0.0005
        
        bar = Bar1m(
            start=datetime.now() - timedelta(minutes=n_bars-i),
            open=base_price * random.uniform(0.998, 1.002),
            high=base_price * random.uniform(1.000, 1.005),
            low=base_price * random.uniform(0.995, 1.000),
            close=base_price,
            volume=random.randint(100000, 1000000),
        )
        
        book = OrderBookTop(
            symbol="005930",
            bid=base_price - spread/2,
            ask=base_price + spread/2,
            bid_size=random.randint(1000, 10000),
            ask_size=random.randint(1000, 10000),
            timestamp=bar.start,
        )
        
        indicators = {
            "prev_close": base_price * random.uniform(0.97, 1.03),
            "ma20": base_price * random.uniform(0.98, 1.02),
            "volume_power": random.uniform(80, 180),
            "atr": base_price * 0.01,
            "news_score": random.randint(-30, 60),
        }
        
        data.append({
            "bar": bar,
            "book": book,
            "indicators": indicators,
            "vwap": base_price * random.uniform(0.995, 1.005),
            "market_regime": "BULL" if trend > 0 else "BEAR",
        })
    
    return data


def main():
    print("\n" + "="*60)
    print("PERFECT100STRATEGY PARAMETER TUNING")
    print("="*60)
    
    fee_calc = FeeCalculator()
    optimizer = ParameterOptimizer(fee_calc)
    
    print("\nGenerating synthetic market data...")
    market_data = generate_synthetic_market_data(n_bars=500)
    print(f"Generated {len(market_data)} bars of data")
    
    param_ranges = {
        "min_score": [50, 55, 60, 65, 70],
        "min_r_ratio": [1.0, 1.5, 2.0, 2.5],
        "max_spread_pct": [0.002, 0.003, 0.004, 0.005],
        "min_bid_ask_ratio": [0.6, 0.8, 1.0],
    }
    
    print("\nRunning grid search...")
    best_params = optimizer.grid_search(
        param_ranges,
        market_data,
        metric="profit_factor",
    )
    
    optimizer.save_results("tuning_results.json")
    
    print("\n" + "="*60)
    print("TUNING COMPLETE")
    print("="*60)
    print("\nRecommended Parameters:")
    print(json.dumps(best_params, indent=2))
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
