#!/usr/bin/env python3
"""
Pro Trader Phase 3 - Trading Session Manager
완전한 무인 자동화를 위한 세션 관리자
"""

import asyncio
import os
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum


class SessionState(Enum):
    IDLE = "IDLE"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    RECOVERING = "RECOVERING"
    EMERGENCY = "EMERGENCY"
    SHUTDOWN = "SHUTDOWN"


class TradeDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass
class TradeRecord:
    symbol: str
    direction: TradeDirection
    entry_price: float
    entry_time: datetime
    qty: int
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    fees: float = 0.0
    status: str = "OPEN"
    notes: str = ""


@dataclass
class DailySummary:
    date: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    total_fees: float = 0.0
    max_drawdown: float = 0.0
    peak_equity: float = 0.0
    current_equity: float = 0.0


@dataclass
class SessionStats:
    start_time: datetime
    total_orders: int = 0
    filled_orders: int = 0
    rejected_orders: int = 0
    total_pnl: float = 0.0
    current_equity: float = 0.0
    peak_equity: float = 0.0
    max_drawdown: float = 0.0
    uptime_seconds: float = 0.0


class TradingSessionManager:
    def __init__(
        self,
        logger,
        config: Dict[str, Any],
        initial_capital: float = 1000000.0,
    ):
        self.logger = logger
        self.config = config
        self.initial_capital = initial_capital
        
        self.state = SessionState.IDLE
        self.direction = TradeDirection.FLAT
        
        self.stats = SessionStats(start_time=datetime.now())
        self.daily_summary = DailySummary(
            date=datetime.now().strftime("%Y%m%d"),
            current_equity=initial_capital,
            peak_equity=initial_capital,
        )
        
        self.open_trades: List[TradeRecord] = []
        self.closed_trades: List[TradeRecord] = []
        
        self.max_daily_loss = config.get("max_daily_loss", -50000)
        self.max_position_size = config.get("max_position_size", 0.3)
        self.max_daily_trades = config.get("max_daily_trades", 10)
        
        self._running = False
        self._pause_event = asyncio.Event()
        self._emergency_event = asyncio.Event()
        
        self._order_lock = asyncio.Lock()
        
        self.alert_thresholds = {
            "daily_loss": config.get("alert_daily_loss", -30000),
            "drawdown": config.get("alert_drawdown", 0.05),
            "position_size": config.get("alert_position_size", 0.25),
            "order_rejection_rate": config.get("alert_rejection_rate", 0.3),
        }
    
    async def start(self) -> bool:
        """세션 시작"""
        if self.state != SessionState.IDLE:
            self.logger.warning(f"Cannot start from state: {self.state}")
            return False
        
        self.state = SessionState.INITIALIZING
        self.stats = SessionStats(start_time=datetime.now())
        
        await self._load_session_state()
        
        self._running = True
        self._pause_event.set()
        self.state = SessionState.RUNNING
        
        self.logger.info(f"Trading session started with capital: {self.initial_capital:,.0f}")
        return True
    
    async def stop(self, reason: str = "manual") -> None:
        """세션 종료"""
        self.logger.info(f"Stopping trading session: {reason}")
        self._running = False
        self.state = SessionState.SHUTDOWN
        
        await self._save_session_state()
        
        self.logger.info(f"Session stopped. Total PnL: {self.stats.total_pnl:,.0f}")
    
    async def pause(self) -> None:
        """세션 일시정지"""
        if self.state != SessionState.RUNNING:
            return
        
        self._pause_event.clear()
        self.state = SessionState.PAUSED
        self.logger.info("Trading session paused")
    
    async def resume(self) -> None:
        """세션 재개"""
        if self.state != SessionState.PAUSED:
            return
        
        self._pause_event.set()
        self.state = SessionState.RUNNING
        self.logger.info("Trading session resumed")
    
    async def emergency_stop(self, reason: str) -> None:
        """비상 정지"""
        self.logger.critical(f"EMERGENCY STOP: {reason}")
        self._emergency_event.set()
        self._running = False
        self.state = SessionState.EMERGENCY
        
        await self._save_session_state()
        
        self.logger.critical(f"Emergency stop triggered. Open trades: {len(self.open_trades)}")
    
    def can_trade(self) -> bool:
        """거래 가능 여부 확인"""
        if self.state != SessionState.RUNNING:
            return False
        
        if not self._pause_event.is_set():
            return False
        
        if self._emergency_event.is_set():
            return False
        
        if self.daily_summary.total_pnl <= self.max_daily_loss:
            self.logger.warning(f"Daily loss limit reached: {self.daily_summary.total_pnl:,.0f}")
            return False
        
        if self.daily_summary.total_trades >= self.max_daily_trades:
            self.logger.warning(f"Daily trade limit reached: {self.daily_summary.total_trades}")
            return False
        
        return True
    
    async def open_trade(
        self,
        symbol: str,
        direction: TradeDirection,
        price: float,
        qty: int,
        fees: float = 0.0,
    ) -> Optional[TradeRecord]:
        """포지션 오픈"""
        if not self.can_trade():
            return None
        
        async with self._order_lock:
            self.stats.total_orders += 1
            
            trade = TradeRecord(
                symbol=symbol,
                direction=direction,
                entry_price=price,
                entry_time=datetime.now(),
                qty=qty,
                fees=fees,
                status="OPEN",
            )
            
            self.open_trades.append(trade)
            self.daily_summary.total_trades += 1
            
            self.logger.info(f"Opened {direction.value} {symbol}: {qty} @ {price:,.0f}")
            
            return trade
    
    async def close_trade(
        self,
        symbol: str,
        price: float,
        reason: str = "TP/SL",
    ) -> Optional[TradeRecord]:
        """포지션 클로즈"""
        async with self._order_lock:
            trade = None
            for t in self.open_trades:
                if t.symbol == symbol:
                    trade = t
                    break
            
            if not trade:
                self.logger.warning(f"No open trade found for {symbol}")
                return None
            
            trade.exit_price = price
            trade.exit_time = datetime.now()
            
            if trade.direction == TradeDirection.LONG:
                pnl = (price - trade.entry_price) * trade.qty - trade.fees
            else:
                pnl = (trade.entry_price - price) * trade.qty - trade.fees
            
            trade.pnl = pnl
            trade.pnl_pct = pnl / (trade.entry_price * trade.qty) * 100
            trade.status = "CLOSED"
            trade.notes = reason
            
            self.open_trades.remove(trade)
            self.closed_trades.append(trade)
            
            self.stats.filled_orders += 1
            self.stats.total_pnl += pnl
            self.daily_summary.total_pnl += pnl
            self.daily_summary.total_fees += trade.fees
            
            if pnl > 0:
                self.daily_summary.winning_trades += 1
            else:
                self.daily_summary.losing_trades += 1
            
            self._update_equity()
            
            self.logger.info(f"Closed {symbol}: PnL={pnl:,.0f} ({trade.pnl_pct:.2f}%)")
            
            await self._check_alerts()
            
            return trade
    
    def _update_equity(self) -> None:
        """ Equity 업데이트 """
        unrealized_pnl = 0.0
        for trade in self.open_trades:
            unrealized_pnl += trade.pnl
        
        self.stats.current_equity = self.initial_capital + self.stats.total_pnl + unrealized_pnl
        self.daily_summary.current_equity = self.stats.current_equity
        
        if self.stats.current_equity > self.stats.peak_equity:
            self.stats.peak_equity = self.stats.current_equity
            self.daily_summary.peak_equity = self.stats.current_equity
        
        dd = (self.stats.peak_equity - self.stats.current_equity) / self.stats.peak_equity
        if dd > self.stats.max_drawdown:
            self.stats.max_drawdown = dd
            self.daily_summary.max_drawdown = dd
    
    async def _check_alerts(self) -> None:
        """알림 임계값 체크"""
        if self.daily_summary.total_pnl <= self.alert_thresholds["daily_loss"]:
            self.logger.warning(f"ALERT: Daily loss threshold reached: {self.daily_summary.total_pnl:,.0f}")
        
        if self.stats.max_drawdown >= self.alert_thresholds["drawdown"]:
            self.logger.warning(f"ALERT: Drawdown threshold reached: {self.stats.max_drawdown:.1%}")
        
        if self.stats.rejected_orders / max(self.stats.total_orders, 1) >= self.alert_thresholds["order_rejection_rate"]:
            self.logger.warning(f"ALERT: High rejection rate: {self.stats.rejected_orders/self.stats.total_orders:.1%}")
    
    def get_status(self) -> Dict[str, Any]:
        """현재 상태 반환"""
        return {
            "state": self.state.value,
            "direction": self.direction.value,
            "stats": {
                "total_orders": self.stats.total_orders,
                "filled_orders": self.stats.filled_orders,
                "rejected_orders": self.stats.rejected_orders,
                "total_pnl": self.stats.total_pnl,
                "current_equity": self.stats.current_equity,
                "peak_equity": self.stats.peak_equity,
                "max_drawdown": self.stats.max_drawdown,
                "uptime_seconds": (datetime.now() - self.stats.start_time).total_seconds(),
            },
            "daily": {
                "date": self.daily_summary.date,
                "total_trades": self.daily_summary.total_trades,
                "winning_trades": self.daily_summary.winning_trades,
                "losing_trades": self.daily_summary.losing_trades,
                "total_pnl": self.daily_summary.total_pnl,
                "total_fees": self.daily_summary.total_fees,
            },
            "open_trades": len(self.open_trades),
        }
    
    async def _save_session_state(self) -> None:
        """세션 상태 저장"""
        state_file = "session_state.json"
        
        state = {
            "stats": {
                "total_orders": self.stats.total_orders,
                "filled_orders": self.stats.filled_orders,
                "total_pnl": self.stats.total_pnl,
                "current_equity": self.stats.current_equity,
            },
            "daily": self.daily_summary.__dict__,
            "open_trades": [
                {
                    "symbol": t.symbol,
                    "direction": t.direction.value,
                    "entry_price": t.entry_price,
                    "entry_time": t.entry_time.isoformat(),
                    "qty": t.qty,
                }
                for t in self.open_trades
            ],
            "saved_at": datetime.now().isoformat(),
        }
        
        try:
            with open(state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save session state: {e}")
    
    async def _load_session_state(self) -> None:
        """세션 상태 로드"""
        state_file = "session_state.json"
        
        if not os.path.exists(state_file):
            return
        
        try:
            with open(state_file, 'r') as f:
                state = json.load(f)
            
            self.logger.info(f"Loaded session state from {state.get('saved_at', 'unknown')}")
        except Exception as e:
            self.logger.error(f"Failed to load session state: {e}")
    
    def get_daily_report(self) -> str:
        """일일 리포트 생성"""
        win_rate = self.daily_summary.winning_trades / max(self.daily_summary.total_trades, 1) * 100
        
        report = f"""
╔══════════════════════════════════════════════════════════════╗
║                    DAILY TRADING REPORT                      ║
╠══════════════════════════════════════════════════════════════╣
║ Date: {self.daily_summary.date}                                              ║
╠══════════════════════════════════════════════════════════════╣
║ Trades: {self.daily_summary.total_trades:3d} (W: {self.daily_summary.winning_trades:2d} / L: {self.daily_summary.losing_trades:2d})                          ║
║ Win Rate: {win_rate:5.1f}%                                              ║
║ PnL: ₩{self.daily_summary.total_pnl:>12,.0f}                                    ║
║ Fees: ₩{self.daily_summary.total_fees:>11,.0f}                                    ║
║ Drawdown: {self.daily_summary.max_drawdown:>8.1%}                                       ║
║ Equity: ₩{self.daily_summary.current_equity:>11,.0f}                                    ║
╚══════════════════════════════════════════════════════════════╝
"""
        return report
