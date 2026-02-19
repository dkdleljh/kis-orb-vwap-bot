import asyncio
import json
import os
import time
from datetime import datetime
from typing import Callable, Iterable

import websockets

from models import OrderBookTop, TradeTick
from utils_time import is_between, now_local, parse_time
from utils_holiday import is_market_open


class KISWebSocket:
    def __init__(self, url: str, symbols: Iterable[str], backoff_seq, logger, approval_key: str = "", timezone: str = "Asia/Seoul"):
        self.url = url
        self.symbols = list(symbols)
        self.backoff_seq = list(backoff_seq)
        self.logger = logger
        self.connected = False
        self._stop = False
        self.approval_key = approval_key
        self.timezone = timezone

        # appkey 중복 사용(OPSP8996) 대응: 점진적 백오프로 WS 재시도 폭주 방지
        self._appkey_backoff_sec = 30
        self._appkey_backoff_sec_max = 300
        self._market_closed_log_interval_sec = int(os.environ.get("KIS_MARKET_CLOSED_LOG_INTERVAL_SEC", "1200"))
        self._last_market_closed_log_at = 0.0

    def _bump_appkey_backoff(self) -> int:
        cur = int(self._appkey_backoff_sec)
        self._appkey_backoff_sec = min(self._appkey_backoff_sec * 2, self._appkey_backoff_sec_max)
        return cur

    def _reset_appkey_backoff(self) -> None:
        self._appkey_backoff_sec = 30

    def _is_market_hours(self) -> bool:
        """장 운영 시간 확인 (평일 09:00-15:30 + 휴장일 제외)"""
        now = now_local(self.timezone)
        
        # 휴장일 체크
        if not is_market_open(now.date()):
            return False
            
        # 장 시간 확인
        market_start = parse_time("09:00:00")
        market_end = parse_time("15:30:00")
        return is_between(market_start, market_end, now)

    def _is_server_available(self) -> bool:
        """KIS WebSocket 서버 가용성 확인"""
        # KIS 서버 점검 시간: 주말 전체 + 평일 08:30-09:00 점검 가능성
        now = now_local(self.timezone)
        # 평일 아침 점검 시간 (08:30-09:00)
        if now.weekday() < 5:  # 평일
            maintenance_start = parse_time("08:30:00")
            maintenance_end = parse_time("09:00:00")
            if is_between(maintenance_start, maintenance_end, now):
                return False
        return True

    async def stop(self):
        self._stop = True

    async def _subscribe(self, ws):
        """KIS WebSocket 실시간 호가/체결 구독"""
        for symbol in self.symbols:
            # 호가 구독 (H0STASP0)
            orderbook_msg = {
                "header": {
                    "approval_key": self.approval_key,
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8"
                },
                "body": {
                    "input": {
                        "tr_id": "H0STASP0",
                        "tr_key": symbol
                    }
                }
            }
            # 체결 구독 (H0STCNT0)
            trade_msg = {
                "header": {
                    "approval_key": self.approval_key,
                    "custtype": "P", 
                    "tr_type": "1",
                    "content-type": "utf-8"
                },
                "body": {
                    "input": {
                        "tr_id": "H0STCNT0",
                        "tr_key": symbol
                    }
                }
            }
            await ws.send(json.dumps(orderbook_msg))
            self.logger.info(f"sent subscribe book: {json.dumps(orderbook_msg)}")
            await ws.send(json.dumps(trade_msg))
            self.logger.info(f"sent subscribe trade: {json.dumps(trade_msg)}")

    def _parse_message(self, raw: str):
        """KIS WebSocket 메시지 파싱"""
        try:
            # JSON 파싱 시도 (PING, 초기 응답)
            data = json.loads(raw)
            
            # PINGPONG 처리 (header에 있을 수 있음)
            if data.get("header", {}).get("tr_id") == "PINGPONG":
                return {"type": "ping"}
            
            # PINGPONG 처리 (body에 있을 수 있음 - 기존 로직 호환)
            if self._get_message_type(data) == "ping":
                return {"type": "ping"}

            # 구독 응답 메시지 처리 (rt_cd, msg1 등)
            if "body" in data and "msg1" in data["body"]:
                msg1 = str(data["body"].get("msg1") or "")
                msg_cd = str(data["body"].get("msg_cd") or "")
                self.logger.info(f"API Response: {msg1}")

                # 동일 appkey가 이미 사용 중일 때(중복 실행/비정상 종료 잔여세션)
                if msg_cd == "OPSP8996" or "ALREADY IN USE appkey" in msg1.upper():
                    return {"type": "appkey_in_use", "data": data}

                return {"type": "response", "data": data}

            # KIS 응답 형식에 따라 파싱 (데이터)
            if isinstance(data, dict) and "body" in data:
                output = data.get("body", {}).get("output", {})
                if not output:
                    return {"type": "unknown_json", "raw": raw}
                    
                return {
                    "type": self._get_message_type(data),
                    "symbol": output.get("mksc_shrn_iscd", ""),
                    "price": float(output.get("stck_prpr", 0)),
                    "volume": int(output.get("trqu", 0)),
                    "bid": float(output.get("bidp", 0)),
                    "ask": float(output.get("askp", 0)),
                    "bid_size": int(output.get("bidq", 0)),
                    "ask_size": int(output.get("askq", 0)),
                    "timestamp": output.get("symd", ""),
                }
            return data
        except json.JSONDecodeError:
            # JSON 아님 -> 텍스트 포맷 (실시간 체결/호가)
            return self._parse_text_message(raw)
        except (KeyError, ValueError):
            return {"type": "unknown", "raw": raw}
    
    def _parse_text_message(self, raw: str) -> dict:
        """실시간 텍스트 데이터 파싱

        KIS 국내주식 실시간은 아래 포맷으로 자주 옵니다.
        - 0|H0STCNT0|001|005930^090336^170800^2^3000^...
        - 0|H0STASP0|001|005930^090336^0^170800^...

        즉,
        - 앞의 3개 필드는 '|' 구분
        - payload는 4번째 필드에 '^'로 이어짐
        """
        parts = raw.split("|")
        if len(parts) < 4:
            return {"type": "unknown", "raw": raw}

        tr_id = parts[1]
        payload_raw = parts[3]
        payload = payload_raw.split("^")
        if not payload:
            return {"type": "unknown", "raw": raw}

        symbol = payload[0]

        if tr_id == "H0STCNT0":  # 주식 체결
            # payload 예시:
            # [0] 종목코드, [1] 체결시간(HHMMSS), [2] 현재가, [3] 전일대비부호, [4] 전일대비(혹은 체결수량/필드), ...
            # 샘플 로그 기준으로는 [2]=현재가가 안정적으로 보입니다.
            try:
                price = float(payload[2]) if len(payload) > 2 else 0.0
                # 체결수량은 문서/계정 설정에 따라 위치가 달라질 수 있어 보수적으로 처리
                # 샘플에서는 [4]=체결수량처럼 보이는 값이 존재
                vol = int(float(payload[4])) if len(payload) > 4 else 0
                ts = payload[1] if len(payload) > 1 else ""

                return {
                    "type": "trade",
                    "symbol": symbol,
                    "price": price,
                    "volume": vol,
                    "timestamp": ts,
                }
            except Exception:
                return {"type": "parse_error", "raw": raw}

        if tr_id == "H0STASP0":  # 주식 호가
            # payload 예시:
            # [0] 종목코드, [1] 호가시간, [2] ???,
            # [3] 매도1 ... [12] 매도10,
            # [13] 매수1 ... [22] 매수10
            try:
                ask1 = float(payload[3]) if len(payload) > 3 else 0.0
                bid1 = float(payload[13]) if len(payload) > 13 else 0.0
                ts = payload[1] if len(payload) > 1 else ""
                return {
                    "type": "book",
                    "symbol": symbol,
                    "ask": ask1,
                    "bid": bid1,
                    "timestamp": ts,
                }
            except Exception:
                return {"type": "parse_error", "raw": raw}

        return {"type": "unknown", "raw": raw}

    def _get_message_type(self, data: dict) -> str:
        tr_id = data.get("body", {}).get("tr_id", "")
        # PINGPONG은 header에 있을 수도 있음.
        if tr_id == "PINGPONG":
            return "ping"
        if data.get("header", {}).get("tr_id") == "PINGPONG":
            return "ping"
            
        elif tr_id == "H0STCNT0":
            return "trade"
        elif tr_id == "H0STASP0":
            return "book"
        return "unknown"

    async def _send_pong(self, ws, raw: str | None = None):
        """KIS 서버 PING에 대한 PONG 응답 전송

        KIS 실시간 서버는 일반적인 WS ping frame이 아니라 애플리케이션 레벨의 PINGPONG 메시지를 보냅니다.
        잘못된 포맷으로 응답하면 OPSP8993(JSON PARSING ERROR)가 연속 발생하며 실시간 데이터가 끊깁니다.

        ✅ 가장 안전한 방식: 서버가 보낸 PING 메시지를 그대로 echo(재전송)
        """
        try:
            if raw:
                await ws.send(raw)
            else:
                await ws.send("PINGPONG")
            self.logger.debug("sent PONG/echo")
        except Exception as e:
            self.logger.debug("pong send failed: %s", e)

    def log_connection_test(self):
        """통신 테스트 로그 기록"""
        self.logger.info("=" * 50)
        self.logger.info(" [Communication Test] Connection Stable ")
        self.logger.info("=" * 50)

    def _to_dt(self, val):
        if isinstance(val, datetime):
            return val
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val)
            except ValueError:
                return datetime.utcnow()
        return datetime.utcnow()

    async def run(
        self,
        on_tick: Callable[[TradeTick], None],
        on_book: Callable[[OrderBookTop], None],
        on_status: Callable[[bool], None],
    ):
        while not self._stop:
            # 장 외 시간 또는 서버 점검 시간에는 연결 시도하지 않음
            if not self._is_market_hours() or not self._is_server_available():
                now_ts = time.time()
                if now_ts - self._last_market_closed_log_at >= self._market_closed_log_interval_sec:
                    self._last_market_closed_log_at = now_ts
                    self.logger.info("market closed or server under maintenance - skipping websocket connection")
                await asyncio.sleep(60)  # 1분마다 확인
                continue

            for delay in self._backoff_iter():
                if self._stop:
                    return

                sleep_s = delay

                try:
                    async with websockets.connect(self.url, ping_interval=None) as ws:
                        self.connected = True
                        on_status(True)
                        self.logger.info("ws connected")

                        # 연결 성공 시 통신 테스트 로그 출력
                        self.log_connection_test()

                        await self._subscribe(ws)
                        self.logger.info("subscription sent")

                        # 정상 구독/수신 시작하면 appkey 백오프는 리셋
                        self._reset_appkey_backoff()

                        async for raw in ws:
                            # 수신된 모든 메시지를 info로 찍으면 로그가 폭주할 수 있음.
                            # 기본은 샘플링된 debug 로그로만 남긴다.
                            try:
                                interval = int(os.environ.get("KIS_WS_RAW_LOG_INTERVAL_SEC", "60"))
                            except Exception:
                                interval = 60
                            now_ts = time.time()
                            last = getattr(self, "_last_raw_log_at", 0.0)
                            if now_ts - last >= max(5, interval):
                                setattr(self, "_last_raw_log_at", now_ts)
                                self.logger.debug(f"recv raw(sampled): {str(raw)[:200]}")

                            msg = self._parse_message(str(raw))
                            mtype = msg.get("type")

                            if mtype == "ping":
                                await self._send_pong(ws, raw=str(raw))
                                self.logger.debug("received PING, sent PONG")
                                continue

                            if mtype == "appkey_in_use":
                                # 동일 appkey 중복 사용 → 즉시 재연결을 난사하지 않고 긴 백오프 적용
                                sleep_s = self._bump_appkey_backoff()
                                self.logger.warning("appkey already in use(OPSP8996) - backing off %ss", sleep_s)
                                self.connected = False
                                on_status(False)
                                try:
                                    await ws.close()
                                except Exception:
                                    pass
                                break

                            if mtype == "trade":
                                tick = TradeTick(
                                    symbol=msg.get("symbol", ""),
                                    price=float(msg["price"]),
                                    volume=float(msg["volume"]),
                                    timestamp=self._to_dt(msg.get("timestamp")),
                                )
                                on_tick(tick)
                            elif mtype == "book":
                                book = OrderBookTop(
                                    symbol=msg.get("symbol", ""),
                                    bid=float(msg["bid"]),
                                    ask=float(msg["ask"]),
                                    bid_size=float(msg.get("bid_size", 0)),
                                    ask_size=float(msg.get("ask_size", 0)),
                                    timestamp=self._to_dt(msg.get("timestamp")),
                                )
                                on_book(book)
                            else:
                                self.logger.warning(f"unhandled message: {msg}")

                except Exception as e:
                    self.connected = False
                    on_status(False)
                    self.logger.error(f"ws error: {e}")

                self.logger.info("ws reconnect backoff %ss", sleep_s)
                await asyncio.sleep(sleep_s)

    def _backoff_iter(self):
        if not self.backoff_seq:
            while True:
                yield 3
        for d in self.backoff_seq:
            yield d
        while True:
            yield self.backoff_seq[-1]
