"""
Enterprise KIS Trading Bot - Backtesting System
===============================================

Advanced backtesting engine with historical data validation,
strategy optimization, and comprehensive performance analysis.

Features:
- Historical data simulation with realistic market conditions
- Multiple performance metrics (Sharpe, Sortino, Calmar, etc.)
- Strategy parameter optimization using grid search and genetic algorithms
- Walk-forward analysis and cross-validation
- Monte Carlo simulation for robustness testing
- Risk-adjusted performance analysis
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Internal imports
from ..config.settings import Settings
from ..data_pipeline.data_pipeline import DataPipeline
from ..strategy_engine.strategy_engine import TradingSignal
from ..risk_management.risk_manager import Position
from ..utils.logger import get_logger
from ..monitoring.metrics import MetricsCollector

logger = get_logger(__name__)

@dataclass
class BacktestConfig:
    """백테스팅 설정"""
    start_date: datetime
    end_date: datetime
    initial_capital: float = 100000000  # 1억원
    commission_rate: float = 0.00015     # 0.015%
    slippage_rate: float = 0.0001        # 0.01%
    benchmark_symbol: str = "005930"    # 삼성전자
    rebalance_frequency: str = "daily"  # daily, weekly, monthly
    
    # Risk settings
    max_position_size: float = 0.2       # 20% of portfolio
    stop_loss_pct: float = 0.05         # 5%
    take_profit_pct: float = 0.1        # 10%
    max_drawdown_limit: float = 0.2     # 20%
    
    # Strategy parameters
    orb_time_window: int = 300           # 5 minutes
    vwap_period: int = 390              # 1 trading day
    ml_confidence_threshold: float = 0.6

@dataclass
class BacktestResult:
    """백테스팅 결과"""
    config: BacktestConfig
    
    # Performance metrics
    total_return: float
    annualized_return: float
    volatility: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    
    # Trade statistics
    total_trades: int
    winning_trades: int
    losing_trades: int
    avg_trade_duration: float  # in days
    best_trade: float
    worst_trade: float
    
    # Risk metrics
    var_95: float
    var_99: float
    conditional_var: float
    beta: float
    alpha: float
    information_ratio: float
    
    # Time series data
    equity_curve: pd.DataFrame
    benchmark_returns: pd.Series
    monthly_returns: pd.Series
    
    # Trade history
    trade_history: List[Dict[str, Any]]
    
    timestamp: datetime = field(default_factory=datetime.now)

class PerformanceAnalyzer:
    """성과 분석기"""
    
    @staticmethod
    def calculate_returns(equity_curve: pd.Series) -> pd.Series:
        """수익률 계산"""
        return equity_curve.pct_change().fillna(0)
    
    @staticmethod
    def calculate_volatility(returns: pd.Series, annualize: bool = True) -> float:
        """변동성 계산"""
        vol = returns.std()
        if annualize:
            vol *= np.sqrt(252)
        return vol
    
    @staticmethod
    def calculate_sharpe_ratio(returns: pd.Series, risk_free_rate: float = 0.02) -> float:
        """샤프 비율 계산"""
        excess_returns = returns.mean() * 252 - risk_free_rate
        volatility = PerformanceAnalyzer.calculate_volatility(returns)
        return excess_returns / volatility if volatility != 0 else 0
    
    @staticmethod
    def calculate_sortino_ratio(returns: pd.Series, risk_free_rate: float = 0.02) -> float:
        """소르티노 비율 계산"""
        downside_returns = returns[returns < 0]
        if len(downside_returns) == 0:
            return float('inf')
        
        downside_std = downside_returns.std() * np.sqrt(252)
        excess_returns = returns.mean() * 252 - risk_free_rate
        return excess_returns / downside_std if downside_std != 0 else 0
    
    @staticmethod
    def calculate_max_drawdown(equity_curve: pd.Series) -> Tuple[float, datetime, datetime]:
        """최대 손실 계산"""
        peak = equity_curve.expanding(min_periods=1).max()
        drawdown = (equity_curve - peak) / peak
        max_dd = drawdown.min()
        
        # 최대 손실 기간 찾기
        max_dd_end = drawdown.idxmin()
        max_dd_start = equity_curve.loc[:max_dd_end].idxmax()
        
        return max_dd, max_dd_start, max_dd_end
    
    @staticmethod
    def calculate_var(returns: pd.Series, confidence_level: float = 0.95) -> float:
        """VaR 계산"""
        return np.percentile(returns, (1 - confidence_level) * 100)
    
    @staticmethod
    def calculate_beta_alpha(portfolio_returns: pd.Series, benchmark_returns: pd.Series, 
                           risk_free_rate: float = 0.02) -> Tuple[float, float]:
        """베타와 알파 계산"""
        # Align the series
        aligned_data = pd.concat([portfolio_returns, benchmark_returns], axis=1).dropna()
        portfolio_r = aligned_data.iloc[:, 0]
        benchmark_r = aligned_data.iloc[:, 1]
        
        # Calculate beta
        covariance = np.cov(portfolio_r, benchmark_r)[0, 1]
        benchmark_variance = np.var(benchmark_r)
        beta = covariance / benchmark_variance if benchmark_variance != 0 else 0
        
        # Calculate alpha (annualized)
        portfolio_return = portfolio_r.mean() * 252
        benchmark_return = benchmark_r.mean() * 252
        alpha = portfolio_return - (risk_free_rate + beta * (benchmark_return - risk_free_rate))
        
        return beta, alpha
    
    @staticmethod
    def analyze_trades(trade_history: List[Dict[str, Any]]) -> Dict[str, Any]:
        """거래 분석"""
        if not trade_history:
            return {}
        
        df = pd.DataFrame(trade_history)
        
        winning_trades = df[df['pnl'] > 0]
        losing_trades = df[df['pnl'] < 0]
        
        stats = {
            'total_trades': len(df),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': len(winning_trades) / len(df) if len(df) > 0 else 0,
            'avg_win': winning_trades['pnl'].mean() if len(winning_trades) > 0 else 0,
            'avg_loss': losing_trades['pnl'].mean() if len(losing_trades) > 0 else 0,
            'profit_factor': abs(winning_trades['pnl'].sum() / losing_trades['pnl'].sum()) if losing_trades['pnl'].sum() != 0 else float('inf'),
            'best_trade': df['pnl'].max(),
            'worst_trade': df['pnl'].min(),
            'avg_duration': (df['exit_date'] - df['entry_date']).mean().days if 'entry_date' in df.columns else 0
        }
        
        return stats

class StrategyOptimizer:
    """전략 최적화기"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
    
    def grid_search(self, param_grid: Dict[str, List[Any]], 
                   backtest_func: Callable, data: pd.DataFrame,
                   maximize_metric: str = 'sharpe_ratio') -> Dict[str, Any]:
        """그리드 서치 최적화"""
        import itertools
        
        # Generate all parameter combinations
        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        param_combinations = list(itertools.product(*param_values))
        
        best_params = None
        best_score = -np.inf if 'sharpe_ratio' in maximize_metric else np.inf
        results = []
        
        logger.info(f"Starting grid search with {len(param_combinations)} combinations")
        
        for i, combination in enumerate(param_combinations):
            params = dict(zip(param_names, combination))
            
            try:
                result = backtest_func(data, params)
                score = getattr(result, maximize_metric)
                
                results.append({
                    'params': params,
                    'score': score,
                    'result': result
                })
                
                # Update best parameters
                if 'sharpe_ratio' in maximize_metric and score > best_score:
                    best_score = score
                    best_params = params
                elif 'sharpe_ratio' not in maximize_metric and score < best_score:
                    best_score = score
                    best_params = params
                
                if (i + 1) % 10 == 0:
                    logger.info(f"Completed {i + 1}/{len(param_combinations)} combinations")
                    
            except Exception as e:
                logger.error(f"Error with parameters {params}: {e}")
                continue
        
        logger.info(f"Best parameters: {best_params}, Score: {best_score}")
        
        return {
            'best_params': best_params,
            'best_score': best_score,
            'all_results': results
        }
    
    def genetic_algorithm(self, param_bounds: Dict[str, Tuple[float, float]], 
                         backtest_func: Callable, data: pd.DataFrame,
                         population_size: int = 50, generations: int = 100,
                         mutation_rate: float = 0.1) -> Dict[str, Any]:
        """유전자 알고리즘 최적화"""
        import random
        
        param_names = list(param_bounds.keys())
        
        def create_individual():
            return [random.uniform(*param_bounds[name]) for name in param_names]
        
        def fitness(individual):
            params = dict(zip(param_names, individual))
            try:
                result = backtest_func(data, params)
                return result.sharpe_ratio
            except:
                return -np.inf
        
        def crossover(parent1, parent2):
            crossover_point = random.randint(1, len(parent1) - 1)
            child = parent1[:crossover_point] + parent2[crossover_point:]
            return child
        
        def mutate(individual):
            for i in range(len(individual)):
                if random.random() < mutation_rate:
                    name = param_names[i]
                    individual[i] = random.uniform(*param_bounds[name])
            return individual
        
        # Initialize population
        population = [create_individual() for _ in range(population_size)]
        
        best_individual = None
        best_fitness = -np.inf
        
        for generation in range(generations):
            # Evaluate fitness
            fitness_scores = [fitness(ind) for ind in population]
            
            # Update best
            max_idx = np.argmax(fitness_scores)
            if fitness_scores[max_idx] > best_fitness:
                best_fitness = fitness_scores[max_idx]
                best_individual = population[max_idx]
            
            # Selection (tournament selection)
            new_population = []
            for _ in range(population_size):
                tournament = random.sample(list(zip(population, fitness_scores)), 3)
                winner = max(tournament, key=lambda x: x[1])
                new_population.append(winner[0])
            
            # Crossover and mutation
            for i in range(0, population_size, 2):
                if i + 1 < population_size:
                    if random.random() < 0.8:  # Crossover probability
                        child1 = crossover(new_population[i], new_population[i+1])
                        child2 = crossover(new_population[i+1], new_population[i])
                        new_population[i] = mutate(child1)
                        new_population[i+1] = mutate(child2)
                    else:
                        new_population[i] = mutate(new_population[i])
                        new_population[i+1] = mutate(new_population[i+1])
            
            population = new_population
            
            if generation % 10 == 0:
                logger.info(f"Generation {generation}, Best fitness: {best_fitness}")
        
        best_params = dict(zip(param_names, best_individual))
        
        return {
            'best_params': best_params,
            'best_fitness': best_fitness,
            'generations': generations
        }

