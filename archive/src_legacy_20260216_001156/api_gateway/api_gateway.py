"""
Enterprise KIS Trading Bot - API Gateway
=============================

High-performance API Gateway for Korea Investment & Securities trading system.
Handles authentication, rate limiting, data collection, and message routing.

Features:
- Asynchronous HTTP/WebSocket connections
- Connection pooling and retry mechanisms
- Rate limiting and circuit breakers
- Message buffering and queuing
- Health monitoring and failover
- Performance metrics and logging
"""

import asyncio
import aiohttp
import json
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from collections import deque
import ssl

# Third-party imports
import redis.asyncio as redis
from kafka import KafkaProducer
import websockets
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

# Internal imports
from ..config.settings import Settings
from ..monitoring.metrics import MetricsCollector
from ..utils.logger import get_logger

logger = get_logger(__name__)

@dataclass
class ConnectionConfig:
    """API 연결 설정"""
    api_url: str
    ws_url: str
    app_key: str
    app_secret: str
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_expiry: Optional[datetime] = None
    
    # Connection parameters
    max_connections: int = 100
    timeout: float = 30.0
    retry_attempts: int = 3
    retry_delay: float = 1.0
    
    # Rate limiting
    requests_per_second: int = 10
    requests_per_minute: int = 200
    
class MarketDataMessage(BaseModel):
    """실시간 시장 데이터 메시지"""
    symbol: str = Field(..., description="종목 코드")
    price: float = Field(..., description="현재가")
    volume: int = Field(..., description="거래량")
    bid_price: float = Field(..., description="매수호가")
    ask_price: float = Field(..., description="매도호가")
    timestamp: datetime = Field(..., description="수신 시간")
    market_type: str = Field(..., description="시장구분")
    
class TradingRequest(BaseModel):
    """트레이딩 요청"""
    symbol: str = Field(..., description="종목 코드")
    side: str = Field(..., description="매매구분 (BUY/SELL)")
    order_type: str = Field(..., description="주문타입")
    quantity: int = Field(..., description="주문수량")
    price: Optional[float] = Field(None, description="주문가격")
    
