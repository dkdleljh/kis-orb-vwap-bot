"""
Resilient API Client - 자동 재시도 및 에러 처리 유틸.
"""

import asyncio
import random
import logging
from typing import Callable, Any
from functools import wraps


def is_rate_limit_error(error: Exception) -> bool:
    error_msg = str(error).lower()
    return "egw00133" in error_msg or "1분당 1회" in error_msg


def is_token_error(error: Exception) -> bool:
    error_msg = str(error).lower()
    return "토큰" in error_msg or "token" in error_msg or "인증" in error_msg


class ResilientClient:
    def __init__(
        self,
        logger: logging.Logger,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        jitter: bool = True,
        rate_limit_cooldown: float = 65.0,
    ):
        self.logger = logger
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.rate_limit_cooldown = rate_limit_cooldown
    
    async def call_with_retry(
        self,
        func: Callable,
        *args,
        **kwargs,
    ) -> Any:
        last_error = None
        
        for attempt in range(self.max_attempts):
            try:
                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
                return result
            
            except Exception as e:
                last_error = e
                self.logger.warning(f"Attempt {attempt + 1} failed: {e}")
                
                if is_rate_limit_error(e):
                    self.logger.warning(f"Rate limit detected, waiting {self.rate_limit_cooldown}s")
                    await asyncio.sleep(self.rate_limit_cooldown)
                    continue
                
                if is_token_error(e):
                    self.logger.warning("Token error detected, may need refresh")
                
                delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                if self.jitter:
                    delay = delay * (0.5 + random.random())
                
                if attempt < self.max_attempts - 1:
                    await asyncio.sleep(delay)
        
        raise last_error


def with_retry(
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
    rate_limit_cooldown: float = 65.0,
):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            resilient = ResilientClient(
                logger=logging.getLogger(func.__module__),
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                jitter=jitter,
                rate_limit_cooldown=rate_limit_cooldown,
            )
            return await resilient.call_with_retry(func, *args, **kwargs)
        return wrapper
    return decorator
