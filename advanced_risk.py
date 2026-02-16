#!/usr/bin/env python3
"""
Pro Trader - Enhanced Risk Manager with Dynamic Position Sizing
실제 수익을 위한 고급 리스크 관리
"""

from typing import Dict, List
from dataclasses import dataclass
from datetime import datetime


@dataclass
class TradeRecord:
    timestamp: datetime
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    pnl_pct: float
    fees: float
    holding_period: int
    exit_reason: str


class AdvancedRiskManager:
    def __init__(
        self,
        initial_capital: float = 1000000,
        max_position_pct: float = 0.20,
        max_daily_loss_pct: float = -0.03,
        max_drawdown_pct: float = -0.10,
        max_consecutive_losses: int = 3,
        base_risk_pct: float = 0.01,
    ):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.peak_capital = initial_capital
        
        self.max_position_pct = max_position_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.max_consecutive_losses = max_consecutive_losses
        self.base_risk_pct = base_risk_pct
        
        self.trade_history: List[TradeRecord] = []
        self.daily_trades: List[TradeRecord] = []
        
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.total_wins = 0
        self.total_losses = 0
        
        self.last_reset_date = datetime.now().date()
        
        self.win_rate = 0.5
        self.avg_win = 0.0
        self.avg_loss = 0.0
        self.profit_factor = 1.0
        
        self.kelly_fraction = 0.25
        self.optimal_fraction = 0.0
        
    def reset_daily(self) -> None:
        today = datetime.now().date()
        if today != self.last_reset_date:
            self.daily_trades = []
            self.last_reset_date = today
    
    def calculate_kelly_fraction(self) -> float:
        if len(self.trade_history) < 10:
            return self.kelly_fraction
        
        wins = [t for t in self.trade_history if t.pnl > 0]
        losses = [t for t in self.trade_history if t.pnl <= 0]
        
        if not wins or not losses:
            return self.kelly_fraction
        
        win_rate = len(wins) / len(self.trade_history)
        avg_win_pct = sum(t.pnl_pct for t in wins) / len(wins)
        avg_loss_pct = abs(sum(t.pnl_pct for t in losses) / len(losses))
        
        if avg_loss_pct == 0:
            return self.kelly_fraction
        
        b = avg_win_pct / avg_loss_pct
        q = 1 - win_rate
        
        kelly = (b * q - q) / b
        
        kelly = max(0, min(kelly, 0.25))
        
        self.win_rate = win_rate
        self.avg_win = avg_win_pct
        self.avg_loss = avg_loss_pct
        self.profit_factor = (win_rate * avg_win_pct) / ((1 - win_rate) * avg_loss_pct) if (1 - win_rate) > 0 else 0
        
        self.optimal_fraction = kelly
        
        return kelly
    
    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss_pct: float,
        signal_strength: float = 1.0,
        market_regime: str = "NEUTRAL",
    ) -> Dict:
        self.reset_daily()
        
        drawdown = (self.peak_capital - self.current_capital) / self.peak_capital
        drawdown_factor = 1.0 - min(drawdown / abs(self.max_drawdown_pct), 1.0)
        
        kelly = self.calculate_kelly_fraction()
        
        regime_multipliers = {
            "BULL": 1.2,
            "NEUTRAL": 1.0,
            "BEAR": 0.6,
            "VOLATILE": 0.5,
        }
        regime_multiplier = regime_multipliers.get(market_regime, 1.0)
        
        loss_streak_factor = 1.0
        if self.consecutive_losses >= self.max_consecutive_losses:
            loss_streak_factor = 0.3
        elif self.consecutive_losses >= 2:
            loss_streak_factor = 0.5
        
        signal_factor = min(signal_strength, 1.5)
        
        final_fraction = (
            kelly 
            * drawdown_factor 
            * regime_multiplier 
            * loss_streak_factor 
            * signal_factor
        )
        
        final_fraction = max(0.02, min(final_fraction, self.max_position_pct))
        
        available_capital = self.current_capital * final_fraction
        
        if stop_loss_pct > 0:
            risk_amount = available_capital * self.base_risk_pct
            qty = int(risk_amount / (entry_price * stop_loss_pct))
        else:
            qty = int(available_capital / entry_price)
        
        return {
            "fraction": final_fraction,
            "capital": available_capital,
            "qty": qty,
            "risk_amount": available_capital * self.base_risk_pct,
            "kelly": kelly,
            "drawdown_factor": drawdown_factor,
            "regime_multiplier": regime_multiplier,
            "loss_streak_factor": loss_streak_factor,
        }
    
    def record_trade(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        qty: int,
        fees: float,
        holding_period: int,
        exit_reason: str,
    ) -> None:
        pnl = (exit_price - entry_price) * qty - fees if side == "BUY" else (entry_price - exit_price) * qty - fees
        pnl_pct = pnl / (entry_price * qty) * 100
        
        trade = TradeRecord(
            timestamp=datetime.now(),
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees=fees,
            holding_period=holding_period,
            exit_reason=exit_reason,
        )
        
        self.trade_history.append(trade)
        self.daily_trades.append(trade)
        
        self.current_capital += pnl
        
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital
        
        if pnl > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
            self.total_wins += 1
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            self.total_losses += 1
        
        if len(self.trade_history) > 1000:
            self.trade_history = self.trade_history[-500:]
    
    def can_trade(self) -> tuple[bool, str]:
        self.reset_daily()
        
        if self.current_capital <= 0:
            return False, "No capital"
        
        daily_pnl = sum(t.pnl for t in self.daily_trades)
        daily_pnl_pct = daily_pnl / self.current_capital
        
        if daily_pnl_pct <= self.max_daily_loss_pct:
            return False, f"Daily loss limit: {daily_pnl_pct:.2%}"
        
        drawdown = (self.peak_capital - self.current_capital) / self.peak_capital
        if drawdown >= abs(self.max_drawdown_pct):
            return False, f"Max drawdown: {drawdown:.2%}"
        
        if len(self.daily_trades) >= 10:
            return False, "Daily trade limit"
        
        return True, "OK"
    
    def get_statistics(self) -> Dict:
        if not self.trade_history:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0,
                "profit_factor": 0,
                "avg_win": 0,
                "avg_loss": 0,
                "current_capital": self.current_capital,
                "total_pnl": 0,
                "total_pnl_pct": 0,
                "max_drawdown": 0,
                "kelly": self.kelly_fraction,
                "consecutive_wins": 0,
                "consecutive_losses": 0,
                "daily_trades": 0,
            }
        
        wins = [t for t in self.trade_history if t.pnl > 0]
        losses = [t for t in self.trade_history if t.pnl <= 0]
        
        total_pnl = sum(t.pnl for t in self.trade_history)
        drawdown = (self.peak_capital - self.current_capital) / self.peak_capital
        
        return {
            "total_trades": len(self.trade_history),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": len(wins) / len(self.trade_history) * 100,
            "profit_factor": self.profit_factor,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "current_capital": self.current_capital,
            "total_pnl": total_pnl,
            "total_pnl_pct": (self.current_capital - self.initial_capital) / self.initial_capital * 100,
            "max_drawdown": drawdown * 100,
            "kelly": self.optimal_fraction,
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "daily_trades": len(self.daily_trades),
        }
    
    def get_performance_report(self) -> str:
        stats = self.get_statistics()
        
        return f"""
╔══════════════════════════════════════════════════════════════════╗
║                    RISK MANAGEMENT REPORT                      ║
╠══════════════════════════════════════════════════════════════════╣
║ Capital:     ₩{stats['current_capital']:>12,.0f}  (Initial: ₩{self.initial_capital:>8,.0f})   ║
║ Total PnL:  ₩{stats['total_pnl']:>12,.0f}  ({stats['total_pnl_pct']:>+6.2f}%)                  ║
╠══════════════════════════════════════════════════════════════════╣
║ Trades:      {stats['total_trades']:>6}  (W: {stats['winning_trades']:>4}  L: {stats['losing_trades']:>4})                    ║
║ Win Rate:   {stats['win_rate']:>6.1f}%                                            ║
║ Profit Factor:{stats['profit_factor']:>7.2f}                                            ║
╠══════════════════════════════════════════════════════════════════╣
║ Avg Win:    {stats['avg_win']:>7.2f}%                                            ║
║ Avg Loss:   {stats['avg_loss']:>7.2f}%                                            ║
║ Kelly:      {stats['kelly']:>7.2f}%                                            ║
╠══════════════════════════════════════════════════════════════════╣
║ Max DD:     {stats['max_drawdown']:>6.2f}%                                            ║
║ Consec.W:  {stats['consecutive_wins']:>5}  Consec.L: {stats['consecutive_losses']:>5}                      ║
║ Daily:      {stats['daily_trades']:>5} trades                                         ║
╚══════════════════════════════════════════════════════════════════╝
"""


