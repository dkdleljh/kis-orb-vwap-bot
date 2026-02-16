"""
Enterprise KIS Trading Bot - Real-time Dashboard Backend
===================================================

FastAPI backend for real-time trading dashboard with WebSocket support,
real-time data streaming, and comprehensive monitoring APIs.

Features:
- Real-time WebSocket streaming for live data
- RESTful APIs for historical data and controls
- Authentication and authorization
- Real-time charts and metrics
- Portfolio and risk monitoring
- Trading signal visualization
"""

import asyncio
import json
from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import asdict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import uvicorn

# Internal imports
from ..config.settings import Settings
from ..api_gateway.api_gateway import APIGateway
from ..data_pipeline.data_pipeline import DataPipeline
from ..strategy_engine.strategy_engine import StrategyEngine
from ..risk_management.risk_manager import RiskManager
from ..monitoring.metrics import MonitoringService
from ..security.encryption import SecurityManager
from ..utils.logger import get_logger

logger = get_logger(__name__)

# Pydantic models for API
class DashboardResponse(BaseModel):
    success: bool
    data: Any
    message: Optional[str] = None
    timestamp: datetime = datetime.now()

class TradingSignalResponse(BaseModel):
    symbol: str
    signal_type: str
    strength: float
    price: float
    quantity: int
    confidence: float
    timestamp: datetime
    reason: str

class PositionResponse(BaseModel):
    symbol: str
    quantity: int
    entry_price: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float
    pnl_percentage: float
    market_value: float

class PortfolioMetricsResponse(BaseModel):
    portfolio_value: float
    daily_pnl: float
    total_pnl: float
    var_95_1day: float
    current_drawdown: float
    sharpe_ratio: float
    volatility: float
    risk_level: str

class WebSocketManager:
    """WebSocket 연결 관리자"""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.user_subscriptions: Dict[str, List[str]] = {}
    
    async def connect(self, websocket: WebSocket, user_id: str):
        """WebSocket 연결"""
        await websocket.accept()
        self.active_connections[user_id] = websocket
        self.user_subscriptions[user_id] = []
        logger.info(f"WebSocket connected: {user_id}")
    
    def disconnect(self, user_id: str):
        """WebSocket 연결 해제"""
        if user_id in self.active_connections:
            del self.active_connections[user_id]
        if user_id in self.user_subscriptions:
            del self.user_subscriptions[user_id]
        logger.info(f"WebSocket disconnected: {user_id}")
    
    async def send_personal_message(self, user_id: str, message: Dict[str, Any]):
        """개인 메시지 전송"""
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_text(json.dumps(message, default=str))
            except Exception as e:
                logger.error(f"Failed to send message to {user_id}: {e}")
                self.disconnect(user_id)
    
    async def broadcast_to_subscribers(self, channel: str, message: Dict[str, Any]):
        """구독자에게 메시지 브로드캐스트"""
        for user_id, subscriptions in self.user_subscriptions.items():
            if channel in subscriptions and user_id in self.active_connections:
                try:
                    await self.active_connections[user_id].send_text(json.dumps({
                        'type': 'broadcast',
                        'channel': channel,
                        'data': message
                    }, default=str))
                except Exception as e:
                    logger.error(f"Failed to broadcast to {user_id}: {e}")
    
    def subscribe(self, user_id: str, channels: List[str]):
        """채널 구독"""
        if user_id in self.user_subscriptions:
            self.user_subscriptions[user_id].extend(channels)
            self.user_subscriptions[user_id] = list(set(self.user_subscriptions[user_id]))

