"""Prefetch KIS REST access token.

Policy (recommended):
- Try to prefetch 1 hour before KR/US regular market open.
- If we fetched a token less than 24h ago, skip prefetch.
- If token is expired / expiring soon, refresh regardless (safety override).

This script is intended to be run by OS cron/systemd timers.
It is safe to run frequently: it will self-throttle via cache + KISAuth rate limits.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# Project root import setup
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_dotenv(path: str) -> None:
    """Very small .env loader (avoids extra dependencies for cron runs)."""
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if (not line) or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and (k not in os.environ):
                    os.environ[k] = v
    except Exception:
        return


_load_dotenv(os.path.join(ROOT, ".env"))

from logger import setup_logger  # type: ignore
from kis_auth import KISAuth, load_auth_from_env  # type: ignore
from utils_holiday import is_market_open as is_kr_market_open  # type: ignore
from modules.mijang import is_us_market_holiday, _get_market_hours_korea  # type: ignore


@dataclass(frozen=True)
class CacheInfo:
    cached_at: float
    expire_at: float


def _load_cache(path: str) -> CacheInfo | None:
    try:
        if not path or (not os.path.exists(path)):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cached_at = float(data.get("cached_at_epoch", 0.0) or 0.0)
        expire_at = float(data.get("expire_at_epoch", 0.0) or 0.0)
        if cached_at <= 0:
            return None
        return CacheInfo(cached_at=cached_at, expire_at=expire_at)
    except Exception:
        return None


def _kst_now() -> datetime:
    if ZoneInfo is None:
        return datetime.now()
    return datetime.now(ZoneInfo("Asia/Seoul"))


def _minutes_since_midnight(t: datetime) -> int:
    return t.hour * 60 + t.minute


def _within_window(now_min: int, target_min: int, window_min: int = 7) -> bool:
    return abs(now_min - target_min) <= window_min


def should_prefetch_kr(now: datetime) -> bool:
    # KR regular open: 09:00 KST → prefetch at 08:00 KST
    if not is_kr_market_open(now.date()):
        return False
    target = 8 * 60  # 08:00
    return _within_window(_minutes_since_midnight(now), target, window_min=7)


def should_prefetch_us(now: datetime) -> bool:
    # US holiday check uses US holiday calendar, but we evaluate using KST date.
    # This is a best-effort heuristic; trading engine still validates sessions.
    if now.weekday() >= 5:
        return False
    if is_us_market_holiday(now.date()):
        return False

    us_open_kst, _us_close_kst = _get_market_hours_korea()
    open_min = us_open_kst.hour * 60 + us_open_kst.minute
    prefetch_min = (open_min - 60) % (24 * 60)

    # Allow window across midnight.
    now_min = _minutes_since_midnight(now)
    if open_min > 0 and prefetch_min > open_min:
        # prefetch is previous day; accept both sides near midnight
        return (now_min >= prefetch_min) or (now_min <= (open_min + 7))
    return _within_window(now_min, prefetch_min, window_min=7)


async def _run() -> int:
    logger = setup_logger(log_dir=os.path.join(ROOT, "logs"), tz="Asia/Seoul")

    base_url = os.environ.get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
    app_key, app_secret, _hts_id = load_auth_from_env()
    if not app_key or not app_secret:
        logger.error("Missing KIS_APP_KEY/KIS_APP_SECRET in env")
        return 2

    now = _kst_now()
    want = should_prefetch_kr(now) or should_prefetch_us(now)
    if not want:
        logger.info("prefetch: skip (not in pre-open window)")
        return 0

    cache_path = os.environ.get("KIS_TOKEN_CACHE_PATH", os.path.join(ROOT, ".kis_token_cache.json"))
    cache = _load_cache(cache_path)

    # Policy knobs
    min_age_sec = int(os.environ.get("KIS_TOKEN_PREFETCH_MIN_AGE_SEC", str(24 * 3600)))
    expires_soon_sec = int(os.environ.get("KIS_TOKEN_EXPIRES_SOON_SEC", "600"))

    now_ts = time.time()

    if cache is not None:
        age = now_ts - cache.cached_at
        expires_in = cache.expire_at - now_ts

        if expires_in > expires_soon_sec and age < min_age_sec:
            logger.info(
                "prefetch: skip (cached %.1fh ago, expires in %.1fmin)",
                age / 3600.0,
                expires_in / 60.0,
            )
            return 0

        if expires_in <= expires_soon_sec:
            logger.info(
                "prefetch: token expires soon (%.1fmin), refreshing",
                expires_in / 60.0,
            )
        else:
            logger.info(
                "prefetch: age %.1fh >= %.1fh; refreshing",
                age / 3600.0,
                min_age_sec / 3600.0,
            )
    else:
        logger.info("prefetch: no cache; fetching")

    auth = KISAuth(base_url=base_url, app_key=app_key, app_secret=app_secret, logger=logger)

    # Force=True to allow night KST prefetch for US trading days.
    await auth.fetch_token(force=True)
    logger.info("prefetch: done")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
