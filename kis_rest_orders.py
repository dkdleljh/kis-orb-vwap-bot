from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import aiohttp
import asyncio

from models import OrderResult, Position


def _validate_symbol(symbol: str) -> None:
    """Validate stock symbol format."""
    if not symbol or not isinstance(symbol, str):
        raise ValueError("Symbol must be a non-empty string")
    if not symbol.isdigit() or len(symbol) != 6:
        raise ValueError(f"Symbol must be 6-digit string, got: {symbol}")


def _validate_positive_int(value: int, name: str) -> None:
    """Validate positive integer."""
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be positive integer, got: {value}")


def _validate_positive_number(value: float, name: str) -> None:
    """Validate positive number."""
    if not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be positive number, got: {value}")


@dataclass
class AccountInfo:
    account_no: str
    product_code: str


class KISRestOrders:
    """KIS REST 주문/조회 클라이언트.

    핵심 목표:
    - 호출마다 ClientSession을 생성/해제하지 않고 재사용(성능/안정성)
    - 타임아웃 정책을 한 군데에서 관리
    """

    def __init__(
        self,
        base_url: str,
        auth,
        account: AccountInfo,
        logger,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout_sec: float = 10.0,
    ):
        self.base_url = base_url
        self.auth = auth
        self.account = account
        self.logger = logger
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)

        self._external_session = session is not None
        self._session: aiohttp.ClientSession | None = session

    async def aclose(self) -> None:
        """Close internally-owned session."""
        if self._session and not self._external_session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        # 내부 생성 세션: 커넥션 풀을 적극 활용
        connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(
            timeout=self._timeout, connector=connector
        )
        return self._session

    async def _auth_headers(self) -> dict:
        await self.auth.fetch_token()
        return self.auth.auth_headers()

    def _check_error(self, data: dict, context: str) -> Optional[str]:
        rt_cd = data.get("rt_cd", "1")
        if rt_cd != "0":
            msg_cd = data.get("msg_cd", "")
            msg1 = data.get("msg1", "")
            msg = msg1 or msg_cd or "Unknown error"
            self.logger.error(
                f"{context} failed: rt_cd={rt_cd} msg_cd={msg_cd} msg={msg}"
            )
            return msg
        return None

    async def place_buy_limit(self, symbol: str, qty: int, price: float) -> OrderResult:
        _validate_symbol(symbol)
        _validate_positive_int(qty, "Quantity")
        _validate_positive_number(price, "Price")

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        payload = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "PDNO": symbol,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(int(price)),
        }
        headers = await self._auth_headers()
        headers.update({"tr_id": "TTTC0802U", "custtype": "P"})

        try:
            session = await self._get_session()
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()

            err = self._check_error(data, f"buy_limit({symbol})")
            if err:
                return OrderResult(order_id="", filled_qty=0, status=f"error: {err}")

            order_id = data.get("output", {}).get("ODNO", "")
            self.logger.info(
                f"buy_limit submitted: {symbol} qty={qty} price={price} order_id={order_id}"
            )
            return OrderResult(order_id=order_id, filled_qty=0, status="submitted")
        except Exception as e:
            self.logger.error(f"buy_limit exception: {e}")
            return OrderResult(order_id="", filled_qty=0, status=f"exception: {e}")

    async def place_sell_limit(
        self, symbol: str, qty: int, price: float
    ) -> OrderResult:
        _validate_symbol(symbol)
        _validate_positive_int(qty, "Quantity")
        _validate_positive_number(price, "Price")

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        payload = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "PDNO": symbol,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(int(price)),
        }
        headers = await self._auth_headers()
        headers.update({"tr_id": "TTTC0801U", "custtype": "P"})

        try:
            session = await self._get_session()
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()

            err = self._check_error(data, f"sell_limit({symbol})")
            if err:
                return OrderResult(order_id="", filled_qty=0, status=f"error: {err}")

            order_id = data.get("output", {}).get("ODNO", "")
            self.logger.info(
                f"sell_limit submitted: {symbol} qty={qty} price={price} order_id={order_id}"
            )
            return OrderResult(order_id=order_id, filled_qty=0, status="submitted")
        except Exception as e:
            self.logger.error(f"sell_limit exception: {e}")
            return OrderResult(order_id="", filled_qty=0, status=f"exception: {e}")

    async def place_sell_market(self, symbol: str, qty: int) -> OrderResult:
        _validate_symbol(symbol)
        _validate_positive_int(qty, "Quantity")

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        payload = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "PDNO": symbol,
            "ORD_DVSN": "01",
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        }
        headers = await self._auth_headers()
        headers.update({"tr_id": "TTTC0801U", "custtype": "P"})

        try:
            session = await self._get_session()
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()

            err = self._check_error(data, f"sell_market({symbol})")
            if err:
                return OrderResult(order_id="", filled_qty=0, status=f"error: {err}")

            order_id = data.get("output", {}).get("ODNO", "")
            self.logger.info(
                f"sell_market submitted: {symbol} qty={qty} order_id={order_id}"
            )
            return OrderResult(order_id=order_id, filled_qty=0, status="submitted")
        except Exception as e:
            self.logger.error(f"sell_market exception: {e}")
            return OrderResult(order_id="", filled_qty=0, status=f"exception: {e}")

    async def cancel_order(self, order_id: str, symbol: str, qty: int) -> bool:
        _validate_symbol(symbol)
        _validate_positive_int(qty, "Quantity")

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"
        payload = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "PDNO": symbol,
            "ORGN_ODNO": order_id,
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        }
        headers = await self._auth_headers()
        headers.update({"tr_id": "TTTC0803U", "custtype": "P"})

        try:
            session = await self._get_session()
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()

            err = self._check_error(data, f"cancel_order({order_id})")
            if err:
                self.logger.error(f"cancel_order failed: {err}")
                return False

            self.logger.info(f"cancel_order success: {order_id}")
            return True
        except Exception as e:
            self.logger.error(f"cancel_order exception: {e}")
            return False

    async def get_positions(self) -> Optional[Position]:
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
        }

        # Retry on rate-limit (EGW00201)
        try:
            session = await self._get_session()
            for attempt in range(4):
                headers = await self._auth_headers()
                headers.update({"tr_id": "TTTC8434R", "custtype": "P"})

                async with session.get(url, params=params, headers=headers) as resp:
                    data = await resp.json()

                err = self._check_error(data, "get_positions")
                if err:
                    if "EGW00201" in err or "초당" in err:
                        # backoff: 1s,2s,4s
                        wait = 1 * (2**attempt)
                        self.logger.warning(f"get_positions rate-limited. retry in {wait}s: {err}")
                        await asyncio.sleep(wait)
                        continue
                    self.logger.error(f"get_positions failed: {err}")
                    return None

                # success
                break
            else:
                return None

            outputs = data.get("output1", [])
            for row in outputs:
                sym = row.get("pdno", "")
                qty = int(float(row.get("hldg_qty", 0) or 0))
                if qty > 0:
                    avg_price_candidates = [
                        "pchs_avg_pric",
                        "pchs_avg_pric_unpr",
                        "avg_pric",
                    ]
                    avg_price = 0.0
                    for k in avg_price_candidates:
                        v = row.get(k)
                        if v:
                            avg_price = float(v)
                            break
                    self.logger.info(f"position found: {sym} qty={qty} avg={avg_price}")
                    return Position(symbol=sym, qty=qty, avg_price=avg_price)
            return None
        except Exception as e:
            self.logger.error(f"get_positions exception: {e}")
            return None

    async def get_all_positions(self) -> Dict[str, Dict[str, any]]:
        """전체 포지션 맵을 반환합니다 (Reconcile용).

        Returns:
            Dict mapping symbol -> {qty, avg_price, ...}
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
        }
        headers = await self._auth_headers()
        headers.update({"tr_id": "TTTC8434R", "custtype": "P"})

        positions: Dict[str, Dict[str, any]] = {}
        try:
            session = await self._get_session()
            async with session.get(url, params=params, headers=headers) as resp:
                data = await resp.json()

            err = self._check_error(data, "get_all_positions")
            if err:
                self.logger.error(f"get_all_positions failed: {err}")
                return positions

            outputs = data.get("output1", [])
            for row in outputs:
                sym = row.get("pdno", "")
                qty = int(float(row.get("hldg_qty", 0) or 0))
                if qty > 0:
                    avg_price_candidates = [
                        "pchs_avg_pric",
                        "pchs_avg_pric_unpr",
                        "avg_pric",
                    ]
                    avg_price = 0.0
                    for k in avg_price_candidates:
                        v = row.get(k)
                        if v:
                            avg_price = float(v)
                            break
                    positions[sym] = {
                        "qty": qty,
                        "avg_price": avg_price,
                    }
                    self.logger.info(
                        f"position for reconcile: {sym} qty={qty} avg={avg_price}"
                    )
            return positions
        except Exception as e:
            self.logger.error(f"get_all_positions exception: {e}")
            return positions

    async def get_cash_available(self, symbol: str, price: float) -> float:
        _validate_symbol(symbol)
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
        headers = await self._auth_headers()
        headers.update({"tr_id": "TTTC8908R", "custtype": "P"})

        base_params = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "PDNO": symbol,
            "ORD_UNPR": str(int(price) if price else 0),
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }

        params_variants: list[tuple[str, dict]] = [
            # Some KIS environments reject order-division fields; try without them first.
            ("NONE", {**base_params}),
            ("ORD_DVSN_CD", {**base_params, "ORD_DVSN_CD": "00"}),
            ("ORD_DVSN", {**base_params, "ORD_DVSN": "00"}),
        ]

        async def _parse_cash(data: dict) -> float:
            cash_candidates = ["ord_psbl_cash", "psbl_cash", "ord_psbl_cash_unpr"]
            for k in cash_candidates:
                v = (data.get("output", {}) or {}).get(k)
                if v not in (None, ""):
                    try:
                        cash = float(v) if v is not None else 0.0
                        self.logger.info(f"cash_available: {cash}")
                        return cash
                    except Exception:
                        continue
            return 0.0

        try:
            session = await self._get_session()
            last_err = None
            for key_name, params in params_variants:
                try:
                    # Retry on rate-limit (EGW00201)
                    for attempt in range(4):
                        async with session.get(url, params=params, headers=headers) as resp:
                            try:
                                data = await resp.json()
                            except Exception:
                                text = await resp.text()
                                self.logger.error(
                                    f"get_cash_available({key_name}) non-json resp: status={resp.status} text={text[:200]}"
                                )
                                data = None

                        if not isinstance(data, dict):
                            break

                        rt_cd = str(data.get("rt_cd", "1"))
                        if rt_cd == "0":
                            return await _parse_cash(data)

                        msg_cd = str(data.get("msg_cd", "") or "")
                        msg1 = str(data.get("msg1", "") or "")
                        msg = msg1 or msg_cd or "Unknown"
                        err = f"rt_cd={rt_cd} msg_cd={msg_cd} msg={msg}"

                        if "EGW00201" in err or "초당" in err:
                            wait = 1 * (2**attempt)
                            self.logger.warning(f"get_cash_available rate-limited. retry in {wait}s: {err}")
                            await asyncio.sleep(wait)
                            continue

                        # fallthrough to variant handling
                        break

                    # if we reached here, data contains an error that wasn't rate-limit handled
                    if not isinstance(data, dict):
                        continue

                    rt_cd = str(data.get("rt_cd", "1"))
                    msg_cd = str(data.get("msg_cd", "") or "")
                    msg1 = str(data.get("msg1", "") or "")
                    msg = msg1 or msg_cd or "Unknown"
                    err = f"rt_cd={rt_cd} msg_cd={msg_cd} msg={msg}"

                    if "INPUT_FIELD_NAME" in msg or "INPUT" in msg:
                        self.logger.warning(
                            f"get_cash_available: {key_name} rejected ({err}) -> trying next variant"
                        )
                        last_err = err
                        continue

                    self.logger.error(f"get_cash_available({key_name}) failed: {err}")
                    last_err = err
                    break
                except Exception as e:
                    last_err = str(e)
                    self.logger.error(f"get_cash_available({key_name}) exception: {e}")
                    continue

            if last_err:
                self.logger.warning(
                    f"get_cash_available failed after variants: {last_err}"
                )
            return 0.0
        except Exception as e:
            self.logger.error(f"get_cash_available exception: {e}")
            return 0.0

    async def get_quote(self, symbol: str) -> dict:
        _validate_symbol(symbol)
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        headers = await self._auth_headers()
        headers.update({"tr_id": "FHKST01010100"})

        try:
            session = await self._get_session()
            async with session.get(url, params=params, headers=headers) as resp:
                data = await resp.json()
            return data
        except Exception as e:
            self.logger.error(f"get_quote({symbol}) exception: {e}")
            return {}

    async def get_open_orders(self) -> list[dict]:
        """Best-effort open order query.

        Phase5 reconcile needs a broker-side view of working (unfilled) orders.

        Recommended behavior:
        - never raise (best-effort)
        - if endpoint/fields mismatch, return [] and let callers treat it as "unknown"

        Notes:
        - KIS provides a cancel/modify-possible order list endpoint commonly used
          as an "open orders" proxy.
        """

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
        headers = await self._auth_headers()
        headers.update({"tr_id": "TTTC8036R", "custtype": "P"})

        # Base params; KIS uses ctx paging keys.
        params = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            # Inquiry divisions vary by document; keep conservative defaults.
            "INQR_DVSN_1": "0",
            "INQR_DVSN_2": "0",
        }

        try:
            session = await self._get_session()
            async with session.get(url, params=params, headers=headers) as resp:
                data = await resp.json()

            err = self._check_error(data, "get_open_orders")
            if err:
                self.logger.warning(f"get_open_orders failed: {err}")
                return []

            # outputs may be output/output1 depending on environment
            out = data.get("output")
            if isinstance(out, list):
                return [dict(x) for x in out]
            out1 = data.get("output1")
            if isinstance(out1, list):
                return [dict(x) for x in out1]

            return []
        except Exception as e:
            self.logger.warning(f"get_open_orders exception: {e}")
            return []

    async def get_fills(
        self,
        symbol: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
    ) -> list[dict]:
        """Get execution/fill history from broker.

        Phase5 LED-01: Fill source for ledger reconciliation.

        Args:
            symbol: Optional stock code (6 digits). If None, returns all symbols.
            start_date: Start date in YYYYMMDD format
            end_date: End date in YYYYMMDD format
            page: Page number for pagination

        Returns:
            List of fill records with fields:
            - fill_id: Unique execution ID
            - order_id: Related order ID
            - symbol: Stock code
            - side: BUY/SELL
            - qty: Executed quantity
            - price: Execution price
            - ts: Execution timestamp
            - fee: Commission (if available)
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-ccnl"
        headers = await self._auth_headers()
        headers.update({"tr_id": "TTTC8001R", "custtype": "P"})

        from datetime import datetime, timedelta

        if not end_date:
            end_date = datetime.now().strftime("%Y%m%d")
        if not start_date:
            start_date = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

        params = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1": "0",
            "INQR_DVSN_2": "0",
            "INQR_STRT_DT": start_date,
            "INQR_END_DT": end_date,
            "SLL_BUY_DVSN_CD": "00",
            "CASH_MGN_DVSN_CD": "00",
            "PRCS_DVSN_CD": "00",
            "OFL_YN": "",
            "page": str(page),
        }

        if symbol:
            params["PDNO"] = symbol

        try:
            session = await self._get_session()
            async with session.get(url, params=params, headers=headers) as resp:
                # KIS sometimes returns JSON with missing/incorrect content-type.
                data = None
                try:
                    data = await resp.json()
                except Exception:
                    try:
                        text = await resp.text()
                        if text:
                            import json as _json
                            data = _json.loads(text)
                    except Exception:
                        # Broker outage / non-json: treat as no fills.
                        return []

            if not isinstance(data, dict):
                return []

            err = self._check_error(data, "get_fills")
            if err:
                self.logger.warning(f"get_fills failed: {err}")
                return []

            fills = []
            out = data.get("output")
            if isinstance(out, list):
                for item in out:
                    fill_id = (
                        item.get("orgn_odno")
                        or item.get("odno")
                        or f"{item.get('cncl_cntr')}@{item.get('exec_prc')}@{item.get('exec_qty')}"
                    )

                    # KIS: 01=SELL, 02=BUY in many endpoints. Keep robust mapping.
                    side_map = {
                        "01": "SELL",
                        "02": "BUY",
                        "1": "BUY",
                        "2": "SELL",
                        "BUY": "BUY",
                        "SELL": "SELL",
                        "买入": "BUY",
                        "卖出": "SELL",
                    }
                    raw_side = item.get("sll_buy_dvsn_cd") or item.get("SLL_BUY_DVSN_CD")
                    side = side_map.get(str(raw_side).strip(), "") or "BUY"

                    # price/qty fields vary by environment; pick the best available.
                    px = None
                    for k in [
                        "exec_prc",
                        "cntr_pric",
                        "cntr_pric2",
                        "ccld_unpr",
                        "cntr_prc",
                        "ord_unpr",
                    ]:
                        v = item.get(k)
                        if v not in (None, ""):
                            try:
                                px = float(v)
                                break
                            except Exception:
                                pass
                    if px is None:
                        px = 0.0

                    q = None
                    for k in ["exec_qty", "cntr_qty", "ccld_qty", "ord_qty"]:
                        v = item.get(k)
                        if v not in (None, ""):
                            try:
                                q = int(float(v))
                                break
                            except Exception:
                                pass
                    if q is None:
                        q = 0

                    ts = ""
                    dt = str(item.get("exec_dt") or item.get("ord_dt") or "")
                    tm = str(item.get("exec_tm") or item.get("ord_tmd") or item.get("cntr_tm") or "")
                    if dt and tm:
                        ts = dt + tm
                    else:
                        ts = dt or tm

                    fills.append(
                        {
                            "fill_id": str(fill_id),
                            "order_id": str(item.get("odno", "")),
                            "symbol": str(item.get("pdno", symbol or "")),
                            "side": side,
                            "qty": int(q),
                            "price": float(px),
                            "ts": ts,
                            "fee": float(item.get("comm_tax", 0) or 0),
                            "broker_order_id": str(item.get("orgn_odno", "")),
                        }
                    )

            return fills

        except Exception as e:
            self.logger.warning(f"get_fills exception: {e}")
            return []

    def _generate_fill_id(self, order_id: str, price: float, qty: int, ts: str) -> str:
        """Generate deterministic fill ID if broker doesn't provide one.

        Used as fallback when broker execution ID is not available.
        """
        import hashlib

        raw = f"{order_id}:{price}:{qty}:{ts}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