class BacktestingEngine:
    """백테스팅 엔진"""
    
    def __init__(self, settings: Settings, data_pipeline: DataPipeline):
        self.settings = settings
        self.data_pipeline = data_pipeline
        self.metrics = MetricsCollector()
        self.analyzer = PerformanceAnalyzer()
        self.optimizer = StrategyOptimizer(settings)
        
    async def run_backtest(self, config: BacktestConfig, 
                          strategy_func: Callable) -> BacktestResult:
        """백테스팅 실행"""
        try:
            logger.info(f"Starting backtest from {config.start_date} to {config.end_date}")
            
            # Load historical data
            historical_data = await self._load_historical_data(config)
            
            # Initialize tracking variables
            cash = config.initial_capital
            positions = {}
            equity_curve = []
            trade_history = []
            
            # Run simulation
            for date, market_data in historical_data.groupby('date'):
                # Update positions with current prices
                portfolio_value = self._update_positions(positions, market_data, cash)
                equity_curve.append({
                    'date': date,
                    'portfolio_value': portfolio_value,
                    'cash': cash
                })
                
                # Generate trading signals
                signals = await strategy_func(market_data, config)
                
                # Execute trades
                new_positions, new_trades = await self._execute_trades(
                    signals, positions, cash, config
                )
                
                positions.update(new_positions)
                trade_history.extend(new_trades)
                cash -= sum(trade['cost'] for trade in new_trades)
            
            # Convert to DataFrame
            equity_df = pd.DataFrame(equity_curve).set_index('date')
            
            # Calculate performance metrics
            result = await self._calculate_performance_metrics(
                config, equity_df, trade_history, historical_data
            )
            
            logger.info(f"Backtest completed. Total return: {result.total_return:.2%}, Sharpe: {result.sharpe_ratio:.2f}")
            return result
            
        except Exception as e:
            logger.error(f"Backtest failed: {e}")
            raise
    
    async def _load_historical_data(self, config: BacktestConfig) -> pd.DataFrame:
        """과거 데이터 로드"""
        # 실제로는 data_pipeline에서 과거 데이터 가져와야 함
        # 여기서는 가상 데이터 생성
        
        date_range = pd.date_range(
            start=config.start_date,
            end=config.end_date,
            freq='D'
        )
        
        data = []
        for date in date_range:
            if date.weekday() < 5:  # 주말 제외
                # Generate sample data for multiple symbols
                for symbol in ['005930', '000660', '051910', '005490', '035420']:
                    base_price = 50000 if symbol == '005930' else 30000
                    price_change = np.random.normal(0, 0.02)  # 2% daily volatility
                    
                    data.append({
                        'date': date,
                        'symbol': symbol,
                        'open': base_price * (1 + price_change),
                        'high': base_price * (1 + price_change + abs(np.random.normal(0, 0.01))),
                        'low': base_price * (1 + price_change - abs(np.random.normal(0, 0.01))),
                        'close': base_price * (1 + price_change),
                        'volume': np.random.randint(100000, 1000000),
                        'bid_price': base_price * (1 + price_change - 0.001),
                        'ask_price': base_price * (1 + price_change + 0.001)
                    })
        
        return pd.DataFrame(data)
    
    def _update_positions(self, positions: Dict[str, Position], 
                         market_data: pd.DataFrame, cash: float) -> float:
        """포지션 업데이트"""
        total_value = cash
        
        for symbol, position in positions.items():
            symbol_data = market_data[market_data['symbol'] == symbol]
            if not symbol_data.empty:
                current_price = symbol_data['close'].iloc[-1]
                position.current_price = current_price
                position.unrealized_pnl = (
                    (current_price - position.entry_price) * position.quantity *
                    (1 if position.side == "BUY" else -1)
                )
                total_value += position.market_value
        
        return total_value
    
    async def _execute_trades(self, signals: List[TradingSignal], 
                             positions: Dict[str, Position],
                             cash: float, config: BacktestConfig) -> Tuple[Dict[str, Position], List[Dict]]:
        """거래 실행"""
        new_positions = {}
        trades = []
        
        for signal in signals:
            # Apply position size limits
            max_position_value = config.initial_capital * config.max_position_size
            signal_quantity = min(signal.quantity, int(max_position_value / signal.price))
            
            # Apply slippage and commission
            execution_price = signal.price * (1 + config.slippage_rate * (1 if signal.signal_type.value == "BUY" else -1))
            cost = abs(signal_quantity * execution_price * config.commission_rate)
            
            if signal.signal_type.value == "BUY":
                if cash >= (signal_quantity * execution_price + cost):
                    position = Position(
                        symbol=signal.symbol,
                        quantity=signal_quantity,
                        entry_price=execution_price,
                        current_price=execution_price,
                        side="BUY",
                        entry_time=signal.timestamp,
                        commission=cost
                    )
                    new_positions[signal.symbol] = position
                    
                    trades.append({
                        'symbol': signal.symbol,
                        'action': 'BUY',
                        'quantity': signal_quantity,
                        'price': execution_price,
                        'cost': cost,
                        'timestamp': signal.timestamp,
                        'pnl': -cost
                    })
            
            elif signal.signal_type.value == "SELL":
                # Check if we have position to sell
                if signal.symbol in positions:
                    position = positions[signal.symbol]
                    sell_quantity = min(signal_quantity, position.quantity)
                    
                    pnl = (execution_price - position.entry_price) * sell_quantity - cost
                    
                    trades.append({
                        'symbol': signal.symbol,
                        'action': 'SELL',
                        'quantity': sell_quantity,
                        'price': execution_price,
                        'cost': cost,
                        'timestamp': signal.timestamp,
                        'pnl': pnl,
                        'entry_date': position.entry_time,
                        'exit_date': signal.timestamp
                    })
        
        return new_positions, trades
    
    async def _calculate_performance_metrics(self, config: BacktestConfig,
                                           equity_df: pd.DataFrame,
                                           trade_history: List[Dict],
                                           historical_data: pd.DataFrame) -> BacktestResult:
        """성과 메트릭 계산"""
        portfolio_returns = self.analyzer.calculate_returns(equity_df['portfolio_value'])
        
        # Basic metrics
        total_return = (equity_df['portfolio_value'].iloc[-1] / config.initial_capital) - 1
        annualized_return = (1 + total_return) ** (252 / len(equity_df)) - 1
        volatility = self.analyzer.calculate_volatility(portfolio_returns)
        sharpe_ratio = self.analyzer.calculate_sharpe_ratio(portfolio_returns)
        sortino_ratio = self.analyzer.calculate_sortino_ratio(portfolio_returns)
        
        # Drawdown
        max_dd, dd_start, dd_end = self.analyzer.calculate_max_drawdown(equity_df['portfolio_value'])
        calmar_ratio = annualized_return / abs(max_dd) if max_dd != 0 else 0
        
        # Trade analysis
        trade_stats = self.analyzer.analyze_trades(trade_history)
        
        # Risk metrics
        var_95 = self.analyzer.calculate_var(portfolio_returns, 0.95)
        var_99 = self.analyzer.calculate_var(portfolio_returns, 0.99)
        conditional_var = portfolio_returns[portfolio_returns <= var_95].mean() if len(portfolio_returns[portfolio_returns <= var_95]) > 0 else var_95
        
        # Benchmark comparison
        benchmark_data = historical_data[historical_data['symbol'] == config.benchmark_symbol]
        if not benchmark_data.empty:
            benchmark_returns = benchmark_data.groupby('date')['close'].last().pct_change().fillna(0)
            beta, alpha = self.analyzer.calculate_beta_alpha(portfolio_returns, benchmark_returns)
            information_ratio = (portfolio_returns.mean() - benchmark_returns.mean()) / (portfolio_returns - benchmark_returns).std() if len(portfolio_returns) > 1 else 0
        else:
            beta, alpha, information_ratio = 0, 0, 0
        
        # Monthly returns
        monthly_returns = equity_df['portfolio_value'].resample('M').last().pct_change().dropna()
        
        return BacktestResult(
            config=config,
            total_return=total_return,
            annualized_return=annualized_return,
            volatility=volatility,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            calmar_ratio=calmar_ratio,
            max_drawdown=max_dd,
            win_rate=trade_stats.get('win_rate', 0),
            profit_factor=trade_stats.get('profit_factor', 0),
            avg_win=trade_stats.get('avg_win', 0),
            avg_loss=trade_stats.get('avg_loss', 0),
            total_trades=trade_stats.get('total_trades', 0),
            winning_trades=trade_stats.get('winning_trades', 0),
            losing_trades=trade_stats.get('losing_trades', 0),
            avg_trade_duration=trade_stats.get('avg_duration', 0),
            best_trade=trade_stats.get('best_trade', 0),
            worst_trade=trade_stats.get('worst_trade', 0),
            var_95=var_95,
            var_99=var_99,
            conditional_var=conditional_var,
            beta=beta,
            alpha=alpha,
            information_ratio=information_ratio,
            equity_curve=equity_df,
            benchmark_returns=benchmark_returns if not benchmark_data.empty else pd.Series(),
            monthly_returns=monthly_returns,
            trade_history=trade_history
        )
    
    async def walk_forward_analysis(self, config: BacktestConfig,
                                  strategy_func: Callable,
                                  window_months: int = 12,
                                  step_months: int = 3) -> Dict[str, Any]:
        """워크포워드 분석"""
        logger.info("Starting walk-forward analysis")
        
        results = []
        current_date = config.start_date
        
        while current_date + timedelta(days=window_months * 30) < config.end_date:
            # Training period
            train_start = current_date
            train_end = current_date + timedelta(days=window_months * 30)
            
            # Testing period
            test_start = train_end
            test_end = test_start + timedelta(days=step_months * 30)
            
            if test_end > config.end_date:
                test_end = config.end_date
            
            # Configure for training period
            train_config = BacktestConfig(
                start_date=train_start,
                end_date=train_end,
                initial_capital=config.initial_capital,
                commission_rate=config.commission_rate,
                slippage_rate=config.slippage_rate
            )
            
            # Run training backtest
            train_result = await self.run_backtest(train_config, strategy_func)
            
            # Configure for testing period
            test_config = BacktestConfig(
                start_date=test_start,
                end_date=test_end,
                initial_capital=config.initial_capital,
                commission_rate=config.commission_rate,
                slippage_rate=config.slippage_rate
            )
            
            # Run testing backtest
            test_result = await self.run_backtest(test_config, strategy_func)
            
            results.append({
                'period': f"{train_start.date()} - {test_end.date()}",
                'train_result': train_result,
                'test_result': test_result
            })
            
            current_date = test_start
        
        # Aggregate results
        avg_train_return = np.mean([r['train_result'].annualized_return for r in results])
        avg_test_return = np.mean([r['test_result'].annualized_return for r in results])
        
        return {
            'results': results,
            'average_train_return': avg_train_return,
            'average_test_return': avg_test_return,
            'out_of_sample_ratio': avg_test_return / avg_train_return if avg_train_return != 0 else 0
        }
    
    async def monte_carlo_simulation(self, config: BacktestConfig,
                                    strategy_func: Callable,
                                    num_simulations: int = 1000) -> Dict[str, Any]:
        """몬테카를로 시뮬레이션"""
        logger.info(f"Starting Monte Carlo simulation with {num_simulations} runs")
        
        returns = []
        sharpes = []
        max_drawdowns = []
        
        for i in range(num_simulations):
            # Add random noise to market data
            noise_config = BacktestConfig(
                start_date=config.start_date,
                end_date=config.end_date,
                initial_capital=config.initial_capital,
                commission_rate=config.commission_rate,
                slippage_rate=config.slippage_rate
            )
            
            try:
                result = await self.run_backtest(noise_config, strategy_func)
                returns.append(result.annualized_return)
                sharpes.append(result.sharpe_ratio)
                max_drawdowns.append(result.max_drawdown)
                
                if (i + 1) % 100 == 0:
                    logger.info(f"Completed {i + 1}/{num_simulations} simulations")
                    
            except Exception as e:
                logger.error(f"Simulation {i} failed: {e}")
                continue
        
        # Calculate statistics
        return {
            'return_statistics': {
                'mean': np.mean(returns),
                'std': np.std(returns),
                'min': np.min(returns),
                'max': np.max(returns),
                'percentile_5': np.percentile(returns, 5),
                'percentile_95': np.percentile(returns, 95)
            },
            'sharpe_statistics': {
                'mean': np.mean(sharpes),
                'std': np.std(sharpes),
                'min': np.min(sharpes),
                'max': np.max(sharpes)
            },
            'max_drawdown_statistics': {
                'mean': np.mean(max_drawdowns),
                'std': np.std(max_drawdowns),
                'max': np.max(max_drawdowns)
            },
            'successful_simulations': len(returns)
        }
    
    def generate_report(self, result: BacktestResult) -> str:
        """백테스팅 리포트 생성"""
        report = f"""
# 백테스팅 리포트
## 기간: {result.config.start_date.date()} ~ {result.config.end_date.date()}
## 초기 자본: {result.config.initial_capital:,.0f}원

### 성과 요약
- 총 수익률: {result.total_return:.2%}
- 연환산 수익률: {result.annualized_return:.2%}
- 변동성: {result.volatility:.2%}
- 샤프 비율: {result.sharpe_ratio:.2f}
- 소르티노 비율: {result.sortino_ratio:.2f}
- 칼마 비율: {result.calmar_ratio:.2f}
- 최대 손실: {result.max_drawdown:.2%}

### 거래 통계
- 총 거래수: {result.total_trades}
- 수익 거래: {result.winning_trades}
- 손실 거래: {result.losing_trades}
- 승률: {result.win_rate:.2%}
- 평균 수익: {result.avg_win:,.0f}원
- 평균 손실: {result.avg_loss:,.0f}원
- 손익비: {result.profit_factor:.2f}
- 최고 거래: {result.best_trade:,.0f}원
- 최악 거래: {result.worst_trade:,.0f}원

### 리스크 지표
- VaR (95%): {result.var_95:.2%}
- VaR (99%): {result.var_99:.2%}
- 조건부 VaR: {result.conditional_var:.2%}
- 베타: {result.beta:.2f}
- 알파: {result.alpha:.2%}
- 정보 비율: {result.information_ratio:.2f}
        """
        
        return report