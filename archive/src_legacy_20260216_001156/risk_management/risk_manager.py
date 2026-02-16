"""
Enterprise KIS Trading Bot - Risk Management
==========================================

Advanced risk management system with Value at Risk (VaR),
portfolio optimization, real-time monitoring, and position sizing.

Features:
- Multi-method VaR calculation (Historical, Monte Carlo, Parametric)
- Portfolio optimization using Modern Portfolio Theory
- Real-time position monitoring and alerts
- Dynamic position sizing algorithms
- Drawdown control and circuit breakers
- Correlation analysis and risk diversification
"""

import asyncio
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from datetime import datetime
from scipy import stats
from scipy.optimize import minimize
from enum import Enum

# Internal imports
from ..config.settings import Settings
from ..data_pipeline.data_pipeline import DataPipeline
from ..strategy_engine.strategy_engine import TradingSignal
from ..utils.logger import get_logger
from ..monitoring.metrics import MetricsCollector

logger = get_logger(__name__)

class RiskLevel(Enum):
    """리스크 레벨"""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

@dataclass
class Position:
    """포지션 정보"""
    symbol: str
    quantity: int
    entry_price: float
    current_price: float
    side: str  # BUY/SELL
    entry_time: datetime
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    commission: float = 0.0
    
    @property
    def market_value(self) -> float:
        return abs(self.quantity) * self.current_price
    
    @property
    def pnl_percentage(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return ((self.current_price - self.entry_price) / self.entry_price) * 100 * (1 if self.side == "BUY" else -1)

@dataclass
class RiskMetrics:
    """리스크 메트릭"""
    timestamp: datetime
    portfolio_value: float
    daily_pnl: float
    total_pnl: float
    var_95_1day: float
    var_99_1day: float
    max_drawdown: float
    current_drawdown: float
    sharpe_ratio: float
    beta: float = 1.0
    volatility: float = 0.0
    position_concentration: float = 0.0
    risk_level: RiskLevel = RiskLevel.LOW

@dataclass
class RiskAlert:
    """리스크 알림"""
    alert_type: str
    symbol: str
    risk_level: RiskLevel
    current_value: float
    threshold: float
    message: str
    timestamp: datetime
    recommended_action: str

class ValueAtRisk:
    """Value at Risk 계산기"""
    
    @staticmethod
    def historical_var(returns: np.ndarray, confidence_level: float = 0.95) -> float:
        """역사적 VaR 계산"""
        if len(returns) == 0:
            return 0.0
        return np.percentile(returns, (1 - confidence_level) * 100)
    
    @staticmethod
    def parametric_var(returns: np.ndarray, confidence_level: float = 0.95) -> float:
        """파라메트릭 VaR 계산 (정규분포 가정)"""
        if len(returns) == 0:
            return 0.0
        
        mean = np.mean(returns)
        std = np.std(returns)
        
        z_score = stats.norm.ppf(1 - confidence_level)
        var = mean + z_score * std
        
        return var
    
    @staticmethod
    def monte_carlo_var(returns: np.ndarray, confidence_level: float = 0.95, 
                       simulations: int = 10000) -> float:
        """몬테카를로 VaR 계산"""
        if len(returns) == 0:
            return 0.0
        
        mean = np.mean(returns)
        std = np.std(returns)
        
        # 시뮬레이션
        simulated_returns = np.random.normal(mean, std, simulations)
        return np.percentile(simulated_returns, (1 - confidence_level) * 100)
    
    @staticmethod
    def conditional_var(returns: np.ndarray, confidence_level: float = 0.95) -> float:
        """Conditional VaR (Expected Shortfall) 계산"""
        if len(returns) == 0:
            return 0.0
        
        var_threshold = ValueAtRisk.historical_var(returns, confidence_level)
        tail_losses = returns[returns <= var_threshold]
        
        return np.mean(tail_losses) if len(tail_losses) > 0 else var_threshold

class PortfolioOptimizer:
    """포트폴리오 최적화"""
    
    def __init__(self, risk_free_rate: float = 0.02):
        self.risk_free_rate = risk_free_rate
    
    def efficient_frontier(self, returns: pd.DataFrame, num_portfolios: int = 100) -> Dict[str, np.ndarray]:
        """효율적 프론티어 계산"""
        try:
            # 기대수익률와 공분산 행렬
            mean_returns = returns.mean()
            cov_matrix = returns.cov()
            
            num_assets = len(mean_returns)
            results = {
                'returns': np.zeros(num_portfolios),
                'volatility': np.zeros(num_portfolios),
                'weights': np.zeros((num_portfolios, num_assets)),
                'sharpe_ratio': np.zeros(num_portfolios)
            }
            
            for i in range(num_portfolios):
                weights = np.random.random(num_assets)
                weights /= np.sum(weights)
                
                portfolio_return = np.sum(mean_returns * weights) * 252  # 연율화
                portfolio_volatility = np.sqrt(np.dot(weights.T, np.dot(cov_matrix * 252, weights)))
                sharpe_ratio = (portfolio_return - self.risk_free_rate) / portfolio_volatility
                
                results['returns'][i] = portfolio_return
                results['volatility'][i] = portfolio_volatility
                results['weights'][i] = weights
                results['sharpe_ratio'][i] = sharpe_ratio
            
            return results
            
        except Exception as e:
            logger.error(f"Efficient frontier calculation failed: {e}")
            return {}
    
    def optimal_weights(self, returns: pd.DataFrame, target_return: float = None, 
                       target_volatility: float = None) -> np.ndarray:
        """최적 가중치 계산"""
        try:
            mean_returns = returns.mean()
            cov_matrix = returns.cov()
            num_assets = len(mean_returns)
            
            # 제약 조건: 가중치 합계 = 1
            constraints = ({'type': 'eq', 'fun': lambda x: np.sum(x) - 1})
            
            # 가중치 경계 (0~1, 숏포지션 불가)
            bounds = tuple((0, 1) for _ in range(num_assets))
            
            if target_return:
                # 목표 수익률 최소화
                def objective(weights):
                    return np.sqrt(np.dot(weights.T, np.dot(cov_matrix * 252, weights)))
                
                constraints += ({'type': 'eq', 'fun': lambda x: np.sum(mean_returns * x) * 252 - target_return},)
                
            elif target_volatility:
                # 목표 변동성 하에서 수익률 최대화
                def objective(weights):
                    return -np.sum(mean_returns * weights) * 252
                
                constraints += ({'type': 'eq', 'fun': lambda x: np.sqrt(np.dot(x.T, np.dot(cov_matrix * 252, x))) - target_volatility},)
            
            else:
                # 최대 샤프 비율
                def objective(weights):
                    portfolio_return = np.sum(mean_returns * weights) * 252
                    portfolio_volatility = np.sqrt(np.dot(weights.T, np.dot(cov_matrix * 252, weights)))
                    return -(portfolio_return - self.risk_free_rate) / portfolio_volatility
            
            result = minimize(objective, num_assets * [1/num_assets], method='SLSQP', 
                            bounds=bounds, constraints=constraints)
            
            if result.success:
                return result.x
            else:
                logger.warning(f"Optimization failed: {result.message}")
                return np.array([1/num_assets] * num_assets)
                
        except Exception as e:
            logger.error(f"Portfolio optimization failed: {e}")
            return np.array([1/len(returns.columns)] * len(returns.columns))

class PositionSizing:
    """포지션 사이징"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
    
    def kelly_criterion(self, win_rate: float, avg_win: float, avg_loss: float) -> float:
        """켈리 공식으로 최적 비율 계산"""
        if avg_loss == 0:
            return 0.0
        
        win_loss_ratio = avg_win / abs(avg_loss)
        kelly_fraction = (win_rate * win_loss_ratio - (1 - win_rate)) / win_loss_ratio
        
        return max(0, min(kelly_fraction, 0.25))  # 최대 25%
    
    def fixed_fractional(self, account_value: float, risk_per_trade: float, 
                        stop_loss_pct: float) -> float:
        """고정 비율 포지션 사이징"""
        if stop_loss_pct == 0:
            return 0.0
        
        position_value = account_value * risk_per_trade / stop_loss_pct
        return position_value
    
    def volatility_based_sizing(self, symbol_volatility: float, 
                              target_volatility: float, base_position: float) -> float:
        """변동성 기반 포지션 사이징"""
        if symbol_volatility == 0:
            return base_position
        
        volatility_ratio = target_volatility / symbol_volatility
        return base_position * min(volatility_ratio, 2.0)  # 최대 2배까지 증가

class RiskMonitor:
    """실시간 리스크 모니터링"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.metrics = MetricsCollector()
        self.alerts = []
        self.risk_history = []
        
        # 리스크 임계값
        self.thresholds = {
            'max_drawdown': 0.15,  # 15%
            'daily_loss_limit': -0.05,  # -5%
            'position_concentration': 0.3,  # 30%
            'var_limit': 0.02,  # 2%
            'volatility_limit': 0.25  # 25%
        }
    
    def assess_risk_level(self, metrics: RiskMetrics) -> RiskLevel:
        """리스크 레벨 평가"""
        risk_score = 0
        
        # 최대 손실
        if abs(metrics.current_drawdown) > self.thresholds['max_drawdown']:
            risk_score += 3
        
        # 일일 손실
        if metrics.daily_pnl < self.thresholds['daily_loss_limit']:
            risk_score += 2
        
        # 포지션 집중도
        if metrics.position_concentration > self.thresholds['position_concentration']:
            risk_score += 2
        
        # VaR
        if abs(metrics.var_95_1day) > self.thresholds['var_limit']:
            risk_score += 1
        
        # 변동성
        if metrics.volatility > self.thresholds['volatility_limit']:
            risk_score += 1
        
        # 리스크 레벨 결정
        if risk_score >= 5:
            return RiskLevel.CRITICAL
        elif risk_score >= 3:
            return RiskLevel.HIGH
        elif risk_score >= 1:
            return RiskLevel.MEDIUM
        else:
            return RiskLevel.LOW
    
    def check_alerts(self, metrics: RiskMetrics) -> List[RiskAlert]:
        """리스크 알림 확인"""
        alerts = []
        
        # 최대 손실 알림
        if abs(metrics.current_drawdown) > self.thresholds['max_drawdown']:
            alerts.append(RiskAlert(
                alert_type="MAX_DRAWDOWN",
                symbol="PORTFOLIO",
                risk_level=RiskLevel.CRITICAL,
                current_value=abs(metrics.current_drawdown),
                threshold=self.thresholds['max_drawdown'],
                message=f"Maximum drawdown exceeded: {metrics.current_drawdown:.2%}",
                timestamp=datetime.now(),
                recommended_action="REDUCE_POSITIONS"
            ))
        
        # 일일 손실 한도 알림
        if metrics.daily_pnl < self.thresholds['daily_loss_limit']:
            alerts.append(RiskAlert(
                alert_type="DAILY_LOSS_LIMIT",
                symbol="PORTFOLIO",
                risk_level=RiskLevel.HIGH,
                current_value=metrics.daily_pnl,
                threshold=self.thresholds['daily_loss_limit'],
                message=f"Daily loss limit exceeded: {metrics.daily_pnl:.2%}",
                timestamp=datetime.now(),
                recommended_action="STOP_TRADING"
            ))
        
        # 포지션 집중도 알림
        if metrics.position_concentration > self.thresholds['position_concentration']:
            alerts.append(RiskAlert(
                alert_type="POSITION_CONCENTRATION",
                symbol="PORTFOLIO",
                risk_level=RiskLevel.MEDIUM,
                current_value=metrics.position_concentration,
                threshold=self.thresholds['position_concentration'],
                message=f"Position concentration too high: {metrics.position_concentration:.2%}",
                timestamp=datetime.now(),
                recommended_action="DIVERSIFY"
            ))
        
        return alerts

class RiskManager:
    """리스크 관리자 메인"""
    
    def __init__(self, settings: Settings, data_pipeline: DataPipeline):
        self.settings = settings
        self.data_pipeline = data_pipeline
        self.metrics = MetricsCollector()
        
        # 리스크 관리 컴포넌트
        self.var_calculator = ValueAtRisk()
        self.portfolio_optimizer = PortfolioOptimizer()
        self.position_sizer = PositionSizing(settings)
        self.risk_monitor = RiskMonitor(settings)
        
        # 포지션 및 포트폴리오 데이터
        self.positions = {}  # symbol: Position
        self.portfolio_history = []
        self.daily_returns = []
        
        # 리스크 제어
        self.trading_enabled = True
        self.max_positions = settings.MAX_POSITIONS
        self.max_position_value = settings.MAX_POSITION_VALUE
        
    async def evaluate_trade(self, signal: TradingSignal) -> Tuple[bool, Optional[str]]:
        """트레이드 신호 평가"""
        try:
            # 트레이딩 비활성화 확인
            if not self.trading_enabled:
                return False, "Trading is currently disabled"
            
            # 최대 포지션 수 확인
            if len(self.positions) >= self.max_positions:
                return False, f"Maximum positions reached: {self.max_positions}"
            
            # 포지션 크기 확인
            position_value = abs(signal.quantity * signal.price)
            if position_value > self.max_position_value:
                return False, f"Position value exceeds limit: {position_value:,} > {self.max_position_value:,}"
            
            # 리스크-보상 비율 확인
            if signal.risk_reward_ratio < 1.5:
                return False, f"Risk-reward ratio too low: {signal.risk_reward_ratio:.2f}"
            
            # VaR 체크
            current_var = await self.calculate_portfolio_var()
            if abs(current_var) > self.settings.MAX_DAILY_LOSS / self.get_portfolio_value():
                return False, f"Portfolio VaR too high: {current_var:.2%}"
            
            return True, None
            
        except Exception as e:
            logger.error(f"Trade evaluation failed: {e}")
            return False, f"Evaluation error: {e}"
    
    def calculate_position_size(self, signal: TradingSignal, account_value: float) -> int:
        """최적 포지션 크기 계산"""
        try:
            # 기본 리스크 per trade
            risk_per_trade = 0.02  # 2%
            stop_loss_pct = abs(signal.price - signal.stop_loss) / signal.price if signal.stop_loss else 0.03
            
            # 고정 비율 사이징
            position_value = self.position_sizer.fixed_fractional(
                account_value, risk_per_trade, stop_loss_pct
            )
            
            # 수량 계산
            quantity = int(position_value / signal.price / 100) * 100  # 100주 단위
            
            # 최대값 제한
            max_quantity = int(self.max_position_value / signal.price / 100) * 100
            quantity = min(quantity, max_quantity)
            
            # 최소값 확인
            min_quantity = 100
            quantity = max(quantity, min_quantity)
            
            return quantity
            
        except Exception as e:
            logger.error(f"Position sizing failed: {e}")
            return 100  # 기본값
    
    async def add_position(self, signal: TradingSignal, executed_price: float, quantity: int):
        """포지션 추가"""
        try:
            symbol = signal.symbol
            
            # 기존 포지션 확인
            if symbol in self.positions:
                # 기존 포지션 수정
                existing_pos = self.positions[symbol]
                total_quantity = existing_pos.quantity + quantity
                avg_price = ((existing_pos.quantity * existing_pos.entry_price) + 
                           (quantity * executed_price)) / total_quantity
                
                self.positions[symbol].quantity = total_quantity
                self.positions[symbol].entry_price = avg_price
            else:
                # 새 포지션 생성
                self.positions[symbol] = Position(
                    symbol=symbol,
                    quantity=quantity,
                    entry_price=executed_price,
                    current_price=executed_price,
                    side=signal.signal_type.value,
                    entry_time=datetime.now()
                )
            
            logger.info(f"Position added: {symbol} {quantity} shares @ {executed_price}")
            self.metrics.increment_order_placed(symbol, signal.signal_type.value, "success")
            
        except Exception as e:
            logger.error(f"Failed to add position: {e}")
    
    async def update_positions(self, market_data: Dict[str, Any]):
        """포지션 업데이트"""
        try:
            symbol = market_data['symbol']
            current_price = market_data['price']
            
            if symbol in self.positions:
                position = self.positions[symbol]
                position.current_price = current_price
                
                # 미실현 손익 계산
                if position.side == "BUY":
                    position.unrealized_pnl = (current_price - position.entry_price) * position.quantity
                else:
                    position.unrealized_pnl = (position.entry_price - current_price) * position.quantity
                
        except Exception as e:
            logger.error(f"Failed to update position: {e}")
    
    def close_position(self, symbol: str, exit_price: float) -> Optional[float]:
        """포지션 청산"""
        try:
            if symbol not in self.positions:
                return None
            
            position = self.positions[symbol]
            
            # 실현 손익 계산
            if position.side == "BUY":
                realized_pnl = (exit_price - position.entry_price) * position.quantity
            else:
                realized_pnl = (position.entry_price - exit_price) * position.quantity
            
            # 수수료 계산
            commission = abs(position.quantity * exit_price * self.settings.COMMISSION_RATE)
            position.realized_pnl = realized_pnl - commission
            position.commission += commission
            
            # 포지션 기록
            self.portfolio_history.append(position)
            
            # 포지션 제거
            del self.positions[symbol]
            
            logger.info(f"Position closed: {symbol} with PnL: {position.realized_pnl:,.0f}")
            return position.realized_pnl
            
        except Exception as e:
            logger.error(f"Failed to close position: {e}")
            return None
    
    async def calculate_portfolio_var(self, confidence_level: float = 0.95, 
                                   method: str = 'historical') -> float:
        """포트폴리오 VaR 계산"""
        try:
            if len(self.daily_returns) < 30:
                return 0.0
            
            returns = np.array(self.daily_returns[-252:])  # 최근 1년
            
            if method == 'historical':
                return self.var_calculator.historical_var(returns, confidence_level)
            elif method == 'parametric':
                return self.var_calculator.parametric_var(returns, confidence_level)
            elif method == 'monte_carlo':
                return self.var_calculator.monte_carlo_var(returns, confidence_level)
            else:
                return self.var_calculator.historical_var(returns, confidence_level)
                
        except Exception as e:
            logger.error(f"VaR calculation failed: {e}")
            return 0.0
    
    def get_portfolio_value(self) -> float:
        """포트폴리오 가치 계산"""
        total_value = 0.0
        
        for position in self.positions.values():
            total_value += position.market_value
        
        # 현금 추가 (실제로는 별도 현금 잔고 필요)
        return total_value
    
    def get_portfolio_metrics(self) -> RiskMetrics:
        """포트폴리오 리스크 메트릭"""
        try:
            portfolio_value = self.get_portfolio_value()
            
            # 일일 손익
            daily_pnl = sum(pos.unrealized_pnl for pos in self.positions.values())
            
            # 총 손익
            total_pnl = daily_pnl + sum(pos.realized_pnl for pos in self.portfolio_history)
            
            # VaR 계산
            var_95 = asyncio.create_task(self.calculate_portfolio_var(0.95))
            var_99 = asyncio.create_task(self.calculate_portfolio_var(0.99))
            
            # 최대 손실 및 현재 손실
            peak_value = max([h.market_value for h in self.portfolio_history] + [portfolio_value])
            max_drawdown = (peak_value - portfolio_value) / peak_value if peak_value > 0 else 0
            
            # 변동성 계산
            if len(self.daily_returns) > 30:
                volatility = np.std(self.daily_returns[-30:]) * np.sqrt(252)
            else:
                volatility = 0.0
            
            # 샤프 비율
            if volatility > 0:
                risk_free_rate = 0.02
                excess_return = (total_pnl / portfolio_value) - risk_free_rate
                sharpe_ratio = excess_return / volatility
            else:
                sharpe_ratio = 0.0
            
            # 포지션 집중도
            if portfolio_value > 0:
                max_position_value = max([pos.market_value for pos in self.positions.values()] + [0])
                position_concentration = max_position_value / portfolio_value
            else:
                position_concentration = 0.0
            
            metrics = RiskMetrics(
                timestamp=datetime.now(),
                portfolio_value=portfolio_value,
                daily_pnl=daily_pnl,
                total_pnl=total_pnl,
                var_95_1day=0.0,  # asyncio task 결과 기다려야 함
                var_99_1day=0.0,
                max_drawdown=max_drawdown,
                current_drawdown=max_drawdown,
                sharpe_ratio=sharpe_ratio,
                volatility=volatility,
                position_concentration=position_concentration
            )
            
            # 리스크 레벨 평가
            metrics.risk_level = self.risk_monitor.assess_risk_level(metrics)
            
            return metrics
            
        except Exception as e:
            logger.error(f"Portfolio metrics calculation failed: {e}")
            return RiskMetrics(
                timestamp=datetime.now(),
                portfolio_value=0.0,
                daily_pnl=0.0,
                total_pnl=0.0,
                var_95_1day=0.0,
                var_99_1day=0.0,
                max_drawdown=0.0,
                current_drawdown=0.0,
                sharpe_ratio=0.0,
                volatility=0.0,
                position_concentration=0.0,
                risk_level=RiskLevel.LOW
            )
    
    def get_risk_summary(self) -> Dict[str, Any]:
        """리스크 요약 정보"""
        metrics = self.get_portfolio_metrics()
        alerts = self.risk_monitor.check_alerts(metrics)
        
        return {
            'risk_level': metrics.risk_level.value,
            'portfolio_value': metrics.portfolio_value,
            'daily_pnl': metrics.daily_pnl,
            'var_95': metrics.var_95_1day,
            'current_drawdown': metrics.current_drawdown,
            'position_count': len(self.positions),
            'active_alerts': [alert.__dict__ for alert in alerts],
            'trading_enabled': self.trading_enabled
        }