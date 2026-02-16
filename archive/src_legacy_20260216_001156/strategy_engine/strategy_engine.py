"""
Enterprise KIS Trading Bot - Strategy Engine
========================================

Advanced trading strategy engine with ORB+VWAP algorithms,
machine learning integration, and real-time market analysis.

Features:
- Enhanced ORB (Opening Range Breakout) with dynamic thresholds
- Advanced VWAP (Volume Weighted Average Price) calculations
- Machine learning prediction models
- Sentiment analysis integration
- Multi-timeframe analysis
- Adaptive risk-reward optimization
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections import deque
from enum import Enum
import talib
from sklearn.ensemble import RandomForestClassifier, GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler
import joblib
import torch
import torch.nn as nn

# Internal imports
from ..config.settings import Settings
from ..data_pipeline.data_pipeline import DataPipeline
from ..utils.logger import get_logger
from ..monitoring.metrics import MetricsCollector

logger = get_logger(__name__)

class SignalType(Enum):
    """거래 신호 타입"""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    EXIT = "EXIT"

@dataclass
class MarketData:
    """시장 데이터 모델"""
    symbol: str
    timestamp: datetime
    price: float
    volume: int
    bid_price: float
    ask_price: float
    open_price: float
    high_price: float
    low_price: float
    vwap: float = 0.0
    orb_high: float = 0.0
    orb_low: float = 0.0
    
    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price
    
    @property
    def mid_price(self) -> float:
        return (self.bid_price + self.ask_price) / 2

@dataclass
class TradingSignal:
    """트레이딩 신호"""
    symbol: str
    signal_type: SignalType
    strength: float  # 0.0 ~ 1.0
    price: float
    quantity: int
    confidence: float  # 0.0 ~ 1.0
    timestamp: datetime
    reason: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward_ratio: float = 0.0

class ORBCalculator:
    """ORB (Opening Range Breakout) 계산기"""
    
    def __init__(self, time_window_minutes: int = 30):
        self.time_window = timedelta(minutes=time_window_minutes)
        self.opening_times = {}  # symbol: open_time
        self.opening_ranges = {}  # symbol: (high, low)
    
    def update_opening_range(self, symbol: str, data: MarketData) -> Tuple[float, float]:
        """오프닝 레인지 업데이트"""
        current_time = data.timestamp.time()
        
        # 오프닝 타임 확인 (9:00 AM)
        if current_time.hour == 9 and current_time.minute < self.time_window.seconds // 60:
            self.opening_times[symbol] = data.timestamp
            self.opening_ranges[symbol] = (data.high_price, data.low_price)
            logger.debug(f"Updated opening range for {symbol}: {self.opening_ranges[symbol]}")
        
        # 오프닝 레인지 내 데이터 업데이트
        elif symbol in self.opening_times:
            time_since_open = data.timestamp - self.opening_times[symbol]
            if time_since_open <= self.time_window:
                current_high, current_low = self.opening_ranges[symbol]
                self.opening_ranges[symbol] = (
                    max(current_high, data.high_price),
                    min(current_low, data.low_price)
                )
        
        return self.opening_ranges.get(symbol, (0.0, 0.0))
    
    def calculate_orb_breakout(self, symbol: str, current_price: float) -> Optional[SignalType]:
        """ORB 브레이크아웃 계산"""
        if symbol not in self.opening_ranges:
            return None
        
        orb_high, orb_low = self.opening_ranges[symbol]
        
        # 상단 브레이크아웃
        if current_price > orb_high:
            return SignalType.BUY
        # 하단 브레이크아웃
        elif current_price < orb_low:
            return SignalType.SELL
        
        return SignalType.HOLD
    
    def get_orb_level(self, symbol: str) -> Tuple[float, float]:
        """ORB 레벨 반환"""
        return self.opening_ranges.get(symbol, (0.0, 0.0))

class VWAPCalculator:
    """VWAP (Volume Weighted Average Price) 계산기"""
    
    def __init__(self, rolling_window: int = 390):  # 1 trading day
        self.rolling_window = rolling_window
        self.price_volume_data = {}  # symbol: deque of (price, volume, timestamp)
    
    def calculate_vwap(self, symbol: str, data: MarketData) -> float:
        """VWAP 계산"""
        if symbol not in self.price_volume_data:
            self.price_volume_data[symbol] = deque(maxlen=self.rolling_window)
        
        # 데이터 추가
        typical_price = (data.high_price + data.low_price + data.close_price) / 3
        self.price_volume_data[symbol].append((typical_price, data.volume, data.timestamp))
        
        if len(self.price_volume_data[symbol]) == 0:
            return data.price
        
        # VWAP 계산
        total_pv = 0.0
        total_volume = 0
        
        for price, volume, timestamp in self.price_volume_data[symbol]:
            total_pv += price * volume
            total_volume += volume
        
        vwap = total_pv / total_volume if total_volume > 0 else data.price
        return vwap

class TechnicalIndicators:
    """기술적 지표 계산기"""
    
    @staticmethod
    def calculate_rsi(prices: pd.Series, period: int = 14) -> np.ndarray:
        """RSI 계산"""
        return talib.RSI(prices.values, timeperiod=period)
    
    @staticmethod
    def calculate_macd(prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """MACD 계산"""
        macd, macd_signal, macd_hist = talib.MACD(prices.values, fastperiod=fast, slowperiod=slow, signalperiod=signal)
        return macd, macd_signal, macd_hist
    
    @staticmethod
    def calculate_bollinger_bands(prices: pd.Series, period: int = 20, std_dev: float = 2) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """볼린저 밴드 계산"""
        upper, middle, lower = talib.BBANDS(prices.values, timeperiod=period, nbdevup=std_dev, nbdevdn=std_dev)
        return upper, middle, lower
    
    @staticmethod
    def calculate_stochastic(high: pd.Series, low: pd.Series, close: pd.Series, k_period: int = 14, d_period: int = 3) -> Tuple[np.ndarray, np.ndarray]:
        """스토캐스틱 오실레이터 계산"""
        slowk, slowd = talib.STOCH(high.values, low.values, close.values, fastk_period=k_period, slowk_period=d_period, slowd_period=d_period)
        return slowk, slowd
    
    @staticmethod
    def calculate_ema(prices: pd.Series, period: int) -> np.ndarray:
        """EMA 계산"""
        return talib.EMA(prices.values, timeperiod=period)
    
    @staticmethod
    def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> np.ndarray:
        """ATR (Average True Range) 계산"""
        return talib.ATR(high.values, low.values, close.values, timeperiod=period)

class MLPredictor(nn.Module):
    """딥러닝 예측 모델"""
    
    def __init__(self, input_size: int, hidden_size: int = 128, num_layers: int = 3, output_size: int = 3):
        super().__init__()
        
        layers = []
        current_size = input_size
        
        for i in range(num_layers):
            layers.append(nn.Linear(current_size, hidden_size))
            layers.append(nn.ReLU())
            layers.append(nn.Dropout(0.2))
            current_size = hidden_size
        
        layers.append(nn.Linear(current_size, output_size))
        layers.append(nn.Softmax(dim=1))
        
        self.model = nn.Sequential(*layers)
    
    def forward(self, x):
        return self.model(x)

class MachineLearningModels:
    """머신러닝 모델 관리자"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.price_classifier = RandomForestClassifier(n_estimators=100, random_state=42)
        self.volatility_regressor = GradientBoostingRegressor(n_estimators=100, random_state=42)
        self.mlp_model = None
        self.scaler = StandardScaler()
        self.model_trained = False
        
    def extract_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """피처 추출"""
        features = pd.DataFrame()
        
        # 기본 피처
        features['returns'] = df['close'].pct_change()
        features['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
        features['price_change'] = df['close'] - df['open']
        features['high_low_ratio'] = df['high'] / df['low']
        features['close_open_ratio'] = df['close'] / df['open']
        
        # 기술적 지표
        features['rsi'] = TechnicalIndicators.calculate_rsi(df['close'])
        features['ema_20'] = TechnicalIndicators.calculate_ema(df['close'], 20)
        features['ema_50'] = TechnicalIndicators.calculate_ema(df['close'], 50)
        
        macd, macd_signal, macd_hist = TechnicalIndicators.calculate_macd(df['close'])
        features['macd'] = macd
        features['macd_signal'] = macd_signal
        features['macd_hist'] = macd_hist
        
        upper, middle, lower = TechnicalIndicators.calculate_bollinger_bands(df['close'])
        features['bb_upper'] = upper
        features['bb_middle'] = middle
        features['bb_lower'] = lower
        features['bb_width'] = (upper - lower) / middle
        
        features['atr'] = TechnicalIndicators.calculate_atr(df['high'], df['low'], df['close'])
        
        # 시간 기반 피처
        features['hour'] = pd.to_datetime(df.index).hour
        features['day_of_week'] = pd.to_datetime(df.index).dayofweek
        
        return features.dropna()
    
    def train_models(self, historical_data: pd.DataFrame):
        """모델 학습"""
        try:
            # 피처 추출
            features = self.extract_features(historical_data)
            
            if len(features) < 100:
                logger.warning("Insufficient data for training")
                return
            
            # 타겟 변수 생성 (다음날 가격 방향)
            target = np.where(historical_data['close'].shift(-1) > historical_data['close'], 1, 0)
            target = target[-len(features):]  # 길이 맞추기
            
            # 데이터 분리
            split_idx = int(len(features) * 0.8)
            X_train, X_test = features[:split_idx], features[split_idx:]
            y_train, y_test = target[:split_idx], target[split_idx:]
            
            # 스케일링
            X_train_scaled = self.scaler.fit_transform(X_train)
            X_test_scaled = self.scaler.transform(X_test)
            
            # 랜덤 포레스트 학습
            self.price_classifier.fit(X_train_scaled, y_train)
            rf_score = self.price_classifier.score(X_test_scaled, y_test)
            logger.info(f"Random Forest accuracy: {rf_score:.3f}")
            
            # 변동성 예측 모델 학습
            volatility_target = historical_data['close'].pct_change().abs().rolling(5).mean()
            volatility_target = volatility_target[-len(features):]
            
            self.volatility_regressor.fit(X_train_scaled, volatility_target[:split_idx])
            
            # 딥러닝 모델 학습
            self.mlp_model = MLPredictor(input_size=X_train_scaled.shape[1])
            criterion = nn.CrossEntropyLoss()
            optimizer = torch.optim.Adam(self.mlp_model.parameters(), lr=0.001)
            
            X_tensor = torch.FloatTensor(X_train_scaled)
            y_tensor = torch.LongTensor(y_train)
            
            for epoch in range(100):
                optimizer.zero_grad()
                outputs = self.mlp_model(X_tensor)
                loss = criterion(outputs, y_tensor)
                loss.backward()
                optimizer.step()
                
                if epoch % 20 == 0:
                    logger.debug(f"Epoch {epoch}, Loss: {loss.item():.4f}")
            
            self.model_trained = True
            logger.info("Machine learning models trained successfully")
            
        except Exception as e:
            logger.error(f"Failed to train models: {e}")
    
    def predict_signal(self, recent_data: pd.DataFrame) -> Tuple[SignalType, float]:
        """신호 예측"""
        if not self.model_trained:
            return SignalType.HOLD, 0.5
        
        try:
            features = self.extract_features(recent_data)
            if len(features) == 0:
                return SignalType.HOLD, 0.5
            
            latest_features = features.iloc[-1:].values
            scaled_features = self.scaler.transform(latest_features)
            
            # 랜덤 포레스트 예측
            rf_pred = self.price_classifier.predict_proba(scaled_features)[0]
            
            # 딥러닝 예측
            with torch.no_grad():
                mlp_pred = self.mlp_model(torch.FloatTensor(scaled_features)).numpy()[0]
            
            # 앙상블 예측
            ensemble_pred = 0.6 * rf_pred + 0.4 * mlp_pred
            
            if ensemble_pred[1] > 0.6:
                return SignalType.BUY, ensemble_pred[1]
            elif ensemble_pred[0] > 0.6:
                return SignalType.SELL, ensemble_pred[0]
            else:
                return SignalType.HOLD, max(ensemble_pred)
                
        except Exception as e:
            logger.error(f"Prediction failed: {e}")
            return SignalType.HOLD, 0.5
    
    def save_models(self, path: str):
        """모델 저장"""
        try:
            joblib.dump(self.price_classifier, f"{path}/price_classifier.pkl")
            joblib.dump(self.volatility_regressor, f"{path}/volatility_regressor.pkl")
            joblib.dump(self.scaler, f"{path}/scaler.pkl")
            
            if self.mlp_model:
                torch.save(self.mlp_model.state_dict(), f"{path}/mlp_model.pth")
            
            logger.info(f"Models saved to {path}")
            
        except Exception as e:
            logger.error(f"Failed to save models: {e}")
    
    def load_models(self, path: str):
        """모델 로드"""
        try:
            self.price_classifier = joblib.load(f"{path}/price_classifier.pkl")
            self.volatility_regressor = joblib.load(f"{path}/volatility_regressor.pkl")
            self.scaler = joblib.load(f"{path}/scaler.pkl")
            
            self.mlp_model = MLPredictor(input_size=self.scaler.n_features_in_)
            self.mlp_model.load_state_dict(torch.load(f"{path}/mlp_model.pth"))
            
            self.model_trained = True
            logger.info(f"Models loaded from {path}")
            
        except Exception as e:
            logger.error(f"Failed to load models: {e}")

class SentimentAnalyzer:
    """감성 분석기"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.news_cache = {}
        self.sentiment_scores = {}
    
    def analyze_news_sentiment(self, symbol: str, news_data: List[Dict[str, Any]]) -> float:
        """뉴스 감성 분석"""
        try:
            total_score = 0.0
            count = 0
            
            for news in news_data:
                if symbol in news.get('symbols', []):
                    score = news.get('sentiment_score', 0.0)
                    total_score += score
                    count += 1
            
            average_score = total_score / count if count > 0 else 0.0
            self.sentiment_scores[symbol] = average_score
            
            return average_score
            
        except Exception as e:
            logger.error(f"Sentiment analysis failed: {e}")
            return 0.0
    
    def get_sentiment_signal(self, symbol: str) -> SignalType:
        """감성 기반 신호"""
        sentiment = self.sentiment_scores.get(symbol, 0.0)
        
        if sentiment > self.settings.NEWS_SENTIMENT_THRESHOLD:
            return SignalType.BUY
        elif sentiment < -self.settings.NEWS_SENTIMENT_THRESHOLD:
            return SignalType.SELL
        else:
            return SignalType.HOLD

class StrategyEngine:
    """전략 엔진 메인"""
    
    def __init__(self, settings: Settings, data_pipeline: DataPipeline):
        self.settings = settings
        self.data_pipeline = data_pipeline
        self.metrics = MetricsCollector()
        
        # 계산기들
        self.orb_calculator = ORBCalculator(time_window_minutes=settings.ORB_TIME_WINDOW // 60)
        self.vwap_calculator = VWAPCalculator()
        self.technical_indicators = TechnicalIndicators()
        
        # ML 모델
        self.ml_models = MachineLearningModels(settings)
        self.sentiment_analyzer = SentimentAnalyzer(settings)
        
        # 데이터 저장
        self.market_data_history = {}  # symbol: deque of MarketData
        self.active_positions = {}  # symbol: position info
        
        # 전략 파라미터
        self.min_signal_strength = 0.6
        self.max_positions_per_symbol = 3
        self.risk_reward_ratio = 2.0
        
    async def initialize(self):
        """전략 엔진 초기화"""
        try:
            # 과거 데이터 로드 및 모델 학습
            for symbol in ['005930', '000660', '051910']:  # 샘플 종목들
                historical_data = await self.data_pipeline.get_historical_data(
                    symbol, 
                    datetime.now() - timedelta(days=365),
                    datetime.now()
                )
                
                if not historical_data.empty:
                    self.ml_models.train_models(historical_data)
                    break  # 하나라도 학습되면 충분
            
            logger.info("Strategy engine initialized")
            
        except Exception as e:
            logger.error(f"Failed to initialize strategy engine: {e}")
    
    async def process_market_data(self, data: Dict[str, Any]) -> List[TradingSignal]:
        """시장 데이터 처리 및 신호 생성"""
        try:
            # MarketData 객체 생성
            market_data = MarketData(
                symbol=data['symbol'],
                timestamp=datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00')),
                price=data['price'],
                volume=data['volume'],
                bid_price=data['bid_price'],
                ask_price=data['ask_price'],
                open_price=data.get('open_price', data['price']),
                high_price=data.get('high_price', data['price']),
                low_price=data.get('low_price', data['price'])
            )
            
            # 기술적 지표 계산
            market_data.vwap = self.vwap_calculator.calculate_vwap(market_data.symbol, market_data)
            market_data.orb_high, market_data.orb_low = self.orb_calculator.update_opening_range(
                market_data.symbol, market_data
            )
            
            # 데이터 저장
            if market_data.symbol not in self.market_data_history:
                self.market_data_history[market_data.symbol] = deque(maxlen=1000)
            self.market_data_history[market_data.symbol].append(market_data)
            
            # 신호 생성
            signals = []
            
            # ORB 신호
            orb_signal = self._generate_orb_signal(market_data)
            if orb_signal:
                signals.append(orb_signal)
            
            # VWAP 신호
            vwap_signal = self._generate_vwap_signal(market_data)
            if vwap_signal:
                signals.append(vwap_signal)
            
            # ML 신호
            ml_signal = await self._generate_ml_signal(market_data)
            if ml_signal:
                signals.append(ml_signal)
            
            # 감성 분석 신호
            sentiment_signal = await self._generate_sentiment_signal(market_data.symbol)
            if sentiment_signal:
                signals.append(sentiment_signal)
            
            # 신호 종합
            final_signal = self._combine_signals(signals)
            
            if final_signal:
                logger.info(f"Generated signal: {final_signal.signal_type.value} for {final_signal.symbol} with strength {final_signal.strength:.3f}")
                self.metrics.increment_trading_placed()
                return [final_signal]
            
            return []
            
        except Exception as e:
            logger.error(f"Error processing market data: {e}")
            return []
    
    def _generate_orb_signal(self, data: MarketData) -> Optional[TradingSignal]:
        """ORB 신호 생성"""
        try:
            orb_breakout = self.orb_calculator.calculate_orb_breakout(data.symbol, data.price)
            
            if orb_breakout in [SignalType.BUY, SignalType.SELL]:
                strength = 0.7
                
                # 리스크 관리
                stop_loss = data.orb_low if orb_breakout == SignalType.BUY else data.orb_high
                take_profit = data.price + (data.price - stop_loss) * self.risk_reward_ratio
                
                return TradingSignal(
                    symbol=data.symbol,
                    signal_type=orb_breakout,
                    strength=strength,
                    price=data.price,
                    quantity=100,  # 기본 수량
                    confidence=0.8,
                    timestamp=data.timestamp,
                    reason=f"ORB Breakout: {orb_breakout.value}",
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    risk_reward_ratio=self.risk_reward_ratio
                )
            
            return None
            
        except Exception as e:
            logger.error(f"ORB signal generation failed: {e}")
            return None
    
    def _generate_vwap_signal(self, data: MarketData) -> Optional[TradingSignal]:
        """VWAP 신호 생성"""
        try:
            if data.vwap == 0:
                return None
            
            # VWAP 대비 현재가 위치
            vwap_ratio = (data.price - data.vwap) / data.vwap
            
            # 스프레드 확인
            spread_ratio = data.spread / data.price
            if spread_ratio > 0.001:  # 0.1% 이상 스프레드는 무시
                return None
            
            if abs(vwap_ratio) > 0.02:  # 2% 이상 VWAP에서 벗어난 경우
                signal_type = SignalType.BUY if vwap_ratio > 0 else SignalType.SELL
                strength = min(abs(vwap_ratio) * 10, 0.8)  # 최대 0.8
                
                return TradingSignal(
                    symbol=data.symbol,
                    signal_type=signal_type,
                    strength=strength,
                    price=data.price,
                    quantity=100,
                    confidence=0.6,
                    timestamp=data.timestamp,
                    reason=f"VWAP Deviation: {vwap_ratio:.3f}",
                    stop_loss=data.vwap * (0.98 if signal_type == SignalType.BUY else 1.02),
                    take_profit=data.vwap * (1.04 if signal_type == SignalType.BUY else 0.96),
                    risk_reward_ratio=2.0
                )
            
            return None
            
        except Exception as e:
            logger.error(f"VWAP signal generation failed: {e}")
            return None
    
    async def _generate_ml_signal(self, data: MarketData) -> Optional[TradingSignal]:
        """머신러닝 신호 생성"""
        try:
            # 최근 데이터 준비
            if data.symbol not in self.market_data_history or len(self.market_data_history[data.symbol]) < 50:
                return None
            
            # DataFrame으로 변환
            recent_data_list = list(self.market_data_history[data.symbol])[-50:]
            df_data = []
            
            for item in recent_data_list:
                df_data.append({
                    'timestamp': item.timestamp,
                    'open': item.open_price,
                    'high': item.high_price,
                    'low': item.low_price,
                    'close': item.price,
                    'volume': item.volume
                })
            
            df = pd.DataFrame(df_data)
            df.set_index('timestamp', inplace=True)
            
            # ML 예측
            signal_type, confidence = self.ml_models.predict_signal(df)
            
            if signal_type in [SignalType.BUY, SignalType.SELL] and confidence > 0.65:
                return TradingSignal(
                    symbol=data.symbol,
                    signal_type=signal_type,
                    strength=confidence,
                    price=data.price,
                    quantity=150,  # ML 신호는 조금 더 적은 수량
                    confidence=confidence,
                    timestamp=data.timestamp,
                    reason=f"ML Prediction: {confidence:.3f}",
                    stop_loss=data.price * 0.98 if signal_type == SignalType.BUY else data.price * 1.02,
                    take_profit=data.price * 1.03 if signal_type == SignalType.BUY else data.price * 0.97,
                    risk_reward_ratio=1.5
                )
            
            return None
            
        except Exception as e:
            logger.error(f"ML signal generation failed: {e}")
            return None
    
    async def _generate_sentiment_signal(self, symbol: str) -> Optional[TradingSignal]:
        """감성 분석 신호 생성"""
        try:
            # 감성 신호 가져오기
            sentiment_signal = self.sentiment_analyzer.get_sentiment_signal(symbol)
            
            if sentiment_signal in [SignalType.BUY, SignalType.SELL]:
                # 최신 시장 데이터 확인
                if symbol in self.market_data_history and self.market_data_history[symbol]:
                    latest_data = self.market_data_history[symbol][-1]
                    
                    return TradingSignal(
                        symbol=symbol,
                        signal_type=sentiment_signal,
                        strength=0.5,  # 감성은 보조 신호
                        price=latest_data.price,
                        quantity=50,  # 감성은 적은 수량
                        confidence=0.4,
                        timestamp=latest_data.timestamp,
                        reason="News Sentiment Analysis",
                        stop_loss=latest_data.price * 0.97 if sentiment_signal == SignalType.BUY else latest_data.price * 1.03,
                        take_profit=latest_data.price * 1.02 if sentiment_signal == SignalType.BUY else latest_data.price * 0.98,
                        risk_reward_ratio=1.0
                    )
            
            return None
            
        except Exception as e:
            logger.error(f"Sentiment signal generation failed: {e}")
            return None
    
    def _combine_signals(self, signals: List[TradingSignal]) -> Optional[TradingSignal]:
        """여러 신호 종합"""
        if not signals:
            return None
        
        # 신호 타입별로 그룹화
        buy_signals = [s for s in signals if s.signal_type == SignalType.BUY]
        sell_signals = [s for s in signals if s.signal_type == SignalType.SELL]
        
        # 가장 강력한 신호 선택
        if buy_signals and sell_signals:
            # 매수/매도 신호가 모두 있는 경우, 강도와 신뢰도가 높은 쪽 선택
            buy_strength = sum(s.strength * s.confidence for s in buy_signals) / len(buy_signals)
            sell_strength = sum(s.strength * s.confidence for s in sell_signals) / len(sell_signals)
            
            if buy_strength > sell_strength:
                signals_to_combine = buy_signals
                final_signal_type = SignalType.BUY
            else:
                signals_to_combine = sell_signals
                final_signal_type = SignalType.SELL
        elif buy_signals:
            signals_to_combine = buy_signals
            final_signal_type = SignalType.BUY
        elif sell_signals:
            signals_to_combine = sell_signals
            final_signal_type = SignalType.SELL
        else:
            return None
        
        # 신호 종합
        avg_strength = np.mean([s.strength for s in signals_to_combine])
        avg_confidence = np.mean([s.confidence for s in signals_to_combine])
        combined_quantity = int(np.mean([s.quantity for s in signals_to_combine]))
        
        # 가장 강력한 신호의 기준으로 가격 설정
        primary_signal = max(signals_to_combine, key=lambda s: s.strength * s.confidence)
        
        return TradingSignal(
            symbol=primary_signal.symbol,
            signal_type=final_signal_type,
            strength=min(avg_strength, 0.9),
            price=primary_signal.price,
            quantity=combined_quantity,
            confidence=avg_confidence,
            timestamp=datetime.now(),
            reason=f"Combined: {len(signals_to_combine)} signals - {', '.join([s.reason.split(':')[0] for s in signals_to_combine])}",
            stop_loss=primary_signal.stop_loss,
            take_profit=primary_signal.take_profit,
            risk_reward_ratio=primary_signal.risk_reward_ratio
        )
    
    def get_strategy_performance(self) -> Dict[str, Any]:
        """전략 성과 조회"""
        return {
            'active_positions': len(self.active_positions),
            'tracked_symbols': len(self.market_data_history),
            'model_trained': self.ml_models.model_trained,
            'metrics_summary': self.metrics.get_summary()
        }