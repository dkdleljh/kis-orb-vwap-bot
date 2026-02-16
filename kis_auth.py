import os
import json
import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import aiohttp


@dataclass
class AuthToken:
    access_token: str
    token_type: str
    expires_in: int


@dataclass
class ApprovalKey:
    approval_key: str
    expires_in: int


class KISAuth:
    """KIS authentication helper with caching and rate-limit protection.

    Safety note:
        This project historically returned dummy tokens outside market hours to
        avoid repeated API calls and rate limits. That behavior is convenient
        for local development but is dangerous in production because it can
        mask authentication failures.

        When live mode is enabled (KIS_LIVE_ENABLED=1), dummy credentials are
        disabled by default. You can override only for debugging by setting
        KIS_ALLOW_DUMMY_CREDENTIALS=1.
    """

    def __init__(self, base_url: str, app_key: str, app_secret: str, logger):
        self.base_url = base_url
        self.app_key = app_key
        self.app_secret = app_secret
        self.logger = logger
        self.token: Optional[AuthToken] = None
        self.approval_key: Optional[ApprovalKey] = None

        self._token_expire_at: float = 0.0
        self._approval_expire_at: float = 0.0
        # 동시 호출 시 토큰 폭주(1분당 1회 제한) 방지
        self._lock = asyncio.Lock()
        self._cache_path = os.environ.get("KIS_TOKEN_CACHE_PATH", ".kis_token_cache.json")
        self._token_min_interval_sec = float(os.environ.get("KIS_TOKEN_MIN_INTERVAL_SEC", "62"))
        self._approval_min_interval_sec = float(os.environ.get("KIS_APPROVAL_MIN_INTERVAL_SEC", "62"))
        self._token_next_allowed_at = 0.0
        self._approval_next_allowed_at = 0.0
        self._market_closed_log_interval_sec = int(os.environ.get("KIS_MARKET_CLOSED_LOG_INTERVAL_SEC", "1200"))
        self._last_market_closed_log_at = 0.0
        self._load_token_cache()

    def _now(self) -> float:
        return time.time()

    def _live_mode_enabled(self) -> bool:
        # Align with TradingEngine safety flags.
        return os.environ.get("KIS_LIVE_ENABLED", "0") == "1"

    def _allow_dummy_credentials(self) -> bool:
        if os.environ.get("KIS_ALLOW_DUMMY_CREDENTIALS", "0") == "1":
            return True
        # In live mode, do not allow dummy credentials by default.
        return not self._live_mode_enabled()

    def _assert_dummy_allowed(self, kind: str) -> None:
        if not self._allow_dummy_credentials():
            raise RuntimeError(
                f"Refusing to use dummy {kind} in live mode. "
                "Set KIS_ALLOW_DUMMY_CREDENTIALS=1 only for debugging."
            )

    def _is_kor_market_window(self, now: datetime) -> bool:
        # KST 기준 정규장(대략) + 주말 제외
        if now.weekday() >= 5:  # 5=Sat, 6=Sun
            return False
        return (8 <= now.hour < 15) or (now.hour == 15 and now.minute <= 40)

    def _should_log_market_closed(self) -> bool:
        ts = self._now()
        if ts - self._last_market_closed_log_at >= self._market_closed_log_interval_sec:
            self._last_market_closed_log_at = ts
            return True
        return False

    def _warn_if_insecure_permissions(self, path: str) -> None:
        try:
            st = os.stat(path)
            if (st.st_mode & 0o077) != 0:
                self.logger.warning(
                    "token cache file permissions are too open: %s (mode=%o). Recommended: chmod 600 %s",
                    path,
                    st.st_mode & 0o777,
                    path,
                )
        except Exception:
            return

    def _load_token_cache(self) -> None:
        if not self._cache_path:
            return
        try:
            if not os.path.exists(self._cache_path):
                return
            self._warn_if_insecure_permissions(self._cache_path)
            with open(self._cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            token = AuthToken(
                access_token=str(data.get("access_token", "")),
                token_type=str(data.get("token_type", "")),
                expires_in=int(data.get("expires_in", 0)),
            )
            expire_at = float(data.get("expire_at_epoch", 0.0) or 0.0)
            if token.access_token and token.token_type and expire_at > (self._now() + 60):
                self.token = token
                self._token_expire_at = expire_at
                self.logger.info("auth token restored from cache")
        except Exception as e:
            self.logger.warning(f"token cache load failed: {e}")

    def _save_token_cache(self) -> None:
        if not self._cache_path or not self.token:
            return
        payload = {
            "access_token": self.token.access_token,
            "token_type": self.token.token_type,
            "expires_in": int(self.token.expires_in),
            "expire_at_epoch": float(self._token_expire_at),
            "cached_at_epoch": self._now(),
        }
        try:
            # Minimal hardening: ensure only the current user can read it.
            # (Full encryption is out of scope without key management.)
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=True)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(self._cache_path, 0o600)
            except Exception:
                # Best effort: chmod may fail on some filesystems.
                pass
        except Exception as e:
            self.logger.warning(f"token cache save failed: {e}")

    async def fetch_token(self, force: bool = False) -> AuthToken:
        """REST 인증 토큰 발급(캐시/만료 관리 포함)."""
        now = datetime.now()

        if self.token and self._now() < (self._token_expire_at - 60):
            return self.token

        if not self._is_kor_market_window(now):
            if not force:
                if self.token:
                    if self._should_log_market_closed():
                        self.logger.info(f"Market Closed ({now.strftime('%H:%M')}). Using cached token.")
                    return self.token

                # No cached token available.
                self._assert_dummy_allowed("token")
                if self._should_log_market_closed():
                    self.logger.info(
                        f"Market Closed ({now.strftime('%H:%M')}). No token. Using dummy token (dev mode)."
                    )
                self.token = AuthToken("DUMMY", "Bearer", 0)
                self._token_expire_at = self._now() + 300
                return self.token

        async with self._lock:
            if self.token and self._now() < (self._token_expire_at - 60):
                return self.token

            if not force:
                if self.token:
                    self.logger.warning("token expired but present; using existing token")
                    return self.token

            url = f"{self.base_url}/oauth2/tokenP"
            payload = {
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            }

            delay = 1.0
            data: dict = {}
            for attempt in range(1, 6):
                wait = self._token_next_allowed_at - self._now()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._token_next_allowed_at = self._now() + self._token_min_interval_sec
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, json=payload, timeout=10) as resp:
                            data = await resp.json()
                            if resp.status != 200:
                                msg = str(data)
                                is_rate_limited = resp.status in (403, 429) or "EGW00133" in msg or "1분당 1회" in msg
                                if is_rate_limited and attempt < 5:
                                    jitter = random.uniform(0.0, 1.0)
                                    sleep_s = min(120.0, delay + jitter)
                                    self.logger.warning(
                                        f"token fetch rate-limited ({resp.status}) attempt={attempt}/5; backoff {sleep_s:.1f}s"
                                    )
                                    await asyncio.sleep(sleep_s)
                                    delay = min(delay * 2.0, 60.0)
                                    continue
                                if self.token:
                                    self.logger.warning(
                                        f"token fetch failed({resp.status}), using existing token: {data}"
                                    )
                                    return self.token
                                raise RuntimeError(f"token_fetch_failed status={resp.status} data={data}")
                    break
                except Exception as e:
                    if attempt < 5:
                        jitter = random.uniform(0.0, 1.0)
                        sleep_s = min(120.0, delay + jitter)
                        self.logger.warning(
                            f"token fetch exception attempt={attempt}/5; backoff {sleep_s:.1f}s: {e}"
                        )
                        await asyncio.sleep(sleep_s)
                        delay = min(delay * 2.0, 60.0)
                        continue
                    if self.token:
                        self.logger.warning(f"token fetch exception, using existing token: {e}")
                        return self.token
                    raise

            token = AuthToken(
                access_token=str(data.get("access_token", "")),
                token_type=str(data.get("token_type", "")),
                expires_in=int(data.get("expires_in", 0)),
            )
            if not token.access_token or not token.token_type:
                raise RuntimeError(f"token_invalid data={data}")

            self.token = token
            self._token_expire_at = self._now() + max(0, token.expires_in)
            self._save_token_cache()
            self.logger.info("auth token fetched")
            return token

    async def fetch_approval_key(self, force: bool = False) -> ApprovalKey:
        """WebSocket 접속용 approval_key 발급(캐시/만료 관리 포함).

        NOTE: 주말/장외에는 approval 발급을 시도하지 않고(불필요한 호출/레이트리밋 방지)
        기존 approval_key가 있으면 재사용합니다.

        For local/dev convenience, a dummy approval key can be returned when
        there is no cached approval key. In live mode this is disabled.
        """
        now = datetime.now()
        if (not force) and (not self._is_kor_market_window(now)):
            if self.approval_key and self._now() < (self._approval_expire_at - 60):
                if self._should_log_market_closed():
                    self.logger.info(f"Market Closed ({now.strftime('%H:%M')}). Using cached approval key.")
                return self.approval_key

            self._assert_dummy_allowed("approval_key")
            if self._should_log_market_closed():
                self.logger.info(
                    f"Market Closed ({now.strftime('%H:%M')}). No approval key. Returning dummy approval key (dev mode)."
                )
            return ApprovalKey("DUMMY", 0)

        async with self._lock:
            if (not force) and self.approval_key and self._now() < (self._approval_expire_at - 60):
                return self.approval_key

            url = f"{self.base_url}/oauth2/Approval"
            payload = {
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "secretkey": self.app_secret,
            }
            delay = 1.0
            data: dict = {}
            for attempt in range(1, 6):
                wait = self._approval_next_allowed_at - self._now()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._approval_next_allowed_at = self._now() + self._approval_min_interval_sec
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, json=payload, timeout=10) as resp:
                            data = await resp.json()
                            if resp.status != 200:
                                msg = str(data)
                                is_rate_limited = resp.status in (403, 429) or "EGW00133" in msg or "1분당 1회" in msg
                                if is_rate_limited and attempt < 5:
                                    jitter = random.uniform(0.0, 1.0)
                                    sleep_s = min(120.0, delay + jitter)
                                    self.logger.warning(
                                        f"approval key rate-limited ({resp.status}) attempt={attempt}/5; backoff {sleep_s:.1f}s"
                                    )
                                    await asyncio.sleep(sleep_s)
                                    delay = min(delay * 2.0, 60.0)
                                    continue
                                raise RuntimeError(
                                    f"approval_key_fetch_failed status={resp.status} data={data}"
                                )
                    break
                except Exception:
                    if attempt >= 5:
                        raise
                    jitter = random.uniform(0.0, 1.0)
                    sleep_s = min(120.0, delay + jitter)
                    await asyncio.sleep(sleep_s)
                    delay = min(delay * 2.0, 60.0)

            approval_key = ApprovalKey(
                approval_key=str(data.get("approval_key", "")),
                expires_in=int(data.get("expires_in", 0)),
            )
            if not approval_key.approval_key:
                raise RuntimeError(f"approval_key_invalid data={data}")

            self.approval_key = approval_key
            self._approval_expire_at = self._now() + max(0, approval_key.expires_in)
            self.logger.info("websocket approval key fetched")
            return approval_key

    def auth_headers(self) -> dict:
        if not self.token:
            raise RuntimeError("token not initialized")
        if self.token.access_token == "DUMMY":
            self._assert_dummy_allowed("token")
        return {
            "authorization": f"{self.token.token_type} {self.token.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }


def load_auth_from_env():
    return (
        os.environ.get("KIS_APP_KEY", ""),
        os.environ.get("KIS_APP_SECRET", ""),
        os.environ.get("KIS_HTS_ID", ""),
    )