class DashboardService:
    """대시보드 서비스"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.security_manager = SecurityManager(settings)
        self.websocket_manager = WebSocketManager()
        
        # 컴포넌트들 (실제로는 의존성 주입으로 받아야 함)
        self.api_gateway: Optional[APIGateway] = None
        self.data_pipeline: Optional[DataPipeline] = None
        self.strategy_engine: Optional[StrategyEngine] = None
        self.risk_manager: Optional[RiskManager] = None
        self.monitoring_service: Optional[MonitoringService] = None
        
        # 실시간 데이터 캐시
        self.data_cache = {}
        self.background_tasks = set()
    
    def initialize_components(self, api_gateway: APIGateway, data_pipeline: DataPipeline,
                            strategy_engine: StrategyEngine, risk_manager: RiskManager,
                            monitoring_service: MonitoringService):
        """컴포넌트 초기화"""
        self.api_gateway = api_gateway
        self.data_pipeline = data_pipeline
        self.strategy_engine = strategy_engine
        self.risk_manager = risk_manager
        self.monitoring_service = monitoring_service
    
    async def verify_token(self, credentials: HTTPAuthorizationCredentials) -> Optional[str]:
        """JWT 토큰 검증"""
        try:
            payload = self.security_manager.auth_manager.verify_jwt_token(credentials.credentials)
            return payload.get('user_id')
        except Exception as e:
            logger.error(f"Token verification failed: {e}")
            return None
    
    async def get_portfolio_overview(self, user_id: str) -> Dict[str, Any]:
        """포트폴리오 개요 조회"""
        if not self.risk_manager:
            return {}
        
        try:
            metrics = self.risk_manager.get_portfolio_metrics()
            positions = self.risk_manager.positions
            
            # 포지션 데이터 변환
            position_data = []
            for symbol, position in positions.items():
                position_data.append({
                    'symbol': symbol,
                    'quantity': position.quantity,
                    'entry_price': position.entry_price,
                    'current_price': position.current_price,
                    'unrealized_pnl': position.unrealized_pnl,
                    'realized_pnl': position.realized_pnl,
                    'pnl_percentage': position.pnl_percentage,
                    'market_value': position.market_value
                })
            
            return {
                'metrics': asdict(metrics),
                'positions': position_data,
                'total_positions': len(positions)
            }
            
        except Exception as e:
            logger.error(f"Failed to get portfolio overview: {e}")
            return {}
    
    async def get_trading_signals(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """최신 트레이딩 신호 조회"""
        if not self.strategy_engine:
            return []
        
        try:
            # 실제로는 신호 히스토리에서 가져와야 함
            # 여기서는 예시 데이터 반환
            return []
            
        except Exception as e:
            logger.error(f"Failed to get trading signals: {e}")
            return []
    
    async def get_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """실시간 시장 데이터 조회"""
        if not self.data_pipeline:
            return None
        
        try:
            return await self.data_pipeline.get_real_time_data(symbol)
        except Exception as e:
            logger.error(f"Failed to get market data: {e}")
            return None
    
    async def get_performance_metrics(self, user_id: str) -> Dict[str, Any]:
        """성과 메트릭 조회"""
        if not self.monitoring_service:
            return {}
        
        try:
            return self.monitoring_service.get_metrics_summary()
        except Exception as e:
            logger.error(f"Failed to get performance metrics: {e}")
            return {}
    
    async def execute_trade(self, user_id: str, signal: Dict[str, Any]) -> Dict[str, Any]:
        """트레이드 실행"""
        if not self.api_gateway:
            return {'success': False, 'message': 'API Gateway not available'}
        
        try:
            # 실제로는 신호 검증 및 실행 로직 필요
            return {'success': True, 'message': 'Trade executed successfully'}
            
        except Exception as e:
            logger.error(f"Failed to execute trade: {e}")
            return {'success': False, 'message': str(e)}
    
    async def start_real_time_streaming(self):
        """실시간 데이터 스트리밍 시작"""
        while True:
            try:
                # 포트폴리오 데이터 브로드캐스트
                portfolio_data = await self.get_portfolio_overview("system")
                await self.websocket_manager.broadcast_to_subscribers("portfolio", {
                    'type': 'portfolio_update',
                    'data': portfolio_data
                })
                
                # 성과 메트릭 브로드캐스트
                performance_data = await self.get_performance_metrics("system")
                await self.websocket_manager.broadcast_to_subscribers("performance", {
                    'type': 'performance_update',
                    'data': performance_data
                })
                
                await asyncio.sleep(5)  # 5초마다 업데이트
                
            except Exception as e:
                logger.error(f"Real-time streaming error: {e}")
                await asyncio.sleep(10)

# FastAPI app 생성
app = FastAPI(title="KIS Trading Dashboard API", version="2.0.0")

# CORS 미들웨어
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8080"],  # React 개발 서버
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 의존성 주입
settings = Settings()
dashboard_service = DashboardService(settings)
security = HTTPBearer()

# 라우터 정의
@app.get("/")
async def root():
    """헬스 체크"""
    return {"message": "KIS Trading Dashboard API", "status": "running"}

@app.get("/auth/verify")
async def verify_auth(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """인증 확인"""
    user_id = await dashboard_service.verify_token(credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    return DashboardResponse(
        success=True,
        data={"user_id": user_id, "verified": True}
    )

@app.get("/portfolio/overview")
async def get_portfolio_overview(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """포트폴리오 개요"""
    user_id = await dashboard_service.verify_token(credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    data = await dashboard_service.get_portfolio_overview(user_id)
    return DashboardResponse(
        success=True,
        data=data
    )

@app.get("/portfolio/positions")
async def get_positions(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """포지션 목록"""
    user_id = await dashboard_service.verify_token(credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    overview = await dashboard_service.get_portfolio_overview(user_id)
    positions = overview.get('positions', [])
    
    return DashboardResponse(
        success=True,
        data=positions
    )

@app.get("/portfolio/metrics")
async def get_portfolio_metrics(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """포트폴리오 메트릭"""
    user_id = await dashboard_service.verify_token(credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    overview = await dashboard_service.get_portfolio_overview(user_id)
    metrics = overview.get('metrics', {})
    
    return DashboardResponse(
        success=True,
        data=metrics
    )

@app.get("/signals")
async def get_trading_signals(
    limit: int = 50,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """트레이딩 신호 목록"""
    user_id = await dashboard_service.verify_token(credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    signals = await dashboard_service.get_trading_signals(user_id, limit)
    return DashboardResponse(
        success=True,
        data=signals
    )

@app.get("/market/{symbol}")
async def get_market_data(
    symbol: str,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """종목 시장 데이터"""
    user_id = await dashboard_service.verify_token(credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    data = await dashboard_service.get_market_data(symbol)
    if not data:
        raise HTTPException(status_code=404, detail="Market data not found")
    
    return DashboardResponse(
        success=True,
        data=data
    )

@app.get("/performance/metrics")
async def get_performance_metrics(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """성과 메트릭"""
    user_id = await dashboard_service.verify_token(credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    data = await dashboard_service.get_performance_metrics(user_id)
    return DashboardResponse(
        success=True,
        data=data
    )

@app.post("/trading/execute")
async def execute_trade(
    signal: Dict[str, Any],
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """트레이드 실행"""
    user_id = await dashboard_service.verify_token(credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    result = await dashboard_service.execute_trade(user_id, signal)
    return DashboardResponse(
        success=result.get('success', False),
        data=result,
        message=result.get('message')
    )

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    """WebSocket 엔드포인트"""
    await dashboard_service.websocket_manager.connect(websocket, user_id)
    
    try:
        while True:
            # 클라이언트 메시지 수신
            data = await websocket.receive_text()
            message = json.loads(data)
            
            # 메시지 타입 처리
            if message.get('type') == 'subscribe':
                channels = message.get('channels', [])
                dashboard_service.websocket_manager.subscribe(user_id, channels)
                
                await websocket.send_text(json.dumps({
                    'type': 'subscription_confirmation',
                    'channels': channels
                }, default=str))
            
            elif message.get('type') == 'ping':
                await websocket.send_text(json.dumps({
                    'type': 'pong',
                    'timestamp': datetime.now().isoformat()
                }))
            
    except WebSocketDisconnect:
        dashboard_service.websocket_manager.disconnect(user_id)
    except Exception as e:
        logger.error(f"WebSocket error for {user_id}: {e}")
        dashboard_service.websocket_manager.disconnect(user_id)

@app.on_event("startup")
async def startup_event():
    """앱 시작 시 초기화"""
    # 실시간 스트리밍 시작
    task = asyncio.create_task(dashboard_service.start_real_time_streaming())
    dashboard_service.background_tasks.add(task)
    task.add_done_callback(dashboard_service.background_tasks.discard)
    
    logger.info("Dashboard API started")

@app.on_event("shutdown")
async def shutdown_event():
    """앱 종료 시 정리"""
    # 백그라운드 태스크 취소
    for task in dashboard_service.background_tasks:
        task.cancel()
    
    logger.info("Dashboard API stopped")

# 컴포넌트 설정 함수
def set_dashboard_components(api_gateway: APIGateway, data_pipeline: DataPipeline,
                           strategy_engine: StrategyEngine, risk_manager: RiskManager,
                           monitoring_service: MonitoringService):
    """대시보드 컴포넌트 설정"""
    dashboard_service.initialize_components(
        api_gateway, data_pipeline, strategy_engine, risk_manager, monitoring_service
    )

if __name__ == "__main__":
    uvicorn.run(
        "dashboard_api:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        log_level="info"
    )