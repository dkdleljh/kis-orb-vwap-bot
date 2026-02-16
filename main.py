import asyncio
import json
import os
import signal
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from types import FrameType
from typing import Any, Dict, IO, Optional, cast

import fcntl

from bars_vwap import BarBuilder1m, VwapCalculator
from config import load_config
from indicators import rsi, sma, bollinger_bands, envelope, ema, macd
from kis_auth import KISAuth, load_auth_from_env
from kis_rest_orders import AccountInfo, KISRestOrders
from kis_ws_marketdata import KISWebSocket
from logger import setup_logger
from models import Bar1m, OrderBookTop, Position, TradeTick
from risk_manager import RiskManager
from strategy_state_machine import Signal, State, StrategyStateMachine
from utils_time import TimeRules, is_after, is_between, now_local, parse_time

from kis_scanner import KisScanner
from fee_calculator import FeeCalculator, USFeeCalculator

from candle_analysis import Candle, CandleAnalyzer
from us_prev_day import fetch_us_prev_daily
from ml_score import calculate_ml_score

from core.event_store import EventStore
from core import events as ievents
from core.oms import OMS, oms_enabled


class TradingEngine:
    """Main orchestrator for realtime KIS trading workflows.

    This engine wires configuration, authentication, market-data streams,
    state transitions, entry/exit execution, and risk guardrails into one
    long-running service process.
    """

    def __init__(self, base_dir: str) -> None:
        """Create a trading engine instance and initialize runtime components.

        Args:
            base_dir: Project base directory used to resolve config and log paths.

        Raises:
            RuntimeError: If required configuration fields are missing.

        Returns:
            None: Initializes in-memory state and service clients.
        """
        self.base_dir = base_dir

        # 1) 설정/환경 로드 (.env 포함)
        self.config = load_config(base_dir)

        # 2) 기본 설정값
        self.tz = self.config.get("timezone", "Asia/Seoul")

        log_dir = self.config.get("logging.dir", "logs")
        if not os.path.isabs(log_dir):
            log_dir = os.path.join(base_dir, log_dir)
        self.logger = setup_logger(log_dir, self.tz)

        # 3) 인증/계좌
        app_key, app_secret, _hts_id = load_auth_from_env()
        if not app_key or not app_secret:
            # .env를 못 읽었거나 키가 비어있으면 이후 REST/WS가 모두 실패함
            self.logger.warning(
                "KIS_APP_KEY / KIS_APP_SECRET 이 비어 있습니다(.env 확인 필요)."
            )

        rest_base_url = self.config.get("rest.base_url")
        if not rest_base_url:
            raise RuntimeError("config.json: rest.base_url 누락")

        self.auth = KISAuth(rest_base_url, app_key, app_secret, self.logger)

        acct_no = str(self.config.get("account.account_no", ""))
        acct_prdt = str(self.config.get("account.account_product_code", ""))
        if not acct_no or not acct_prdt:
            raise RuntimeError(
                "config.json: account.account_no / account.account_product_code 누락"
            )
        account = AccountInfo(account_no=acct_no, product_code=acct_prdt)
        self.rest = KISRestOrders(rest_base_url, self.auth, account, self.logger)

        # 4) 시간 규칙
        tr = self.config.get("time_rules", {}) or {}
        self.time_rules = TimeRules(
            observe_start=parse_time(tr.get("observe_start", "08:59:00")),
            or_start=parse_time(tr.get("or_start", "09:00:00")),
            or_end=parse_time(tr.get("or_end", "09:05:00")),
            entry_start=parse_time(tr.get("entry_start", "09:05:05")),
            force_exit=parse_time(tr.get("force_exit", "15:15:00")),
        )
        self.early_exit = parse_time(tr.get("early_exit", "15:00:00"))

        # 5) 트레이딩 파라미터
        tcfg = self.config.get("trading", {}) or {}
        self.entry_budget_pct = float(tcfg.get("entry_budget_pct", 0.20))
        self.stop_loss_pct = float(tcfg.get("stop_loss_pct", -0.015))
        self.take_profit_pct = float(tcfg.get("take_profit_pct", 0.030))
        self.quick_profit_pct = float(tcfg.get("quick_profit_pct", 0.010))
        self.min_profit_for_guarantee_pct = float(
            tcfg.get("min_profit_for_guarantee_pct", 0.008)
        )
        self.entry_retry_limit = int(tcfg.get("entry_retry_limit", 5))
        self.max_spread_pct = float(tcfg.get("max_spread_pct", 0.005))

        max_entries = int(tcfg.get("max_entries_per_day", 10))
        daily_loss_limit = float(tcfg.get("daily_loss_limit_pct", -0.05))
        max_consecutive_stop = int(tcfg.get("max_consecutive_stop", 3))
        self.risk = RiskManager(max_entries, daily_loss_limit, max_consecutive_stop)

        # [NEW] Fee Calculator for real-time profit calculation
        self.fee_calculator = FeeCalculator(
            commission_rate=0.00015,
            commission_min=1000,
            slippage_rate=0.001,
            tax_rate=0.002,
            is_overseas=False,
            min_commission_check=True,
        )
        self.us_fee_calculator = USFeeCalculator(
            commission_per_share=0.005,
            slippage_rate=0.001,
        )

        # 6) 상태머신/시장 상태
        self.state_machine = StrategyStateMachine(logger=self.logger)
        self.market_regime = "NEUTRAL"

        # 7) 종목(기본)
        self.symbol_lever = str(self.config.get("products.lever", "122630"))
        self.symbol_inverse = str(self.config.get("products.inverse", "114800"))

        # [NEW] 스캐너
        self.scanner = KisScanner(self.auth, rest_base_url)

        self.base_universe = [
            self.symbol_lever,
            self.symbol_inverse,
            "251340",
            "251780",
            "130680",
            "233740",
            "214420",
            "251920",
            "252670",
            "219480",
            "252990",
            "250790",
            "005930",
            "000660",
            "086520",
            "247540",
            "035420",
            "005490",
            "051910",
            "028300",
            "006400",
            "017670",
            "035720",
            "090460",
            "018700",
            "012450",
            "096770",
            "004020",
            "011070",
            "020560",
            "009830",
            "012750",
            "078130",
            "003380",
            "012610",
            "064350",
            "006360",
            "000880",
            "003670",
            "049770",
            "029780",
            "018260",
            "025860",
            "001040",
            "001120",
            "001430",
            "001450",
            "001680",
            "001740",
            "002020",
            "002190",
            "002240",
            "002380",
            "002410",
            "002620",
            "003030",
            "003490",
            "004170",
            "004250",
            "004370",
            "004540",
            "004800",
            "005300",
            "005690",
            "005940",
            "005960",
            "006120",
            "006200",
            "006280",
            "006800",
            "007070",
            "007810",
            "008770",
            "009420",
            "009970",
            "010120",
            "010130",
            "010140",
            "010400",
            "010600",
            "011150",
            "011170",
            "011790",
            "011980",
            "012030",
            "012450",
            "012470",
            "012630",
            "013360",
            "017800",
            "018880",
            "019170",
            "019680",
            "020150",
            "021240",
            "023590",
            "024070",
            "026890",
            "026960",
            "027050",
            "028260",
            "02850K",
            "029530",
            "030200",
            "030610",
            "032190",
            "032350",
            "032830",
            "033180",
            "033250",
            "034220",
            "034730",
            "034830",
            "035150",
            "035420",
            "035890",
            "036010",
            "036460",
            "036570",
            "037400",
            "037460",
            "037710",
            "037720",
            "037730",
            "037830",
            "038540",
            "038670",
            "038975",
            "039290",
            "039570",
            "040160",
            "040610",
            "041190",
            "041830",
            "041960",
            "042300",
            "042660",
            "042700",
            "042820",
            "043150",
            "043200",
            "043380",
            "043650",
            "044480",
            "044490",
            "044810",
            "045520",
            "045890",
            "046070",
            "046120",
            "046180",
            "046210",
            "046310",
            "046890",
            "047050",
            "047080",
            "047560",
            "047810",
            "047920",
            "048410",
            "048430",
            "048470",
            "048530",
            "048550",
            "048660",
            "048800",
            "049120",
            "049180",
            "049550",
            "050320",
            "050540",
            "050610",
            "050890",
            "050960",
            "051210",
            "051380",
            "051390",
            "051600",
            "051630",
            "051900",
            "051910",
            "051960",
            "052060",
            "052300",
            "052690",
            "053050",
            "053080",
            "053210",
            "053260",
            "053370",
            "053430",
            "053450",
            "053460",
            "053520",
            "053590",
            "053610",
            "053690",
            "053700",
            "053950",
            "054050",
            "054220",
            "054410",
            "054450",
            "054620",
            "054950",
            "055360",
            "055550",
            "055590",
            "055810",
            "056080",
            "056730",
            "057050",
            "057680",
            "058110",
            "058470",
            "058650",
            "058860",
            "059090",
            "060250",
            "060280",
            "060310",
            "060980",
            "061970",
            "063170",
            "064160",
            "064450",
            "065350",
            "066570",
            "066970",
            "068270",
            "068290",
            "069500",
            "069510",
            "069640",
            "069680",
            "070960",
            "071050",
            "071200",
            "072470",
            "072720",
            "073010",
            "074110",
            "075580",
            "076180",
            "077500",
            "078590",
            "078720",
            "078920",
            "079160",
            "079550",
            "079940",
            "080220",
            "080440",
            "081000",
            "081660",
            "082740",
            "082900",
            "083450",
            "083550",
            "084010",
            "084680",
            "084900",
            "085310",
            "085620",
            "085720",
            "086450",
            "086520",
            "086900",
            "087010",
            "088130",
            "088280",
            "088350",
            "089030",
            "089470",
            "090350",
            "090370",
            "090430",
            "090710",
            "090960",
            "091120",
            "091590",
            "091700",
            "091990",
            "092220",
            "092300",
            "092530",
            "092780",
            "093050",
            "093320",
            "093370",
            "093640",
            "093920",
            "094170",
            "094480",
            "094820",
            "095340",
            "095660",
            "095700",
            "095720",
            "096300",
            "096690",
            "097080",
            "097230",
            "097520",
            "097870",
            "098120",
            "098360",
            "099140",
            "099320",
            "099430",
            "099520",
            "100130",
            "100220",
            "100590",
            "100660",
            "100750",
            "101170",
            "101240",
            "101530",
            "101790",
            "102120",
            "102280",
            "102460",
            "102940",
            "103140",
            "103230",
            "103380",
            "104120",
            "104520",
            "104620",
            "105010",
            "105550",
            "105600",
            "105740",
            "105840",
            "106010",
            "106240",
            "106520",
            "107390",
            "108450",
            "108670",
            "108790",
            "108860",
            "109070",
            "109960",
            "110310",
            "110790",
            "111110",
            "111120",
            "111380",
            "111770",
            "112040",
            "112190",
            "112610",
            "113810",
            "114090",
            "114450",
            "115570",
            "115580",
            "115730",
            "115800",
            "115960",
            "116100",
            "117670",
            "117910",
            "118000",
            "118170",
            "118990",
            "119500",
            "119610",
            "120030",
            "120260",
            "120350",
            "120500",
            "121440",
            "121800",
            "122090",
            "122350",
            "122630",
            "122640",
            "122830",
            "123010",
            "123020",
            "123330",
            "123700",
            "123860",
            "124560",
            "124830",
            "124840",
            "124970",
            "125040",
            "125210",
            "125320",
            "125440",
            "125450",
            "126090",
            "126340",
            "126600",
            "126700",
            "126880",
            "127980",
            "128030",
            "128090",
            "128360",
            "128390",
            "128535",
            "128640",
            "128660",
            "128940",
            "129480",
            "129530",
            "129890",
            "130310",
            "130500",
            "130680",
            "130910",
            "131010",
            "131180",
            "131220",
            "131290",
            "131760",
            "131970",
            "132030",
            "132350",
            "132450",
            "133690",
            "133750",
            "134060",
            "134380",
            "134560",
            "134780",
            "135080",
            "135380",
            "135440",
            "136420",
            "136480",
            "136510",
            "137400",
            "138030",
            "138250",
            "138260",
            "138580",
            "139130",
            "140070",
            "140090",
            "140130",
            "140480",
            "141080",
            "141210",
            "142280",
            "142760",
            "143210",
            "143250",
            "144510",
            "144600",
            "144950",
            "145210",
            "145670",
            "146320",
            "146980",
            "148070",
            "148780",
            "149200",
            "149950",
            "150050",
            "150090",
            "150400",
            "151190",
            "151340",
            "151860",
            "152250",
            "152380",
            "153360",
            "153600",
            "154030",
            "154050",
            "155390",
            "156080",
            "157060",
            "157510",
            "157700",
            "158300",
            "158430",
            "158900",
            "159010",
            "159580",
            "159650",
            "159720",
            "159910",
            "161000",
            "161030",
            "161120",
            "161390",
            "161890",
            "162120",
            "162480",
            "163520",
            "163560",
            "163700",
            "163810",
            "163860",
            "164060",
            "164090",
            "164400",
            "165980",
            "166090",
            "166400",
            "166420",
            "167810",
            "168330",
            "169330",
            "169740",
            "170030",
            "170790",
            "170900",
            "171090",
            "171120",
            "171800",
            "171970",
            "172080",
            "172190",
            "172440",
            "173360",
            "174350",
            "175180",
            "175250",
            "175300",
            "176440",
            "177350",
            "177540",
            "177830",
            "178780",
            "178920",
            "179600",
            "179720",
            "180060",
            "180400",
            "180640",
            "180690",
            "180800",
            "181090",
            "181560",
            "182360",
            "182400",
            "182480",
            "182560",
            "182720",
            "183190",
            "183490",
            "183710",
            "184230",
            "184350",
            "184450",
            "184630",
            "185190",
            "185730",
            "185850",
            "186170",
            "186300",
            "186380",
            "186630",
            "186700",
            "186860",
            "187010",
            "187670",
            "187870",
            "188350",
            "188460",
            "188520",
            "188560",
            "188830",
            "189010",
            "189330",
            "189340",
            "189400",
            "189460",
            "189580",
            "189660",
            "189680",
            "189720",
            "189860",
            "189900",
            "190410",
            "190650",
            "190680",
            "190780",
            "190900",
            "191410",
            "191600",
            "192080",
            "192390",
            "192650",
            "193250",
            "193750",
            "194700",
            "195030",
            "195360",
            "195940",
            "196170",
            "196700",
            "197140",
            "197200",
            "197660",
            "197750",
            "197850",
            "198080",
            "198440",
            "198670",
            "198790",
            "199400",
            "199420",
            "199910",
            "200110",
            "200250",
            "200470",
            "200580",
            "200700",
            "200800",
            "200860",
            "200920",
            "201230",
            "201490",
            "201810",
            "202270",
            "202280",
            "202320",
            "202680",
            "202940",
            "203000",
            "203230",
            "203450",
            "203580",
            "203750",
            "204320",
            "204620",
            "204780",
            "205100",
            "205290",
            "205720",
            "205920",
            "206400",
            "207810",
            "208140",
            "208350",
            "208640",
            "209380",
            "209650",
            "210540",
            "210780",
            "211270",
            "211600",
            "211790",
            "212010",
            "212180",
            "212630",
            "212710",
            "212750",
            "213090",
            "213500",
            "214430",
            "214520",
            "214680",
            "215000",
            "215050",
            "215200",
            "215380",
            "215790",
            "215900",
            "216200",
            "216280",
            "216470",
            "216580",
            "216720",
            "217730",
            "217800",
            "218150",
            "218410",
            "218500",
            "218600",
            "219010",
            "219110",
            "219420",
            "219650",
            "220100",
            "220180",
            "220250",
            "220360",
            "220970",
            "221200",
            "221600",
            "222110",
            "222230",
            "222310",
            "222450",
            "222800",
            "222870",
            "223310",
            "223600",
            "223720",
            "223950",
            "224090",
            "224400",
            "224760",
            "224970",
            "225190",
            "225220",
            "225650",
            "225770",
            "225930",
            "226100",
            "226320",
            "226350",
            "226440",
            "226490",
            "226950",
            "227420",
            "227610",
            "227630",
            "227830",
            "228340",
            "228670",
            "229200",
            "229500",
            "229730",
            "230240",
            "230400",
            "230980",
            "231300",
            "231310",
            "231920",
            "232430",
            "232580",
            "232750",
            "233800",
            "234080",
            "234340",
            "234690",
            "235070",
            "235420",
            "235500",
            "235690",
            "236030",
            "236200",
            "236360",
            "237450",
            "237690",
            "238090",
            "238200",
            "238810",
            "239610",
            "240520",
            "241620",
            "241830",
            "242040",
            "242310",
            "242510",
            "242560",
            "242570",
            "242580",
            "242610",
            "242640",
            "242650",
            "242800",
            "242820",
            "242830",
            "242850",
            "242880",
            "242970",
            "243070",
            "243120",
            "243240",
            "243310",
            "243350",
            "243420",
            "243590",
            "243760",
            "244030",
            "244170",
            "244280",
            "244520",
            "244850",
            "245170",
            "245340",
            "245380",
            "245450",
            "245540",
            "245620",
            "245990",
            "246010",
            "246050",
            "246200",
            "246250",
            "246320",
            "246440",
            "246450",
            "246690",
            "246980",
            "247540",
            "247560",
            "247740",
            "248070",
            "248250",
            "248540",
            "248600",
            "248850",
            "249000",
            "249420",
            "249480",
            "249630",
            "250000",
            "250060",
            "250220",
            "250680",
            "250950",
            "251280",
            "251290",
            "251340",
            "251370",
            "251420",
            "251620",
            "251780",
            "252010",
            "252020",
            "252140",
            "252160",
            "252300",
            "252500",
            "252650",
            "252670",
            "252770",
            "253180",
            "253250",
            "253310",
            "253440",
            "253590",
            "253720",
            "254120",
            "254400",
            "254450",
            "254560",
            "254790",
            "255200",
            "255220",
            "255440",
            "255720",
            "256050",
            "256150",
            "256250",
            "256280",
            "256630",
            "257020",
            "257150",
            "257190",
            "257370",
            "257720",
            "258010",
            "258200",
            "258470",
            "258610",
            "258970",
            "259100",
            "259170",
            "259450",
            "259630",
            "259960",
            "260210",
            "260630",
            "260720",
            "261080",
            "261130",
            "261220",
            "261240",
            "261380",
            "261720",
            "261780",
            "262050",
            "262470",
            "262980",
            "263050",
            "263810",
            "264200",
            "264450",
            "264700",
            "264900",
            "265150",
            "265520",
            "265590",
            "265720",
            "266080",
            "266150",
            "266730",
            "267060",
            "267260",
            "267290",
            "267440",
            "267850",
            "268060",
            "268300",
            "268420",
            "268600",
            "269200",
            "269480",
            "270660",
        ]
        self.target_symbols = list(dict.fromkeys(self.base_universe))

        # 8) 웹소켓
        ws_url = self.config.get("ws.url", "")
        if not ws_url:
            raise RuntimeError("config.json: ws.url 누락")
        backoff_seq = self.config.get("ws.reconnect_backoff_sec", [1, 2, 3, 5, 8, 13])
        self.ws = KISWebSocket(
            ws_url,
            symbols=self.target_symbols,
            backoff_seq=backoff_seq,
            logger=self.logger,
            approval_key="",
            timezone=self.tz,
        )
        self.ws_connected = False

        # 9) 실시간 데이터 버퍼
        self.bar_builders: Dict[str, BarBuilder1m] = {}
        self.vwap_by_symbol: Dict[str, VwapCalculator] = {}
        self.last_price: Dict[str, float] = {}
        self.last_book: Dict[str, OrderBookTop] = {}
        self.bar_history: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"closes": deque(maxlen=600), "volumes": deque(maxlen=600)}
        )

        # 10) 캐시/기타
        self.peak_pnl_pct: Dict[str, float] = {}
        self._fallback_or_start: Optional[datetime] = None
        self._fallback_or_end: Optional[datetime] = None

        # 전략 점수제에 필요한 보조 데이터
        self.prev_close_by_symbol: Dict[str, float] = {}
        self._prev_close_loaded_ymd: str = ""

        # [NEW] 전일/당일/세션별(프리/본/애프터/나이트) 봉 분석
        self.candle_analyzer = CandleAnalyzer()

        # [PHASE1] Append-only event log for replay/explainability (best-effort)
        events_dir = os.path.join(self.base_dir, "logs", "events")
        self.run_id = os.environ.get("KIS_RUN_ID", "") or f"{int(time.time())}"
        self.event_store = EventStore(events_dir, enabled=True, run_id=self.run_id)

        # [PHASE3] Optional OMS (default OFF; non-invasive)
        self.oms: OMS | None = OMS() if oms_enabled() else None

        # Best-effort error counters for health/status (never used for control-flow)
        self._error_counts: dict[str, int] = defaultdict(int)

        # 뉴스 점수 캐시(비동기 갱신)
        self.news_score_by_symbol: Dict[str, int] = {}
        self.news_score_updated_at: Dict[str, float] = {}

        # 11) Production safety defaults
        self.live_enabled = os.environ.get("KIS_LIVE_ENABLED", "0") == "1"
        self.live_confirmed = os.environ.get("KIS_LIVE_CONFIRM", "") == "YES"
        self.max_position_qty = max(
            1, int(os.environ.get("KIS_MAX_POSITION_QTY", "200"))
        )
        self.max_trades_per_day = max(
            1, int(os.environ.get("KIS_MAX_TRADES_PER_DAY", "5"))
        )
        self.healthcheck_interval_sec = max(
            10, int(os.environ.get("KIS_HEALTHCHECK_INTERVAL_SEC", "60"))
        )
        self.status_path = os.path.join(self.base_dir, "logs", "health_status.json")
        self.trades_today = 0
        self._trade_counter_ymd = now_local(self.tz).strftime("%Y%m%d")

        # Concurrency guards (idempotency-lite)
        self._trade_lock = asyncio.Lock()
        self._entry_inflight: set[str] = set()
        self.logger.info(
            "Execution mode: live_enabled=%s confirmed=%s active_live=%s max_qty=%s max_trades/day=%s",
            self.live_enabled,
            self.live_confirmed,
            self.live_ordering_enabled(),
            self.max_position_qty,
            self.max_trades_per_day,
        )

    async def start(self) -> None:
        """Initialize external dependencies and start background loops.

        This method acquires authentication credentials, refreshes target symbols,
        restores positions, and launches websocket/time/position/health tasks.

        Raises:
            Exception: Propagates unrecoverable startup failures.

        Returns:
            None: Runs until one of the awaited background tasks exits.
        """
        # [RECOMMENDED] Pre-flight auth before the opening range window.
        # 개인 운용 기준: 09:00 직전에 토큰/approval을 확보해 WS/전략 준비 지연을 줄입니다.
        try:
            now = now_local(self.tz)
            if now.time() < self.time_rules.or_start:
                # Force real credentials (no dummy) so that failures surface early.
                await self.auth.fetch_token(force=True)
                await self.auth.fetch_approval_key(force=True)
        except Exception as e:
            # Preflight should not crash the engine; the normal retry loops below will handle it.
            self.logger.warning(f"Preflight auth failed (will retry in normal loop): {e}")

        # KIS 토큰은 1분당 1회 제한(EGW00133)이 있어, 스케줄러/재기동 상황에서 바로 죽지 않도록 재시도합니다.
        while True:
            try:
                await self.auth.fetch_token()
                break
            except Exception as e:
                msg = str(e)
                if "EGW00133" in msg or "1분당 1회" in msg:
                    self.logger.warning(
                        f"Token rate limit(EGW00133). retry in 65s: {e}"
                    )
                    await asyncio.sleep(65)
                    continue
                raise

        while True:
            try:
                await self.auth.fetch_approval_key()
                break
            except Exception as e:
                msg = str(e)
                if "EGW00133" in msg or "1분당 1회" in msg:
                    self.logger.warning(
                        f"ApprovalKey rate limit(EGW00133). retry in 65s: {e}"
                    )
                    await asyncio.sleep(65)
                    continue
                raise

        # prev_close 캐시 로드(점수제 GAP_UP 등에 필요)
        await self._load_prev_closes_if_needed(self.target_symbols)

        # [NEW][RECOMMENDED] US 전일 일봉도 best-effort로 채워 gap/일봉 분석에 사용
        await self._bootstrap_us_prev_day_candles(self.target_symbols)

        # [NEW] 장 시작 전/초반에 주도주 스캔 (Top 20 발굴)
        self.logger.info("Scanning for Market Leaders...")
        hot_stocks = await self.scanner.get_top_trading_value(limit=20)

        if hot_stocks:
            # 기본 유니버스 + 핫한 종목 합치기 (중복 제거)
            new_targets = list(set(self.base_universe + hot_stocks))
            # WS 구독 한도(40개) 고려하여 자르기
            if len(new_targets) > 40:
                new_targets = new_targets[:40]

            self.target_symbols = new_targets
            self.logger.info(
                f"Target Universe Updated ({len(self.target_symbols)}): {self.target_symbols}"
            )

            # WS 객체 재생성 또는 심볼 업데이트 필요
            # 현재 WS는 init에서 생성되므로, start() 내에서 ws.symbols를 업데이트해야 함
            self.ws.symbols = self.target_symbols

            # universe가 바뀌었으니 prev_close도 보강
            await self._load_prev_closes_if_needed(self.target_symbols)
        else:
            err = getattr(self.scanner, "last_error", "")
            if err:
                self.logger.warning(
                    f"Scanner found no stocks. Using base universe. last_error={err}"
                )
            else:
                self.logger.warning("Scanner found no stocks. Using base universe.")

        if self.auth.approval_key:
            self.ws.approval_key = self.auth.approval_key.approval_key
            self.logger.info("updated ws approval key")

        await self.restore_position()
        # ... (이하 동일) ...

        # OR 시간이 이미 지나서 실행된 경우: 시작 시점부터 5분을 대체 OR로 설정
        self._maybe_init_fallback_or()

        self.update_state_by_time()

        tasks = [
            asyncio.create_task(
                self.ws.run(self.on_tick, self.on_book, self.on_ws_status)
            ),
            asyncio.create_task(self.monitor_time_loop()),
            asyncio.create_task(self.monitor_position_loop()),
            asyncio.create_task(self.healthcheck_loop()),
        ]
        await asyncio.gather(*tasks)

    async def restore_position(self) -> None:
        """Restore an existing broker position into the strategy state.

        Returns:
            None: Updates state-machine position cache in place.
        """
        pos = await self.rest.get_positions()
        if pos:
            self.state_machine.position = pos
            self.state_machine.set_state(State.IN_POSITION)

            # Best-effort snapshot on restore (startup/resume trigger)
            try:
                self._record_position_snapshot(str(pos.symbol), trigger="restore")
            except Exception:
                pass
            self.logger.info(
                f"restored position: {pos.symbol} qty={pos.qty} avg={pos.avg_price}"
            )
        else:
            self.state_machine.position = None

    def _maybe_init_fallback_or(self) -> None:
        """Create a fallback opening-range window when startup is late.

        Returns:
            None: Mutates fallback OR timestamps and strategy state when needed.
        """
        now_dt = now_local(self.tz)

        # 이미 OR이 구축되어 있으면 불필요
        if self.state_machine.or_state:
            return

        # 정상 OR 구간 전이면 대체 OR 불필요
        if now_dt.time() < self.time_rules.or_end:
            return

        # 장 시작 이후, OR 시간을 놓친 상태라면: 시작 시각부터 5분
        start = now_dt.replace(second=0, microsecond=0)
        end = start + timedelta(minutes=5)

        self._fallback_or_start = start
        self._fallback_or_end = end
        self.state_machine.set_state(State.BUILD_OR)
        self.logger.info(f"fallback OR enabled: {start.time()} ~ {end.time()}")

    def on_ws_status(self, connected: bool) -> None:
        """Handle websocket connectivity status updates.

        Args:
            connected: Current websocket connection state.

        Returns:
            None: Stores status for downstream trading guards.
        """
        self.ws_connected = connected

    def on_tick(self, tick: TradeTick) -> None:
        """Process an incoming trade tick and update realtime buffers.

        Args:
            tick: Trade tick payload from websocket stream.

        Returns:
            None: Updates VWAP calculators, price cache, and bar builders.
        """
        if not tick.symbol:
            return

        # [PHASE1][RECOMMENDED] Do NOT log every tick by default (file growth).
        # Enable only for debugging/research.
        if os.environ.get("KIS_EVENT_LOG_TICKS", "0") == "1":
            try:
                self.event_store.append(
                    ievents.Event.make(
                        type="Tick",
                        symbol=tick.symbol,
                        payload={
                            "price": float(tick.price),
                            "volume": float(tick.volume),
                            "ts": tick.timestamp.isoformat(),
                        },
                    )
                )
            except Exception:
                pass
        if tick.symbol not in self.vwap_by_symbol:
            self.vwap_by_symbol[tick.symbol] = VwapCalculator()
        if tick.symbol not in self.bar_builders:
            symbol_for_callback = tick.symbol

            def callback(bar):
                self.on_bar_close(symbol_for_callback, bar)

            self.bar_builders[tick.symbol] = BarBuilder1m(callback)
        self.vwap_by_symbol[tick.symbol].update(tick)
        self.last_price[tick.symbol] = tick.price
        self.bar_builders[tick.symbol].update(tick)

    def on_book(self, book: OrderBookTop) -> None:
        """Process an incoming order-book top snapshot.

        Args:
            book: Best bid/ask snapshot for a symbol.

        Returns:
            None: Updates latest order-book cache.
        """
        if not book.symbol:
            return
        self.last_book[book.symbol] = book

    def on_bar_close(self, symbol: str, bar: Bar1m) -> None:
        """Handle one-minute bar close and evaluate entry opportunities.

        Args:
            symbol: Symbol associated with the closed bar.
            bar: Finalized one-minute OHLCV bar.

        Returns:
            None: Updates indicators/state and may schedule entry handling.
        """
        self.logger.info(
            f"[Bar] {symbol} bar closed: O={bar.open} H={bar.high} L={bar.low} C={bar.close} V={bar.volume}"
        )

        # [PHASE1] event log (bar close)
        try:
            self.event_store.append(
                ievents.Event.make(
                    type="Bar1mClosed",
                    symbol=symbol,
                    payload={
                        "start": bar.start.isoformat(),
                        "open": float(bar.open),
                        "high": float(bar.high),
                        "low": float(bar.low),
                        "close": float(bar.close),
                        "volume": float(bar.volume),
                    },
                )
            )
        except Exception:
            pass

        # 히스토리 업데이트
        self.bar_history[symbol]["closes"].append(bar.close)
        self.bar_history[symbol]["volumes"].append(bar.volume)

        # [NEW] 전일/당일/세션별 봉(프리/본/애프터/나이트) 집계
        try:
            switched = self.candle_analyzer.update(symbol, bar)
            if switched is not None:
                snap = self.candle_analyzer.snapshot(symbol)
                self.logger.info(
                    "[Candle] %s session switched -> %s gap=%.3f range=%.3f",
                    symbol,
                    switched.value,
                    float(snap.get("gap_pct", 0.0) or 0.0),
                    float(snap.get("today_range_pct", 0.0) or 0.0),
                )
        except Exception as e:
            self.logger.debug(f"candle analyzer update failed: {e}")

        # 시장 지표(KODEX 레버리지) 추세 업데이트
        if symbol == self.symbol_lever:
            closes = list(self.bar_history[symbol]["closes"])
            if len(closes) >= 60:
                ma60 = sma(closes, 60)
                if bar.close > ma60:
                    self.market_regime = "BULL"
                else:
                    self.market_regime = "BEAR"
                self.logger.info(
                    f"[Market Regime] {self.market_regime} (Price={bar.close} MA60={ma60:.1f})"
                )

        now_dt = now_local(self.tz)

        # 대체 OR 윈도우(늦게 실행된 경우): 시작 시점부터 5분간 OR 업데이트
        if self._fallback_or_start and self._fallback_or_end:
            if self._fallback_or_start <= now_dt < self._fallback_or_end:
                self.state_machine.update_or(symbol, bar)
                return
            if (
                now_dt >= self._fallback_or_end
                and self.state_machine.state == State.BUILD_OR
            ):
                self.state_machine.set_state(State.WAIT_SIGNAL)

        if is_between(self.time_rules.or_start, self.time_rules.or_end, now_dt):
            self.state_machine.update_or(symbol, bar)
            return

        if is_after(self.time_rules.entry_start, now_dt):
            if self.state_machine.state == State.BUILD_OR:
                self.state_machine.set_state(State.WAIT_SIGNAL)

        if self.state_machine.state != State.WAIT_SIGNAL:
            self.logger.debug(f"[Entry] skipped: state={self.state_machine.state}")
            return

        if self.kill_switch_on():
            self.logger.debug("[Entry] skipped: kill switch on")
            return

        if not self.ws_connected:
            self.logger.debug("[Entry] skipped: ws not connected")
            return

        if not self.risk.can_enter():
            self.logger.debug("[Entry] skipped: risk cannot enter")
            self.state_machine.set_state(State.DONE_TODAY)
            return

        last_price = self.last_price.get(symbol)
        book = self.last_book.get(symbol)
        vwap_calc = self.vwap_by_symbol.get(symbol)
        vwap = vwap_calc.vwap() if vwap_calc else None
        if last_price is None or book is None:
            self.logger.debug("[Entry] skipped: no price/book data")
            return
        if last_price <= 0 or book.ask <= 0 or book.bid <= 0:
            self.logger.debug(
                f"[Entry] skipped: invalid price/book: last_price={last_price} ask={book.ask} bid={book.bid}"
            )
            return

        # --- 보조지표 계산 ---
        closes = list(self.bar_history[symbol]["closes"])
        volumes = list(self.bar_history[symbol]["volumes"])

        indicators = {}

        try:
            # 점수제에 필요한 기본값들
            indicators["prev_close"] = float(
                self.prev_close_by_symbol.get(symbol, 0.0) or 0.0
            )
            indicators["news_score"] = int(
                self.news_score_by_symbol.get(symbol, 0) or 0
            )
            indicators["volume_power"] = 100.0

            # 뉴스 점수는 비동기 갱신(여기서는 캐시 값만 사용)
            self._maybe_refresh_news_score(symbol)

            # --- Bootstrap indicators ---
            # 5분만 쌓여도 최소한의 점수제 계산이 돌아가게 합니다.
            if len(closes) >= 5:
                indicators["ma5"] = sma(closes, 5)
                indicators["vol_ma5"] = sma(volumes, 5)

            # RSI는 14개부터 계산 가능
            if len(closes) >= 14:
                indicators["rsi"] = rsi(closes, 14)

            # EMA (9, 21) - 21개부터 계산 가능
            if len(closes) >= 21:
                indicators["ema9"] = ema(closes, 9)
                indicators["ema21"] = ema(closes, 21)

            # MACD (12, 26, 9) - 35개부터 계산 가능
            if len(closes) >= 35:
                macd_line, macd_signal, macd_hist = macd(closes, 12, 26, 9)
                indicators["macd_line"] = macd_line
                indicators["macd_signal"] = macd_signal
                indicators["macd_hist"] = macd_hist

            if len(closes) >= 20:
                indicators["ma20"] = sma(closes, 20)
                indicators["vol_ma20"] = sma(volumes, 20)

                # 볼린저/엔벨로프는 20개부터
                bb_up, bb_mid, bb_low = bollinger_bands(closes, 20, 2.0)
                indicators["bb_up"] = bb_up
                indicators["bb_mid"] = bb_mid

                env_up, env_mid, env_low = envelope(closes, 20, 2.0)
                indicators["env_up"] = env_up
                indicators["env_low"] = env_low
            else:
                # 20개가 안 쌓였으면 점수제용 ma20/vol_ma20을 ma5/vol_ma5로 대체
                if "ma5" in indicators and "ma20" not in indicators:
                    indicators["ma20"] = indicators["ma5"]
                if "vol_ma5" in indicators and "vol_ma20" not in indicators:
                    indicators["vol_ma20"] = indicators["vol_ma5"]

            # 체결강도(volume_power) 계산: 가능하면 vol_ma20(또는 대체된 값) 사용
            try:
                base_vol_ma = float(indicators.get("vol_ma20", 0) or 0)
                if base_vol_ma > 0:
                    indicators["volume_power"] = float(bar.volume) / base_vol_ma * 100.0
            except Exception:
                pass

            indicators["ml_score"] = calculate_ml_score(indicators, last_price, vwap)

        except Exception as e:
            self.logger.error(f"Indicator calc failed for {symbol}: {e}")
            return

        try:
            ml_score = indicators.get("ml_score", 50)
            if ml_score < 40:
                self.logger.debug(f"[ML Filter] {symbol} ML score too low: {ml_score}")
                return

            signal = self.state_machine.evaluate_entry(
                bar=bar,
                last_price=last_price,
                vwap=vwap,
                book=book,
                lever_symbol=self.symbol_lever,
                inverse_symbol=self.symbol_inverse,
                max_spread_pct=self.max_spread_pct,
                indicators=indicators,
                market_regime=self.market_regime,  # 시장 상태 전달
            )
            if signal.side:
                self.logger.info(
                    f"signal {signal.symbol} close={bar.close} vwap={vwap} rsi={indicators.get('rsi', 0):.1f} ma20={indicators.get('ma20', 0):.0f}"
                )
                try:
                    self.event_store.append(
                        ievents.Signal(
                            symbol=str(signal.symbol),
                            side=str(signal.side),
                            strength=float(indicators.get('ml_score', 50) or 50) / 100.0,
                            reason='state_machine',
                            model='ml_score_heuristic',
                        ).to_event(run_id=self.run_id)
                    )
                except Exception:
                    pass

                asyncio.create_task(self.handle_entry(signal, book, bar_start=bar.start))
        except Exception as e:
            self.logger.error(f"Evaluate entry failed for {symbol}: {e}")


    def _append_event(self, ev: ievents.Event) -> None:
        try:
            self.event_store.append(ev)
        except Exception:
            # The event_store itself is best-effort, but keep a separate counter
            # for higher-level monitoring.
            try:
                self._error_counts["event_append"] += 1
            except Exception:
                pass

    def _record_position_snapshot(self, symbol: str, *, trigger: str, note: str = "") -> None:
        """Best-effort PositionSnapshot logging.

        Phase3 baseline logs a snapshot even in paper mode.
        Cash is unknown in the current runtime path, so we default to 0.0.
        """
        qty = 0
        avg = 0.0
        cash = 0.0

        try:
            pos2 = self.state_machine.position
            if pos2 and pos2.symbol == symbol:
                qty = int(pos2.qty)
                avg = float(pos2.avg_price)
        except Exception:
            pass

        try:
            self._append_event(
                ievents.PositionSnapshot(
                    symbol=symbol,
                    qty=qty,
                    avg_price=avg,
                    cash=cash,
                    equity=None,
                    trigger=trigger,
                    note=note,
                ).to_event(run_id=self.run_id)
            )
        except Exception:
            self._error_counts["snapshot_event"] += 1

    def _make_idempotency_key(self, symbol: str, side: str, *, bar_start: datetime | None = None) -> str:
        """Create a stable idempotency key for an order decision.

        Phase3 recommendation:
        - tie the key to bar.start (minute bucket) instead of wall-clock seconds
        - keep it stable across retries for the same bar

        Notes:
        - For non-bar-driven actions (e.g. exits), callers may pass ``bar_start=None``;
          we fall back to current local minute.
        """
        from datetime import timezone

        dt = bar_start or now_local(self.tz)
        try:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self.tz)
            dt_utc = dt.astimezone(timezone.utc)
        except Exception:
            dt_utc = dt

        # minute-granularity is enough for 1m bars and avoids second-level jitter
        ts = dt_utc.strftime("%Y%m%dT%H%MZ")
        return f"{self.run_id}:{symbol}:{side}:{ts}"

    async def handle_entry(self, signal: Signal, book: OrderBookTop, *, bar_start: datetime | None = None) -> None:
        """Attempt to enter a position from a validated trading signal.

        Args:
            signal: Entry decision generated by the strategy state machine.
            book: Latest best bid/ask snapshot for price and spread checks.

        Returns:
            None: Updates engine/strategy state and may place entry orders.
        """
        if self.state_machine.state != State.WAIT_SIGNAL:
            return

        symbol = cast(str, signal.symbol)

        idempotency_key = self._make_idempotency_key(symbol, str(signal.side or 'BUY'), bar_start=bar_start)

        # Concurrency guard: prevent duplicate concurrent entries per symbol
        if not hasattr(self, "_trade_lock"):
            self._trade_lock = asyncio.Lock()
        if not hasattr(self, "_entry_inflight"):
            self._entry_inflight = set()

        async with self._trade_lock:
            if symbol in self._entry_inflight:
                self.logger.debug(f"[Entry] skipped: inflight symbol={symbol}")
                return
            self._entry_inflight.add(symbol)

        try:
            # --- 뉴스 필터 체크 (여기서 비동기 호출) ---
            # 인버스는 헷징/하락 베팅이므로 뉴스 필터 생략 (악재가 곧 호재)
            if symbol != self.symbol_inverse:
                try:
                    # 3초 타임아웃을 걸고 뉴스 점수 확인
                    news_result = await asyncio.wait_for(
                        self.state_machine.news_analyzer.get_sentiment_score(symbol),
                        timeout=3.0,
                    )
                    score = news_result.get("score", 0)
                    self.logger.info(
                        f"[News Filter] {symbol} Score: {score} ({news_result.get('summary')})"
                    )

                    if score < -20:
                        self.logger.warning(
                            f"[News Filter] BLOCKED: Sentiment is too negative ({score})"
                        )
                        # 진입 취소 (State 유지)
                        return

                    if score >= 50:
                        self.logger.info(
                            f"[News Filter] STRONG BUY SIGNAL: Score {score}"
                        )
                except Exception as e:
                    self.logger.warning(
                        f"[News Filter] Check failed, proceeding with technical only: {e}"
                    )
            # ------------------------------------------

            self.state_machine.set_state(State.ENTRY_PENDING)

            if book.ask <= 0:
                try:
                    self.event_store.append(ievents.RiskDecision(symbol=symbol, allowed=False, reason="ask=0", idempotency_key=idempotency_key).to_event(run_id=self.run_id))
                    if self.oms is not None:
                        self.oms.mark_risk(idempotency_key=idempotency_key, allowed=False)
                except Exception:
                    pass
                self.logger.info("entry skipped: ask=0")
                self.state_machine.set_state(State.WAIT_SIGNAL)
                return

            cash = await self.rest.get_cash_available(symbol, book.ask)

            if cash <= 0:
                self.logger.warning("cash query failed (0), using default budget")
                cash = 1000  # 최소 1,000원으로 저가 주식도 매수 가능

            budget = cash * self.entry_budget_pct
            qty = int(budget // book.ask)
            qty = min(qty, self.max_position_qty)

            # qty=0 원인을 로그로 남겨서 즉시 진단 가능하게
            if qty <= 0:
                try:
                    self.event_store.append(ievents.RiskDecision(symbol=symbol, allowed=False, reason="qty=0", idempotency_key=idempotency_key).to_event(run_id=self.run_id))
                    if self.oms is not None:
                        self.oms.mark_risk(idempotency_key=idempotency_key, allowed=False)
                except Exception:
                    pass
                self.logger.info(
                    f"entry skipped: qty=0 cash={cash:.0f} budget={budget:.0f} pct={self.entry_budget_pct:.3f} ask={book.ask:.0f}"
                )
                self.state_machine.set_state(State.WAIT_SIGNAL)
                return

            if self.trades_today >= self.max_trades_per_day:
                try:
                    self.event_store.append(ievents.RiskDecision(symbol=symbol, allowed=False, reason="max_trades_per_day", idempotency_key=idempotency_key).to_event(run_id=self.run_id))
                    if self.oms is not None:
                        self.oms.mark_risk(idempotency_key=idempotency_key, allowed=False)
                except Exception:
                    pass
                self.logger.warning(
                    "entry blocked by max trades/day guardrail (%s/%s)",
                    self.trades_today,
                    self.max_trades_per_day,
                )
                self.state_machine.set_state(State.DONE_TODAY)
                return

            # 여기부터가 '실제 진입 시도'
            try:
                self.event_store.append(
                    ievents.OrderIntent(
                        symbol=symbol,
                        side="BUY",
                        qty=int(qty),
                        order_type="LMT",
                        limit_price=float(book.ask),
                        idempotency_key=idempotency_key,
                    ).to_event(run_id=self.run_id)
                )
            except Exception:
                pass

            if self.oms is not None:
                try:
                    self.oms.register_intent(symbol=symbol, side="BUY", qty=int(qty), idempotency_key=idempotency_key)
                except Exception:
                    self._error_counts["oms_register_intent"] += 1

            try:
                self.event_store.append(
                    ievents.RiskDecision(
                        symbol=symbol,
                        allowed=True,
                        reason="ok",
                        idempotency_key=idempotency_key,
                    ).to_event(run_id=self.run_id)
                )
                if self.oms is not None:
                    self.oms.mark_risk(idempotency_key=idempotency_key, allowed=True)
            except Exception:
                pass

            self.risk.record_entry()
            self.trades_today += 1

            if not self.live_ordering_enabled():
                self.state_machine.position = Position(
                    symbol=symbol,
                    qty=qty,
                    avg_price=float(book.ask),
                    entry_time=now_local(self.tz),
                )
                self.state_machine.set_state(State.IN_POSITION)
                self.logger.info(
                    "[PAPER] entry simulated %s qty=%s price=%s", symbol, qty, book.ask
                )
                try:
                    self.event_store.append(
                        ievents.OrderSubmitted(
                            symbol=symbol,
                            idempotency_key=idempotency_key,
                            broker_order_id="PAPER",
                        ).to_event(run_id=self.run_id)
                    )
                    self.event_store.append(
                        ievents.OrderAck(
                            symbol=symbol,
                            idempotency_key=idempotency_key,
                            broker_order_id="PAPER",
                            status="ACK",
                        ).to_event(run_id=self.run_id)
                    )
                    self.event_store.append(
                        ievents.Fill(
                            symbol=symbol,
                            side="BUY",
                            qty=int(qty),
                            price=float(book.ask),
                            broker_order_id="PAPER",
                            idempotency_key=idempotency_key,
                            fee=0.0,
                        ).to_event(run_id=self.run_id)
                    )

                    if self.oms is not None:
                        self.oms.apply_fill(idempotency_key=idempotency_key, fill_qty=int(qty))
                    self._record_position_snapshot(symbol, trigger="fill", note="paper_entry")
                except Exception:
                    pass
                self.peak_pnl_pct[symbol] = -0.01
                return

            for i in range(self.entry_retry_limit):
                if self.kill_switch_on():
                    break
                order = await self.rest.place_buy_limit(symbol, qty, book.ask)
                self.logger.info(
                    f"entry order submitted {symbol} qty={qty} id={order.order_id}"
                )
                try:
                    broker_order_id = str(order.order_id or "")
                    self.event_store.append(
                        ievents.OrderSubmitted(
                            symbol=symbol,
                            idempotency_key=idempotency_key,
                            broker_order_id=broker_order_id,
                        ).to_event(run_id=self.run_id)
                    )
                    if self.oms is not None:
                        self.oms.mark_submitted(idempotency_key=idempotency_key, broker_order_id=broker_order_id)
                    if broker_order_id:
                        self.event_store.append(
                            ievents.OrderAck(
                                symbol=symbol,
                                idempotency_key=idempotency_key,
                                broker_order_id=broker_order_id,
                                status="ACK",
                            ).to_event(run_id=self.run_id)
                        )
                        if self.oms is not None:
                            self.oms.mark_acked(idempotency_key=idempotency_key, broker_order_id=broker_order_id)
                except Exception:
                    pass
                await asyncio.sleep(2)
                pos = await self.rest.get_positions()
                if pos and pos.symbol == symbol and pos.qty >= qty:
                    pos.entry_time = now_local(self.tz)
                    self.state_machine.position = pos
                    self.state_machine.set_state(State.IN_POSITION)
                    self.logger.info(
                        f"entry filled {symbol} qty={pos.qty} avg={pos.avg_price}"
                    )
                    try:
                        broker_order_id = str(order.order_id or "")
                        self.event_store.append(
                            ievents.Fill(
                                symbol=symbol,
                                side="BUY",
                                qty=int(pos.qty),
                                price=float(pos.avg_price),
                                broker_order_id=broker_order_id,
                                idempotency_key=idempotency_key,
                                fee=0.0,
                            ).to_event(run_id=self.run_id)
                        )
                        if self.oms is not None:
                            self.oms.apply_fill(idempotency_key=idempotency_key, fill_qty=int(pos.qty))
                        self._record_position_snapshot(symbol, trigger="fill", note="live_entry")
                    except Exception:
                        pass
                    # 진입 성공 시 Peak PnL 초기화
                    self.peak_pnl_pct[symbol] = -0.01
                    return
                if order.order_id:
                    await self.rest.cancel_order(order.order_id, symbol, qty)
                    self.logger.info(f"entry cancel {order.order_id}")

            # 진입 실패 시 (예: 증거금 부족, 통신 오류 등)
            self.state_machine.set_state(State.WAIT_SIGNAL)
            self.logger.info("entry failed after retries")

            # 쿨다운 적용: 실패 직후 동일 신호로 무한 재진입 방지 (30초 대기)
            self.logger.info("cooling down for 30s to prevent spamming...")
            await asyncio.sleep(30)

        finally:
            self._entry_inflight.discard(symbol)

    async def handle_exit(self, reason: str, use_market: bool = True) -> None:
        """Attempt to close the current position using configured exit logic.

        Args:
            reason: Human-readable reason for the exit attempt.
            use_market: Exit preference flag kept for interface compatibility.

        Returns:
            None: Updates position/risk state after execution attempts.
        """
        if self.state_machine.state not in (State.IN_POSITION, State.EXIT_PENDING):
            return
        pos = self.state_machine.position
        if not pos:
            return
        self.state_machine.set_state(State.EXIT_PENDING)

        symbol = str(pos.symbol)
        idempotency_key = self._make_idempotency_key(symbol, "SELL")
        try:
            self.event_store.append(
                ievents.OrderIntent(
                    symbol=symbol,
                    side="SELL",
                    qty=int(pos.qty),
                    order_type="MKT" if use_market else "LMT",
                    limit_price=None,
                    idempotency_key=idempotency_key,
                ).to_event(run_id=self.run_id)
            )
        except Exception:
            pass

        if not self.live_ordering_enabled():
            exit_price = self.last_price.get(pos.symbol, pos.avg_price)
            gross_pnl_pct = (
                (exit_price - pos.avg_price) / pos.avg_price if pos.avg_price else 0.0
            )
            net_pnl_pct = self.fee_calculator.get_net_pnl_percent(
                pos.avg_price, exit_price
            )
            pnl_pct = net_pnl_pct
            is_stop = pnl_pct <= self.stop_loss_pct
            self.risk.record_exit(pnl_pct, is_stop)
            self.state_machine.position = None
            self.state_machine.set_state(State.WAIT_SIGNAL)
            self.logger.info(
                "[PAPER] exit simulated reason=%s pnl=%.4f(gross=%.4f, net=%.4f)",
                reason,
                pnl_pct,
                gross_pnl_pct,
                net_pnl_pct,
            )

            try:
                self.event_store.append(
                    ievents.OrderSubmitted(
                        symbol=symbol,
                        idempotency_key=idempotency_key,
                        broker_order_id="PAPER",
                    ).to_event(run_id=self.run_id)
                )
                self.event_store.append(
                    ievents.OrderAck(
                        symbol=symbol,
                        idempotency_key=idempotency_key,
                        broker_order_id="PAPER",
                        status="ACK",
                    ).to_event(run_id=self.run_id)
                )
                self.event_store.append(
                    ievents.Fill(
                        symbol=symbol,
                        side="SELL",
                        qty=int(pos.qty),
                        price=float(exit_price),
                        broker_order_id="PAPER",
                        idempotency_key=idempotency_key,
                        fee=0.0,
                    ).to_event(run_id=self.run_id)
                )
                if self.oms is not None:
                    self.oms.apply_fill(idempotency_key=idempotency_key, fill_qty=int(pos.qty))
                self._record_position_snapshot(symbol, trigger="fill", note=f"paper_exit:{reason}")
            except Exception:
                pass

            return

        # Smart Market Order: 무조건 시장가가 아니라, 최우선 매수호가(bid)에 지정가 매도
        # 호가 정보가 없으면 어쩔 수 없이 시장가 사용
        current_book = self.last_book.get(pos.symbol)

        if current_book and current_book.bid > 0:
            # 매수 1호가에 던짐 (시장가와 체결 효과는 같으나, 급락 시 안전장치)
            price = current_book.bid
            order = await self.rest.place_sell_limit(pos.symbol, pos.qty, price)
            self.logger.info(
                f"exit order(SmartLimit) {reason} price={price} id={order.order_id}"
            )
        else:
            # 호가 정보 없으면 시장가
            order = await self.rest.place_sell_market(pos.symbol, pos.qty)
            self.logger.info(f"exit order(Market) {reason} id={order.order_id}")

        try:
            broker_order_id = str(order.order_id or "")
            self.event_store.append(
                ievents.OrderSubmitted(
                    symbol=symbol,
                    idempotency_key=idempotency_key,
                    broker_order_id=broker_order_id,
                ).to_event(run_id=self.run_id)
            )
            if broker_order_id:
                self.event_store.append(
                    ievents.OrderAck(
                        symbol=symbol,
                        idempotency_key=idempotency_key,
                        broker_order_id=broker_order_id,
                        status="ACK",
                    ).to_event(run_id=self.run_id)
                )
        except Exception:
            pass

        await asyncio.sleep(2)
        pos_after = await self.rest.get_positions()
        if not pos_after:
            exit_price = self.last_price.get("_last", pos.avg_price)
            pnl_pct = (exit_price - pos.avg_price) / pos.avg_price
            is_stop = pnl_pct <= self.stop_loss_pct
            self.risk.record_exit(pnl_pct, is_stop)

            try:
                broker_order_id = str(order.order_id or "")
                self.event_store.append(
                    ievents.Fill(
                        symbol=symbol,
                        side="SELL",
                        qty=int(pos.qty),
                        price=float(exit_price),
                        broker_order_id=broker_order_id,
                        idempotency_key=idempotency_key,
                        fee=0.0,
                    ).to_event(run_id=self.run_id)
                )
                if self.oms is not None:
                    self.oms.apply_fill(idempotency_key=idempotency_key, fill_qty=int(pos.qty))
                self._record_position_snapshot(symbol, trigger="fill", note=f"live_exit:{reason}")
            except Exception:
                pass

            self.state_machine.position = None
            self.state_machine.set_state(State.WAIT_SIGNAL)
            self.logger.info(f"exit done pnl={pnl_pct:.4f}")
        else:
            self.logger.info("exit pending; will retry monitoring")

    async def monitor_time_loop(self) -> None:
        """Continuously update time-based state transitions.

        Returns:
            None: Runs as an infinite periodic background loop.
        """
        while True:
            self._reset_daily_counters_if_needed()
            self.update_state_by_time()
            await asyncio.sleep(1)

    def update_state_by_time(self) -> None:
        """Apply time-rule-driven transitions for trading session state.

        Returns:
            None: Mutates state machine according to current local time.
        """
        now_dt = now_local(self.tz)

        # 1) 강제 종료 시간 (15:15 ~ 15:20)
        # 15:15: 1차 청산 시도 (지정가/시장가)
        # 15:18: 2차 강제 청산 (남은 물량 시장가 투척)
        force_exit_time = self.time_rules.force_exit
        final_kill_time = force_exit_time.replace(minute=18)

        if is_after(final_kill_time, now_dt):
            if self.state_machine.in_position():
                # 15:18 넘으면 묻지도 따지지도 않고 시장가 청산
                self.logger.warning(
                    "🚨 EMERGENCY EXIT (Market Close): Dumping all positions!"
                )
                # 비동기로 던져버림 (fire and forget 스타일)
                asyncio.create_task(
                    self.handle_exit("emergency_market_close", use_market=True)
                )

            if self.state_machine.state != State.DONE_TODAY:
                self.state_machine.set_state(State.DONE_TODAY)
            return

        # 0-1) 조기 청산 (15:00): 수익 여부와 관계없이 무조건 청산
        if is_after(self.early_exit, now_dt):
            if self.state_machine.in_position():
                pos = self.state_machine.position
                pnl_pct = 0.0
                if pos:
                    last_price = self.last_price.get(pos.symbol)
                    if last_price:
                        pnl_pct = (last_price - pos.avg_price) / pos.avg_price
                self.logger.warning(
                    f" EARLY EXIT (15:00): Closing all positions! PnL={pnl_pct:.2%}"
                )
                asyncio.create_task(self.handle_exit("early_exit_15_00"))

            if self.state_machine.state != State.DONE_TODAY:
                self.state_machine.set_state(State.DONE_TODAY)
            return

        if is_after(force_exit_time, now_dt):
            if self.state_machine.in_position():
                # 15:15 ~ 15:18: 일반적인 강제 청산 시도
                asyncio.create_task(self.handle_exit("force_exit"))

            if self.state_machine.state != State.DONE_TODAY:
                self.state_machine.set_state(State.DONE_TODAY)
            return

        if self.state_machine.state == State.DONE_TODAY:
            return

        # 2) 대체 OR 윈도우(늦게 실행된 경우) 처리
        if self._fallback_or_start and self._fallback_or_end:
            if self._fallback_or_start <= now_dt < self._fallback_or_end:
                if self.state_machine.state != State.BUILD_OR:
                    self.state_machine.set_state(State.BUILD_OR)
                return
            if (
                now_dt >= self._fallback_or_end
                and self.state_machine.state == State.BUILD_OR
            ):
                self.state_machine.set_state(State.WAIT_SIGNAL)
                return

        # 3) 정상 OR 시간대 처리
        if is_between(self.time_rules.or_start, self.time_rules.or_end, now_dt):
            if self.state_machine.state != State.BUILD_OR:
                self.state_machine.set_state(State.BUILD_OR)
            return

        if is_after(self.time_rules.entry_start, now_dt):
            if self.state_machine.state in (State.WAIT_OPEN, State.BUILD_OR):
                self.state_machine.set_state(State.WAIT_SIGNAL)
            return

        if not is_after(self.time_rules.observe_start, now_dt):
            self.state_machine.set_state(State.WAIT_OPEN)

    async def monitor_position_loop(self) -> None:
        """Continuously evaluate kill switch and exit conditions.

        Returns:
            None: Runs as an infinite periodic background loop.
        """
        while True:
            await self.check_kill_switch()
            await self.check_force_exit()
            await self.check_tp_sl()
            await asyncio.sleep(1)

    async def check_kill_switch(self) -> None:
        """Disable new entries when an external kill switch is active.

        Returns:
            None: Updates strategy state to DONE_TODAY when applicable.
        """
        if self.kill_switch_on() and self.state_machine.state == State.WAIT_SIGNAL:
            self.state_machine.set_state(State.DONE_TODAY)
            self.logger.info("kill switch on; no new entries")

    async def check_force_exit(self) -> None:
        """Trigger forced exit after configured forced-exit time.

        Returns:
            None: Schedules or executes position close when required.
        """
        now_dt = now_local(self.tz)

        if self.state_machine.in_position():
            # 조기 청산 (15:00): 수익 여부와 관계없이 무조건 청산
            if is_after(self.early_exit, now_dt):
                pnl_pct = 0.0
                pos = self.state_machine.position
                if pos:
                    last_price = self.last_price.get(pos.symbol)
                    if last_price:
                        pnl_pct = (last_price - pos.avg_price) / pos.avg_price
                self.logger.warning(
                    f" EARLY EXIT (15:00): Closing all positions! PnL={pnl_pct:.2%}"
                )
                await self.handle_exit("early_exit_15_00")
                return

            # 강제 청산 (15:15)
            if is_after(self.time_rules.force_exit, now_dt):
                await self.handle_exit("force_exit")

    async def check_tp_sl(self) -> None:
        """Evaluate advanced take-profit and stop-loss exit conditions.

        Returns:
            None: May trigger partial or full position exits.
        """
        if not self.state_machine.in_position():
            return
        pos = self.state_machine.position
        if not pos:
            return

        last_price = self.last_price.get(pos.symbol)
        if last_price is None and not self.ws_connected:
            quote = await self.rest.get_quote(pos.symbol)
            try:
                out = quote.get("output", {}) or {}
                price_candidates = ["stck_prpr", "stck_clpr", "prdy_clpr", "stck_sdpr"]
                for k in price_candidates:
                    v = out.get(k)
                    if v:
                        last_price = float(v)
                        break
            except Exception:
                last_price = None

        if not last_price:
            return

        gross_pnl_pct = (last_price - pos.avg_price) / pos.avg_price
        net_pnl_pct = self.fee_calculator.get_net_pnl_percent(pos.avg_price, last_price)
        pnl_pct = net_pnl_pct
        pos.unrealized_pnl_pct = pnl_pct
        pos.unrealized_gross_pnl_pct = gross_pnl_pct

        self.logger.debug(
            f"[Fee Adjust] {pos.symbol} Gross={gross_pnl_pct:.4f} Net={net_pnl_pct:.4f}"
        )

        # --- 1% 수익 달성 로직 (핵심) ---

        # 0. Quick Profit (1%): 수익 1% 이상 시 즉시 전량 익절
        if pnl_pct >= self.quick_profit_pct and not pos.profit_locked:
            self.logger.info(
                f" PROFIT TARGET🎯 QUICK REACHED: {pos.symbol} PnL={pnl_pct:.2%}"
            )
            pos.profit_locked = True
            await self.handle_exit("quick_profit_1pct")
            return

        # 0-1. Profit Lock: 1% 달성 후 0.5% 이상 하락 시 익절 (수익 보장)
        if pos.profit_locked and pnl_pct < self.min_profit_for_guarantee_pct:
            self.logger.info(
                f"🔒 PROFIT LOCKED - Protecting gains: {pos.symbol} PnL={pnl_pct:.2%}"
            )
            await self.handle_exit("profit_lock_protection")
            return

        # --- 고급 청산 로직 (Advanced Exit) ---

        # 1. 부분 익절 (Scale-out): 1% 수익 시 절반 익절
        if pnl_pct >= self.quick_profit_pct and not pos.tp1_done and pos.qty > 1:
            half_qty = int(pos.qty * 0.5)
            if half_qty > 0:
                self.logger.info(
                    f"💰 TP1 (Scale-out): {pos.symbol} PnL={pnl_pct:.2%} Qty={half_qty}"
                )
                await self.rest.place_sell_market(pos.symbol, half_qty)
                pos.qty -= half_qty
                pos.tp1_done = True
                pos.profit_locked = True
                return

        # 2. 고점 수익률 갱신 (Trailing Stop용)
        if pos.symbol in self.peak_pnl_pct:
            self.peak_pnl_pct[pos.symbol] = max(self.peak_pnl_pct[pos.symbol], pnl_pct)
        else:
            self.peak_pnl_pct[pos.symbol] = pnl_pct

        peak_pnl = self.peak_pnl_pct[pos.symbol]

        # 3. 본전 스탑 (Breakeven): 수익이 +1.2% 이상 났다가 +0.3% 미만으로 떨어지면 즉시 청산
        if peak_pnl >= 0.012 and pnl_pct < 0.003:
            await self.handle_exit("breakeven_stop")
            return

        # 4. 트레일링 스탑 (Trailing): 수익이 +3.0% 이상 났다가, 고점 대비 -1.0% 하락하면 익절
        if peak_pnl >= 0.030 and (peak_pnl - pnl_pct) >= 0.010:
            await self.handle_exit(f"trailing_stop (peak={peak_pnl:.2%})")
            return

        # 5. 기존 TP/SL (전량 청산)
        if pnl_pct >= self.take_profit_pct:
            await self.handle_exit("take_profit")
        elif pnl_pct <= self.stop_loss_pct:
            await self.handle_exit("stop_loss")

    def kill_switch_on(self) -> bool:
        """Check whether trading should stop via environment or flag file.

        Returns:
            bool: True when the kill switch is active.
        """
        if os.environ.get("KIS_KILL_SWITCH", "0") == "1":
            return True
        return os.path.exists(os.path.join(self.base_dir, "STOP_TRADING.flag"))

    def live_ordering_enabled(self) -> bool:
        """Check whether live order placement is currently allowed.

        Returns:
            bool: True when live mode and confirmation gate are both enabled.
        """
        return self.live_enabled and self.live_confirmed

    def _reset_daily_counters_if_needed(self) -> None:
        """Reset per-day trade counters when date boundary changes.

        Returns:
            None: Updates cached trading day and counter values in place.
        """
        ymd = now_local(self.tz).strftime("%Y%m%d")
        if ymd != self._trade_counter_ymd:
            self._trade_counter_ymd = ymd
            self.trades_today = 0

    def get_health_status(self) -> dict[str, object]:
        """Build a serializable snapshot of current engine health state.

        Returns:
            dict[str, object]: Health metadata for monitoring and diagnostics.
        """
        pos = self.state_machine.position
        # [PHASE1] health payload should include event store hints for ops/debug.
        events_dir = getattr(self.event_store, "base_dir", None)
        try:
            recent_counts = self.event_store.recent_event_counts(days=2)
        except Exception:
            recent_counts = {}

        universe_summary = {
            "base_universe_count": int(len(getattr(self, "base_universe", []) or [])),
            "target_symbols_count": int(len(self.target_symbols)),
            "sample": list(self.target_symbols)[:10],
        }

        return {
            "timestamp": now_local(self.tz).isoformat(),
            "state": str(self.state_machine.state),
            "ws_connected": bool(self.ws_connected),
            "kill_switch": bool(self.kill_switch_on()),
            "live_enabled": bool(self.live_enabled),
            "live_confirmed": bool(self.live_confirmed),
            "live_ordering_active": bool(self.live_ordering_enabled()),
            "trades_today": int(self.trades_today),
            "max_trades_per_day": int(self.max_trades_per_day),
            "max_position_qty": int(self.max_position_qty),
            "position": pos.to_dict() if pos else None,
            "symbols": list(self.target_symbols),
            "universe": universe_summary,
            "events": {
                "base_dir": events_dir,
                "recent_counts": recent_counts,
            },
        }

    async def healthcheck_loop(self) -> None:
        """Write periodic health snapshots and emit status logs.

        Returns:
            None: Runs as an infinite periodic background loop.
        """
        os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
        while True:
            status = self.get_health_status()
            try:
                with open(self.status_path, "w", encoding="utf-8") as f:
                    json.dump(status, f, ensure_ascii=True, indent=2)
            except Exception as e:
                self.logger.warning(f"healthcheck write failed: {e}")
            self.logger.info(
                "health state=%s ws=%s live=%s kill=%s trades=%s/%s",
                status["state"],
                status["ws_connected"],
                status["live_ordering_active"],
                status["kill_switch"],
                status["trades_today"],
                status["max_trades_per_day"],
            )
            await asyncio.sleep(self.healthcheck_interval_sec)

    # (position_snapshot_loop removed in Phase3 commit; reintroduced in Phase4)

    async def _load_prev_closes_if_needed(self, symbols: list[str]) -> None:
        """Populate cached previous-close values for supplied symbols.

        Args:
            symbols: Symbols requiring previous-close bootstrap data.

        Returns:
            None: Updates ``prev_close_by_symbol`` cache in place.
        """
        from datetime import datetime

        ymd = datetime.now().strftime("%Y%m%d")
        # 하루에 한 번만 전체 로드(혹은 필요한 심볼만 보강)
        if self._prev_close_loaded_ymd != ymd:
            self.prev_close_by_symbol.clear()
            self._prev_close_loaded_ymd = ymd

        for sym in symbols:
            if sym in self.prev_close_by_symbol:
                continue
            try:
                q = await self.rest.get_quote(sym)
                out = (q or {}).get("output", {}) or {}

                # KIS 응답 키는 문서/환경에 따라 조금씩 다를 수 있어 후보를 여러 개 둠
                candidates = [
                    "stck_sdpr",  # 전일종가(많이 사용)
                    "prdy_clpr",  # 전일종가 후보
                    "stck_prdy_clpr",
                    "stck_clpr",  # 종가
                ]
                prev = 0.0
                for k in candidates:
                    v = out.get(k)
                    if v is None or v == "":
                        continue
                    try:
                        prev = float(v)
                        break
                    except Exception:
                        continue

                if prev > 0:
                    self.prev_close_by_symbol[sym] = prev

                    # [NEW] 전일 일봉(가능한 경우)도 같이 캐시: gap/전일 range 등에 사용
                    try:
                        prev_o = float(out.get("prdy_oprc") or 0)
                        prev_h = float(out.get("prdy_hgpr") or 0)
                        prev_l = float(out.get("prdy_lwpr") or 0)
                        prev_c = float(prev)
                        if prev_o > 0 and prev_h > 0 and prev_l > 0 and prev_c > 0:
                            self.candle_analyzer.set_prev_day_candle(
                                sym,
                                Candle(
                                    open=prev_o,
                                    high=prev_h,
                                    low=prev_l,
                                    close=prev_c,
                                    volume=0.0,
                                ),
                            )
                    except Exception:
                        pass
                else:
                    # 최소한 현재가라도 넣어 0으로 인해 점수 계산이 죽지 않게
                    try:
                        cur = float(out.get("stck_prpr", 0) or 0)
                    except Exception:
                        cur = 0
                    if cur > 0:
                        self.prev_close_by_symbol[sym] = cur

            except Exception as e:
                self.logger.warning(f"prev_close load failed for {sym}: {e}")

    async def _bootstrap_us_prev_day_candles(self, symbols: list[str]) -> None:
        """Best-effort bootstrap of US previous-day daily candles.

        This is used to make gap/session analysis available even before enough
        intraday 1m bars have accumulated.
        """
        try:
            # only once per day
            from datetime import datetime

            ymd = datetime.now().strftime("%Y%m%d")
            key = f"us_prev_day_bootstrap:{ymd}"
            if getattr(self, "_bootstrapped_flags", None) is None:
                self._bootstrapped_flags = set()
            if key in self._bootstrapped_flags:
                return
            self._bootstrapped_flags.add(key)

            us_syms: list[str] = []
            tasks = []
            for sym in symbols:
                if sym.isdigit():
                    continue
                if sym in self.candle_analyzer.prev_day_by_symbol:
                    continue
                us_syms.append(sym)
                tasks.append(fetch_us_prev_daily(sym))

            if not tasks:
                return

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for sym, res in zip(us_syms, results):
                if isinstance(res, Exception) or res is None:
                    continue
                self.candle_analyzer.set_prev_day_candle(sym, res)
        except Exception as e:
            self.logger.debug(f"us prev-day bootstrap failed: {e}")

    def _maybe_refresh_news_score(self, symbol: str) -> None:
        """Refresh cached news sentiment score asynchronously when stale.

        Args:
            symbol: Symbol whose sentiment cache may need refresh.

        Returns:
            None: Schedules async refresh task when refresh interval elapsed.
        """
        if symbol == self.symbol_inverse:
            return

        now_ts = time.time()
        last_ts = float(self.news_score_updated_at.get(symbol, 0.0) or 0.0)
        # 10분에 한 번만 갱신
        if (now_ts - last_ts) < 600:
            return

        self.news_score_updated_at[symbol] = now_ts

        async def _worker() -> None:
            try:
                res = await self.state_machine.news_analyzer.get_sentiment_score(symbol)
                score = int(res.get("score", 0) or 0)
                self.news_score_by_symbol[symbol] = score
                self.logger.info(f"[News Cache] {symbol} score={score}")
            except Exception as e:
                # 실패해도 기존 캐시 유지
                self.logger.debug(f"[News Cache] update failed {symbol}: {e}")

        try:
            asyncio.create_task(_worker())
        except Exception:
            pass


def _acquire_singleton_lock(base_dir: str) -> IO[str]:
    """Acquire an exclusive singleton lock file for this bot process.

    Args:
        base_dir: Project base directory where lock file is stored.

    Raises:
        RuntimeError: If lock acquisition still fails after recovery attempts.

    Returns:
        IO[str]: Open lock-file handle that must stay alive while running.
    """
    lock_path = os.path.join(base_dir, ".kis_bot.lock")

    # [Self-Healing] 락 파일이 있는데 프로세스가 없으면 삭제 (좀비 락 정리)
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r") as f:
                pid = int(f.read().strip())
            # 해당 PID가 실제로 살아있는지 확인
            os.kill(pid, 0)  # 0번 시그널은 에러 체크용
        except (ValueError, ProcessLookupError, FileNotFoundError):
            # 프로세스가 없거나 파일이 깨졌으면 락 삭제
            try:
                os.remove(lock_path)
            except Exception:
                pass

    f = open(lock_path, "a+", encoding="utf-8")

    def try_lock() -> bool:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    if try_lock():
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        return f

    # 락이 잡혀 있으면 기존 PID 종료 시도
    f.seek(0)
    existing = (f.read() or "").strip()
    try:
        old_pid = int(existing)
    except Exception:
        old_pid = None

    if old_pid:
        try:
            os.kill(old_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass

        # 종료 대기 후 재시도
        deadline = time.time() + 8
        while time.time() < deadline:
            if try_lock():
                f.seek(0)
                f.truncate()
                f.write(str(os.getpid()))
                f.flush()
                return f
            time.sleep(0.2)

        # 그래도 안 되면 강제 종료 후 최종 시도
        try:
            os.kill(old_pid, signal.SIGKILL)
        except Exception:
            pass
        time.sleep(0.5)

    # 마지막 재시도
    if not try_lock():
        raise RuntimeError(
            "이미 실행 중인 KIS 봇이 종료되지 않아 시작할 수 없습니다(락 점유)."
        )

    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


async def run_module_system(base_dir: str) -> None:
    """Initialize enabled modules and run their monitoring loops.

    Args:
        base_dir: Project base directory used for config and environment loading.

    Returns:
        None: Runs until interrupted, then shuts modules down gracefully.
    """
    from modules.base import ModuleContext
    from modules.kukjang import KukjangModule
    from modules.hwanjeon import HwanjeonModule
    from modules.mijang import MijangModule
    from modules import base
    from kis_rest_orders import AccountInfo
    from kis_rest_overseas import KISOverseasRestOrders
    from kis_scanner import KisScanner
    from us_scanner import USStockScanner

    config = load_config(base_dir)
    log_dir = config.get("logging.dir", "logs")
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(base_dir, log_dir)
    logger = setup_logger(log_dir, config.get("timezone", "Asia/Seoul"))

    app_key, app_secret, _hts_id = load_auth_from_env()
    if not app_key or not app_secret:
        logger.warning("KIS_APP_KEY / KIS_APP_SECRET is empty")
        return

    rest_base_url = config.get("rest.base_url")
    auth = KISAuth(rest_base_url, app_key, app_secret, logger)

    acct_no = str(config.get("account.account_no", ""))
    acct_prdt = str(config.get("account.account_product_code", ""))
    account = AccountInfo(account_no=acct_no, product_code=acct_prdt)

    await auth.fetch_token()

    modules_config = config.get("modules", {})
    modules: Dict[str, base.BaseTradingModule] = {}

    kukjang_cfg = modules_config.get("kukjang", {})
    if kukjang_cfg.get("enabled", True):
        logger.info("Loading Kukjang module...")
        rest_client = KISRestOrders(rest_base_url, auth, account, logger)

        scanner = KisScanner(auth, rest_base_url)

        tr = config.get("time_rules", {}) or {}
        time_rules = TimeRules(
            observe_start=parse_time(tr.get("observe_start", "08:59:00")),
            or_start=parse_time(tr.get("or_start", "09:00:00")),
            or_end=parse_time(tr.get("or_end", "09:05:00")),
            entry_start=parse_time(tr.get("entry_start", "09:05:05")),
            force_exit=parse_time(tr.get("force_exit", "15:15:00")),
        )

        ctx = ModuleContext(
            name="kukjang",
            enabled=True,
            supports_trading=True,
            symbols=kukjang_cfg.get("symbols", ["122630", "114800"]),
            config=kukjang_cfg,
        )

        kukjang = KukjangModule(
            ctx, logger, rest_client, auth, config.get("trading", {}), time_rules
        )
        kukjang.set_scanner(scanner)
        modules["kukjang"] = kukjang
        logger.info("Kukjang module loaded")

    hwanjeon_cfg = modules_config.get("hwanjeon", {})
    if hwanjeon_cfg.get("enabled", False):
        logger.info("Loading Hwanjeon module...")

        ctx = ModuleContext(
            name="hwanjeon",
            enabled=True,
            supports_trading=False,
            symbols=hwanjeon_cfg.get("currencies", ["USD"]),
            config=hwanjeon_cfg,
        )

        hwanjeon = HwanjeonModule(ctx, logger, auth, rest_base_url, hwanjeon_cfg)
        modules["hwanjeon"] = hwanjeon
        logger.info("Hwanjeon module loaded")

    mijang_cfg = modules_config.get("mijang", {})
    if mijang_cfg.get("enabled", False):
        logger.info("Loading Mijang module...")

        overseas_client = KISOverseasRestOrders(
            rest_base_url,
            auth,
            account,
            logger,
            exchange=mijang_cfg.get("exchange", "NASD"),
        )

        us_scanner = USStockScanner(auth, rest_base_url, logger)

        ctx = ModuleContext(
            name="mijang",
            enabled=True,
            supports_trading=True,
            symbols=mijang_cfg.get("symbols", ["AAPL", "TSLA"]),
            exchange=mijang_cfg.get("exchange", "NASD"),
            config=mijang_cfg,
        )

        mijang = MijangModule(ctx, logger, overseas_client, mijang_cfg)
        mijang.set_scanner(us_scanner)
        modules["mijang"] = mijang
        logger.info("Mijang module loaded")

    if not modules:
        logger.warning("No modules enabled. Exiting.")
        return

    for name, mod in modules.items():
        await mod.initialize()

    tasks = []
    for name, mod in modules.items():
        if hasattr(mod, "monitor_loop"):
            tasks.append(asyncio.create_task(mod.monitor_loop()))

    logger.info(f"Running {len(modules)} modules: {list(modules.keys())}")

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        for name, mod in modules.items():
            await mod.shutdown()


def _load_dotenv_like(path: str) -> None:
    """Load environment variables from a simple dotenv-style file.

    Args:
        path: Absolute or relative path to the dotenv-like file.

    Returns:
        None: Populates missing keys in ``os.environ`` when file exists.
    """
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        return


def main_original() -> None:
    """Run the legacy single-engine entrypoint with process locking.

    Returns:
        None: Blocks until shutdown or keyboard interruption.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    _load_dotenv_like(os.path.join(base_dir, ".env"))

    lock_f = _acquire_singleton_lock(base_dir)
    prev_term_handler = signal.getsignal(signal.SIGTERM)

    def _sigterm_handler(_signum: int, _frame: Optional[FrameType]) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm_handler)
    try:
        engine = TradingEngine(base_dir)
        asyncio.run(engine.start())
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, prev_term_handler)
        try:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            lock_f.close()
        except Exception:
            pass


if __name__ == "__main__":
    import sys

    from healthcheck import read_health_status

    if len(sys.argv) > 1 and sys.argv[1] in ("health", "--health"):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        _load_dotenv_like(os.path.join(base_dir, ".env"))
        status = read_health_status(base_dir)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        raise SystemExit(0 if status.get("ok") else 1)

    if len(sys.argv) > 1 and sys.argv[1] == "--modules":
        base_dir = os.path.dirname(os.path.abspath(__file__))
        _load_dotenv_like(os.path.join(base_dir, ".env"))
        lock_f = _acquire_singleton_lock(base_dir)
        prev_term_handler = signal.getsignal(signal.SIGTERM)

        def _sigterm_handler(_signum: int, _frame: Optional[FrameType]) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _sigterm_handler)
        try:
            asyncio.run(run_module_system(base_dir))
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, prev_term_handler)
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                lock_f.close()
            except Exception:
                pass
    else:
        main_original()
