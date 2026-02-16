"""
Enterprise KIS Trading Bot - Data Pipeline
=====================================

High-performance data processing pipeline with Apache Kafka, Redis,
and ClickHouse for real-time trading data ingestion, processing, and storage.

Features:
- Multi-source data ingestion (market data, trading orders, news)
- Real-time stream processing with Kafka
- Caching layer with Redis
- Time-series analytics with ClickHouse
- Data quality validation and monitoring
- Backpressure handling and flow control
"""

import asyncio
import json
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from collections import deque
import pandas as pd

# Kafka imports
from kafka import KafkaConsumer, KafkaProducer

# Redis imports
import redis.asyncio as redis
import redis.exceptions

# ClickHouse imports
import clickhouse_connect
from clickhouse_connect.driver import Client

# Internal imports
from ..config.settings import Settings
from ..utils.logger import get_logger
from ..monitoring.metrics import MetricsCollector

logger = get_logger(__name__)

@dataclass
class MarketDataMessage:
    """시장 데이터 메시지"""
    symbol: str
    timestamp: datetime
    price: float
    volume: int
    bid_price: float
    ask_price: float
    open_price: float
    high_price: float
    low_price: float
    market_type: str
    trading_volume: float
    change_rate: float
    
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        return data

@dataclass
class TradingOrderMessage:
    """트레이딩 주문 메시지"""
    order_id: str
    symbol: str
    side: str  # BUY/SELL
    order_type: str
    quantity: int
    price: Optional[float]
    status: str
    timestamp: datetime
    user_id: str
    
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        return data

@dataclass
class NewsDataMessage:
    """뉴스 데이터 메시지"""
    news_id: str
    title: str
    content: str
    symbols: List[str]
    sentiment_score: float
    timestamp: datetime
    source: str
    
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        return data

class DataValidator:
    """데이터 유효성 검사기"""
    
    @staticmethod
    def validate_market_data(message: Dict[str, Any]) -> bool:
        """시장 데이터 유효성 검사"""
        required_fields = ['symbol', 'timestamp', 'price', 'volume']
        
        for field in required_fields:
            if field not in message:
                logger.error(f"Missing required field: {field}")
                return False
        
        # 가격 유효성 검사
        if message['price'] <= 0:
            logger.error(f"Invalid price: {message['price']}")
            return False
        
        # 거래량 유효성 검사
        if message['volume'] < 0:
            logger.error(f"Invalid volume: {message['volume']}")
            return False
        
        # 시간 유효성 검사
        try:
            timestamp = datetime.fromisoformat(message['timestamp'].replace('Z', '+00:00'))
            if abs((datetime.utcnow() - timestamp).total_seconds()) > 300:  # 5분 이상 과거 데이터
                logger.warning(f"Old timestamp: {timestamp}")
        except:
            logger.error(f"Invalid timestamp format: {message['timestamp']}")
            return False
        
        return True
    
    @staticmethod
    def validate_trading_order(message: Dict[str, Any]) -> bool:
        """트레이딩 주문 유효성 검사"""
        required_fields = ['order_id', 'symbol', 'side', 'order_type', 'quantity']
        
        for field in required_fields:
            if field not in message:
                logger.error(f"Missing required field: {field}")
                return False
        
        # 주문 타입 유효성 검사
        if message['side'] not in ['BUY', 'SELL']:
            logger.error(f"Invalid side: {message['side']}")
            return False
        
        # 수량 유효성 검사
        if message['quantity'] <= 0:
            logger.error(f"Invalid quantity: {message['quantity']}")
            return False
        
        return True

