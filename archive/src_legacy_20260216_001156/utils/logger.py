"""
Enterprise KIS Trading Bot - Logging Utilities
=======================================

Centralized logging configuration and utilities
for the enterprise trading platform.
"""

import sys
import logging
import logging.handlers
import json
import traceback
from typing import Dict, Any, Optional
from datetime import datetime
from pathlib import Path

class JSONFormatter(logging.Formatter):
    """JSON 형식 로그 포매터"""
    
    def format(self, record: logging.LogRecord) -> str:
        """로그 레코드를 JSON 형식으로 포맷팅"""
        log_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
            'thread': record.thread,
            'process': record.process
        }
        
        # 예외 정보 추가
        if record.exc_info:
            log_data['exception'] = {
                'type': record.exc_info[0].__name__,
                'message': str(record.exc_info[1]),
                'traceback': traceback.format_exception(*record.exc_info)
            }
        
        # 추가 컨텍스트 정보
        if hasattr(record, 'extra_context'):
            log_data['extra_context'] = record.extra_context
        
        return json.dumps(log_data, ensure_ascii=False, default=str)

class ColoredFormatter(logging.Formatter):
    """컬러 콘솔 로그 포매터"""
    
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
        'RESET': '\033[0m'        # Reset
    }
    
    def format(self, record: logging.LogRecord) -> str:
        """로그 레코드를 컬러로 포맷팅"""
        color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        reset = self.COLORS['RESET']
        
        # 기본 포맷
        formatter = logging.Formatter(
            f'{color}%(asctime)s - %(name)s - %(levelname)s - {reset}%(message)s'
        )
        
        return formatter.format(record)

