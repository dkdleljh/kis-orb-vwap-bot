"""
Enterprise KIS Trading Bot - Monitoring & Metrics
==========================================

Real-time monitoring, metrics collection, and alerting system
for the high-frequency trading platform.
"""

import time
import asyncio
import logging
from typing import Dict, Any, Callable
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from collections import defaultdict, deque
import statistics
import psutil
from prometheus_client import Counter, Histogram, Gauge, start_http_server

from ..config.settings import Settings
from ..utils.logger import get_logger

logger = get_logger(__name__)

@dataclass
class PerformanceMetrics:
    """성능 메트릭 데이터 모델"""
    timestamp: datetime
    request_count: int
    success_count: int
    error_count: int
    average_response_time: float
    cpu_usage: float
    memory_usage: float
    active_connections: int
    messages_processed: int
    orders_placed: int
    trading_volume: float

@dataclass
class AlertThreshold:
    """알림 임계값 설정"""
    metric_name: str
    threshold_value: float
    comparison: str  # 'gt', 'lt', 'eq'
    severity: str  # 'low', 'medium', 'high', 'critical'
    cooldown_minutes: int = 5

class MetricsCollector:
    """메트릭 수집기"""
    
    def __init__(self):
        # Prometheus metrics
        self.http_requests_total = Counter(
            'http_requests_total',
            'Total HTTP requests',
            ['method', 'endpoint', 'status']
        )
        
        self.http_request_duration = Histogram(
            'http_request_duration_seconds',
            'HTTP request duration',
            ['method', 'endpoint']
        )
        
        self.active_connections = Gauge(
            'active_connections',
            'Number of active connections'
        )
        
        self.trading_orders_total = Counter(
            'trading_orders_total',
            'Total trading orders',
            ['symbol', 'side', 'status']
        )
        
        self.portfolio_value = Gauge(
            'portfolio_value_krw',
            'Portfolio value in KRW'
        )
        
        self.system_cpu_usage = Gauge(
            'system_cpu_usage_percent',
            'System CPU usage percentage'
        )
        
        self.system_memory_usage = Gauge(
            'system_memory_usage_percent',
            'System memory usage percentage'
        )
        
        self.market_data_messages_total = Counter(
            'market_data_messages_total',
            'Total market data messages received',
            ['symbol']
        )
        
        self.order_errors_total = Counter(
            'order_errors_total',
            'Total order errors',
            ['error_type']
        )
        
        # Internal metrics
        self.request_times = deque(maxlen=1000)
        self.error_counts = defaultdict(int)
        self.success_counts = defaultdict(int)
        self.message_counts = defaultdict(int)
        self.order_counts = defaultdict(int)
        self.trading_volumes = defaultdict(float)
        
        # Performance tracking
        self.performance_history = deque(maxlen=1000)
        
    def record_request_start(self, method: str, endpoint: str) -> float:
        """요청 시작 시간 기록"""
        start_time = time.time()
        return start_time
    
    def record_request_success(self, duration: float, method: str = 'GET', endpoint: str = '/'):
        """성공적인 요청 기록"""
        self.http_requests_total.labels(method=method, endpoint=endpoint, status='200').inc()
        self.http_request_duration.labels(method=method, endpoint=endpoint).observe(duration)
        self.request_times.append(duration)
        
        self.success_counts['total'] += 1
        logger.debug(f"Request completed successfully in {duration:.3f}s")
    
    def record_request_error(self, method: str = 'GET', endpoint: str = '/', status_code: int = 500):
        """요청 에러 기록"""
        self.http_requests_total.labels(method=method, endpoint=endpoint, status=str(status_code)).inc()
        self.error_counts['total'] += 1
        self.order_errors_total.labels(error_type='request_error').inc()
        
        logger.warning(f"Request failed with status {status_code}")
    
    def record_request_timeout(self, method: str = 'GET', endpoint: str = '/'):
        """요청 타임아웃 기록"""
        self.http_requests_total.labels(method=method, endpoint=endpoint, status='timeout').inc()
        self.error_counts['timeout'] += 1
        self.order_errors_total.labels(error_type='timeout').inc()
        
        logger.warning("Request timed out")
    
    def increment_market_data_received(self, symbol: str = 'unknown'):
        """시장 데이터 수신 증가"""
        self.market_data_messages_total.labels(symbol=symbol).inc()
        self.message_counts[symbol] += 1
    
    def increment_order_placed(self, symbol: str = 'unknown', side: str = 'unknown', status: str = 'success'):
        """주문 실행 증가"""
        self.trading_orders_total.labels(symbol=symbol, side=side, status=status).inc()
        self.order_counts['total'] += 1
    
    def record_order_error(self, error_type: str = 'unknown'):
        """주문 에러 기록"""
        self.order_errors_total.labels(error_type=error_type).inc()
        self.error_counts[error_type] += 1
    
    def update_portfolio_value(self, value: float):
        """포트폴리오 가치 업데이트"""
        self.portfolio_value.set(value)
    
    def update_system_metrics(self):
        """시스템 메트릭 업데이트"""
        try:
            # CPU 사용량
            cpu_percent = psutil.cpu_percent(interval=1)
            self.system_cpu_usage.set(cpu_percent)
            
            # 메모리 사용량
            memory = psutil.virtual_memory()
            self.system_memory_usage.set(memory.percent)
            
        except Exception as e:
            logger.error(f"Failed to update system metrics: {e}")
    
    def record_trading_volume(self, symbol: str, volume: float):
        """거래량 기록"""
        self.trading_volumes[symbol] += volume
    
    def get_current_performance(self) -> PerformanceMetrics:
        """현재 성능 메트릭 반환"""
        current_time = datetime.now()
        
        # 평균 응답 시간 계산
        avg_response_time = 0.0
        if self.request_times:
            avg_response_time = statistics.mean(self.request_times)
        
        # 시스템 리소스 사용량
        try:
            cpu_usage = psutil.cpu_percent()
            memory_usage = psutil.virtual_memory().percent
        except:
            cpu_usage = 0.0
            memory_usage = 0.0
        
        metrics = PerformanceMetrics(
            timestamp=current_time,
            request_count=len(self.request_times),
            success_count=self.success_counts.get('total', 0),
            error_count=self.error_counts.get('total', 0),
            average_response_time=avg_response_time,
            cpu_usage=cpu_usage,
            memory_usage=memory_usage,
            active_connections=int(self.active_connections._value.get()),
            messages_processed=sum(self.message_counts.values()),
            orders_placed=self.order_counts.get('total', 0),
            trading_volume=sum(self.trading_volumes.values())
        )
        
        self.performance_history.append(metrics)
        return metrics
    
    def get_summary(self) -> Dict[str, Any]:
        """메트릭 요약 반환"""
        if not self.performance_history:
            return {}
        
        latest = self.performance_history[-1]
        
        # 최근 1시간 데이터 분석
        one_hour_ago = datetime.now() - timedelta(hours=1)
        recent_metrics = [
            m for m in self.performance_history 
            if m.timestamp > one_hour_ago
        ]
        
        summary = {
            'current': asdict(latest),
            'one_hour_stats': {
                'avg_response_time': statistics.mean([m.average_response_time for m in recent_metrics]) if recent_metrics else 0,
                'total_requests': sum([m.request_count for m in recent_metrics]) if recent_metrics else 0,
                'success_rate': (sum([m.success_count for m in recent_metrics]) / max(sum([m.request_count for m in recent_metrics]), 1)) * 100 if recent_metrics else 0,
                'total_trades': sum([m.orders_placed for m in recent_metrics]) if recent_metrics else 0,
                'trading_volume': sum([m.trading_volume for m in recent_metrics]) if recent_metrics else 0
            },
            'top_symbols': dict(sorted(self.trading_volumes.items(), key=lambda x: x[1], reverse=True)[:10]),
            'error_breakdown': dict(self.error_counts)
        }
        
        return summary

