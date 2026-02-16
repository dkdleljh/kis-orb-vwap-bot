"""
Base Trading Module - 추상 基底 클래스 for all trading modules.

이 모듈은 모든 트레이딩 모듈(Kukjang, Hwanjeon, Mijang)의 공통 인터페이스를 정의합니다.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
import logging

from models import Position, OrderBookTop, TradeTick, Bar1m, OrderResult


@dataclass
class ModuleContext:
    """모듈 실행에 필요한 컨텍스트 정보"""
    name: str                    # 모듈 이름 (kukjang, hwanjeon, mijang)
    enabled: bool               # 활성화 여부
    supports_trading: bool      # 실제 거래 지원 여부
    symbols: List[str]          # 거래/모니터링할 종목 목록
    exchange: Optional[str] = None  # 거래소 코드 (NASD, NYSE, AMEX 등)
    config: Dict[str, Any] = field(default_factory=dict)  # 모듈별 설정
    state: Dict[str, Any] = field(default_factory=dict)   # 모듈별 상태


class BaseTradingModule(ABC):
    """
    트레이딩 모듈의 추상 基底 클래스.
    
    모든 모듈은 이 클래스를 상속받아 구현해야 합니다.
    """
    
    def __init__(self, context: ModuleContext, logger: logging.Logger):
        self.context = context
        self.logger = logger
        self._running = False
    
    @property
    def name(self) -> str:
        return self.context.name
    
    @property
    def is_enabled(self) -> bool:
        return self.context.enabled
    
    @property
    def supports_trading(self) -> bool:
        return self.context.supports_trading
    
    @property
    def symbols(self) -> List[str]:
        return self.context.symbols
    
    async def initialize(self) -> None:
        """
        모듈 초기화. 각 모듈에서 필요한 리소스准备를 수행합니다.
        """
        self.logger.info(f"[{self.name}] Initializing module...")
        self._running = True
        await self._on_initialize()
        self.logger.info(f"[{self.name}] Module initialized successfully")
    
    async def shutdown(self) -> None:
        """
        모듈 종료. 리소스 정리 작업을 수행합니다.
        """
        self.logger.info(f"[{self.name}] Shutting down module...")
        self._running = False
        await self._on_shutdown()
        self.logger.info(f"[{self.name}] Module shutdown complete")
    
    async def _on_initialize(self) -> None:
        """서브클래스에서 구현할 초기화 로직"""
        pass
    
    async def _on_shutdown(self) -> None:
        """서브클래스에서 구현할 종료 로직"""
        pass
    
    # =========================================================================
    # 필수 구현 메서드 (Trading Interface)
    # =========================================================================
    
    @abstractmethod
    async def get_cash_available(self, symbol: str, price: float = 0.0) -> float:
        """
        매수 가능한 현금을 조회합니다.
        
        Args:
            symbol: 종목코드
            price: 예상 주문 가격
            
        Returns:
            매수 가능한 금액
        """
        pass
    
    @abstractmethod
    async def place_buy_order(self, symbol: str, qty: int, price: float) -> OrderResult:
        """
        매수 주문을 넣습니다.
        
        Args:
            symbol: 종목코드
            qty: 수량
            price: 가격 (지정가)
            
        Returns:
            OrderResult: 주문 결과
        """
        pass
    
    @abstractmethod
    async def place_sell_order(self, symbol: str, qty: int, price: Optional[float] = None) -> OrderResult:
        """
        매도 주문을 넣습니다.
        
        Args:
            symbol: 종목코드
            qty: 수량
            price: 가격 (None이면 시장가)
            
        Returns:
            OrderResult: 주문 결과
        """
        pass
    
    @abstractmethod
    async def get_positions(self) -> Optional[Position]:
        """
        현재 포지션을 조회합니다.
        
        Returns:
            Position 객체 또는 None
        """
        pass
    
    @abstractmethod
    async def get_quote(self, symbol: str) -> dict:
        """
        현재 시세를 조회합니다.
        
        Args:
            symbol: 종목코드
            
        Returns:
            시세 정보 딕셔너리
        """
        pass
    
    # =========================================================================
    # 옵션 구현 메서드 (Hooks)
    # =========================================================================
    
    async def on_tick(self, tick: TradeTick) -> None:
        """
        Tick 데이터 수신 시 호출됩니다.
        기본 구현은 no-op입니다.
        """
        pass
    
    async def on_book(self, book: OrderBookTop) -> None:
        """
        호가 데이터 수신 시 호출됩니다.
        기본 구현은 no-op입니다.
        """
        pass
    
    async def on_bar_close(self, symbol: str, bar: Bar1m) -> None:
        """
        Bar(1분) 클로즈 시 호출됩니다.
        기본 구현은 no-op입니다.
        """
        pass
    
    async def monitor_loop(self) -> None:
        """
        모니터링 루프. 각 모듈에서 독립적으로 실행됩니다.
        기본 구현은 no-op입니다.
        """
        pass
    
    # =========================================================================
    # 유틸리티 메서드
    # =========================================================================
    
    def is_running(self) -> bool:
        """모듈이 실행 중인지 반환합니다."""
        return self._running
    
    def get_state(self, key: str, default: Any = None) -> Any:
        """모듈 상태 값을 가져옵니다."""
        return self.context.state.get(key, default)
    
    def set_state(self, key: str, value: Any) -> None:
        """모듈 상태 값을 설정합니다."""
        self.context.state[key] = value
    
    def log_info(self, message: str) -> None:
        """모듈 이름이 포함된 INFO 로그"""
        self.logger.info(f"[{self.name}] {message}")
    
    def log_warning(self, message: str) -> None:
        """모듈 이름이 포함된 WARNING 로그"""
        self.logger.warning(f"[{self.name}] {message}")
    
    def log_error(self, message: str) -> None:
        """모듈 이름이 포함된 ERROR 로그"""
        self.logger.error(f"[{self.name}] {message}")