class KafkaManager:
    """Kafka 관리자"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.producer: Optional[KafkaProducer] = None
        self.consumers: Dict[str, KafkaConsumer] = {}
        self.metrics = MetricsCollector()
    
    def create_producer(self) -> KafkaProducer:
        """Kafka 프로듀서 생성"""
        if not self.producer:
            self.producer = KafkaProducer(
                bootstrap_servers=self.settings.KAFKA_BOOTSTRAP_SERVERS,
                value_serializer=lambda x: json.dumps(x, default=str).encode('utf-8'),
                key_serializer=lambda x: x.encode('utf-8') if x else None,
                acks='all',
                retries=3,
                batch_size=16384,
                linger_ms=10,
                buffer_memory=33554432,
                compression_type='gzip',
                max_in_flight_requests_per_connection=1,
                enable_idempotence=True
            )
            logger.info("Kafka producer created")
        
        return self.producer
    
    def create_consumer(self, topic: str, group_id: str) -> KafkaConsumer:
        """Kafka 컨슈머 생성"""
        if topic not in self.consumers:
            self.consumers[topic] = KafkaConsumer(
                topic,
                bootstrap_servers=self.settings.KAFKA_BOOTSTRAP_SERVERS,
                group_id=group_id,
                value_deserializer=lambda x: json.loads(x.decode('utf-8')),
                key_deserializer=lambda x: x.decode('utf-8') if x else None,
                auto_offset_reset='latest',
                enable_auto_commit=True,
                auto_commit_interval_ms=1000,
                session_timeout_ms=30000,
                heartbeat_interval_ms=3000,
                max_poll_records=100,
                max_poll_interval_ms=300000,
                consumer_timeout_ms=1000
            )
            logger.info(f"Kafka consumer created for topic: {topic}")
        
        return self.consumers[topic]
    
    async def send_message(self, topic: str, message: Dict[str, Any], key: str = None):
        """메시지 전송"""
        try:
            producer = self.create_producer()
            future = producer.send(topic, value=message, key=key)
            
            # 비동기 전송 완료 대기
            record_metadata = await asyncio.get_event_loop().run_in_executor(
                None, future.get
            )
            
            self.metrics.record_request_success(0.001)  # Kafka send 성공
            logger.debug(f"Message sent to {topic}: partition={record_metadata.partition}, offset={record_metadata.offset}")
            
        except Exception as e:
            self.metrics.record_request_error()
            logger.error(f"Failed to send message to {topic}: {e}")
            raise
    
    async def consume_messages(self, topic: str, group_id: str, 
                             callback: Callable[[Dict[str, Any]], None]):
        """메시지 수집 및 처리"""
        try:
            consumer = self.create_consumer(topic, group_id)
            
            for message in consumer:
                try:
                    await callback(message.value)
                    logger.debug(f"Processed message from {topic}")
                    
                except Exception as e:
                    logger.error(f"Error processing message from {topic}: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Error consuming from {topic}: {e}")
            raise
    
    def close(self):
        """리소스 정리"""
        if self.producer:
            self.producer.close()
        
        for consumer in self.consumers.values():
            consumer.close()
        
        logger.info("Kafka manager closed")

class RedisManager:
    """Redis 관리자"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Optional[redis.Redis] = None
        self.metrics = MetricsCollector()
    
    async def connect(self):
        """Redis 연결"""
        if not self.client:
            self.client = redis.from_url(
                self.settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                retry_on_timeout=True,
                socket_keepalive=True,
                socket_keepalive_options={},
                health_check_interval=30
            )
            
            # 연결 테스트
            await self.client.ping()
            logger.info("Redis connected")
    
    async def set_with_ttl(self, key: str, value: Any, ttl: int = 3600):
        """TTL과 함께 데이터 저장"""
        try:
            await self.connect()
            await self.client.setex(key, ttl, json.dumps(value, default=str))
            logger.debug(f"Stored {key} with TTL {ttl}s")
            
        except Exception as e:
            self.metrics.record_request_error()
            logger.error(f"Failed to store {key}: {e}")
            raise
    
    async def get(self, key: str) -> Optional[Any]:
        """데이터 조회"""
        try:
            await self.connect()
            data = await self.client.get(key)
            
            if data:
                return json.loads(data)
            return None
            
        except Exception as e:
            self.metrics.record_request_error()
            logger.error(f"Failed to get {key}: {e}")
            return None
    
    async def delete(self, key: str):
        """데이터 삭제"""
        try:
            await self.connect()
            await self.client.delete(key)
            logger.debug(f"Deleted {key}")
            
        except Exception as e:
            self.metrics.record_request_error()
            logger.error(f"Failed to delete {key}: {e}")
    
    async def get_market_data_latest(self, symbol: str) -> Optional[Dict[str, Any]]:
        """최신 시장 데이터 조회"""
        key = f"market_data:{symbol}"
        return await self.get(key)
    
    async def set_market_data_latest(self, symbol: str, data: Dict[str, Any], ttl: int = 60):
        """최신 시장 데이터 저장"""
        key = f"market_data:{symbol}"
        await self.set_with_ttl(key, data, ttl)
    
    async def get_order_status(self, order_id: str) -> Optional[str]:
        """주문 상태 조회"""
        key = f"order_status:{order_id}"
        result = await self.get(key)
        return result.get('status') if result else None
    
    async def set_order_status(self, order_id: str, status: str, ttl: int = 86400):
        """주문 상태 저장"""
        key = f"order_status:{order_id}"
        await self.set_with_ttl(key, {'status': status, 'timestamp': datetime.now().isoformat()}, ttl)
    
    async def get_portfolio_cache(self, user_id: str) -> Optional[Dict[str, Any]]:
        """포트폴리오 캐시 조회"""
        key = f"portfolio:{user_id}"
        return await self.get(key)
    
    async def set_portfolio_cache(self, user_id: str, portfolio_data: Dict[str, Any], ttl: int = 300):
        """포트폴리오 캐시 저장"""
        key = f"portfolio:{user_id}"
        await self.set_with_ttl(key, portfolio_data, ttl)
    
    async def close(self):
        """연결 종료"""
        if self.client:
            await self.client.close()
        logger.info("Redis manager closed")