class APIGateway:
    """
    KIS API Gateway - 엔터프라이즈급 API 연결 관리자
    """
    
    def __init__(self, config: ConnectionConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws_connections: Dict[str, websockets.WebSocketServerProtocol] = {}
        self.redis_client: Optional[redis.Redis] = None
        self.kafka_producer: Optional[KafkaProducer] = None
        
        # Rate limiting
        self.request_times = deque(maxlen=config.requests_per_minute)
        self.semaphore = asyncio.Semaphore(config.max_connections)
        
        # Circuit breaker
        self.failure_count = 0
        self.last_failure_time = None
        self.circuit_breaker_threshold = 5
        self.circuit_breaker_timeout = 60  # seconds
        
        # Message buffering
        self.message_buffer = asyncio.Queue(maxsize=1000)
        self.batch_size = 100
        self.batch_timeout = 1.0  # seconds
        
        # Metrics
        self.metrics = MetricsCollector()
        
        # Background tasks
        self.background_tasks = set()
        
    async def initialize(self):
        """API Gateway 초기화"""
        try:
            # HTTP 세션 설정
            connector = aiohttp.TCPConnector(
                limit=self.config.max_connections,
                limit_per_host=self.config.max_connections,
                ssl=ssl.create_default_context(),
                keepalive_timeout=30,
                enable_cleanup_closed=True
            )
            
            timeout = aiohttp.ClientTimeout(total=self.config.timeout)
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout
            )
            
            # Redis 연결
            self.redis_client = redis.from_url(
                self.settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True
            )
            
            # Kafka 프로듀서 설정
            self.kafka_producer = KafkaProducer(
                bootstrap_servers=self.settings.KAFKA_BOOTSTRAP_SERVERS,
                value_serializer=lambda x: json.dumps(x).encode('utf-8'),
                acks='all',
                retries=3,
                batch_size=self.batch_size,
                linger_ms=1000
            )
            
            # 인증 토큰 획득
            await self._refresh_access_token()
            
            # 백그라운드 태스크 시작
            task = asyncio.create_task(self._message_batcher())
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
            
            task = asyncio.create_task(self._health_monitor())
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
            
            logger.info("API Gateway initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize API Gateway: {e}")
            raise
    
    async def _refresh_access_token(self):
        """액세스 토큰 갱신"""
        if (self.config.token_expiry and 
            datetime.now() < self.config.token_expiry - timedelta(minutes=5)):
            return  # Token still valid
        
        try:
            url = f"{self.config.api_url}/oauth2/tokenP"
            payload = {
                "grant_type": "client_credentials",
                "appkey": self.config.app_key,
                "appsecret": self.config.app_secret
            }
            
            async with self.session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    self.config.access_token = data['access_token']
                    self.config.token_expiry = datetime.now() + timedelta(hours=24)
                    
                    # Redis에 토큰 저장
                    await self.redis_client.setex(
                        "kis_access_token",
                        86400,  # 24 hours
                        self.config.access_token
                    )
                    
                    logger.info("Access token refreshed successfully")
                else:
                    raise Exception(f"Token refresh failed: {response.status}")
                    
        except Exception as e:
            logger.error(f"Failed to refresh access token: {e}")
            raise
    
    async def _check_circuit_breaker(self) -> bool:
        """서킷 브레이커 상태 확인"""
        if self.failure_count >= self.circuit_breaker_threshold:
            if (self.last_failure_time and 
                time.time() - self.last_failure_time < self.circuit_breaker_timeout):
                return False  # Circuit breaker is open
            else:
                # Reset circuit breaker
                self.failure_count = 0
                self.last_failure_time = None
        return True  # Circuit breaker is closed
    
    async def _rate_limit(self):
        """레이트 리미팅"""
        now = time.time()
        self.request_times.append(now)
        
        # Check per-second rate
        recent_requests = [t for t in self.request_times if now - t < 1.0]
        if len(recent_requests) >= self.config.requests_per_second:
            sleep_time = 1.0 - (now - recent_requests[0])
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
        
        # Check per-minute rate
        if len(self.request_times) >= self.config.requests_per_minute:
            sleep_time = 60.0 - (now - self.request_times[0])
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
    
    async def make_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """API 요청 실행"""
        async with self.semaphore:
            if not await self._check_circuit_breaker():
                raise HTTPException(status_code=503, detail="Service temporarily unavailable")
            
            await self._rate_limit()
            
            try:
                await self._refresh_access_token()
                
                headers = kwargs.get('headers', {})
                headers.update({
                    'Authorization': f'Bearer {self.config.access_token}',
                    'appKey': self.config.app_key,
                    'appSecret': self.config.app_secret,
                    'Content-Type': 'application/json'
                })
                kwargs['headers'] = headers
                
                url = f"{self.config.api_url}{endpoint}"
                
                start_time = time.time()
                async with self.session.request(method, url, **kwargs) as response:
                    duration = time.time() - start_time
                    
                    if response.status == 200:
                        data = await response.json()
                        self.metrics.record_request_success(duration)
                        return data
                    else:
                        self.failure_count += 1
                        self.last_failure_time = time.time()
                        self.metrics.record_request_error()
                        raise HTTPException(
                            status_code=response.status,
                            detail=f"API request failed: {await response.text()}"
                        )
                        
            except asyncio.TimeoutError:
                self.failure_count += 1
                self.last_failure_time = time.time()
                self.metrics.record_request_timeout()
                raise HTTPException(status_code=504, detail="Request timeout")
            
            except Exception as e:
                self.failure_count += 1
                self.last_failure_time = time.time()
                self.metrics.record_request_error()
                logger.error(f"API request failed: {e}")
                raise
    
    async def subscribe_market_data(self, symbols: List[str]):
        """실시간 시장 데이터 구독"""
        try:
            for symbol in symbols:
                ws_url = f"{self.config.ws_url}/marketdata"
                
                async with websockets.connect(
                    ws_url,
                    extra_headers={
                        'Authorization': f'Bearer {self.config.access_token}',
                        'appKey': self.config.app_key
                    }
                ) as websocket:
                    self.ws_connections[symbol] = websocket
                    
                    # 구독 요청
                    subscribe_msg = {
                        "tr_id": "H0STCNT0",
                        "tr_key": symbol
                    }
                    await websocket.send(json.dumps(subscribe_msg))
                    
                    # 데이터 수신 루프
                    async for message in websocket:
                        try:
                            data = json.loads(message)
                            await self._process_market_data(data)
                        except Exception as e:
                            logger.error(f"Error processing market data: {e}")
                            
        except Exception as e:
            logger.error(f"Failed to subscribe market data: {e}")
            raise
    
    async def _process_market_data(self, data: Dict[str, Any]):
        """실시간 시장 데이터 처리"""
        try:
            # 메시지 버퍼에 추가
            await self.message_buffer.put(data)
            
            # Redis에 실시간 데이터 저장
            symbol = data.get('symbol')
            if symbol:
                await self.redis_client.setex(
                    f"market_data:{symbol}",
                    60,  # 1 minute TTL
                    json.dumps(data)
                )
            
            # Kafka로 데이터 전송
            await self.kafka_producer.send(
                'market_data',
                value=data
            )
            
            self.metrics.increment_market_data_received()
            
        except Exception as e:
            logger.error(f"Failed to process market data: {e}")
    
    async def _message_batcher(self):
        """메시지 배치 처리"""
        while True:
            try:
                batch = []
                deadline = time.time() + self.batch_timeout
                
                # 배치 크기 또는 타임아웃 도달 시까지 수집
                while len(batch) < self.batch_size and time.time() < deadline:
                    try:
                        item = await asyncio.wait_for(
                            self.message_buffer.get(), 
                            timeout=deadline - time.time()
                        )
                        batch.append(item)
                    except asyncio.TimeoutError:
                        break
                
                if batch:
                    await self.kafka_producer.send(
                        'market_data_batch',
                        value={'messages': batch, 'timestamp': datetime.now().isoformat()}
                    )
                    
            except Exception as e:
                logger.error(f"Error in message batcher: {e}")
                await asyncio.sleep(1)
    
    async def _health_monitor(self):
        """헬스 모니터링"""
        while True:
            try:
                # API 헬스 체크
                await self.make_request('GET', '/health')
                
                # WebSocket 연결 상태 확인
                disconnected_symbols = []
                for symbol, ws in self.ws_connections.items():
                    if ws.closed:
                        disconnected_symbols.append(symbol)
                
                # 재연결
                if disconnected_symbols:
                    logger.warning(f"Reconnecting to {len(disconnected_symbols)} symbols")
                    await self.subscribe_market_data(disconnected_symbols)
                
                self.metrics.record_health_check_success()
                
            except Exception as e:
                self.metrics.record_health_check_failure()
                logger.error(f"Health check failed: {e}")
            
            await asyncio.sleep(30)  # Check every 30 seconds
    
    async def place_order(self, request: TradingRequest) -> Dict[str, Any]:
        """주문 실행"""
        try:
            payload = {
                "CANO": self.settings.ACCOUNT_NUMBER,
                "ACNT_PRD_CD": self.settings.ACCOUNT_PRODUCT_CODE,
                "PDNO": request.symbol,
                "ORD_DVSN_CD": request.order_type,
                "ORD_QTY": str(request.quantity),
                "ORD_UNPR": str(request.price) if request.price else "",
                "SLL_BUY_DVSN_CD": "02" if request.side == "BUY" else "01"
            }
            
            result = await self.make_request(
                'POST',
                '/uapi/domestic-stock/v1/trading/order-cash',
                json=payload
            )
            
            # 주문 결과 Kafka로 전송
            await self.kafka_producer.send(
                'trading_orders',
                value={
                    'order': asdict(request),
                    'result': result,
                    'timestamp': datetime.now().isoformat()
                }
            )
            
            self.metrics.increment_order_placed()
            return result
            
        except Exception as e:
            self.metrics.record_order_error()
            logger.error(f"Failed to place order: {e}")
            raise
    
    async def get_portfolio(self) -> Dict[str, Any]:
        """포트폴리오 정보 조회"""
        try:
            result = await self.make_request(
                'GET',
                '/uapi/domestic-stock/v1/trading/inquire-balance',
                params={
                    'CANO': self.settings.ACCOUNT_NUMBER,
                    'ACNT_PRD_CD': self.settings.ACCOUNT_PRODUCT_CODE,
                    'AFHR_FLPR_YN': 'N',
                    'OFL_YN': 'N',
                    'INQR_DVSN': '02'
                }
            )
            return result
            
        except Exception as e:
            logger.error(f"Failed to get portfolio: {e}")
            raise
    
    async def close(self):
        """리소스 정리"""
        try:
            # WebSocket 연결 종료
            for ws in self.ws_connections.values():
                if not ws.closed:
                    await ws.close()
            
            # HTTP 세션 종료
            if self.session:
                await self.session.close()
            
            # Kafka 프로듀서 종료
            if self.kafka_producer:
                self.kafka_producer.close()
            
            # Redis 연결 종료
            if self.redis_client:
                await self.redis_client.close()
            
            # 백그라운드 태스크 취소
            for task in self.background_tasks:
                task.cancel()
            
            logger.info("API Gateway closed successfully")
            
        except Exception as e:
            logger.error(f"Error closing API Gateway: {e}")

