"""KIS Overseas Stock REST API Client.

해외주식(NASDAQ/NYSE 등) 주문/조회 REST 클라이언트입니다.

✅ 운영 품질 목표(100점):
- ClientSession 재사용(요청마다 세션 생성 금지)
- timeout 정책을 한 곳에서 관리
- best-effort 실패 처리(필요 시 호출부가 분기 가능)

NOTE:
- 본 파일은 국내 REST 클라이언트(kis_rest_orders.py)의 패턴을 최대한 맞춥니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Any

import aiohttp
import asyncio
import json
import random

from models import OrderResult, Position
from kis_rest_orders import AccountInfo
from core.rate_limiter import get_global_rate_limiter


# KIS exchange codes:
# - Trading/order endpoints often use: NASD / NYSE / AMEX
# - Quote endpoints often use: NAS / NYS / AMS
EXCHANGE_LABELS = {
    "NASD": "나스닥",
    "NYSE": "뉴욕",
    "AMEX": "아멕스",
    "SEHK": "홍콩",
    "TKSE": "도쿄",
}

QUOTE_EXCD_MAP = {
    "NASD": "NAS",
    "NYSE": "NYS",
    "AMEX": "AMS",
}


def _validate_symbol(symbol: str) -> None:
    if not symbol or not isinstance(symbol, str):
        raise ValueError("Symbol must be a non-empty string")


def _validate_positive_int(value: int, name: str) -> None:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be positive integer, got: {value}")


def _validate_positive_number(value: float, name: str) -> None:
    if not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be positive number, got: {value}")


class KISOverseasRestOrders:
    """해외주식 주문 및 시세 조회 클라이언트 (세션 재사용).

    Note:
    - Some legacy codepaths expect `get_positions()` to return a single Position.
      For full portfolio visibility, prefer `get_positions_list()`.
    """

    def __init__(
        self,
        base_url: str,
        auth,
        account: AccountInfo,
        logger,
        exchange: str = "NASD",
        *,
        session: aiohttp.ClientSession | None = None,
        timeout_sec: float = 10.0,
    ):
        self.base_url = base_url
        self.auth = auth
        self.account = account
        self.logger = logger
        self.exchange = exchange  # trading code (NASD/NYSE/AMEX)

        # Best-effort symbol→exchange cache (helps route AMEX/NASD/NYSE correctly)
        self._symbol_exchange_cache: dict[str, str] = {}

        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._external_session = session is not None
        self._session: aiohttp.ClientSession | None = session
        self._rate_limiter = get_global_rate_limiter()

    async def aclose(self) -> None:
        if self._session and not self._external_session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(timeout=self._timeout, connector=connector)
        return self._session

    async def _auth_headers(self) -> dict:
        # 해외도 토큰은 필요. (필요 시 force는 호출부에서)
        await self.auth.fetch_token()
        return self.auth.auth_headers()

    def _check_error(self, data: dict, context: str) -> Optional[str]:
        rt_cd = str(data.get("rt_cd", "1"))
        if rt_cd != "0":
            msg = str(data.get("msg1") or data.get("msg_cd") or "Unknown error")
            self.logger.error(f"{context} failed: rt_cd={rt_cd} msg={msg}")
            return msg
        return None

    @staticmethod
    def _is_rate_limit_error(data: dict) -> bool:
        msg_cd = str(data.get("msg_cd", "") or "")
        msg1 = str(data.get("msg1", "") or "")
        merged = f"{msg_cd} {msg1}"
        return ("EGW00201" in merged) or ("초당" in merged)

    @staticmethod
    def _is_cancel_retry_error(data: dict) -> bool:
        msg_cd = str(data.get("msg_cd", "") or "")
        msg1 = str(data.get("msg1", "") or "")
        merged = f"{msg_cd} {msg1}"
        return "IGW00019" in merged

    @staticmethod
    def _retry_delay(attempt: int, *, base: float = 0.5, cap: float = 8.0) -> float:
        jitter = random.uniform(0.0, 0.35)
        return min(cap, base * (2 ** max(0, attempt))) + jitter

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        data_payload: str | None = None,
        context: str,
        max_attempts: int = 1,
        retry_on_rate_limit: bool = False,
        retry_on_cancel_error: bool = False,
        retry_on_transport: bool = False,
    ) -> dict:
        session = await self._get_session()
        last_data: dict | None = None

        for attempt in range(max(1, int(max_attempts))):
            try:
                await self._rate_limiter.acquire()
                async with session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    headers=headers,
                    data=data_payload,
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        text = await resp.text()
                        try:
                            data = json.loads(text) if text else {}
                        except Exception:
                            data = {
                                "rt_cd": "1",
                                "msg_cd": f"HTTP{resp.status}",
                                "msg1": text[:200],
                            }
            except aiohttp.ClientError as e:
                if retry_on_transport and attempt < (max_attempts - 1):
                    wait = self._retry_delay(attempt)
                    self.logger.warning(
                        f"{context} transport retry {attempt + 1}/{max_attempts} in {wait:.2f}s: {e}"
                    )
                    await asyncio.sleep(wait)
                    continue
                raise

            if not isinstance(data, dict):
                data = {}
            last_data = data

            should_retry = False
            if retry_on_rate_limit and self._is_rate_limit_error(data):
                should_retry = True
            if retry_on_cancel_error and self._is_cancel_retry_error(data):
                should_retry = True

            if should_retry and attempt < (max_attempts - 1):
                wait = self._retry_delay(attempt)
                self.logger.warning(
                    f"{context} retryable broker error {attempt + 1}/{max_attempts} in {wait:.2f}s: msg_cd={data.get('msg_cd')} msg={data.get('msg1')}"
                )
                await asyncio.sleep(wait)
                continue

            return data

        return last_data or {}

    def _get_tr_id(self, endpoint: str, is_paper: bool = False) -> str:
        tr_id_map = {
            # Overseas order TR IDs (US):
            # - buy:  TTTT1002U
            # - sell: TTTT1006U
            # - cancel/modify: TTTT1004U (per legacy samples)
            "order_buy": "TTTT1002U" if not is_paper else "VTTT1002U",
            "order_sell": "TTTT1006U" if not is_paper else "VTTT1006U",
            "cancel": "TTTT1004U" if not is_paper else "VTTT1004U",
            # Inquire balance (day): TTTS3012R (night): JTTT3012R
            "balance": "TTTS3012R" if not is_paper else "VTTS3012R",
            # Overseas price quote (real): HHDFS00000300
            "quote": "HHDFS00000300" if not is_paper else "VHDFS00000300",
            "price_detail": "HHDFS00000300" if not is_paper else "VHDFS00000300",
            # Inquire possible amount (day): TTTS3007R (night): JTTT3007R
            "psamount": "TTTS3007R",
            "dayornight": "JTTT3010R",
            # Inquire not-concluded (미체결): TTTS3018R
            "nccs": "TTTS3018R" if not is_paper else "VTTS3018R",
            # Reserved order list (US): TTTT3039R
            "resv_list_us": "TTTT3039R" if not is_paper else "VTTT3039R",
            # Reserved order cancel (US): TTTT3017U
            "resv_ccnl_us": "TTTT3017U" if not is_paper else "VTTT3017U",
        }
        return tr_id_map.get(endpoint, "TTTP6002E")

    def _quote_excd(self, exchange: str | None = None) -> str:
        ex = (exchange or self.exchange)
        return QUOTE_EXCD_MAP.get(ex, ex)

    def _resolve_exchange(self, symbol: str, exchange: str | None = None) -> str:
        """Resolve exchange for a symbol.

        Priority:
        1) explicit exchange override
        2) cached mapping from balance rows
        3) default self.exchange
        """
        if exchange:
            return exchange
        sym = (symbol or "").strip().upper()
        if sym and sym in self._symbol_exchange_cache:
            return self._symbol_exchange_cache[sym]
        return self.exchange

    async def place_buy_order(
        self,
        symbol: str,
        qty: int,
        price: float,
        order_type: str = "00",
        *,
        exchange: str | None = None,
    ) -> OrderResult:
        _validate_symbol(symbol)
        _validate_positive_int(qty, "Quantity")
        # order_type: 00=limit, 01=market (best-effort assumption)
        if str(order_type).strip() == "00":
            _validate_positive_number(price, "Price")

        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        payload = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "OVRS_EXCG_CD": self._resolve_exchange(symbol, exchange),
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{float(price):.2f}",
            # sell type: required; buy uses empty
            "SLL_TYPE": "",
            "CTAC_TLNO": "",
            "MGCO_APTM_ODNO": "",
            "ORD_SVR_DVSN_CD": "0",
        }

        headers = await self._auth_headers()
        headers.update({"tr_id": self._get_tr_id("order_buy"), "custtype": "P"})
        try:
            headers["hashkey"] = await self.auth.hashkey(payload)
        except Exception as e:
            return OrderResult(order_id="", filled_qty=0, status=f"hashkey_error: {e}")

        try:
            body = json.dumps(payload)
            data = await self._request_json(
                "POST",
                url,
                data_payload=body,
                headers=headers,
                context=f"overseas_buy({symbol})",
                max_attempts=4,
                retry_on_rate_limit=True,
                retry_on_transport=True,
            )

            err = self._check_error(data, f"overseas_buy({symbol})")
            if err:
                return OrderResult(order_id="", filled_qty=0, status=f"error: {err}")

            order_id = (data.get("output", {}) or {}).get("ODNO", "")
            self.logger.info(f"overseas_buy submitted: {symbol} qty={qty} price={price} order_id={order_id}")
            return OrderResult(order_id=str(order_id), filled_qty=0, status="submitted")
        except Exception as e:
            self.logger.error(f"overseas_buy exception: {e}")
            return OrderResult(order_id="", filled_qty=0, status=f"exception: {e}")

    async def place_sell_market(self, symbol: str, qty: int) -> OrderResult:
        # order_type: 01=market (best-effort; some environments reject 03)
        return await self.place_sell_order(symbol, qty, price=None, order_type="01")

    async def place_sell_order(
        self,
        symbol: str,
        qty: int,
        price: Optional[float] = None,
        order_type: str = "00",
        *,
        exchange: str | None = None,
    ) -> OrderResult:
        _validate_symbol(symbol)
        _validate_positive_int(qty, "Quantity")
        if price is not None:
            _validate_positive_number(price, "Price")

        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        base_payload = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "OVRS_EXCG_CD": self._resolve_exchange(symbol, exchange),
            "PDNO": symbol,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{float(price):.2f}" if price is not None else "0",
            # sell requires SLL_TYPE=00 (legacy sample)
            "SLL_TYPE": "00",
            "CTAC_TLNO": "",
            "MGCO_APTM_ODNO": "",
            "ORD_SVR_DVSN_CD": "0",
        }

        # Some KIS environments accept ORD_DVSN, others expect ORD_DVSN_CD.
        payload_variants = [
            {**base_payload, "ORD_DVSN": str(order_type)},
            {**base_payload, "ORD_DVSN_CD": str(order_type)},
        ]

        headers = await self._auth_headers()
        headers.update({"tr_id": self._get_tr_id("order_sell"), "custtype": "P"})

        try:
            last_err: str | None = None
            for payload in payload_variants:
                try:
                    hdrs = dict(headers)
                    try:
                        hdrs["hashkey"] = await self.auth.hashkey(payload)
                    except Exception as e:
                        return OrderResult(order_id="", filled_qty=0, status=f"hashkey_error: {e}")

                    body = json.dumps(payload)
                    data = await self._request_json(
                        "POST",
                        url,
                        data_payload=body,
                        headers=hdrs,
                        context=f"overseas_sell({symbol})",
                        max_attempts=4,
                        retry_on_rate_limit=True,
                        retry_on_transport=True,
                    )

                    err = self._check_error(data, f"overseas_sell({symbol})")
                    if err:
                        last_err = err
                        # If order-division field name is rejected, try next variant.
                        if "주문구분" in err or "ORD_DVSN" in err or "INPUT" in err:
                            continue
                        return OrderResult(order_id="", filled_qty=0, status=f"error: {err}")

                    order_id = (data.get("output", {}) or {}).get("ODNO", "")
                    self.logger.info(
                        f"overseas_sell submitted: {symbol} qty={qty} order_id={order_id}"
                    )
                    return OrderResult(order_id=str(order_id), filled_qty=0, status="submitted")
                except Exception as e:
                    last_err = str(e)
                    continue

            return OrderResult(order_id="", filled_qty=0, status=f"error: {last_err or 'Unknown'}")
        except Exception as e:
            self.logger.error(f"overseas_sell exception: {e}")
            return OrderResult(order_id="", filled_qty=0, status=f"exception: {e}")

    async def cancel_order(self, order_id: str, symbol: str, qty: int, *, exchange: str | None = None) -> bool:
        _validate_symbol(symbol)
        _validate_positive_int(qty, "Quantity")

        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order-rvsecncl"
        payload = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "OVRS_EXCG_CD": self._resolve_exchange(symbol, exchange),
            "PDNO": symbol,
            "ORGN_ODNO": order_id,
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": "0",
            "MGCO_APTM_ODNO": "",
            "ORD_SVR_DVSN_CD": "0",
        }

        headers = await self._auth_headers()
        headers.update({"tr_id": self._get_tr_id("cancel"), "custtype": "P"})
        try:
            headers["hashkey"] = await self.auth.hashkey(payload)
        except Exception as e:
            self.logger.error(f"overseas_cancel hashkey_error: {e}")
            return False

        try:
            body = json.dumps(payload)
            data = await self._request_json(
                "POST",
                url,
                data_payload=body,
                headers=headers,
                context=f"overseas_cancel({order_id})",
                max_attempts=4,
                retry_on_rate_limit=True,
                retry_on_cancel_error=True,
                retry_on_transport=True,
            )

            err = self._check_error(data, f"overseas_cancel({order_id})")
            if err:
                self.logger.error(f"overseas_cancel failed: {err}")
                return False

            self.logger.info(f"overseas_cancel success: {order_id}")
            return True
        except Exception as e:
            self.logger.error(f"overseas_cancel exception: {e}")
            return False

    async def get_balance(self, *, exchange: str | None = None) -> Optional[list]:
        """Return overseas positions list (output1) best-effort."""
        data = await self.get_balance_raw(exchange=exchange)
        if not data:
            return None
        out1 = data.get("output1")
        return out1 if isinstance(out1, list) else None

    async def inquire_nccs(
        self,
        *,
        exchange: str,
        sort_sqn: str = "DS",
        max_pages: int = 5,
    ) -> list[dict]:
        """Inquire overseas not-concluded orders (미체결내역).

        Endpoint:
        - /uapi/overseas-stock/v1/trading/inquire-nccs

        Notes:
        - `exchange` must be provided (NASD/NYSE/AMEX...).
        - Uses continuation keys CTX_AREA_FK200 / CTX_AREA_NK200 when present.
        """
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
        headers = await self._auth_headers()
        headers.update({"tr_id": self._get_tr_id("nccs"), "custtype": "P"})

        fk200 = ""
        nk200 = ""
        out_rows: list[dict] = []

        for _ in range(max(1, int(max_pages))):
            params = {
                "CANO": self.account.account_no,
                "ACNT_PRDT_CD": self.account.product_code,
                "OVRS_EXCG_CD": exchange,
                "SORT_SQN": sort_sqn,
                "CTX_AREA_FK200": fk200,
                "CTX_AREA_NK200": nk200,
            }
            data = await self._request_json(
                "GET",
                url,
                params=params,
                headers=headers,
                context=f"inquire_nccs({exchange})",
                max_attempts=4,
                retry_on_rate_limit=True,
                retry_on_transport=True,
            )

            err = self._check_error(data, f"inquire_nccs({exchange})")
            if err:
                break

            body_out = data.get("output")
            if isinstance(body_out, list):
                for r in body_out:
                    if isinstance(r, dict):
                        out_rows.append(r)
            elif isinstance(body_out, dict):
                out_rows.append(body_out)

            fk200 = str(data.get("ctx_area_fk200") or data.get("CTX_AREA_FK200") or "")
            nk200 = str(data.get("ctx_area_nk200") or data.get("CTX_AREA_NK200") or "")
            if not fk200 and not nk200:
                break

        return out_rows

    async def order_resv_list_us(
        self,
        *,
        exchange: str,
        inqr_strt_dt: str,
        inqr_end_dt: str,
        inqr_dvsn_cd: str = "00",
        prdt_type_cd: str = "512",
        max_pages: int = 5,
    ) -> list[dict]:
        """Overseas reserved order list (US).

        Endpoint:
        - /uapi/overseas-stock/v1/trading/order-resv-list

        Params notes (from official samples):
        - INQR_DVSN_CD: 00 전체 / 01 일반해외주식 / 02 미니스탁
        - PRDT_TYPE_CD: default 512
        - This API may not support demo accounts.
        """
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order-resv-list"
        headers = await self._auth_headers()
        headers.update({"tr_id": self._get_tr_id("resv_list_us"), "custtype": "P"})

        fk200 = ""
        nk200 = ""
        out_rows: list[dict] = []

        for _ in range(max(1, int(max_pages))):
            params = {
                "CANO": self.account.account_no,
                "ACNT_PRDT_CD": self.account.product_code,
                "INQR_STRT_DT": inqr_strt_dt,
                "INQR_END_DT": inqr_end_dt,
                "INQR_DVSN_CD": inqr_dvsn_cd,
                "OVRS_EXCG_CD": exchange,
                "PRDT_TYPE_CD": prdt_type_cd,
                "CTX_AREA_FK200": fk200,
                "CTX_AREA_NK200": nk200,
            }
            data = await self._request_json(
                "GET",
                url,
                params=params,
                headers=headers,
                context=f"order_resv_list_us({exchange})",
                max_attempts=4,
                retry_on_rate_limit=True,
                retry_on_transport=True,
            )

            err = self._check_error(data, f"order_resv_list_us({exchange})")
            if err:
                break

            body_out = data.get("output")
            if isinstance(body_out, list):
                for r in body_out:
                    if isinstance(r, dict):
                        out_rows.append(r)
            elif isinstance(body_out, dict):
                out_rows.append(body_out)

            fk200 = str(data.get("ctx_area_fk200") or data.get("CTX_AREA_FK200") or "")
            nk200 = str(data.get("ctx_area_nk200") or data.get("CTX_AREA_NK200") or "")
            if not fk200 and not nk200:
                break

        return out_rows

    async def order_resv_ccnl_us(
        self,
        *,
        rsvn_ord_rcit_dt: str,
        ovrs_rsvn_odno: str,
    ) -> dict:
        """Cancel US reserved order.

        Endpoint:
        - /uapi/overseas-stock/v1/trading/order-resv-ccnl

        Required:
        - RSVN_ORD_RCIT_DT: overseas order receipt date (YYYYMMDD)
        - OVRS_RSVN_ODNO: reserved order id (ODNO)
        """
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order-resv-ccnl"
        payload = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "RSVN_ORD_RCIT_DT": str(rsvn_ord_rcit_dt),
            "OVRS_RSVN_ODNO": str(ovrs_rsvn_odno),
        }

        headers = await self._auth_headers()
        headers.update({"tr_id": self._get_tr_id("resv_ccnl_us"), "custtype": "P"})
        try:
            headers["hashkey"] = await self.auth.hashkey(payload)
        except Exception:
            pass

        data = await self._request_json(
            "POST",
            url,
            data_payload=json.dumps(payload),
            headers=headers,
            context="order_resv_ccnl_us",
            max_attempts=4,
            retry_on_rate_limit=True,
            retry_on_transport=True,
        )

        # best-effort error logging
        self._check_error(data, "order_resv_ccnl_us")
        return data

    async def get_day_or_night(self) -> Optional[str]:
        """Return PSBL_YN from dayornight endpoint.

        Returns:
            'Y' for night ledger, 'N' for day ledger (best-effort).
        """
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/dayornight"
        headers = await self._auth_headers()
        headers.update({"tr_id": self._get_tr_id("dayornight"), "custtype": "P"})
        try:
            data = await self._request_json(
                "GET",
                url,
                headers=headers,
                context="overseas_dayornight",
                max_attempts=4,
                retry_on_rate_limit=True,
                retry_on_transport=True,
            )
            err = self._check_error(data, "overseas_dayornight")
            if err:
                return None
            out = data.get("output", {}) or {}
            v = out.get("PSBL_YN")
            return str(v).strip() if v is not None else None
        except Exception as e:
            self.logger.warning(f"overseas_dayornight exception: {e}")
            return None

    async def get_balance_raw(self, *, exchange: str | None = None) -> Optional[dict]:
        """Return raw overseas balance payload (includes output1 positions + output2 summary)."""
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        # KIS 환경/계정에 따라 요구 파라미터가 조금씩 달라 best-effort variants 사용
        ex = exchange or self.exchange
        base_params = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "OVRS_EXCG_CD": ex,
            "TR_CNDT_CD": "00",
            "TR_CRCY_CD": "USD",
            # paging ctx keys (required in some environments)
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }

        variants = [
            ("TTTS3012R", base_params),
            ("JTTT3012R", base_params),
        ]

        try:
            last_err = None
            for tr_id, params in variants:
                headers = await self._auth_headers()
                headers.update({"tr_id": tr_id, "custtype": "P"})

                data = await self._request_json(
                    "GET",
                    url,
                    params=params,
                    headers=headers,
                    context=f"overseas_get_balance[{tr_id}]",
                    max_attempts=4,
                    retry_on_rate_limit=True,
                    retry_on_transport=True,
                )

                err = self._check_error(data, f"overseas_get_balance[{tr_id}]")
                if err:
                    last_err = err
                    continue

                return data

            if last_err:
                self.logger.warning(f"overseas_get_balance failed after variants: {last_err}")
            return None
        except Exception as e:
            self.logger.error(f"overseas_get_balance exception: {e}")
            return None

    def _row_to_position(self, row: dict) -> Optional[Position]:
        """Convert a KIS inquire-balance output1 row into Position (best-effort)."""
        if not isinstance(row, dict):
            return None

        sym = str(row.get("ovrs_pdno", "") or "").strip()
        if not sym:
            return None

        # exchange mapping (KIS may report holdings under AMEX even for US ETFs)
        ex = str(row.get("ovrs_excg_cd", "") or "").strip().upper()
        if ex:
            self._symbol_exchange_cache[sym.upper()] = ex

        # qty field variants
        qty_raw = row.get("ovrs_cblc_qty")
        if qty_raw in (None, ""):
            qty_raw = row.get("ovrs_hldg_qty")
        try:
            qty = int(float(qty_raw or 0))
        except Exception:
            qty = 0
        if qty <= 0:
            return None

        # avg price field variants
        avg_raw = row.get("pchs_avg_pric")
        if avg_raw in (None, ""):
            avg_raw = row.get("pchs_amt")
        try:
            avg_price = float(avg_raw or 0)
        except Exception:
            avg_price = 0.0

        # optional: current price / pnl
        last_raw = row.get("now_pric2")
        try:
            last_price = float(last_raw) if last_raw not in (None, "") else None
        except Exception:
            last_price = None

        pnl_raw = row.get("evlu_pfls_rt")
        try:
            pnl_pct = float(pnl_raw) / 100.0 if pnl_raw not in (None, "") else 0.0
        except Exception:
            pnl_pct = 0.0

        pos = Position(symbol=sym, qty=qty, avg_price=avg_price)
        # stash best-effort extra fields if Position supports them
        try:
            pos.unrealized_pnl_pct = pnl_pct
        except Exception:
            pass
        # Attach last price as attribute (non-breaking; used by reporting scripts)
        try:
            setattr(pos, "last_price", last_price)
        except Exception:
            pass
        return pos

    async def get_positions_list(self, *, exchange: str | None = None) -> list[Position]:
        """Return all overseas positions (non-zero holdings) best-effort.

        If exchange is provided, query that exchange bucket explicitly.
        """
        balance = await self.get_balance(exchange=exchange)
        if not balance:
            return []

        positions: list[Position] = []
        for row in balance:
            pos = self._row_to_position(row)  # type: ignore[arg-type]
            if pos is not None:
                positions.append(pos)
        return positions

    async def get_positions(self) -> Optional[Position]:
        """Return a single overseas position (first non-zero holding) for legacy callsites."""
        positions = await self.get_positions_list()
        return positions[0] if positions else None

    async def get_cash_available_detail(self, symbol: str = "AAPL", price: float = 100.0) -> dict[str, Any]:
        """Best-effort overseas order-possible cash/amount with diagnostics.

        Returns:
            dict with keys:
            - cash_available: float (USD, best-effort)
            - ord_psbl_qty: int | None
            - err: str | None
            - exchange: str | None
            - day_or_night: str | None
            - tr_id: str | None
        """
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"

        try:
            last_err = None
            resolved_ex = self._resolve_exchange(symbol)
            exchange_candidates: list[str] = [resolved_ex, "NASD", "NYSE", "AMEX"]
            seen_ex: set[str] = set()
            deduped_exchanges: list[str] = []
            for ex in exchange_candidates:
                if not ex or ex in seen_ex:
                    continue
                seen_ex.add(ex)
                deduped_exchanges.append(ex)

            day_or_night_candidates: list[str | None] = [None]
            try:
                detected = await self.get_day_or_night()
                detected_dn = detected.strip().upper() if isinstance(detected, str) else None
            except Exception:
                detected_dn = None
            for dn in (detected_dn, "N", "D"):
                if dn and dn not in day_or_night_candidates:
                    day_or_night_candidates.append(dn)

            # TR IDs (day/night) – some environments accept these.
            tr_ids = ["TTTS3007R", "JTTT3007R"]
            for tr in tr_ids:
                headers = await self._auth_headers()
                headers.update({"tr_id": tr, "custtype": "P"})

                for ex in deduped_exchanges:
                    base = {
                        "CANO": self.account.account_no,
                        "ACNT_PRDT_CD": self.account.product_code,
                        # Some environments expect OVRS_EXCG_CD, others expect EXCD.
                        "OVRS_EXCG_CD": ex,
                        "OVRS_ORD_UNPR": str(float(price)),
                        "ITEM_CD": symbol,
                    }
                    for dn in day_or_night_candidates:
                        base_with_dn = {**base}
                        if dn:
                            base_with_dn["DAY_OR_NIGHT"] = dn

                        base_variants = [
                            base_with_dn,
                            {**base_with_dn, "EXCD": ex},
                            {**base_with_dn, "OVRS_PDNO": symbol},
                            {**base_with_dn, "OVRS_PDNO": symbol, "EXCD": ex},
                            {**base_with_dn, "PDNO": symbol},
                            {**base_with_dn, "PDNO": symbol, "EXCD": ex},
                        ]

                        for params in base_variants:
                            # Many examples use POST; some expect query params.
                            data = await self._request_json(
                                "POST",
                                url,
                                params=params,
                                headers=headers,
                                context=f"overseas_get_cash_available[{tr}]",
                                max_attempts=4,
                                retry_on_rate_limit=True,
                                retry_on_transport=True,
                            )

                            err = self._check_error(data, f"overseas_get_cash_available[{tr}]")
                            if err:
                                # For rt_cd=7 "상품이 없습니다", continue trying exchange/day-night variants.
                                msg = str(data.get("msg1") or "")
                                rt_cd = str(data.get("rt_cd") or "")
                                if rt_cd == "7" and ("상품이 없습니다" in msg or "상품이 없" in msg):
                                    last_err = f"rt_cd={rt_cd} msg={msg}"
                                    continue

                                last_err = err
                                continue

                            out = data.get("output", {}) or {}
                            cash = 0.0
                            for k in ("ovrs_ord_psbl_amt", "ord_psbl_cash", "psbl_cash"):
                                v = out.get(k)
                                if v not in (None, ""):
                                    try:
                                        cash = float(v)
                                        break
                                    except Exception:
                                        continue

                            ord_psbl_qty: int | None = None
                            for k in ("ord_psbl_qty", "ovrs_ord_psbl_qty", "psbl_qty"):
                                v = out.get(k)
                                if v in (None, ""):
                                    continue
                                try:
                                    ord_psbl_qty = int(float(v))
                                    break
                                except Exception:
                                    continue

                            return {
                                "cash_available": cash,
                                "ord_psbl_qty": ord_psbl_qty,
                                "err": None,
                                "exchange": ex,
                                "day_or_night": dn,
                                "tr_id": tr,
                            }

            if last_err:
                self.logger.warning(f"overseas_get_cash_available failed after variants: {last_err}")
            return {
                "cash_available": 0.0,
                "ord_psbl_qty": None,
                "err": str(last_err) if last_err else None,
                "exchange": None,
                "day_or_night": None,
                "tr_id": None,
            }
        except Exception as e:
            self.logger.error(f"overseas_get_cash_available exception: {e}")
            return {
                "cash_available": 0.0,
                "ord_psbl_qty": None,
                "err": str(e),
                "exchange": None,
                "day_or_night": None,
                "tr_id": None,
            }

    async def get_cash_available(self, symbol: str = "AAPL", price: float = 100.0) -> float:
        """Backward-compatible cash_available-only wrapper."""
        try:
            out = await self.get_cash_available_detail(symbol, price)
            return float(out.get("cash_available") or 0.0)
        except Exception:
            return 0.0

    async def get_quote(self, symbol: str, *, exchange: str | None = None) -> dict:
        _validate_symbol(symbol)
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"

        headers = await self._auth_headers()
        headers.update({"tr_id": self._get_tr_id("quote"), "custtype": "P"})

        # Try resolved exchange first; if it fails, try other US exchanges and cache the first success.
        tried: list[str] = []
        primary = self._resolve_exchange(symbol, exchange)
        candidates = [primary, "NASD", "NYSE", "AMEX"]

        try:
            last_exc: Exception | None = None
            for ex in candidates:
                if not ex or ex in tried:
                    continue
                tried.append(ex)
                params = {"EXCD": self._quote_excd(ex), "SYMB": symbol}
                try:
                    data = await self._request_json(
                        "GET",
                        url,
                        params=params,
                        headers=headers,
                        context=f"overseas_get_quote({symbol})[{ex}]",
                        max_attempts=4,
                        retry_on_rate_limit=True,
                        retry_on_transport=True,
                    )
                    # success heuristic
                    if isinstance(data, dict) and str(data.get("rt_cd", "0")) == "0":
                        self._symbol_exchange_cache[symbol.strip().upper()] = ex
                        return data
                    # Some environments omit rt_cd; treat presence of output as success.
                    if isinstance(data, dict) and data.get("output"):
                        self._symbol_exchange_cache[symbol.strip().upper()] = ex
                        return data
                except Exception as e:
                    last_exc = e
                    continue

            if last_exc is not None:
                self.logger.error(f"overseas_get_quote({symbol}) exception: {last_exc}")
            return {}
        except Exception as e:
            self.logger.error(f"overseas_get_quote({symbol}) exception: {e}")
            return {}

    async def get_current_price(self, symbol: str) -> Optional[float]:
        quote = await self.get_quote(symbol)
        try:
            output = quote.get("output", {}) if isinstance(quote, dict) else {}
            for key in ("last", "lastxch"):
                v = output.get(key)
                if v:
                    return float(v)
        except Exception as e:
            self.logger.error(f"overseas_get_current_price error: {e}")
        return None