def setup_logging(
    log_level: str = "INFO",
    log_file_path: Optional[str] = None,
    enable_json: bool = False,
    enable_console: bool = True
) -> logging.Logger:
    """
    로깅 시스템 설정
    
    Args:
        log_level: 로그 레벨 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file_path: 로그 파일 경로
        enable_json: JSON 형식 로그 활성화 여부
        enable_console: 콘솔 로그 활성화 여부
    
    Returns:
        설정된 루트 로거
    """
    # 루트 로거 설정
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))
    
    # 기존 핸들러 제거
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # 로그 포매터 설정
    if enable_json:
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
    
    # 콘솔 핸들러
    if enable_console:
        console_handler = logging.StreamHandler(sys.stdout)
        if enable_json:
            console_handler.setFormatter(formatter)
        else:
            console_handler.setFormatter(ColoredFormatter())
        root_logger.addHandler(console_handler)
    
    # 파일 핸들러
    if log_file_path:
        # 로그 디렉토리 생성
        log_dir = Path(log_file_path)
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # 일반 로그 파일
        file_handler = logging.handlers.RotatingFileHandler(
            filename=log_dir / "app.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        
        # 에러 로그 파일
        error_handler = logging.handlers.RotatingFileHandler(
            filename=log_dir / "error.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        root_logger.addHandler(error_handler)
        
        # 감사 로그 파일
        audit_handler = logging.handlers.RotatingFileHandler(
            filename=log_dir / "audit.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        audit_handler.setFormatter(JSONFormatter())
        audit_logger = logging.getLogger('audit')
        audit_logger.addHandler(audit_handler)
        audit_logger.setLevel(logging.INFO)
        audit_logger.propagate = False  # 부모 로거로 전송 방지
    
    # 타사 라이브러리 로그 레벨 설정
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    logging.getLogger('websockets').setLevel(logging.WARNING)
    
    return root_logger

def get_logger(name: str) -> logging.Logger:
    """
    이름으로 로거 가져오기
    
    Args:
        name: 로거 이름
    
    Returns:
        로거 인스턴스
    """
    return logging.getLogger(name)

class TradingLoggerAdapter(logging.LoggerAdapter):
    """트레이딩 전용 로거 어댑터"""
    
    def __init__(self, logger: logging.Logger, extra: Dict[str, Any] = None):
        super().__init__(logger, extra or {})
    
    def process(self, msg: str, kwargs: Dict[str, Any]) -> tuple:
        """로그 메시지와 키워드 인자 처리"""
        # 트레이딩 관련 컨텍스트 추가
        if 'extra' not in kwargs:
            kwargs['extra'] = {}
        
        kwargs['extra'].update(self.extra)
        kwargs['extra']['component'] = 'trading'
        
        return msg, kwargs

class SecurityLoggerAdapter(logging.LoggerAdapter):
    """보안 전용 로거 어댑터"""
    
    def __init__(self, logger: logging.Logger, extra: Dict[str, Any] = None):
        super().__init__(logger, extra or {})
    
    def process(self, msg: str, kwargs: Dict[str, Any]) -> tuple:
        """보안 로그 메시지 처리"""
        if 'extra' not in kwargs:
            kwargs['extra'] = {}
        
        kwargs['extra'].update(self.extra)
        kwargs['extra']['component'] = 'security'
        kwargs['extra']['security_context'] = True
        
        return msg, kwargs

def create_trading_logger(name: str, symbol: str = None, 
                         strategy: str = None, **context) -> TradingLoggerAdapter:
    """
    트레이딩 로거 생성
    
    Args:
        name: 로거 이름
        symbol: 종목 코드
        strategy: 전략 이름
        **context: 추가 컨텍스트 정보
    
    Returns:
        트레이딩 로거 어댑터
    """
    logger = get_logger(name)
    extra = {}
    
    if symbol:
        extra['symbol'] = symbol
    if strategy:
        extra['strategy'] = strategy
    extra.update(context)
    
    return TradingLoggerAdapter(logger, extra)

def create_security_logger(name: str, user_id: str = None, 
                         ip_address: str = None, **context) -> SecurityLoggerAdapter:
    """
    보안 로거 생성
    
    Args:
        name: 로거 이름
        user_id: 사용자 ID
        ip_address: IP 주소
        **context: 추가 컨텍스트 정보
    
    Returns:
        보안 로거 어댑터
    """
    logger = get_logger(name)
    extra = {}
    
    if user_id:
        extra['user_id'] = user_id
    if ip_address:
        extra['ip_address'] = ip_address
    extra.update(context)
    
    return SecurityLoggerAdapter(logger, extra)

class PerformanceLogger:
    """성능 측정 로거"""
    
    def __init__(self, logger: logging.Logger, operation: str):
        self.logger = logger
        self.operation = operation
        self.start_time = None
        self.context = {}
    
    def __enter__(self):
        self.start_time = datetime.utcnow()
        self.logger.debug(f"Starting operation: {self.operation}")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = (datetime.utcnow() - self.start_time).total_seconds()
        
        if exc_type:
            self.logger.error(
                f"Operation failed: {self.operation} (duration: {duration:.3f}s)",
                exc_info=(exc_type, exc_val, exc_tb),
                extra={'extra_context': self.context}
            )
        else:
            self.logger.info(
                f"Operation completed: {self.operation} (duration: {duration:.3f}s)",
                extra={'extra_context': self.context}
            )
    
    def add_context(self, **kwargs):
        """컨텍스트 정보 추가"""
        self.context.update(kwargs)

def log_function_call(logger: logging.Logger):
    """
    함수 호출 로깅 데코레이터
    
    Args:
        logger: 로거 인스턴스
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            func_name = f"{func.__module__}.{func.__name__}"
            
            with PerformanceLogger(logger, f"Function call: {func_name}") as perf:
                perf.add_context(
                    function_name=func.__name__,
                    module=func.__module__,
                    args_count=len(args),
                    kwargs_count=len(kwargs)
                )
                
                try:
                    result = func(*args, **kwargs)
                    
                    if hasattr(result, '__len__') and not isinstance(result, str):
                        perf.add_context(result_length=len(result))
                    
                    return result
                    
                except Exception as e:
                    perf.add_context(
                        error_type=type(e).__name__,
                        error_message=str(e)
                    )
                    raise
        
        return wrapper
    return decorator

def configure_structured_logging():
    """구조화된 로깅 설정"""
    
    class StructuredLogger:
        def __init__(self, name: str):
            self.logger = get_logger(name)
        
        def info(self, message: str, **kwargs):
            self.logger.info(message, extra={'extra_context': kwargs})
        
        def error(self, message: str, **kwargs):
            self.logger.error(message, extra={'extra_context': kwargs})
        
        def warning(self, message: str, **kwargs):
            self.logger.warning(message, extra={'extra_context': kwargs})
        
        def debug(self, message: str, **kwargs):
            self.logger.debug(message, extra={'extra_context': kwargs})
    
    return StructuredLogger