# FastAPI 앱 생성
app = FastAPI(title="KIS Trading Gateway", version="2.0.0")

# 전역 API Gateway 인스턴스
api_gateway: Optional[APIGateway] = None

@app.on_event("startup")
async def startup_event():
    """앱 시작 시 API Gateway 초기화"""
    global api_gateway
    
    settings = Settings()
    config = ConnectionConfig(
        api_url=settings.KIS_API_URL,
        ws_url=settings.KIS_WS_URL,
        app_key=settings.KIS_APP_KEY,
        app_secret=settings.KIS_APP_SECRET
    )
    
    api_gateway = APIGateway(config, settings)
    await api_gateway.initialize()

@app.on_event("shutdown")
async def shutdown_event():
    """앱 종료 시 리소스 정리"""
    if api_gateway:
        await api_gateway.close()

# API 엔드포인트
@app.post("/trading/orders")
async def place_order(request: TradingRequest):
    """주문 실행 API"""
    if not api_gateway:
        raise HTTPException(status_code=503, detail="API Gateway not initialized")
    
    try:
        result = await api_gateway.place_order(request)
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/portfolio")
async def get_portfolio():
    """포트폴리오 정보 조회 API"""
    if not api_gateway:
        raise HTTPException(status_code=503, detail="API Gateway not initialized")
    
    try:
        result = await api_gateway.get_portfolio()
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/market-data/subscribe")
async def subscribe_market_data(symbols: List[str]):
    """실시간 시장 데이터 구독 API"""
    if not api_gateway:
        raise HTTPException(status_code=503, detail="API Gateway not initialized")
    
    try:
        asyncio.create_task(api_gateway.subscribe_market_data(symbols))
        return {"success": True, "message": f"Subscribed to {len(symbols)} symbols"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """헬스 체크 API"""
    if not api_gateway:
        return {"status": "unhealthy", "reason": "API Gateway not initialized"}
    
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "metrics": api_gateway.metrics.get_summary()
    }

if __name__ == "__main__":
    uvicorn.run(
        "api_gateway:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )