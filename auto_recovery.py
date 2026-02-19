#!/usr/bin/env python3
"""
Pro Trader Phase 3 - Auto Recovery Manager
자동 복구 및 비상 메커니즘
"""

import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable, Iterable, Tuple
from dataclasses import dataclass
from enum import Enum


class RecoveryEvent(Enum):
    CONNECTION_LOST = "CONNECTION_LOST"
    API_ERROR = "API_ERROR"
    ORDER_FAILED = "ORDER_FAILED"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    STUCK_ORDER = "STUCK_ORDER"
    SYSTEM_ERROR = "SYSTEM_ERROR"


@dataclass
class RecoveryAction:
    event: RecoveryEvent
    timestamp: datetime
    success: bool
    details: str
    retry_count: int = 0


class AutoRecoveryManager:
    def __init__(self, logger, config: Dict[str, Any]):
        self.logger = logger
        self.config = config
        
        self.max_retries = config.get("max_retries", 3)
        self.retry_delay = config.get("retry_delay", 5)
        self.circuit_breaker_threshold = config.get("circuit_breaker_threshold", 5)
        
        self.recovery_history: List[RecoveryAction] = []
        self.circuit_breaker_count = 0
        self.circuit_breaker_open = False
        self.last_circuit_reset = datetime.now()
        
        self._recovery_handlers: Dict[RecoveryEvent, Callable] = {}
        self._health_check_interval = config.get("health_check_interval", 30)
        self._running = False
    
    def register_handler(self, event: RecoveryEvent, handler: Callable) -> None:
        self._recovery_handlers[event] = handler
    
    async def handle_connection_loss(self, reconnect_func: Callable) -> bool:
        """연결 상실 복구"""
        for attempt in range(self.max_retries):
            self.logger.warning(f"Connection lost, reconnecting... (attempt {attempt + 1}/{self.max_retries})")
            
            try:
                await asyncio.sleep(self.retry_delay * (attempt + 1))
                result = await reconnect_func()
                
                if result:
                    self.logger.info("Connection restored successfully")
                    self._record_recovery(RecoveryEvent.CONNECTION_LOST, True, "Reconnected")
                    return True
                    
            except Exception as e:
                self.logger.error(f"Reconnect attempt {attempt + 1} failed: {e}")
        
        self._record_recovery(RecoveryEvent.CONNECTION_LOST, False, "Max retries exceeded")
        return False
    
    async def handle_api_error(self, api_func: Callable, *args, **kwargs) -> Optional[Any]:
        """API 에러 복구"""
        for attempt in range(self.max_retries):
            try:
                result = await api_func(*args, **kwargs)
                return result
                
            except Exception as e:
                error_msg = str(e)
                self.logger.warning(f"API error (attempt {attempt + 1}/{self.max_retries}): {error_msg}")
                
                if "rate limit" in error_msg.lower():
                    wait_time = 65
                    self.logger.info(f"Rate limited, waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    await asyncio.sleep(self.retry_delay)
        
        self._record_recovery(RecoveryEvent.API_ERROR, False, "Max retries exceeded")
        self.circuit_breaker_count += 1
        
        if self.circuit_breaker_count >= self.circuit_breaker_threshold:
            await self._trigger_circuit_breaker()
        
        return None
    
    async def handle_order_failure(
        self,
        place_order_func: Callable,
        cancel_order_func: Callable,
        symbol: str,
        qty: int,
        price: float,
    ) -> Optional[Any]:
        """주문 실패 복구.

        Recommended contract:
        - place_order_func(symbol, qty, price) -> result with .order_id
        - cancel_order_func(order_id, symbol, qty) (best-effort)

        NOTE: If an order_id is not available, cancellation is skipped.
        """

        last_order_id: str | None = None

        for attempt in range(self.max_retries):
            try:
                result = await place_order_func(symbol, qty, price)

                if result and getattr(result, "order_id", ""):
                    last_order_id = str(getattr(result, "order_id"))
                    self.logger.info(f"Order placed successfully: {last_order_id}")
                    return result

            except Exception as e:
                self.logger.error(f"Order failed (attempt {attempt + 1}): {e}")

            await asyncio.sleep(self.retry_delay * (attempt + 1))

        self.logger.error(
            f"Order failed after {self.max_retries} attempts, attempting cancellation (best-effort)"
        )

        if last_order_id:
            try:
                await cancel_order_func(last_order_id, symbol, qty)
            except Exception as e:
                self.logger.error(f"Cancel order also failed: {e}")
        else:
            self.logger.warning("No order_id captured; skipping cancellation")

        self._record_recovery(RecoveryEvent.ORDER_FAILED, False, f"Order failed for {symbol}")
        return None
    
    async def verify_position(self, expected_pos: Any, actual_pos: Any) -> bool:
        """포지션 불일치 복구"""
        if expected_pos is None and actual_pos is None:
            return True
        
        if expected_pos is None and actual_pos is not None:
            self.logger.warning(f"Unexpected position found: {actual_pos.symbol} qty={actual_pos.qty}")
            self._record_recovery(RecoveryEvent.POSITION_MISMATCH, True, "Unexpected position detected")
            return False
        
        if expected_pos is not None and actual_pos is None:
            self.logger.error(f"Position missing: expected {expected_pos.symbol}")
            self._record_recovery(RecoveryEvent.POSITION_MISMATCH, False, "Position missing")
            return False
        
        if expected_pos.symbol != actual_pos.symbol:
            self.logger.error(f"Symbol mismatch: expected {expected_pos.symbol}, got {actual_pos.symbol}")
            return False
        
        return True
    
    async def health_check(
        self, check_funcs: Iterable[Tuple[str, Callable[[], Any]]]
    ) -> Dict[str, bool]:
        """전체 건강성 체크"""
        results: Dict[str, bool] = {}

        for name, check_func in check_funcs:
            try:
                result = await asyncio.wait_for(check_func(), timeout=10)
                results[name] = result
            except asyncio.TimeoutError:
                self.logger.error(f"Health check timeout: {name}")
                results[name] = False
            except Exception as e:
                self.logger.error(f"Health check failed: {name} - {e}")
                results[name] = False
        
        all_healthy = all(results.values())
        
        if not all_healthy:
            self.logger.warning(f"Health check failed: {results}")
        
        return results
    
    async def _trigger_circuit_breaker(self) -> None:
        """서킷 브레이커 발동"""
        if self.circuit_breaker_open:
            return
        
        self.circuit_breaker_open = True
        self.logger.critical("CIRCUIT BREAKER OPEN - Stopping all trading")
        
        wait_time = self.config.get("circuit_breaker_wait", 300)
        
        asyncio.create_task(self._reset_circuit_breaker(wait_time))
    
    async def _reset_circuit_breaker(self, wait_time: int) -> None:
        """서킷 브레이커 리셋"""
        await asyncio.sleep(wait_time)
        
        self.circuit_breaker_open = False
        self.circuit_breaker_count = 0
        self.last_circuit_reset = datetime.now()
        
        self.logger.info("Circuit breaker reset - Trading can resume")
    
    def _record_recovery(self, event: RecoveryEvent, success: bool, details: str) -> None:
        """복구 기록"""
        action = RecoveryAction(
            event=event,
            timestamp=datetime.now(),
            success=success,
            details=details,
        )
        self.recovery_history.append(action)
        
        if len(self.recovery_history) > 100:
            self.recovery_history = self.recovery_history[-100:]
    
    def get_recovery_stats(self) -> Dict[str, Any]:
        """복구 통계"""
        total = len(self.recovery_history)
        successful = sum(1 for r in self.recovery_history if r.success)
        
        return {
            "total_recoveries": total,
            "successful_recoveries": successful,
            "success_rate": successful / max(total, 1),
            "circuit_breaker_open": self.circuit_breaker_open,
            "circuit_breaker_count": self.circuit_breaker_count,
            "last_recovery": self.recovery_history[-1].__dict__ if self.recovery_history else None,
        }


class EmergencyManager:
    def __init__(self, logger, config: Dict[str, Any]):
        self.logger = logger
        self.config = config
        
        self.emergency_flags = {
            "market_close": False,
            "system_error": False,
            "manual_stop": False,
            "max_loss": False,
        }
        
        self.emergency_handlers: List[Callable] = []
    
    def set_flag(self, flag: str, value: bool) -> None:
        if flag in self.emergency_flags:
            self.emergency_flags[flag] = value
            self.logger.warning(f"Emergency flag set: {flag} = {value}")
    
    def is_emergency(self) -> bool:
        return any(self.emergency_flags.values())
    
    def get_active_flags(self) -> List[str]:
        return [f for f, v in self.emergency_flags.items() if v]
    
    async def execute_emergency(self, positions: List[Any], close_func: Callable) -> None:
        """비상 상태에서 모든 포지션 클로즈"""
        if not self.is_emergency():
            return
        
        self.logger.critical(f"EXECUTING EMERGENCY: {self.get_active_flags()}")
        
        for pos in positions:
            try:
                self.logger.info(f"Emergency closing: {pos.symbol}")
                await close_func(pos.symbol, pos.qty)
            except Exception as e:
                self.logger.error(f"Failed to close {pos.symbol}: {e}")
        
        self.logger.critical("Emergency execution complete")
    
    def register_emergency_handler(self, handler: Callable) -> None:
        self.emergency_handlers.append(handler)
    
    def clear_flags(self) -> None:
        for flag in self.emergency_flags:
            self.emergency_flags[flag] = False
        self.logger.info("All emergency flags cleared")