class ClickHouseManager:
    """ClickHouse 관리자"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Optional[Client] = None
        self.metrics = MetricsCollector()
    
    def connect(self):
        """ClickHouse 연결"""
        if not self.client:
            self.client = clickhouse_connect.get_client(
                host=self.settings.CLICKHOUSE_HOST,
                port=self.settings.CLICKHOUSE_PORT,
                username=self.settings.CLICKHOUSE_USER,
                password=self.settings.CLICKHOUSE_PASSWORD,
                database=self.settings.CLICKHOUSE_DB,
                connect_timeout=10,
                send_receive_timeout=30,
                compress=True,
                query_retries=3
            )
            
            # 테이블 생성
            self._create_tables()
            logger.info("ClickHouse connected")
    
    def _create_tables(self):
        """테이블 생성"""
        try:
            # 시장 데이터 테이블
            self.client.command("""
                CREATE TABLE IF NOT EXISTS market_data (
                    symbol String,
                    timestamp DateTime64(3),
                    price Float64,
                    volume UInt64,
                    bid_price Float64,
                    ask_price Float64,
                    open_price Float64,
                    high_price Float64,
                    low_price Float64,
                    market_type String,
                    trading_volume Float64,
                    change_rate Float64,
                    created_date Date MATERIALIZED toDate(timestamp)
                ) ENGINE = MergeTree()
                PARTITION BY toYYYYMM(timestamp)
                ORDER BY (symbol, timestamp)
                SETTINGS index_granularity = 8192
            """)
            
            # 주문 데이터 테이블
            self.client.command("""
                CREATE TABLE IF NOT EXISTS trading_orders (
                    order_id String,
                    symbol String,
                    side String,
                    order_type String,
                    quantity UInt64,
                    price Nullable(Float64),
                    status String,
                    timestamp DateTime64(3),
                    user_id String,
                    created_date Date MATERIALIZED toDate(timestamp)
                ) ENGINE = MergeTree()
                PARTITION BY toYYYYMM(timestamp)
                ORDER BY (order_id, timestamp)
                SETTINGS index_granularity = 8192
            """)
            
            # 뉴스 데이터 테이블
            self.client.command("""
                CREATE TABLE IF NOT EXISTS news_data (
                    news_id String,
                    title String,
                    content String,
                    symbols Array(String),
                    sentiment_score Float64,
                    timestamp DateTime64(3),
                    source String,
                    created_date Date MATERIALIZED toDate(timestamp)
                ) ENGINE = MergeTree()
                PARTITION BY toYYYYMM(timestamp)
                ORDER BY (news_id, timestamp)
                SETTINGS index_granularity = 8192
            """)
            
            logger.info("ClickHouse tables created/verified")
            
        except Exception as e:
            logger.error(f"Failed to create tables: {e}")
            raise
    
    def insert_market_data(self, data: List[MarketDataMessage]):
        """시장 데이터 삽입"""
        try:
            self.connect()
            
            # 데이터프레임으로 변환
            df_data = []
            for msg in data:
                msg_dict = msg.to_dict()
                df_data.append({
                    'symbol': msg_dict['symbol'],
                    'timestamp': msg_dict['timestamp'],
                    'price': msg_dict['price'],
                    'volume': msg_dict['volume'],
                    'bid_price': msg_dict['bid_price'],
                    'ask_price': msg_dict['ask_price'],
                    'open_price': msg_dict['open_price'],
                    'high_price': msg_dict['high_price'],
                    'low_price': msg_dict['low_price'],
                    'market_type': msg_dict['market_type'],
                    'trading_volume': msg_dict['trading_volume'],
                    'change_rate': msg_dict['change_rate']
                })
            
            df = pd.DataFrame(df_data)
            self.client.insert_df('market_data', df)
            
            self.metrics.record_request_success(0.01)
            logger.debug(f"Inserted {len(data)} market data records")
            
        except Exception as e:
            self.metrics.record_request_error()
            logger.error(f"Failed to insert market data: {e}")
            raise
    
    def insert_trading_orders(self, data: List[TradingOrderMessage]):
        """주문 데이터 삽입"""
        try:
            self.connect()
            
            df_data = []
            for msg in data:
                msg_dict = msg.to_dict()
                df_data.append({
                    'order_id': msg_dict['order_id'],
                    'symbol': msg_dict['symbol'],
                    'side': msg_dict['side'],
                    'order_type': msg_dict['order_type'],
                    'quantity': msg_dict['quantity'],
                    'price': msg_dict['price'],
                    'status': msg_dict['status'],
                    'timestamp': msg_dict['timestamp'],
                    'user_id': msg_dict['user_id']
                })
            
            df = pd.DataFrame(df_data)
            self.client.insert_df('trading_orders', df)
            
            self.metrics.record_request_success(0.01)
            logger.debug(f"Inserted {len(data)} trading order records")
            
        except Exception as e:
            self.metrics.record_request_error()
            logger.error(f"Failed to insert trading orders: {e}")
            raise
    
    def query_market_data(self, symbol: str, start_time: datetime, 
                         end_time: datetime) -> pd.DataFrame:
        """시장 데이터 조회"""
        try:
            self.connect()
            
            query = f"""
                SELECT *
                FROM market_data
                WHERE symbol = '{symbol}'
                  AND timestamp BETWEEN '{start_time}' AND '{end_time}'
                ORDER BY timestamp
            """
            
            result = self.client.query_df(query)
            return result
            
        except Exception as e:
            self.metrics.record_request_error()
            logger.error(f"Failed to query market data: {e}")
            return pd.DataFrame()
    
    def get_symbol_statistics(self, symbol: str, days: int = 30) -> Dict[str, Any]:
        """종목 통계 정보 조회"""
        try:
            self.connect()
            
            start_time = datetime.now() - timedelta(days=days)
            
            query = f"""
                SELECT
                    count() as total_records,
                    min(price) as min_price,
                    max(price) as max_price,
                    avg(price) as avg_price,
                    sum(volume) as total_volume,
                    avg(change_rate) as avg_change_rate
                FROM market_data
                WHERE symbol = '{symbol}'
                  AND timestamp >= '{start_time}'
            """
            
            result = self.client.query(query)
            return result.first_row if result else {}
            
        except Exception as e:
            self.metrics.record_request_error()
            logger.error(f"Failed to get symbol statistics: {e}")
            return {}
    
    def close(self):
        """연결 종료"""
        if self.client:
            self.client.close()
        logger.info("ClickHouse manager closed")

class DataPipeline:
    """데이터 파이프라인 메니저"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.kafka_manager = KafkaManager(settings)
        self.redis_manager = RedisManager(settings)
        self.clickhouse_manager = ClickHouseManager(settings)
        self.validator = DataValidator()
        self.metrics = MetricsCollector()
        
        # 데이터 버퍼
        self.market_data_buffer = deque(maxlen=1000)
        self.order_data_buffer = deque(maxlen=100)
        self.news_data_buffer = deque(maxlen=500)
        
        # 백그라운드 태스크
        self.background_tasks = set()
        self.running = False
    
    async def start(self):
        """데이터 파이프라인 시작"""
        if self.running:
            return
        
        self.running = True
        
        # Kafka 컨슈머 시작
        for topic in [self.settings.KAFKA_TOPIC_MARKET_DATA, 
                     self.settings.KAFKA_TOPIC_TRADING_ORDERS,
                     'news_data']:
            task = asyncio.create_task(
                self.kafka_manager.consume_messages(
                    topic, f"data_pipeline_{topic}", self._process_message
                )
            )
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
        
        # 데이터 배치 처리 시작
        task = asyncio.create_task(self._batch_processor())
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        
        logger.info("Data pipeline started")
    
    async def stop(self):
        """데이터 파이프라인 중지"""
        self.running = False
        
        # 백그라운드 태스크 취소
        for task in self.background_tasks:
            task.cancel()
        
        # 리소스 정리
        self.kafka_manager.close()
        await self.redis_manager.close()
        self.clickhouse_manager.close()
        
        logger.info("Data pipeline stopped")
    
    async def _process_message(self, message: Dict[str, Any]):
        """메시지 처리"""
        try:
            # 메시지 타입 확인 및 검증
            if 'price' in message and 'symbol' in message:
                # 시장 데이터
                if self.validator.validate_market_data(message):
                    self.market_data_buffer.append(MarketDataMessage(**message))
                    await self.redis_manager.set_market_data_latest(
                        message['symbol'], message, ttl=60
                    )
                    self.metrics.increment_market_data_received(message['symbol'])
                    
            elif 'order_id' in message and 'side' in message:
                # 주문 데이터
                if self.validator.validate_trading_order(message):
                    self.order_data_buffer.append(TradingOrderMessage(**message))
                    await self.redis_manager.set_order_status(
                        message['order_id'], message['status']
                    )
                    self.metrics.increment_order_placed(
                        message['symbol'], message['side'], message['status']
                    )
                    
            elif 'news_id' in message and 'title' in message:
                # 뉴스 데이터
                self.news_data_buffer.append(NewsDataMessage(**message))
                
        except Exception as e:
            logger.error(f"Error processing message: {e}")
    
    async def _batch_processor(self):
        """데이터 배치 처리"""
        while self.running:
            try:
                # 시장 데이터 배치 처리
                if len(self.market_data_buffer) >= 100:
                    batch = [self.market_data_buffer.popleft() for _ in range(min(100, len(self.market_data_buffer)))]
                    self.clickhouse_manager.insert_market_data(batch)
                
                # 주문 데이터 배치 처리
                if len(self.order_data_buffer) >= 50:
                    batch = [self.order_data_buffer.popleft() for _ in range(min(50, len(self.order_data_buffer)))]
                    self.clickhouse_manager.insert_trading_orders(batch)
                
                # 대기
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Error in batch processor: {e}")
                await asyncio.sleep(5)
    
    async def get_real_time_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """실시간 데이터 조회"""
        return await self.redis_manager.get_market_data_latest(symbol)
    
    async def get_historical_data(self, symbol: str, start_time: datetime, 
                                end_time: datetime) -> pd.DataFrame:
        """과거 데이터 조회"""
        return self.clickhouse_manager.query_market_data(symbol, start_time, end_time)
    
    async def get_symbol_statistics(self, symbol: str) -> Dict[str, Any]:
        """종목 통계 조회"""
        return self.clickhouse_manager.get_symbol_statistics(symbol)
    
    def get_pipeline_metrics(self) -> Dict[str, Any]:
        """파이프라인 메트릭 조회"""
        return {
            'buffer_sizes': {
                'market_data': len(self.market_data_buffer),
                'order_data': len(self.order_data_buffer),
                'news_data': len(self.news_data_buffer)
            },
            'metrics_summary': self.metrics.get_summary()
        }