"""US pre-market quote collector.

Collects best-effort quote snapshots for a list of symbols and stores JSONL rows.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List


@dataclass
class CollectStats:
    requested: int = 0
    collected: int = 0
    failed: int = 0
    skipped_cooldown: int = 0


class PremarketCollector:
    def __init__(
        self,
        *,
        rest_client: Any,
        logger: Any,
        data_root: Path,
        market: str = "US",
        poll_sec: int = 60,
        max_recent: int = 720,
    ) -> None:
        self.rest = rest_client
        self.logger = logger
        self.data_root = Path(data_root)
        self.market = str(market or "US").strip().upper()
        if self.market not in {"US", "KR"}:
            self.market = "US"
        self.poll_sec = max(10, int(poll_sec))

        self._cooldown_until: Dict[str, float] = {}
        self._fail_streak: Dict[str, int] = defaultdict(int)
        self._recent: Dict[str, Deque[Dict[str, Any]]] = defaultdict(lambda: deque(maxlen=max_recent))

    def _extract_quote_fields_us(self, quote: Dict[str, Any]) -> Dict[str, float | None]:
        out = quote.get("output") if isinstance(quote.get("output"), dict) else quote

        def _get_float(*keys: str) -> float | None:
            for key in keys:
                try:
                    v = out.get(key)
                except Exception:
                    v = None
                if v in (None, ""):
                    continue
                try:
                    return float(v)
                except Exception:
                    continue
            return None

        return {
            "last": _get_float("last", "lastxch", "last_price", "price"),
            "bid": _get_float("bid", "bidp1", "bid_price", "bido"),
            "ask": _get_float("ask", "askp1", "ask_price", "askp"),
            "vol": _get_float("tvol", "volume", "vol"),
        }

    def _extract_quote_fields_kr(self, quote: Dict[str, Any]) -> Dict[str, float | None]:
        out = quote.get("output") if isinstance(quote.get("output"), dict) else quote

        def _get_float(*keys: str) -> float | None:
            for key in keys:
                try:
                    v = out.get(key)
                except Exception:
                    v = None
                if v in (None, ""):
                    continue
                try:
                    return float(v)
                except Exception:
                    continue
            return None

        return {
            "last": _get_float("stck_prpr", "last", "last_price", "price"),
            "bid": _get_float("stck_bprc", "bid", "bidp1", "bid_price", "bido"),
            "ask": _get_float("stck_aprc", "ask", "askp1", "ask_price", "askp"),
            "vol": _get_float("acml_vol", "tvol", "volume", "vol"),
        }

    def _extract_quote_fields(self, quote: Dict[str, Any]) -> Dict[str, float | None]:
        if self.market == "KR":
            return self._extract_quote_fields_kr(quote)
        return self._extract_quote_fields_us(quote)

    def _day_dir(self, now: datetime) -> Path:
        return self.data_root / f"premarket_{self.market.lower()}" / now.strftime("%Y%m%d")

    def _symbol_path(self, symbol: str, now: datetime) -> Path:
        return self._day_dir(now) / f"{symbol}.jsonl"

    def recent_samples(self, symbol: str) -> List[Dict[str, Any]]:
        return list(self._recent.get(symbol, []))

    async def _fetch_symbol(self, symbol: str) -> Dict[str, Any] | None:
        now_ts = datetime.now().timestamp()
        if now_ts < self._cooldown_until.get(symbol, 0.0):
            return None

        last_err: Exception | None = None
        for i in range(3):
            try:
                q = await self.rest.get_quote(symbol)
                fields = self._extract_quote_fields(q if isinstance(q, dict) else {})
                if fields.get("last") is None:
                    raise ValueError("quote missing last")
                self._fail_streak[symbol] = 0
                now = datetime.now()
                sample = {
                    "timestamp": now.isoformat(),
                    "symbol": symbol,
                    "last": float(fields.get("last") or 0.0),
                    "bid": float(fields.get("bid") or 0.0),
                    "ask": float(fields.get("ask") or 0.0),
                    "vol": float(fields.get("vol") or 0.0),
                }
                return sample
            except Exception as e:
                last_err = e
                await asyncio.sleep(min(2.0, (0.2 * (2**i))))

        self._fail_streak[symbol] += 1
        if self._fail_streak[symbol] >= 3:
            cooldown = min(300.0, 30.0 * float(self._fail_streak[symbol]))
            self._cooldown_until[symbol] = now_ts + cooldown
        if last_err:
            self.logger.warning(f"premarket quote fetch failed: {symbol} err={last_err}")
        return {}

    async def collect_once(self, symbols: List[str]) -> CollectStats:
        stats = CollectStats(requested=len(symbols))
        now = datetime.now()
        day_dir = self._day_dir(now)
        day_dir.mkdir(parents=True, exist_ok=True)

        for raw in symbols:
            symbol = (raw or "").strip().upper()
            if not symbol:
                continue

            if datetime.now().timestamp() < self._cooldown_until.get(symbol, 0.0):
                stats.skipped_cooldown += 1
                continue

            sample = await self._fetch_symbol(symbol)
            if sample is None:
                stats.skipped_cooldown += 1
                continue
            if not sample:
                stats.failed += 1
                continue

            path = self._symbol_path(symbol, now)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")

            self._recent[symbol].append(sample)
            stats.collected += 1

        return stats
