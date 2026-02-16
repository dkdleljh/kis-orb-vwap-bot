"""
KIS Overseas Stock REST API Client.

한국투자증권 해외주식(미국/NYSE) API 클라이언트입니다.
"""

from typing import Optional
import aiohttp

from models import OrderResult, Position
from kis_rest_orders import AccountInfo


EXCHANGE_CODES = {
    "NASD": "나스닥",
    "NYSE": "뉴욕",
    "AMEX": "아메리칸스톡익스체인지",
    "SEHK": "홍콩",
    "TKSE": "도쿄",
}


class KISOverseasRestOrders:
    """해외주식 주문 및 시세 조회 클라이언트"""
    
    def __init__(self, base_url: str, auth, account: "AccountInfo", logger, exchange: str = "NASD"):
        self.base_url = base_url
        self.auth = auth
        self.account = account
        self.logger = logger
        self.exchange = exchange
    
    def _check_error(self, data: dict, context: str) -> Optional[str]:
        rt_cd = data.get("rt_cd", "1")
        if rt_cd != "0":
            msg = data.get("msg1", data.get("msg_cd", "Unknown error"))
            self.logger.error(f"{context} failed: rt_cd={rt_cd} msg={msg}")
            return msg
        return None
    
    def _get_tr_id(self, endpoint: str, is_paper: bool = False) -> str:
        tr_id_map = {
            "order": "TTTP6002E" if not is_paper else "VTTP6002E",
            "order_market": "TTTP6002E",
            "balance": "TTTP6003E" if not is_paper else "VTTP6003E",
            "quote": "HHHPSTKIF010M1000" if not is_paper else "VHPSTKIF010M1000",
            "price_detail": "HHHPSTKIF010M1000" if not is_paper else "VHPSTKIF010M1000",
        }
        return tr_id_map.get(endpoint, "TTTP6002E")
    
    async def place_buy_order(
        self, symbol: str, qty: int, price: float, order_type: str = "00"
    ) -> OrderResult:
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        
        payload = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "OVRS_EXCG_CD": self.exchange,
            "PDNO": symbol,
            "ORD_DVSN_CD": order_type,
            "ORD_QTY": str(qty),
            "ORD_SLVL_CD": "00",
            "ORD_CNDT_CD": "00",
        }
        
        if order_type == "00":
            payload["ORD_PRVR_NMBC"] = ""
        
        headers = self.auth.auth_headers()
        headers["tr_id"] = self._get_tr_id("order")
        headers["custtype"] = "P"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
                    data = await resp.json()
            
            err = self._check_error(data, f"buy({symbol})")
            if err:
                return OrderResult(order_id="", filled_qty=0, status=f"error: {err}")
            
            order_id = data.get("output", {}).get("ODNO", "")
            self.logger.info(f"overseas_buy submitted: {symbol} qty={qty} price={price} order_id={order_id}")
            return OrderResult(order_id=order_id, filled_qty=0, status="submitted")
        except Exception as e:
            self.logger.error(f"buy exception: {e}")
            return OrderResult(order_id="", filled_qty=0, status=f"exception: {e}")
    
    async def place_sell_market(self, symbol: str, qty: int) -> OrderResult:
        return await self.place_sell_order(symbol, qty, price=None, order_type="03")
    
    async def place_sell_order(
        self, symbol: str, qty: int, price: Optional[float] = None, order_type: str = "00"
    ) -> OrderResult:
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        
        payload = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "OVRS_EXCG_CD": self.exchange,
            "PDNO": symbol,
            "ORD_DVSN_CD": order_type,
            "ORD_QTY": str(qty),
            "ORD_SLVL_CD": "00",
            "ORD_CNDT_CD": "00",
        }
        
        headers = self.auth.auth_headers()
        headers["tr_id"] = self._get_tr_id("order")
        headers["custtype"] = "P"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
                    data = await resp.json()
            
            err = self._check_error(data, f"sell({symbol})")
            if err:
                return OrderResult(order_id="", filled_qty=0, status=f"error: {err}")
            
            order_id = data.get("output", {}).get("ODNO", "")
            self.logger.info(f"overseas_sell submitted: {symbol} qty={qty} order_id={order_id}")
            return OrderResult(order_id=order_id, filled_qty=0, status="submitted")
        except Exception as e:
            self.logger.error(f"sell exception: {e}")
            return OrderResult(order_id="", filled_qty=0, status=f"exception: {e}")
    
    async def cancel_order(self, order_id: str, symbol: str, qty: int) -> bool:
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order-rvsecncl"
        
        payload = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "OVRS_EXCG_CD": self.exchange,
            "ORGN_ODNO": order_id,
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(qty),
        }
        
        headers = self.auth.auth_headers()
        headers["tr_id"] = "TTTP6004E"
        headers["custtype"] = "P"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
                    data = await resp.json()
            
            err = self._check_error(data, f"cancel({order_id})")
            if err:
                self.logger.error(f"cancel failed: {err}")
                return False
            
            self.logger.info(f"cancel success: {order_id}")
            return True
        except Exception as e:
            self.logger.error(f"cancel exception: {e}")
            return False
    
    async def get_balance(self) -> Optional[dict]:
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        
        params = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "OVRS_EXCG_CD": self.exchange,
            "TR_CNDT_CD": "00",
        }
        
        headers = self.auth.auth_headers()
        headers["tr_id"] = self._get_tr_id("balance")
        headers["custtype"] = "P"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                    data = await resp.json()
            
            err = self._check_error(data, "get_balance")
            if err:
                self.logger.error(f"get_balance failed: {err}")
                return None
            
            return data.get("output1", [])
        except Exception as e:
            self.logger.error(f"get_balance exception: {e}")
            return None
    
    async def get_positions(self) -> Optional[Position]:
        balance = await self.get_balance()
        if not balance:
            return None
        
        for row in balance:
            sym = row.get("ovrs_pdno", "")
            qty = int(float(row.get("ovrs_hldg_qty", 0) or 0))
            if qty > 0:
                avg_price = float(row.get("pchs_amt", 0) or 0)
                self.logger.info(f"overseas position: {sym} qty={qty} avg={avg_price}")
                return Position(symbol=sym, qty=qty, avg_price=avg_price)
        
        return None
    
    async def get_cash_available(self, symbol: str = "") -> float:
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
        
        params = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "OVRS_EXCG_CD": self.exchange,
        }
        
        headers = self.auth.auth_headers()
        headers["tr_id"] = "TTTP6005R"
        headers["custtype"] = "P"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                    data = await resp.json()
            
            err = self._check_error(data, "get_cash_available")
            if err:
                self.logger.warning(f"get_cash_available error: {err}")
                return 0.0
            
            cash = float(data.get("output", {}).get("ord_psbl_cash", 0) or 0)
            self.logger.info(f"overseas cash_available: {cash}")
            return cash
        except Exception as e:
            self.logger.error(f"get_cash_available exception: {e}")
            return 0.0
    
    async def get_quote(self, symbol: str) -> dict:
        url = f"{self.base_url}/uapi/overseas-stock/v1/quotations/price-detail"
        
        params = {
            "EXCD": self.exchange,
            "SYMB": symbol,
        }
        
        headers = self.auth.auth_headers()
        headers["tr_id"] = self._get_tr_id("quote")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                    data = await resp.json()
            return data
        except Exception as e:
            self.logger.error(f"get_quote({symbol}) exception: {e}")
            return {}
    
    async def get_current_price(self, symbol: str) -> Optional[float]:
        quote = await self.get_quote(symbol)
        try:
            output = quote.get("output", {})
            price = output.get("last")
            if price:
                return float(price)
            
            price = output.get("lastxch")
            if price:
                return float(price)
        except Exception as e:
            self.logger.error(f"get_current_price error: {e}")
        
        return None