class AlertManager:
    """알림 관리자"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.metrics_collector = MetricsCollector()
        self.alert_thresholds = []
        self.last_alert_times = {}
        self.alert_handlers = []
        
        # 기본 알림 임계값 설정
        self._setup_default_thresholds()
        
    def _setup_default_thresholds(self):
        """기본 알림 임계값 설정"""
        self.alert_thresholds = [
            AlertThreshold('cpu_usage', 80.0, 'gt', 'medium', 5),
            AlertThreshold('memory_usage', 85.0, 'gt', 'medium', 5),
            AlertThreshold('average_response_time', 2.0, 'gt', 'medium', 3),
            AlertThreshold('error_rate', 10.0, 'gt', 'high', 2),
            AlertThreshold('portfolio_loss', -5000000, 'lt', 'critical', 1),  # 5백만원 손실
            AlertThreshold('trading_volume', 100000000, 'gt', 'medium', 10),  # 1억원 이상 거래
        ]
    
    def add_alert_handler(self, handler: Callable[[Dict[str, Any]], None]):
        """알림 핸들러 추가"""
        self.alert_handlers.append(handler)
    
    def check_thresholds(self, metrics: PerformanceMetrics):
        """임계값 확인 및 알림 발송"""
        current_time = datetime.now()
        
        for threshold in self.alert_thresholds:
            # 쿨다운 확인
            last_alert_time = self.last_alert_times.get(threshold.metric_name)
            if last_alert_time:
                time_since_last = (current_time - last_alert_time).total_seconds()
                if time_since_last < threshold.cooldown_minutes * 60:
                    continue
            
            # 메트릭 값 가져오기
            current_value = getattr(metrics, threshold.metric_name, None)
            if current_value is None:
                continue
            
            # 임계값 비교
            triggered = False
            if threshold.comparison == 'gt' and current_value > threshold.threshold_value:
                triggered = True
            elif threshold.comparison == 'lt' and current_value < threshold.threshold_value:
                triggered = True
            elif threshold.comparison == 'eq' and current_value == threshold.threshold_value:
                triggered = True
            
            if triggered:
                self._send_alert(threshold, current_value, metrics)
                self.last_alert_times[threshold.metric_name] = current_time
    
    def _send_alert(self, threshold: AlertThreshold, current_value: float, metrics: PerformanceMetrics):
        """알림 발송"""
        alert_data = {
            'timestamp': datetime.now().isoformat(),
            'metric_name': threshold.metric_name,
            'current_value': current_value,
            'threshold_value': threshold.threshold_value,
            'severity': threshold.severity,
            'message': f"{threshold.metric_name} is {current_value:.2f} (threshold: {threshold.threshold_value})",
            'context': asdict(metrics)
        }
        
        # 알림 핸들러 실행
        for handler in self.alert_handlers:
            try:
                handler(alert_data)
            except Exception as e:
                logger.error(f"Alert handler failed: {e}")
        
        logger.warning(f"Alert triggered: {alert_data['message']}")

class LogAlertHandler:
    """로그 알림 핸들러"""
    
    def __init__(self):
        self.logger = logging.getLogger('alerts')
        
        # 알림 로그 핸들러 설정
        handler = logging.FileHandler('/var/log/kis_trading/alerts.log')
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.WARNING)
    
    def __call__(self, alert_data: Dict[str, Any]):
        """알림 로깅"""
        log_message = (
            f"ALERT - {alert_data['severity'].upper()}: "
            f"{alert_data['message']} "
            f"(Value: {alert_data['current_value']}, "
            f"Threshold: {alert_data['threshold_value']})"
        )
        
        if alert_data['severity'] in ['high', 'critical']:
            self.logger.critical(log_message)
        elif alert_data['severity'] == 'medium':
            self.logger.warning(log_message)
        else:
            self.logger.info(log_message)

class SlackAlertHandler:
    """Slack 알림 핸들러"""
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        import aiohttp
        self.session = aiohttp.ClientSession()
    
    async def __call__(self, alert_data: Dict[str, Any]):
        """Slack 알림 발송"""
        try:
            payload = {
                'text': "🚨 Trading Bot Alert",
                'attachments': [{
                    'color': self._get_color_by_severity(alert_data['severity']),
                    'fields': [
                        {'title': 'Metric', 'value': alert_data['metric_name'], 'short': True},
                        {'title': 'Current Value', 'value': f"{alert_data['current_value']:.2f}", 'short': True},
                        {'title': 'Threshold', 'value': f"{alert_data['threshold_value']:.2f}", 'short': True},
                        {'title': 'Severity', 'value': alert_data['severity'].upper(), 'short': True},
                        {'title': 'Message', 'value': alert_data['message'], 'short': False}
                    ],
                    'ts': int(time.time())
                }]
            }
            
            async with self.session.post(self.webhook_url, json=payload) as response:
                if response.status != 200:
                    logger.error(f"Failed to send Slack alert: {response.status}")
                
        except Exception as e:
            logger.error(f"Slack alert handler failed: {e}")
    
    def _get_color_by_severity(self, severity: str) -> str:
        """심각도에 따른 색상 반환"""
        colors = {
            'low': 'good',
            'medium': 'warning',
            'high': 'danger',
            'critical': '#ff0000'
        }
        return colors.get(severity, 'warning')
    
    async def close(self):
        """세션 종료"""
        await self.session.close()

class MonitoringService:
    """모니터링 서비스"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.metrics_collector = MetricsCollector()
        self.alert_manager = AlertManager(settings)
        self.running = False
        self.monitoring_task = None
        
        # 기본 알림 핸들러 등록
        self.alert_manager.add_alert_handler(LogAlertHandler())
        
        # Prometheus metrics server 시작
        try:
            start_http_server(settings.METRICS_PORT)
            logger.info(f"Prometheus metrics server started on port {settings.METRICS_PORT}")
        except Exception as e:
            logger.error(f"Failed to start metrics server: {e}")
    
    def add_slack_alerts(self, webhook_url: str):
        """Slack 알림 추가"""
        slack_handler = SlackAlertHandler(webhook_url)
        self.alert_manager.add_alert_handler(slack_handler)
        
        # 나중에 정리할 수 있도록 핸들러 저장
        if not hasattr(self, 'slack_handlers'):
            self.slack_handlers = []
        self.slack_handlers.append(slack_handler)
    
    async def start_monitoring(self):
        """모니터링 시작"""
        if self.running:
            return
        
        self.running = True
        self.monitoring_task = asyncio.create_task(self._monitoring_loop())
        logger.info("Monitoring service started")
    
    async def stop_monitoring(self):
        """모니터링 중지"""
        self.running = False
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
        
        # Slack 핸들러 정리
        if hasattr(self, 'slack_handlers'):
            for handler in self.slack_handlers:
                await handler.close()
        
        logger.info("Monitoring service stopped")
    
    async def _monitoring_loop(self):
        """모니터링 루프"""
        while self.running:
            try:
                # 시스템 메트릭 업데이트
                self.metrics_collector.update_system_metrics()
                
                # 현재 성능 메트릭 가져오기
                current_metrics = self.metrics_collector.get_current_performance()
                
                # 알림 임계값 확인
                self.alert_manager.check_thresholds(current_metrics)
                
                # 다음 루프까지 대기
                await asyncio.sleep(10)  # 10초마다 체크
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Monitoring loop error: {e}")
                await asyncio.sleep(10)
    
    def get_metrics_summary(self) -> Dict[str, Any]:
        """메트릭 요약 반환"""
        return self.metrics_collector.get_summary()