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
from typing import Optional

import aiohttp
import asyncio
import json

from models import OrderResult, Position
from kis_rest_orders import AccountInfo


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
            session = await self._get_session()
            body = json.dumps(payload)
            async with session.post(url, data=body, headers=headers) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    return OrderResult(order_id="", filled_qty=0, status=f"http={resp.status} non-json: {text[:200]}")

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
            headers["hashkey"] = await self.auth.hashkey(payload)
        except Exception as e:
            return OrderResult(order_id="", filled_qty=0, status=f"hashkey_error: {e}")

        try:
            session = await self._get_session()

            last_err: str | None = None
            for payload in payload_variants:
                try:
                    body = json.dumps(payload)
                    async with session.post(url, data=body, headers=headers) as resp:
                        data = await resp.json()

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
            session = await self._get_session()
            body = json.dumps(payload)
            async with session.post(url, data=body, headers=headers) as resp:
                data = await resp.json()

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

        session = await self._get_session()
        for _ in range(max(1, int(max_pages))):
            params = {
                "CANO": self.account.account_no,
                "ACNT_PRDT_CD": self.account.product_code,
                "OVRS_EXCG_CD": exchange,
                "SORT_SQN": sort_sqn,
                "CTX_AREA_FK200": fk200,
                "CTX_AREA_NK200": nk200,
            }
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()

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

        session = await self._get_session()
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
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()

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

    async def get_day_or_night(self) -> Optional[str]:
        """Return PSBL_YN from dayornight endpoint.

        Returns:
            'Y' for night ledger, 'N' for day ledger (best-effort).
        """
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/dayornight"
        headers = await self._auth_headers()
        headers.update({"tr_id": self._get_tr_id("dayornight"), "custtype": "P"})
        try:
            session = await self._get_session()
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
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
            session = await self._get_session()
            last_err = None
            for tr_id, params in variants:
                headers = await self._auth_headers()
                headers.update({"tr_id": tr_id, "custtype": "P"})

                # Retry on rate-limit (EGW00201)
                for attempt in range(4):
                    async with session.get(url, params=params, headers=headers) as resp:
                        data = await resp.json()

                    err = self._check_error(data, f"overseas_get_balance[{tr_id}]")
                    if err:
                        if "EGW00201" in err or "초당" in err:
                            wait = 1 * (2**attempt)
                            self.logger.warning(f"overseas_get_balance rate-limited. retry in {wait}s: {err}")
                            await asyncio.sleep(wait)
                            continue
                        last_err = err
                        break

                    return data

                if last_err:
                    continue

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

    async def get_cash_available(self, symbol: str = "AAPL", price: float = 100.0) -> float:
        """Best-effort overseas order-possible cash/amount.

        KIS 해외 '매수가능금액조회(inquire-psamount)'는 종목코드/주문단가를 요구하는 경우가 많아
        대표 심볼 + 소액 단가로 조회합니다.

        Returns:
            float: ovrs_ord_psbl_amt (통화 기준, 보통 USD)
        """
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"

        # param variants
        ex = self._resolve_exchange(symbol)
        base = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            # Some environments expect OVRS_EXCG_CD, others expect EXCD.
            "OVRS_EXCG_CD": ex,
            "OVRS_ORD_UNPR": str(float(price)),
            "ITEM_CD": symbol,
        }

        base_variants = [
            base,
            {**base, "EXCD": self.exchange},
            {**base, "OVRS_PDNO": symbol},
            {**base, "OVRS_PDNO": symbol, "EXCD": self.exchange},
            {**base, "PDNO": symbol},
            {**base, "PDNO": symbol, "EXCD": self.exchange},
        ]

        # TR IDs (day/night) – some environments accept these.
        tr_ids = ["TTTS3007R", "JTTT3007R"]

        try:
            session = await self._get_session()
            last_err = None
            for tr in tr_ids:
                headers = await self._auth_headers()
                headers.update({"tr_id": tr, "custtype": "P"})

                for params in base_variants:
                    # Many examples use POST; some expect query params.
                    for attempt in range(4):
                        async with session.post(url, params=params, headers=headers) as resp:
                            data = await resp.json()

                        err = self._check_error(data, f"overseas_get_cash_available[{tr}]")
                        if err:
                            if "EGW00201" in err or "초당" in err:
                                wait = 1 * (2**attempt)
                                self.logger.warning(
                                    f"overseas_get_cash_available rate-limited. retry in {wait}s: {err}"
                                )
                                await asyncio.sleep(wait)
                                continue
                            last_err = err
                            break

                        out = data.get("output", {}) or {}
                        # candidates
                        for k in ("ovrs_ord_psbl_amt", "ord_psbl_cash", "psbl_cash"):
                            v = out.get(k)
                            if v not in (None, ""):
                                try:
                                    return float(v)
                                except Exception:
                                    continue
                        break

                    # if this params variant failed, try next variant
                    if last_err:
                        continue

            if last_err:
                self.logger.warning(f"overseas_get_cash_available failed after variants: {last_err}")
            return 0.0
        except Exception as e:
            self.logger.error(f"overseas_get_cash_available exception: {e}")
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
            session = await self._get_session()
            last_exc: Exception | None = None
            for ex in candidates:
                if not ex or ex in tried:
                    continue
                tried.append(ex)
                params = {"EXCD": self._quote_excd(ex), "SYMB": symbol}
                try:
                    async with session.get(url, params=params, headers=headers) as resp:
                        data = await resp.json()
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