class VolatilityAdaptiveStops:
    def __init__(self, atr_multiplier: float = 2.0, use_atr: bool = True):
        self.atr_multiplier = atr_multiplier
        self.use_atr = use_atr
    
    def calculate_stops(
        self,
        entry_price: float,
        atr: float,
        volatility: float,
        market_regime: str,
        side: str = "BUY",
    ) -> Dict[str, float]:
        if self.use_atr and atr > 0:
            base_stop = atr * self.atr_multiplier
        else:
            base_stop = entry_price * volatility
        
        regime_stop_multipliers = {
            "BULL": 1.5,
            "NEUTRAL": 2.0,
            "BEAR": 1.2,
            "VOLATILE": 2.5,
        }
        
        multiplier = regime_stop_multipliers.get(market_regime, 2.0)
        stop_distance = base_stop * multiplier
        
        if side == "BUY":
            stop_loss = entry_price - stop_distance
            take_profits = [
                entry_price + stop_distance * 1.5,
                entry_price + stop_distance * 2.5,
                entry_price + stop_distance * 4.0,
            ]
        else:
            stop_loss = entry_price + stop_distance
            take_profits = [
                entry_price - stop_distance * 1.5,
                entry_price - stop_distance * 2.5,
                entry_price - stop_distance * 4.0,
            ]
        
        return {
            "stop_loss": stop_loss,
            "stop_distance_pct": stop_distance / entry_price * 100,
            "tp1": take_profits[0],
            "tp2": take_profits[1],
            "tp3": take_profits[2],
            "risk_reward_1": 1.5,
            "risk_reward_2": 2.5,
            "risk_reward_3": 4.0,
        }
    
    def calculate_trailing_stop(
        self,
        current_price: float,
        peak_price: float,
        entry_price: float,
        atr: float,
        side: str = "BUY",
    ) -> float:
        if side == "BUY":
            if atr > 0:
                trailing = atr * 1.5
            else:
                trailing = entry_price * 0.015
            
            stop = peak_price - trailing
            breakeven = entry_price + (entry_price * 0.003)
            
            return max(stop, breakeven)
        else:
            if atr > 0:
                trailing = atr * 1.5
            else:
                trailing = entry_price * 0.015
            
            stop = peak_price + trailing
            breakeven = entry_price - (entry_price * 0.003)
            
            return min(stop, breakeven)
