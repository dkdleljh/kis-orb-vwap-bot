"""
Advanced Risk Manager - 100점 트레이더를 위한 고급 리스크 관리.
"""

import time
from dataclasses import dataclass
from typing import List, Dict
from collections import deque


@dataclass
class TradeRecord:
    timestamp: float
    symbol: str
    side: str
    qty: int
    price: float
    pnl_pct: float
    exit_reason: str


class AdvancedRiskManager:
    def __init__(
        self,
        max_entries_per_day: int = 10,
        daily_loss_limit_pct: float = -0.05,
        max_consecutive_stop: int = 3,
        max_position_pct: float = 0.20,
        circuit_breaker_losses: int = 5,
        circuit_breaker_cooldown_min: int = 60,
        max_drawdown_pct: float = -0.15,
        max_daily_trades: int = 15,
    ):
        self.max_entries = max_entries_per_day
        self.daily_loss_limit = daily_loss_limit_pct
        self.max_consecutive_stop = max_consecutive_stop
        self.max_position_pct = max_position_pct
        self.circuit_breaker_losses = circuit_breaker_losses
        self.circuit_breaker_cooldown = circuit_breaker_cooldown_min * 60
        self.max_drawdown = max_drawdown_pct
        self.max_daily_trades = max_daily_trades
        
        self.entries_today = 0
        self.daily_pnl = 0.0
        self.consecutive_stops = 0
        self.consecutive_losses = 0
        self.circuit_breaker_triggered = False
        self.circuit_breaker_end_time = 0.0
        
        self.peak_equity = 1000000.0
        self.current_equity = 1000000.0
        self.max_drawdown_reached = 0.0
        
        self.trade_history: deque = deque(maxlen=1000)
        self.daily_trades: List[TradeRecord] = []
        self._last_reset_day = ""
        
        self.trade_log: List[Dict] = []
    
    def can_enter(self, current_price: float, atr: float = 0) -> tuple[bool, str]:
        now = time.time()
        
        if self.circuit_breaker_triggered:
            if now < self.circuit_breaker_end_time:
                return False, "CIRCUIT_BREAKER"
            self.circuit_breaker_triggered = False
            self.consecutive_losses = 0
        
        if self.entries_today >= self.max_entries:
            return False, "MAX_ENTRIES"
        
        if self.daily_pnl <= self.daily_loss_limit:
            return False, "DAILY_LOSS_LIMIT"
        
        if self.consecutive_stops >= self.max_consecutive_stop:
            return False, "CONSECUTIVE_STOPS"
        
        if len(self.daily_trades) >= self.max_daily_trades:
            return False, "MAX_DAILY_TRADES"
        
        return True, "OK"
    
    def calculate_position_size(
        self,
        cash: float,
        price: float,
        atr: float = 0,
        atr_percent: float = 0,
    ) -> int:
        base_qty = int((cash * self.max_position_pct) // price)
        
        if atr_percent > 0:
            risk_per_share = atr
            if risk_per_share > 0:
                max_risk = cash * 0.01
                atr_based_qty = int(max_risk // risk_per_share)
                base_qty = min(base_qty, atr_based_qty)
        
        return max(1, base_qty)
    
    def calculate_atr_stop_loss(
        self,
        entry_price: float,
        atr: float,
        atr_multiplier: float = 2.0,
        is_long: bool = True,
    ) -> float:
        stop_distance = atr * atr_multiplier
        if is_long:
            return entry_price - stop_distance
        return entry_price + stop_distance
    
    def record_entry(self, symbol: str) -> None:
        self.entries_today += 1
        self._check_daily_reset()
    
    def record_exit(
        self,
        symbol: str,
        pnl_pct: float,
        exit_reason: str,
    ) -> None:
        self.daily_pnl += pnl_pct
        
        trade = TradeRecord(
            timestamp=time.time(),
            symbol=symbol,
            side="BUY",
            qty=0,
            price=0,
            pnl_pct=pnl_pct,
            exit_reason=exit_reason,
        )
        self.daily_trades.append(trade)
        self.trade_history.append(trade)
        
        if pnl_pct < 0:
            self.consecutive_losses += 1
            self.consecutive_stops += 1
            
            if self.consecutive_losses >= self.circuit_breaker_losses:
                self.circuit_breaker_triggered = True
                self.circuit_breaker_end_time = time.time() + self.circuit_breaker_cooldown
        else:
            self.consecutive_stops = 0
        
        self._update_equity(pnl_pct)
    
    def _update_equity(self, pnl_pct: float) -> None:
        self.current_equity *= (1 + pnl_pct)
        
        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity
        
        drawdown = (self.current_equity - self.peak_equity) / self.peak_equity
        if drawdown < self.max_drawdown_reached:
            self.max_drawdown_reached = drawdown
        
        if drawdown <= self.max_drawdown:
            self.circuit_breaker_triggered = True
            self.circuit_breaker_end_time = time.time() + (self.circuit_breaker_cooldown * 2)
    
    def _check_daily_reset(self) -> None:
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        
        if self._last_reset_day != today:
            self.entries_today = 0
            self.daily_pnl = 0.0
            self.consecutive_stops = 0
            self.consecutive_losses = 0
            self.daily_trades = []
            self._last_reset_day = today
    
    def get_stats(self) -> Dict:
        return {
            "entries_today": self.entries_today,
            "daily_pnl": self.daily_pnl,
            "consecutive_stops": self.consecutive_stops,
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker": self.circuit_breaker_triggered,
            "current_equity": self.current_equity,
            "peak_equity": self.peak_equity,
            "drawdown": self.max_drawdown_reached,
            "total_trades": len(self.trade_history),
        }
    
    def log_trade(
        self,
        symbol: str,
        action: str,
        price: float,
        qty: int,
        reason: str = "",
    ) -> None:
        self.trade_log.append({
            "time": time.time(),
            "symbol": symbol,
            "action": action,
            "price": price,
            "qty": qty,
            "reason": reason,
        })
