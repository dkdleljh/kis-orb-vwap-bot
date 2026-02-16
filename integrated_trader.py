#!/usr/bin/env python3
"""
Pro Trader - Integrated Trading System
모든 핵심 모듈을 통합한 완전한 무인 자동화 시스템
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, Any
from dataclasses import dataclass
from enum import Enum

from trading_session import TradingSessionManager, TradeDirection
from advanced_risk import AdvancedRiskManager, VolatilityAdaptiveStops
from auto_recovery import AutoRecoveryManager
from paper_trading import PaperTradingSimulator, SimulationMode
from perfect_strategy import Perfect100Strategy
from fee_calculator import FeeCalculator


class TraderMode(Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass
class TraderConfig:
    """Trader 전체 설정"""
    # Capital
    initial_capital: float = 10_000_000  # 1천만원
    
    # Mode
    mode: TraderMode = TraderMode.PAPER
    
    # Risk Management (from advanced_risk.py)
    max_position_pct: float = 0.20  # 최대 포지션 20%
    max_daily_loss_pct: float = -0.03  # 일일 손실 한도 -3%
    max_drawdown_pct: float = -0.10  # 최대 드로다운 -10%
    max_consecutive_losses: int = 3
    base_risk_pct: float = 0.01
    
    # Auto Recovery
    max_retries: int = 3
    retry_delay: int = 5
    circuit_breaker_threshold: int = 5
    
    # Position Limits
    max_daily_trades: int = 10
    max_position_size: float = 0.3
    
    # Volatility Stops
    atr_multiplier: float = 2.0
    use_atr: bool = True
    
    # Slippage
    slippage_factor: float = 1.0
    
    # Strategy
    min_score: int = 60
    min_r_ratio: float = 1.5


class IntegratedTrader:
    """
    완전 통합 트레이더
    - AdvancedRiskManager: Kelly Criterion, 동적 포지션 sizing
    - TradingSessionManager: 세션 및 포지션 관리
    - AutoRecoveryManager: 자동 복구 및 서킷 브레이커
    - PaperTradingSimulator: 백테스트/-paper trading
    """
    
    def __init__(
        self,
        config: TraderConfig,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        
        # Initialize components
        self._init_risk_manager()
        self._init_session_manager()
        self._init_recovery_manager()
        self._init_simulator()
        self._init_strategy()
        self._init_fee_calculator()
        
        self._running = False
        self._market_regime = "NEUTRAL"
    
    def _init_risk_manager(self) -> None:
        """리스크 매니저 초기화"""
        self.risk_manager = AdvancedRiskManager(
            initial_capital=self.config.initial_capital,
            max_position_pct=self.config.max_position_pct,
            max_daily_loss_pct=self.config.max_daily_loss_pct,
            max_drawdown_pct=self.config.max_drawdown_pct,
            max_consecutive_losses=self.config.max_consecutive_losses,
            base_risk_pct=self.config.base_risk_pct,
        )
        self.logger.info(f"Risk Manager initialized: capital={self.config.initial_capital:,.0f}")
    
    def _init_session_manager(self) -> None:
        """세션 매니저 초기화"""
        session_config = {
            "max_daily_loss": self.config.initial_capital * self.config.max_daily_loss_pct,
            "max_position_size": self.config.max_position_size,
            "max_daily_trades": self.config.max_daily_trades,
            "alert_daily_loss": self.config.initial_capital * self.config.max_daily_loss_pct * 0.6,
            "alert_drawdown": abs(self.config.max_drawdown_pct) * 0.5,
            "alert_position_size": self.config.max_position_pct * 0.8,
            "alert_rejection_rate": 0.3,
        }
        
        self.session_manager = TradingSessionManager(
            logger=self.logger,
            config=session_config,
            initial_capital=self.config.initial_capital,
        )
        self.logger.info("Session Manager initialized")
    
    def _init_recovery_manager(self) -> None:
        """복구 매니저 초기화"""
        recovery_config = {
            "max_retries": self.config.max_retries,
            "retry_delay": self.config.retry_delay,
            "circuit_breaker_threshold": self.config.circuit_breaker_threshold,
            "health_check_interval": 30,
        }
        
        self.recovery_manager = AutoRecoveryManager(
            logger=self.logger,
            config=recovery_config,
        )
        self.logger.info("Recovery Manager initialized")
    
    def _init_simulator(self) -> None:
        """시뮬레이터 초기화"""
        if self.config.mode == TraderMode.BACKTEST:
            mode = SimulationMode.BACKTEST
        elif self.config.mode == TraderMode.PAPER:
            mode = SimulationMode.PAPER
        else:
            mode = SimulationMode.LIVE
        
        self.simulator = PaperTradingSimulator(
            logger=self.logger,
            initial_capital=self.config.initial_capital,
            slippage_factor=self.config.slippage_factor,
            mode=mode,
        )
        self.logger.info(f"Simulator initialized: mode={mode.value}")
    
    def _init_strategy(self) -> None:
        """전략 초기화"""
        self.strategy = Perfect100Strategy(
            min_score=self.config.min_score,
            min_r_ratio=self.config.min_r_ratio,
        )
        self.logger.info(f"Strategy initialized: min_score={self.config.min_score}, min_r_ratio={self.config.min_r_ratio}")
    
    def _init_fee_calculator(self) -> None:
        """수수료 계산기 초기화"""
        self.fee_calculator = FeeCalculator(min_commission_check=False)
        self.logger.info("Fee Calculator initialized")
    
    # ==================== Risk Management Integration ====================
    
    def can_trade(self, symbol: Optional[str] = None) -> tuple[bool, str]:
        """
        통합 거래 가능 여부 확인
        Returns: (can_trade: bool, reason: str)
        """
        # Check Risk Manager
        risk_ok, risk_reason = self.risk_manager.can_trade()
        if not risk_ok:
            return False, f"Risk: {risk_reason}"
        
        # Check Session Manager
        if not self.session_manager.can_trade():
            return False, "Session: Trading not allowed"
        
        # Check Circuit Breaker
        if self.recovery_manager.circuit_breaker_open:
            return False, "Circuit breaker open"
        
        return True, "OK"
    
    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss_pct: float,
        signal_strength: float = 1.0,
    ) -> Dict:
        """
        동적 포지션 사이즈 계산 (Kelly Criterion 기반)
        """
        result = self.risk_manager.calculate_position_size(
            entry_price=entry_price,
            stop_loss_pct=stop_loss_pct,
            signal_strength=signal_strength,
            market_regime=self._market_regime,
        )
        
        # Also check session limits
        max_capital = self.config.initial_capital * self.config.max_position_size
        result["capital"] = min(result["capital"], max_capital)
        result["fraction"] = min(result["fraction"], self.config.max_position_size)
        
        return result
    
    def get_stop_levels(
        self,
        entry_price: float,
        atr: float = 0,
        volatility: float = 0.02,
        side: str = "BUY",
    ) -> Dict:
        """
        변동성 적응형 스톱 레벨 계산
        """
        stops = VolatilityAdaptiveStops(
            atr_multiplier=self.config.atr_multiplier,
            use_atr=self.config.use_atr,
        )
        
        return stops.calculate_stops(
            entry_price=entry_price,
            atr=atr,
            volatility=volatility,
            market_regime=self._market_regime,
            side=side,
        )
    
    def get_trailing_stop(
        self,
        current_price: float,
        peak_price: float,
        entry_price: float,
        atr: float = 0,
        side: str = "BUY",
    ) -> float:
        """
        트레일링 스톱 계산
        """
        stops = VolatilityAdaptiveStops(
            atr_multiplier=self.config.atr_multiplier,
            use_atr=self.config.use_atr,
        )
        
        return stops.calculate_trailing_stop(
            current_price=current_price,
            peak_price=peak_price,
            entry_price=entry_price,
            atr=atr,
            side=side,
        )
    
    # ==================== Trade Execution ====================
    
    async def execute_entry(
        self,
        symbol: str,
        side: str,
        price: float,
        stop_loss_pct: float,
        signal_strength: float = 1.0,
        atr: float = 0,
        market: str = "KOR",
    ) -> Optional[Dict]:
        """
        진입 실행 (통합风险管理)
        """
        # Check if we can trade
        can_trade, reason = self.can_trade(symbol)
        if not can_trade:
            self.logger.warning(f"Cannot trade {symbol}: {reason}")
            return None
        
        # Calculate position size
        pos_size = self.calculate_position_size(
            entry_price=price,
            stop_loss_pct=stop_loss_pct,
            signal_strength=signal_strength,
        )
        
        qty = pos_size["qty"]
        if qty <= 0:
            self.logger.warning(f"Position size too small: {qty}")
            return None
        
        # Calculate fees (entry)
        entry_costs = self.fee_calculator.calculate_entry_cost(price, qty)
        fees = entry_costs.total_cost
        
        # Execute based on mode
        if self.config.mode == TraderMode.LIVE:
            # TODO: Implement live trading
            self.logger.error("Live trading not implemented")
            return None
        else:
            # Paper/Backtest mode
            order = self.simulator.place_order(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                order_type="market",
            )
            
            if order.status == "FILLED" and order.filled_price is not None:
                # Record trade in risk manager
                self.risk_manager.record_trade(
                    symbol=symbol,
                    side=side,
                    entry_price=order.filled_price,
                    exit_price=0,
                    qty=qty,
                    fees=fees,
                    holding_period=0,
                    exit_reason="OPEN",
                )
                
                # Get stop levels
                stop_levels = self.get_stop_levels(
                    entry_price=order.filled_price,
                    atr=atr,
                    side=side,
                )
                
                # Open trade in session
                direction = TradeDirection.LONG if side == "BUY" else TradeDirection.SHORT
                trade = await self.session_manager.open_trade(
                    symbol=symbol,
                    direction=direction,
                    price=order.filled_price,
                    qty=qty,
                    fees=fees,
                )
                
                if trade is None:
                    self.logger.error(f"Failed to open trade for {symbol}")
                    return None
                
                self.logger.info(f"Entry executed: {symbol} {side} {qty} @ {order.filled_price:,.0f}")
                
                return {
                    "order": order,
                    "position_size": pos_size,
                    "stop_levels": stop_levels,
                    "fees": fees,
                }
        
        return None
    
    async def execute_exit(
        self,
        symbol: str,
        price: float,
        reason: str = "TP/SL",
        market: str = "KOR",
    ) -> Optional[Dict]:
        """
        청산 실행
        """
        # Get open trade
        trade = None
        for t in self.session_manager.open_trades:
            if t.symbol == symbol:
                trade = t
                break
        
        if not trade:
            self.logger.warning(f"No open position for {symbol}")
            return None
        
        # Calculate fees (exit)
        exit_costs = self.fee_calculator.calculate_exit_cost(price, trade.qty)
        fees = exit_costs.total_cost
        
        # Execute based on mode
        if self.config.mode == TraderMode.LIVE:
            self.logger.error("Live trading not implemented")
            return None
        else:
            # Paper/Backtest mode
            order = self.simulator.place_order(
                symbol=symbol,
                side="SELL" if trade.direction == TradeDirection.LONG else "BUY",
                qty=trade.qty,
                price=price,
                order_type="market",
            )
            
            if order.status == "FILLED" and order.filled_price is not None:
                # Close trade in session
                closed_trade = await self.session_manager.close_trade(
                    symbol=symbol,
                    price=order.filled_price,
                    reason=reason,
                )
                
                if closed_trade is None:
                    self.logger.error(f"Failed to close trade for {symbol}")
                    return None
                
                # Record in risk manager
                holding_period = (datetime.now() - trade.entry_time).days
                self.risk_manager.record_trade(
                    symbol=symbol,
                    side=trade.direction.value,
                    entry_price=trade.entry_price,
                    exit_price=order.filled_price,
                    qty=trade.qty,
                    fees=fees + trade.fees,
                    holding_period=holding_period,
                    exit_reason=reason,
                )
                
                self.logger.info(f"Exit executed: {symbol} {reason} PnL={closed_trade.pnl:,.0f}")
                
                return {
                    "order": order,
                    "pnl": closed_trade.pnl,
                    "pnl_pct": closed_trade.pnl_pct,
                    "fees": fees,
                }
        
        return None
    
    # ==================== Market Regime ====================
    
    def set_market_regime(self, regime: str) -> None:
        """시장 환경 설정"""
        valid_regimes = ["BULL", "NEUTRAL", "BEAR", "VOLATILE"]
        if regime not in valid_regimes:
            self.logger.warning(f"Invalid regime: {regime}, using NEUTRAL")
            regime = "NEUTRAL"
        
        self._market_regime = regime
        self.logger.info(f"Market regime set to: {regime}")
    
    # ==================== Status & Reports ====================
    
    def get_status(self) -> Dict[str, Any]:
        """전체 상태 조회"""
        risk_stats = self.risk_manager.get_statistics()
        session_status = self.session_manager.get_status()
        
        return {
            "mode": self.config.mode.value,
            "running": self._running,
            "market_regime": self._market_regime,
            "risk": risk_stats,
            "session": session_status,
            "circuit_breaker": {
                "open": self.recovery_manager.circuit_breaker_open,
                "count": self.recovery_manager.circuit_breaker_count,
            },
        }
    
    def get_risk_report(self) -> str:
        """리스크 리포트"""
        return self.risk_manager.get_performance_report()
    
    def get_daily_report(self) -> str:
        """일일 리포트"""
        return self.session_manager.get_daily_report()
    
    # ==================== Lifecycle ====================
    
    async def start(self) -> bool:
        """트레이더 시작"""
        if self._running:
            self.logger.warning("Trader already running")
            return False
        
        self.logger.info(f"Starting Integrated Trader: mode={self.config.mode.value}")
        
        # Start session manager
        await self.session_manager.start()
        
        self._running = True
        self.logger.info("Integrated Trader started successfully")
        
        return True
    
    async def stop(self, reason: str = "manual") -> None:
        """트레이더 정지"""
        self.logger.info(f"Stopping Integrated Trader: {reason}")
        
        self._running = False
        await self.session_manager.stop(reason)
        
        self.logger.info("Integrated Trader stopped")
    
    async def reset(self) -> None:
        """초기화"""
        self.logger.info("Resetting Integrated Trader")
        
        await self.stop("reset")
        
        self._init_risk_manager()
        self._init_session_manager()
        self.simulator.reset()
        
        self.logger.info("Integrated Trader reset complete")


# ==================== Convenience Factory ====================

def create_backtester(
    initial_capital: float = 10_000_000,
    min_score: int = 60,
    min_r_ratio: float = 1.5,
    logger: Optional[logging.Logger] = None,
) -> IntegratedTrader:
    """백테스터 생성"""
    config = TraderConfig(
        initial_capital=initial_capital,
        mode=TraderMode.BACKTEST,
        min_score=min_score,
        min_r_ratio=min_r_ratio,
    )
    return IntegratedTrader(config=config, logger=logger)


def create_paper_trader(
    initial_capital: float = 10_000_000,
    min_score: int = 60,
    min_r_ratio: float = 1.5,
    logger: Optional[logging.Logger] = None,
) -> IntegratedTrader:
    """-paper 트레이더 생성"""
    config = TraderConfig(
        initial_capital=initial_capital,
        mode=TraderMode.PAPER,
        min_score=min_score,
        min_r_ratio=min_r_ratio,
    )
    return IntegratedTrader(config=config, logger=logger)


def create_live_trader(
    initial_capital: float = 10_000_000,
    min_score: int = 60,
    min_r_ratio: float = 1.5,
    logger: Optional[logging.Logger] = None,
) -> IntegratedTrader:
    """실거래 트레이더 생성"""
    config = TraderConfig(
        initial_capital=initial_capital,
        mode=TraderMode.LIVE,
        min_score=min_score,
        min_r_ratio=min_r_ratio,
    )
    return IntegratedTrader(config=config, logger=logger)


# ==================== Test ====================

if __name__ == "__main__":
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    # Test integrated trader
    print("=" * 60)
    print("Integrated Trader Test")
    print("=" * 60)
    
    # Create paper trader
    trader = create_paper_trader(initial_capital=10_000_000)
    
    print(f"\n1. Trader created: {trader.config.mode.value} mode")
    print(f"   Initial capital: {trader.config.initial_capital:,.0f}")
    
    # Check can trade
    asyncio.run(trader.start())
    can_trade, reason = trader.can_trade("005930")
    print(f"\n2. Can trade: {can_trade} ({reason})")
    
    # Test position sizing
    pos_size = trader.calculate_position_size(
        entry_price=80000,
        stop_loss_pct=0.03,
        signal_strength=1.2,
    )
    print("\n3. Position size (entry=80,000, SL=3%):")
    print(f"   Fraction: {pos_size['fraction']:.2%}")
    print(f"   Capital: {pos_size['capital']:,.0f}")
    print(f"   Qty: {pos_size['qty']}")
    print(f"   Kelly: {pos_size['kelly']:.2%}")
    
    # Test stop levels
    stops = trader.get_stop_levels(
        entry_price=80000,
        atr=2000,
        volatility=0.02,
        side="BUY",
    )
    print("\n4. Stop levels (entry=80,000, ATR=2,000):")
    print(f"   Stop Loss: {stops['stop_loss']:,.0f}")
    print(f"   TP1: {stops['tp1']:,.0f}")
    print(f"   TP2: {stops['tp2']:,.0f}")
    print(f"   TP3: {stops['tp3']:,.0f}")
    
    # Set market regime
    trader.set_market_regime("BULL")
    print(f"\n5. Market regime: {trader._market_regime}")
    
    # Get status
    status = trader.get_status()
    print("\n6. Status:")
    print(f"   Mode: {status['mode']}")
    print(f"   Running: {status['running']}")
    print(f"   Circuit Breaker: {status['circuit_breaker']['open']}")
    
    print("\n" + "=" * 60)
    print("Test Complete!")
    print("=" * 60)
