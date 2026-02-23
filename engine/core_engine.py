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
from indicators import rsi, sma, bollinger_bands, envelope, ema, macd, atr
from kis_auth import KISAuth, load_auth_from_env
from kis_rest_orders import AccountInfo, KISRestOrders
from kis_ws_marketdata import KISWebSocket
from logger import setup_logger
from models import Bar1m, OrderBookTop, Position, TradeTick
from risk_manager import RiskManager
from strategy_state_machine import Signal, State, StrategyStateMachine
from utils_time import is_after, is_between, now_local

from kis_scanner import KisScanner
from fee_calculator import FeeCalculator, USFeeCalculator

from candle_analysis import Candle, CandleAnalyzer
from us_prev_day import fetch_us_prev_daily
from ml_score import calculate_ml_score

from core.event_store import EventStore
from core import events as ievents
from core.ledger import Ledger, ledger_enabled
from core.oms import OMS, oms_enabled
from core.reconcile import (
    reconcile_enabled,
    reconcile_open_orders,
    reconcile_positions,
    should_block_new_entries,
)
from core.entry_gates import EntryGateEvaluator
from core.order_executor import OrderExecutor
from core.position_manager import PositionManager
from core.session_rules import (
    build_time_rules,
    exit_phase,
)
from engine import io_adapters as io_adapters_impl
from engine import state as state_impl
from engine import strategy as strategy_impl


class TradingEngine:
    """Main orchestrator for realtime KIS trading workflows.

    This engine wires configuration, authentication, market-data streams,
    state transitions, entry/exit execution, and risk guardrails into one
    long-running service process.

    Notes:
    - This process owns long-lived aiohttp sessions (REST clients). Always close
      them on shutdown to avoid `Unclosed client session` warnings and resource leaks.
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

        acct_no_raw = str(self.config.get("account.account_no", ""))
        acct_prdt_raw = str(self.config.get("account.account_product_code", ""))

        # Allow env override to avoid storing sensitive account number in config.json
        env_acct_no = os.getenv("KIS_ACCOUNT_NO", "").strip()
        env_acct_prdt = os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "").strip()
        if (not acct_no_raw) or (acct_no_raw.upper() == "YOUR_ACCOUNT_NO"):
            if env_acct_no:
                acct_no_raw = env_acct_no
        if (not acct_prdt_raw) or (acct_prdt_raw.upper() in {"YOUR_ACCOUNT_PRODUCT_CODE", ""}):
            if env_acct_prdt:
                acct_prdt_raw = env_acct_prdt
        if not acct_no_raw or not acct_prdt_raw:
            raise RuntimeError(
                "config.json: account.account_no / account.account_product_code 누락"
            )

        # Normalize account number: KIS expects CANO to be digits (typically 8).
        acct_no = "".join(ch for ch in acct_no_raw if ch.isdigit())
        acct_prdt = "".join(ch for ch in acct_prdt_raw if ch.isdigit())

        # If user put something like 12345678-01 in account_no, split it best-effort.
        if len(acct_no) > 8 and not acct_prdt:
            acct_prdt = acct_no[8:10]
            acct_no = acct_no[:8]

        invalid_account = False
        if len(acct_no) != 8:
            invalid_account = True
            self.logger.error(f"account_no(CANO) length unexpected: raw={acct_no_raw!r} norm={acct_no!r}")
        if len(acct_prdt) != 2:
            invalid_account = True
            self.logger.error(f"account_product_code length unexpected: raw={acct_prdt_raw!r} norm={acct_prdt!r}")

        if invalid_account:
            # Safety: when account identifiers are invalid, force kill switch ON to prevent live orders.
            os.environ["KIS_KILL_SWITCH"] = "1"
            try:
                flag = os.path.join(base_dir, "STOP_TRADING.flag")
                with open(flag, "w", encoding="utf-8") as fh:
                    fh.write("AUTO: invalid account config (CANO/ACNT_PRDT_CD). Set KIS_ACCOUNT_NO/KIS_ACCOUNT_PRODUCT_CODE or fix config.json\n")
            except Exception:
                pass
            self.logger.critical("Invalid account identifiers -> kill switch forced ON (no live orders will be placed)")

        account = AccountInfo(account_no=acct_no, product_code=acct_prdt)
        self.rest = KISRestOrders(rest_base_url, self.auth, account, self.logger)

        # 4) 시간 규칙
        self.time_rules, self.early_exit = build_time_rules(
            self.config.get("time_rules", {}),
            self.logger,
            log_prefix="[TradingEngine]",
        )

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
        self.max_concurrent_positions = max(
            1, int(tcfg.get("max_concurrent_positions", 20))
        )
        self.max_trades_per_symbol = max(
            1, int(tcfg.get("max_trades_per_symbol", 3))
        )
        self.symbol_cooldown_sec = int(float(tcfg.get("symbol_cooldown_sec", 900) or 900))
        self.symbol_cooldown_sec = max(0, min(24 * 3600, self.symbol_cooldown_sec))
        self.atr_min_percent = float(tcfg.get("atr_min_percent", 0.35) or 0.35)
        self.atr_multiplier_sl = float(tcfg.get("atr_multiplier", 2.0))
        self.min_hold_sec = int(float(tcfg.get("min_hold_sec", os.environ.get("KIS_MIN_HOLD_SEC", "120"))))

        # Per-symbol cooldown memory (entry attempts)
        self._symbol_cooldown_until: dict[str, float] = {}

        # Portfolio safety (recommended defaults)
        self.cash_reserve_pct = float(tcfg.get("cash_reserve_pct", 0.20))
        self.cash_reserve_pct = max(0.0, min(0.80, self.cash_reserve_pct))
        self.max_new_entries_per_minute = int(tcfg.get("max_new_entries_per_minute", 2))
        self.max_new_entries_per_minute = max(1, min(60, self.max_new_entries_per_minute))

        max_entries = int(tcfg.get("max_entries_per_day", 10))
        daily_loss_limit = float(tcfg.get("daily_loss_limit_pct", -0.05))
        max_consecutive_stop = int(tcfg.get("max_consecutive_stop", 3))
        self.risk = RiskManager(max_entries, daily_loss_limit, max_consecutive_stop)

        # [NEW] Fee Calculator for real-time profit calculation
        fees_cfg = tcfg.get("fees", {})
        self.fee_calculator = FeeCalculator(
            commission_rate=float(fees_cfg.get("commission_rate", 0.00015)),
            commission_min=float(fees_cfg.get("commission_min", 0)),
            slippage_rate=float(fees_cfg.get("slippage_rate", 0.001)),
            tax_rate=float(fees_cfg.get("tax_rate", 0.002)),
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

        # Base universe (KR symbols). Filter out any non-6-digit codes (e.g., '02850K').
        raw_universe = [
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
        ]

        def _is_kr_symbol(sym: str) -> bool:
            s = str(sym or "")
            return s.isdigit() and len(s) == 6

        self.base_universe = [s for s in raw_universe if _is_kr_symbol(s)]
        dropped = [s for s in raw_universe if s not in self.base_universe]
        if dropped:
            self.logger.warning(f"Dropped invalid symbols from base_universe: {dropped}")

        self.target_symbols = list(dict.fromkeys(self.base_universe))

        # Prevent websocket subscribe overflow (OPSP0008) by limiting symbols.
        # Tunable via env for temporary ops.
        try:
            max_ws = int(os.environ.get("KIS_WS_MAX_SYMBOLS", "0") or 0)
        except Exception:
            max_ws = 0
        if max_ws and max_ws > 0:
            self.target_symbols = self.target_symbols[:max_ws]
            self.logger.warning(f"WS symbol cap applied: {max_ws} -> subscribing {len(self.target_symbols)} symbols")

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
            lambda: {
                "closes": deque(maxlen=600),
                "volumes": deque(maxlen=600),
                "highs": deque(maxlen=600),
                "lows": deque(maxlen=600),
            }
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

        # [PHASE4] Optional fill-based Ledger (default OFF; non-invasive)
        self.ledger: Ledger | None = None
        if ledger_enabled():
            try:
                starting_cash = float(
                    os.environ.get("KIS_LEDGER_STARTING_CASH", "0") or 0.0
                )
            except Exception:
                starting_cash = 0.0
            self.ledger = Ledger(starting_cash=starting_cash)

        # [PHASE5] Optional reconcile loop (default OFF)
        self.reconcile_on: bool = bool(reconcile_enabled())
        self.reconcile_interval_sec = max(
            0, int(os.environ.get("KIS_RECONCILE_INTERVAL_SEC", "0") or 0)
        )
        self._reconcile_last: dict[str, object] = {"ts": None, "issue_count": 0}

        # Best-effort error counters for health/status (never used for control-flow)
        self._error_counts: dict[str, int] = defaultdict(int)

        # 뉴스 점수 캐시(비동기 갱신)
        self.news_score_by_symbol: Dict[str, int] = {}
        self.news_score_updated_at: Dict[str, float] = {}
        self.news_status_by_symbol: Dict[str, Dict[str, Any]] = {}
        self.slippage_stats_by_symbol: Dict[str, Any] = {}
        self.slippage_block_until: Dict[str, float] = {}

        # 11) Production safety defaults
        self.live_enabled = os.environ.get("KIS_LIVE_ENABLED", "0") == "1"
        self.live_confirmed = os.environ.get("KIS_LIVE_CONFIRM", "") == "YES"
        self.max_position_qty = max(
            1, int(os.environ.get("KIS_MAX_POSITION_QTY", "200"))
        )
        # Max trades per day: allow per-market split.
        # Defaults:
        # - KIS_MAX_TRADES_PER_DAY (global fallback)
        # - KIS_MAX_TRADES_PER_DAY_KR / _US (preferred)
        self.max_trades_per_day = max(1, int(os.environ.get("KIS_MAX_TRADES_PER_DAY", "5")))
        self.max_trades_per_day_kr = max(
            1, int(os.environ.get("KIS_MAX_TRADES_PER_DAY_KR", str(self.max_trades_per_day)))
        )
        self.max_trades_per_day_us = max(
            1, int(os.environ.get("KIS_MAX_TRADES_PER_DAY_US", str(self.max_trades_per_day)))
        )
        self.healthcheck_interval_sec = max(
            10, int(os.environ.get("KIS_HEALTHCHECK_INTERVAL_SEC", "60"))
        )
        self.position_snapshot_interval_sec = max(
            0, int(os.environ.get("KIS_POSITION_SNAPSHOT_INTERVAL_SEC", "0") or 0)
        )
        self.status_path = os.path.join(self.base_dir, "logs", "health_status.json")
        self.trades_today = 0  # backward-compat total
        self.trades_today_kr = 0
        self.trades_today_us = 0
        self.trades_today_by_symbol: Dict[str, int] = defaultdict(int)
        self._trade_counter_ymd = now_local(self.tz).strftime("%Y%m%d")

        # Concurrency guards (idempotency-lite)
        self._trade_lock = asyncio.Lock()
        self._entry_inflight: set[str] = set()
        self._exit_inflight: set[str] = set()
        # Entry pacing guardrail: store recent entry timestamps (epoch seconds)
        self._entry_ts = deque(maxlen=5000)
        self._last_signal_context_by_symbol: Dict[str, Dict[str, Any]] = {}

        # Extracted orchestration modules (refactor-only; behavior stays in delegated logic).
        self.position_manager = PositionManager(self)
        self.entry_gates = EntryGateEvaluator(self)
        self.order_executor = OrderExecutor(self)

        # Daily baseline capture marker (for daily return reporting)
        self._baseline_written_ymd = ""

        self.logger.info(
            "Execution mode: live_enabled=%s confirmed=%s active_live=%s max_qty=%s max_trades/day=%s max_positions=%s",
            self.live_enabled,
            self.live_confirmed,
            self.live_ordering_enabled(),
            self.max_position_qty,
            self.max_trades_per_day,
            self.max_concurrent_positions,
        )

    def _active_positions(self) -> dict[str, Position]:
        """Return a copy of currently tracked open positions."""
        return self.position_manager.active_positions()

    def _write_fill_alert(self, data: dict) -> None:
        """Append a fill alert to the queue for external notification (OpenClaw)."""
        import json
        from datetime import datetime
        
        try:
            alert_path = os.path.join(self.base_dir, "logs", "fill_alerts.jsonl")
            entry = {
                "ts": datetime.now().isoformat(),
                **data
            }
            with open(alert_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            self.logger.warning(f"Failed to write fill alert: {e}")

    async def aclose(self) -> None:
        """Close internally-owned resources (best-effort).

        This is safe to call multiple times.
        """
        try:
            if hasattr(self, "rest") and self.rest is not None:
                await self.rest.aclose()
        except Exception:
            pass

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
            self.logger.warning(
                f"Preflight auth failed (will retry in normal loop): {e}"
            )

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
                if "Refusing to use dummy approval_key" in msg:
                    # Outside market hours, WS approval key may be unnecessary.
                    # Do not crash the engine; continue in REST-only mode.
                    self.logger.warning(
                        f"ApprovalKey unavailable outside market window (REST-only): {e}"
                    )
                    break
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
        if self.position_snapshot_interval_sec > 0:
            tasks.append(asyncio.create_task(self.position_snapshot_loop()))
        if self.reconcile_on and self.reconcile_interval_sec > 0:
            tasks.append(asyncio.create_task(self.reconcile_loop()))

        await asyncio.gather(*tasks)

    async def restore_position(self) -> None:
        """Restore an existing broker position into the strategy state.

        Returns:
            None: Updates state-machine position cache in place.
        """
        await self.position_manager.restore_positions()

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
        self.bar_history[symbol]["highs"].append(bar.high)
        self.bar_history[symbol]["lows"].append(bar.low)

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
            highs = list(self.bar_history[symbol]["highs"])
            lows = list(self.bar_history[symbol]["lows"])

            if len(closes) >= 60:
                ma60 = sma(closes, 60)
                ma20 = sma(closes, 20) if len(closes) >= 20 else ma60

                price = bar.close
                above_ma20 = price > ma20
                above_ma60 = price > ma60
                ma20_above_ma60 = ma20 > ma60

                rsi_val = 50
                if len(closes) >= 15:
                    rsi_val = rsi(closes, 14)

                rsi_bullish = rsi_val > 55
                rsi_bearish = rsi_val < 45

                if above_ma20 and above_ma60 and ma20_above_ma60 and rsi_bullish:
                    self.market_regime = "BULL"
                elif (not above_ma20 or not above_ma60) and rsi_bearish:
                    self.market_regime = "BEAR"
                else:
                    self.market_regime = "NEUTRAL"

                self.logger.info(
                    f"[Market Regime] {self.market_regime} (Price={price:.0f} MA20={ma20:.0f} MA60={ma60:.0f} RSI={rsi_val:.1f})"
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

        if self.state_machine.in_position(symbol):
            self.logger.debug("[Entry] skipped: already in position symbol=%s", symbol)
            return

        if self.state_machine.active_position_count() >= self.max_concurrent_positions:
            self.logger.debug(
                "[Entry] skipped: max_concurrent_positions reached (%s/%s)",
                self.state_machine.active_position_count(),
                self.max_concurrent_positions,
            )
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
        highs = list(self.bar_history[symbol]["highs"])
        lows = list(self.bar_history[symbol]["lows"])

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

            if len(highs) >= 15 and len(lows) >= 15 and len(closes) >= 15:
                atr_value = atr(highs, lows, closes, 14)
                indicators["atr"] = atr_value
                if last_price > 0:
                    indicators["atr_percent"] = (atr_value / last_price) * 100.0

            indicators["ml_score"] = calculate_ml_score(indicators, last_price, vwap)

        except Exception as e:
            self.logger.error(f"Indicator calc failed for {symbol}: {e}")
            return

        try:
            # Optional debug of skip reasons (today-only troubleshooting)
            debug_skips = str(os.environ.get("KIS_DEBUG_ENTRY_SKIPS", "0")).strip() in {"1","true","TRUE","yes","YES"}

            ml_score = indicators.get("ml_score", 50)
            if ml_score < 40:
                if str(os.environ.get("KIS_DEBUG_ENTRY_SKIPS", "0")).strip() in {"1","true","TRUE","yes","YES"}:
                    self.logger.info(f"[SKIP] {symbol} ML score too low: {ml_score}")
                else:
                    self.logger.debug(f"[ML Filter] {symbol} ML score too low: {ml_score}")
                return

            # --- Dynamic entry threshold (recommended defaults) ---
            # Base from config: trading.scoring.kr_scalp_entry_threshold (fallback=50)
            base_thr = 50.0
            try:
                sc = (self.config.get("trading", {}) or {}).get("scoring", {}) or {}
                base_thr = float(sc.get("kr_scalp_entry_threshold", 50) or 50)
            except Exception:
                base_thr = 50.0

            spread_pct = 0.0
            try:
                if book is not None and float(book.ask) > 0 and float(book.bid) > 0:
                    spread_pct = (float(book.ask) - float(book.bid)) / float(book.ask)
            except Exception:
                spread_pct = 0.0
            atr_pct = float(indicators.get("atr_percent", 0) or 0)

            # Filter: avoid low-ATR names where fees dominate (reduces buy-high/sell-low churn).
            try:
                if float(atr_pct) > 0 and float(atr_pct) < float(self.atr_min_percent):
                    if str(os.environ.get("KIS_DEBUG_ENTRY_SKIPS", "0")).strip() in {"1","true","TRUE","yes","YES"}:
                        self.logger.info(
                            "[SKIP] %s ATR too low: atr_pct=%.3f < min=%.3f",
                            symbol,
                            float(atr_pct),
                            float(self.atr_min_percent),
                        )
                    else:
                        self.logger.debug(
                            "[ATR Filter] %s atr_pct=%.3f < min=%.3f -> skip",
                            symbol,
                            float(atr_pct),
                            float(self.atr_min_percent),
                        )
                    return
            except Exception:
                pass

            # Adjustments (tunable via env for today-only ops):
            # - high vol / wide spread -> stricter
            # - BULL -> slightly looser
            # - BEAR -> stricter
            def _fenv(name: str, default: float) -> float:
                try:
                    return float(os.environ.get(name, str(default)) or default)
                except Exception:
                    return default

            ATR_ADJ_3 = _fenv("KIS_ADJ_ATR_GE_3", 5.0)
            ATR_ADJ_5 = _fenv("KIS_ADJ_ATR_GE_5", 5.0)
            SPREAD_ADJ = _fenv("KIS_ADJ_SPREAD_GE_0P3", 5.0)
            BULL_ADJ = _fenv("KIS_ADJ_BULL", -3.0)
            BEAR_ADJ = _fenv("KIS_ADJ_BEAR", 10.0)

            adj = 0.0
            if atr_pct >= 3.0:
                adj += ATR_ADJ_3
            if atr_pct >= 5.0:
                adj += ATR_ADJ_5
            if spread_pct >= 0.003:
                adj += SPREAD_ADJ
            if self.market_regime == "BULL":
                adj += BULL_ADJ
            if self.market_regime == "BEAR":
                adj += BEAR_ADJ

            min_score = max(45.0, min(85.0, base_thr + adj))

            if debug_skips and (book is None or vwap is None or last_price <= 0):
                self.logger.info(
                    f"[SKIP] {symbol} missing inputs book={book is not None} vwap={vwap is not None} last_price={last_price}"
                )

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
                min_score=min_score,
            )
            if signal.side:
                signal_symbol = str(signal.symbol or symbol)
                signal_side = str(signal.side or "BUY")
                signal_corr_id = self._make_correlation_id(signal_symbol, signal_side)
                signal_idempotency = self._make_idempotency_key(
                    signal_symbol, signal_side, bar_start=bar.start
                )
                self.logger.info(
                    f"signal {signal.symbol} close={bar.close} vwap={vwap} rsi={indicators.get('rsi', 0):.1f} ma20={indicators.get('ma20', 0):.0f}"
                )
                signal_context = {
                    "module": "engine_orb_vwap",
                    "idempotency_key": signal_idempotency,
                    "correlation_id": signal_corr_id,
                    "close": float(bar.close),
                    "vwap": float(vwap) if vwap is not None else None,
                    "spread_pct": (
                        ((float(book.ask) - float(book.bid)) / float(book.ask))
                        if (book is not None and float(book.ask) > 0 and float(book.bid) > 0)
                        else None
                    ),
                    "rsi": float(indicators.get("rsi", 0) or 0),
                    "ma20": float(indicators.get("ma20", 0) or 0),
                    "ml_score": float(indicators.get("ml_score", 50) or 50),
                    "score": float(getattr(signal, "score", 0.0) or 0.0),
                    "min_score": float(min_score),
                    "reasons": list(getattr(signal, "reasons", []) or []),
                    "reason_short": ",".join(list(getattr(signal, "reasons", []) or [])[:6]),
                    "atr": float(indicators.get("atr", 0) or 0),
                    "atr_percent": float(indicators.get("atr_percent", 0) or 0),
                    "market_regime": str(self.market_regime or ""),
                }
                self._last_signal_context_by_symbol[signal_symbol] = dict(signal_context)
                try:
                    self.event_store.append(
                        ievents.Signal(
                            symbol=str(signal.symbol),
                            side=str(signal.side),
                            strength=float(indicators.get("ml_score", 50) or 50)
                            / 100.0,
                            reason="state_machine",
                            model="ml_score_heuristic",
                            correlation_id=signal_corr_id,
                            context=signal_context,
                        ).to_event(run_id=self.run_id)
                    )
                except Exception:
                    pass

                asyncio.create_task(
                    self.handle_entry(signal, book, bar_start=bar.start)
                )
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

    def _record_position_snapshot(
        self, symbol: str, *, trigger: str, note: str = "", correlation_id: str = ""
    ) -> None:
        self.position_manager.record_position_snapshot(
            symbol, trigger=trigger, note=note, correlation_id=correlation_id
        )

    def _build_risk_context(
        self,
        *,
        signal: Signal,
        book: OrderBookTop,
        cash: float | None = None,
        equity_est: float | None = None,
        exposure: float | None = None,
        remaining_cap: float | None = None,
        sym_remaining: float | None = None,
        reserve_amt: float | None = None,
        budget_pct: float | None = None,
        budget: float | None = None,
        max_total_position_pct: float | None = None,
        max_symbol_position_pct: float | None = None,
    ) -> dict[str, Any]:
        return self.entry_gates.build_risk_context(
            signal=signal,
            book=book,
            cash=cash,
            equity_est=equity_est,
            exposure=exposure,
            remaining_cap=remaining_cap,
            sym_remaining=sym_remaining,
            reserve_amt=reserve_amt,
            budget_pct=budget_pct,
            budget=budget,
            max_total_position_pct=max_total_position_pct,
            max_symbol_position_pct=max_symbol_position_pct,
        )

    def _make_idempotency_key(
        self, symbol: str, side: str, *, bar_start: datetime | None = None
    ) -> str:
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

    def _make_correlation_id(self, symbol: str, side: str) -> str:
        # Use unified correlation id generator across engine/modules/scripts.
        from core.correlation import new_corr

        s = (side or "").strip().upper() or "X"
        return new_corr(f"eng_{s.lower()}")

    async def handle_entry(
        self, signal: Signal, book: OrderBookTop, *, bar_start: datetime | None = None
    ) -> None:
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
        side = str(signal.side or "BUY")

        if self.kill_switch_on():
            self.logger.info("[Entry] blocked by kill switch symbol=%s", symbol)
            return
        if self.state_machine.in_position(symbol):
            self.logger.info("[Entry] skipped; symbol already held symbol=%s", symbol)
            return
        if self.state_machine.active_position_count() >= self.max_concurrent_positions:
            self.logger.info(
                "[Entry] blocked by max_concurrent_positions (%s/%s)",
                self.state_machine.active_position_count(),
                self.max_concurrent_positions,
            )
            return

        idempotency_key = self._make_idempotency_key(symbol, side, bar_start=bar_start)
        correlation_id = self._make_correlation_id(symbol, side)

        async with self._trade_lock:
            if symbol in self._entry_inflight:
                self.logger.debug(f"[Entry] skipped: inflight symbol={symbol}")
                return
            self._entry_inflight.add(symbol)

        try:
            gate = await self.entry_gates.evaluate(
                signal, book, idempotency_key=idempotency_key, correlation_id=correlation_id
            )
            if not gate.allowed or gate.qty <= 0:
                return

            qty = int(gate.qty)
            is_kr = bool(str(symbol).isdigit())
            try:
                if self.symbol_cooldown_sec > 0:
                    self._symbol_cooldown_until[str(symbol)] = time.time() + float(
                        self.symbol_cooldown_sec
                    )

                self.event_store.append(
                    ievents.OrderIntent(
                        symbol=symbol,
                        side="BUY",
                        qty=int(qty),
                        order_type="LMT",
                        limit_price=float(book.ask),
                        idempotency_key=idempotency_key,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                    ).to_event(run_id=self.run_id)
                )
                self.event_store.append(
                    ievents.RiskDecision(
                        symbol=symbol,
                        allowed=True,
                        reason="ok",
                        idempotency_key=idempotency_key,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                    ).to_event(run_id=self.run_id)
                )
            except Exception:
                pass

            if self.oms is not None:
                try:
                    self.oms.register_intent(
                        symbol=symbol,
                        side="BUY",
                        qty=int(qty),
                        idempotency_key=idempotency_key,
                    )
                    self.oms.mark_risk(idempotency_key=idempotency_key, allowed=True)
                except Exception:
                    self._error_counts["oms_entry"] += 1

            self.risk.record_entry()
            try:
                self._entry_ts.append(time.time())
            except Exception:
                pass
            self.trades_today += 1
            self.trades_today_by_symbol[symbol] += 1
            if is_kr:
                self.trades_today_kr += 1
            else:
                self.trades_today_us += 1

            await self.order_executor.execute_entry(
                symbol=symbol,
                qty=qty,
                ask=float(book.ask),
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
        finally:
            self._entry_inflight.discard(symbol)

    async def handle_exit(
        self, reason: str, *, symbol: str | None = None, use_market: bool = True
    ) -> None:
        """Attempt to close the current position using configured exit logic.

        Args:
            reason: Human-readable reason for the exit attempt.
            symbol: Optional symbol to exit. ``None`` means exit all open symbols.
            use_market: Exit preference flag kept for interface compatibility.

        Returns:
            None: Updates position/risk state after execution attempts.
        """
        if symbol is None:
            symbols = list(self._active_positions().keys())
            for sym in symbols:
                await self.handle_exit(reason, symbol=sym, use_market=use_market)
            return
        sym = str(symbol)
        pos = self.state_machine.get_position(sym)
        if not pos:
            return
        if sym in self._exit_inflight:
            return
        self._exit_inflight.add(sym)

        idempotency_key = self._make_idempotency_key(sym, "SELL")
        correlation_id = self._make_correlation_id(sym, "SELL")
        try:
            await self.order_executor.execute_exit(
                reason=reason,
                symbol=sym,
                pos=pos,
                use_market=use_market,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
        finally:
            if self.state_machine.state != State.DONE_TODAY:
                self.state_machine.set_state(State.WAIT_SIGNAL)
            self._exit_inflight.discard(sym)

    async def monitor_time_loop(self) -> None:
        """Continuously update time-based state transitions.

        Returns:
            None: Runs as an infinite periodic background loop.
        """
        while True:
            self._reset_daily_counters_if_needed()
            self.update_state_by_time()
            # Best-effort daily baseline capture for return reporting
            await self._maybe_write_daily_baseline()
            await asyncio.sleep(1)

    def update_state_by_time(self) -> None:
        """Apply time-rule-driven transitions for trading session state.

        Returns:
            None: Mutates state machine according to current local time.
        """
        now_dt = now_local(self.tz)

        phase, final_kill_time = exit_phase(now_dt, self.early_exit, self.time_rules.force_exit)

        # 1) 강제 종료 시간 (15:15 ~ 15:18)
        # 15:15: 1차 청산 시도
        # 15:18: 2차 강제 청산 (남은 물량 시장가 투척)
        if phase == "emergency":
            if self.state_machine.active_position_count() > 0:
                # 15:18 넘으면 묻지도 따지지도 않고 시장가 청산
                self.logger.warning(
                    "EMERGENCY EXIT (%s): Dumping all positions by market order!",
                    final_kill_time,
                )
                # 비동기로 던져버림 (fire and forget 스타일)
                asyncio.create_task(
                    self.handle_exit(
                        "emergency_market_close", symbol=None, use_market=True
                    )
                )

            if self.state_machine.state != State.DONE_TODAY:
                self.state_machine.set_state(State.DONE_TODAY)
            return

        # 0-1) 조기 청산 (15:00): 수익 여부와 관계없이 무조건 청산
        if phase == "early":
            if self.state_machine.active_position_count() > 0:
                self.logger.warning(
                    " EARLY EXIT (15:00): Closing all positions! count=%s",
                    self.state_machine.active_position_count(),
                )
                asyncio.create_task(self.handle_exit("early_exit_15_00", symbol=None))

            if self.state_machine.state != State.DONE_TODAY:
                self.state_machine.set_state(State.DONE_TODAY)
            return

        if phase == "force":
            if self.state_machine.active_position_count() > 0:
                # 15:15 ~ 15:18: 일반적인 강제 청산 시도
                asyncio.create_task(self.handle_exit("force_exit", symbol=None))

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

        if self.state_machine.active_position_count() > 0:
            phase, _ = exit_phase(now_dt, self.early_exit, self.time_rules.force_exit)
            if phase == "emergency":
                await self.handle_exit("emergency_market_close", symbol=None, use_market=True)
                return
            # 조기 청산 (15:00): 수익 여부와 관계없이 무조건 청산
            if phase == "early":
                self.logger.warning(
                    " EARLY EXIT (15:00): Closing all positions! count=%s",
                    self.state_machine.active_position_count(),
                )
                await self.handle_exit("early_exit_15_00", symbol=None)
                return

            # 강제 청산 (15:15)
            if phase == "force":
                await self.handle_exit("force_exit", symbol=None)

    async def check_tp_sl(self) -> None:
        """Evaluate advanced take-profit and stop-loss exit conditions.

        Returns:
            None: May trigger partial or full position exits.
        """
        positions = list(self._active_positions().values())
        if not positions:
            return

        for pos in positions:
            await self._check_tp_sl_for_symbol(pos)

    async def _check_tp_sl_for_symbol(self, pos: Position) -> None:
        """Evaluate TP/SL rules for one symbol-specific open position."""
        if not self.state_machine.in_position(pos.symbol):
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

        # Guard: avoid immediate stop-out right after entry (microstructure noise).
        try:
            if pos.entry_time is not None:
                held = (now_local(self.tz) - pos.entry_time).total_seconds()
                if held < float(self.min_hold_sec):
                    return
        except Exception:
            pass

        gross_pnl_pct = (last_price - pos.avg_price) / pos.avg_price
        net_pnl_pct = self.fee_calculator.get_net_pnl_percent(pos.avg_price, last_price)

        # Use NET for reporting, but use GROSS for exit triggers.
        pnl_net = net_pnl_pct
        pnl_gross = gross_pnl_pct
        pnl_exit = pnl_gross

        pos.unrealized_pnl_pct = pnl_net
        pos.unrealized_gross_pnl_pct = pnl_gross

        self.logger.debug(
            f"[Fee Adjust] {pos.symbol} Gross={gross_pnl_pct:.4f} Net={net_pnl_pct:.4f}"
        )

        # --- 1% 수익 달성 로직 (핵심) ---

        # [MODIFIED] 동적 TP/SL (ATR 기반) 먼저 체크
        # 스마트 변동 익절: 시장 상황(ATR)에 맞춰 유연하게 익절/손절
        bar_hist = self.bar_history.get(pos.symbol)
        if bar_hist:
            highs = list(bar_hist.get("highs", []))
            lows = list(bar_hist.get("lows", []))
            closes_for_atr = list(bar_hist.get("closes", []))
            if len(highs) >= 15 and len(lows) >= 15 and len(closes_for_atr) >= 15:
                latest_atr = atr(highs, lows, closes_for_atr, 14)
                if latest_atr > 0 and pos.avg_price > 0:
                    # ATR 배수 설정 (config에서 가져오거나 기본값 사용)
                    # 현재 config.kr.json의 atr_multiplier는 2.0 (손절용)
                    # 익절은 보통 손절폭의 1.5~2배로 설정 (Risk:Reward 비율 고려)
                    
                    # 손절: ATR * 2.0 (기본값)
                    atr_multiplier_sl = float(self.atr_multiplier_sl)
                    
                    # 익절: ATR * 3.0 (변동성이 클 때는 더 크게 먹고, 작을 때는 작게 먹음)
                    # 혹은 손절폭 대비 1.5배 설정
                    atr_multiplier_tp = atr_multiplier_sl * 1.5 

                    dynamic_stop_loss_pct = (
                        -(latest_atr / pos.avg_price) * atr_multiplier_sl
                    )
                    dynamic_take_profit_pct = (
                        latest_atr / pos.avg_price
                    ) * atr_multiplier_tp
                    
                    if pnl_exit >= dynamic_take_profit_pct:
                        self.logger.info(f"🎯 SMART PROFIT (ATR): {pos.symbol} GrossPnL={pnl_exit:.2%} Target={dynamic_take_profit_pct:.2%} (Net={pnl_net:.2%})")
                        await self.handle_exit("take_profit (ATR)", symbol=pos.symbol)
                        return
                    elif pnl_exit <= dynamic_stop_loss_pct:
                        self.logger.info(f"🛡 SMART STOP (ATR): {pos.symbol} GrossPnL={pnl_exit:.2%} Limit={dynamic_stop_loss_pct:.2%} (Net={pnl_net:.2%})")
                        await self.handle_exit("stop_loss (ATR)", symbol=pos.symbol)
                        return

        # --- 고급 청산 로직 (Advanced Exit) ---
        # 권장 순서(안정적인 수익 분포):
        # 1) TP1(부분익절) -> 2) Profit Lock 보호 -> 3) (필요시) 전량 익절

        # 1. 부분 익절 (Scale-out): +1% 도달 시 절반 익절
        # - 포지션이 2주 이상일 때만 수행
        # - 수행 후 profit_locked=True로 전환해서 이익 보호 로직이 동작하도록 함
        if pnl_exit >= self.quick_profit_pct and not pos.tp1_done and pos.qty > 1:
            half_qty = int(pos.qty * 0.5)
            if half_qty > 0:
                if self.kill_switch_on():
                    self.logger.warning(
                        "TP1 skipped by kill switch symbol=%s qty=%s",
                        pos.symbol,
                        half_qty,
                    )
                    return
                self.logger.info(
                    f"💰 TP1 (Scale-out): {pos.symbol} GrossPnL={pnl_exit:.2%} Qty={half_qty} (Net={pnl_net:.2%})"
                )
                await self.rest.place_sell_market(pos.symbol, half_qty)
                pos.qty -= half_qty
                pos.tp1_done = True
                pos.profit_locked = True
                return

        # 2. Quick Profit (1%):
        # - 포지션이 1주뿐이거나(부분익절 불가), TP1 이후에도 계속 강하면 전량 익절
        if pnl_exit >= self.quick_profit_pct and not pos.profit_locked:
            self.logger.info(
                f" PROFIT TARGET🎯 QUICK REACHED: {pos.symbol} GrossPnL={pnl_exit:.2%} (Net={pnl_net:.2%})"
            )
            pos.profit_locked = True
            await self.handle_exit("quick_profit_1pct", symbol=pos.symbol)
            return

        # 3. Profit Lock: +1% 달성 후 수익이 다시 꺾이면 청산하여 이익 보호
        if pos.profit_locked and pnl_exit < self.min_profit_for_guarantee_pct:
            self.logger.info(
                f"🔒 PROFIT LOCKED - Protecting gains: {pos.symbol} GrossPnL={pnl_exit:.2%} (Net={pnl_net:.2%})"
            )
            await self.handle_exit("profit_lock_protection", symbol=pos.symbol)
            return

        # 2. 고점 수익률 갱신 (Trailing Stop용)
        if pos.symbol in self.peak_pnl_pct:
            self.peak_pnl_pct[pos.symbol] = max(self.peak_pnl_pct[pos.symbol], pnl_exit)
        else:
            self.peak_pnl_pct[pos.symbol] = pnl_exit

        peak_pnl = self.peak_pnl_pct[pos.symbol]

        # 3. 본전 스탑 (Breakeven): 수익이 +1.2% 이상 났다가 +0.3% 미만으로 떨어지면 즉시 청산
        if peak_pnl >= 0.012 and pnl_exit < 0.003:
            await self.handle_exit("breakeven_stop", symbol=pos.symbol)
            return

        # 4. 트레일링 스탑 (Trailing): 수익이 +3.0% 이상 났다가, 고점 대비 -1.0% 하락하면 익절
        if peak_pnl >= 0.030 and (peak_pnl - pnl_exit) >= 0.010:
            await self.handle_exit(
                f"trailing_stop (peak={peak_pnl:.2%})", symbol=pos.symbol
            )
            return

        # 5. 동적 TP/SL (ATR 기반) 또는 고정 TP/SL
        # (위에서 이미 처리했으므로 여기서는 제거하거나 고정 TP/SL만 남김)
        if pnl_exit >= self.take_profit_pct:
            await self.handle_exit("take_profit", symbol=pos.symbol)
        elif pnl_exit <= self.stop_loss_pct:
            await self.handle_exit("stop_loss", symbol=pos.symbol)

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

        Notes:
        - Kill switch should always override live flags.

        Returns:
            bool: True when live mode + confirmation are enabled and kill switch is OFF.
        """
        return bool(self.live_enabled and self.live_confirmed and (not self.kill_switch_on()))

    def _reset_daily_counters_if_needed(self) -> None:
        """Reset per-day trade counters when date boundary changes.

        Returns:
            None: Updates cached trading day and counter values in place.
        """
        ymd = now_local(self.tz).strftime("%Y%m%d")
        if ymd != self._trade_counter_ymd:
            self._trade_counter_ymd = ymd
            self.trades_today = 0
            self.trades_today_kr = 0
            self.trades_today_us = 0
            self.trades_today_by_symbol.clear()
            # Reset baseline marker so we capture once per day.
            try:
                self._baseline_written_ymd = ""
            except Exception:
                pass

    async def _maybe_write_daily_baseline(self) -> None:
        """Write a best-effort daily baseline for '당일 수익률(추정)' reporting.

        This is used by `scripts/daily_trade_report.py` to compute an approximate
        day return percent: (realized + unrealized) / baseline_equity.

        Baseline is written once per trading day after observe_start.
        """
        try:
            now_dt = now_local(self.tz)
            ymd = now_dt.strftime("%Y%m%d")
            if not hasattr(self, "_baseline_written_ymd"):
                self._baseline_written_ymd = ""
            if self._baseline_written_ymd == ymd:
                return

            # Capture after observe_start to approximate day-start equity.
            if not is_after(self.time_rules.observe_start, now_dt):
                return

            # Use a stable KR symbol for cash query.
            cash = 0.0
            try:
                cash = float(await self.rest.get_cash_available("005930", 1.0))
            except Exception:
                cash = 0.0
            finally:
                # avoid leaking aiohttp sessions created during baseline capture
                try:
                    await self.rest.close()
                except Exception:
                    pass

            # Exposure from broker positions (KR only). Best-effort.
            exposure = 0.0
            try:
                pos_map = await self.rest.get_all_positions()
                for sym, info in (pos_map or {}).items():
                    try:
                        qty = int(float((info or {}).get("qty", 0) or 0))
                        avg = float((info or {}).get("avg_price", 0.0) or 0.0)
                        if qty > 0 and avg > 0:
                            exposure += float(avg) * int(qty)
                    except Exception:
                        continue
            except Exception:
                exposure = 0.0

            baseline_equity = float(cash) + float(exposure)
            out = {
                "ts": now_dt.isoformat(),
                "ymd": ymd,
                "cash": float(cash),
                "exposure": float(exposure),
                "equity": float(baseline_equity),
            }

            import json
            import os

            path = os.path.join(self.base_dir, "logs", f"daily_baseline_{ymd}.json")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=2)
            except Exception:
                return

            self._baseline_written_ymd = ymd
        except Exception:
            return

    def get_health_status(self) -> dict[str, object]:
        """Build a serializable snapshot of current engine health state.

        Returns:
            dict[str, object]: Health metadata for monitoring and diagnostics.
        """
        positions = list(self._active_positions().values())
        pos = positions[0] if positions else None
        positions_payload = [p.to_dict() for p in positions]
        # [PHASE1] health payload should include event store hints for ops/debug.
        events_dir = getattr(self.event_store, "base_dir", None)
        try:
            recent_counts = self.event_store.recent_event_counts(days=2)
        except Exception:
            recent_counts = {}

        try:
            recent_type_dist = self.event_store.recent_type_distribution(max_lines=5000)
        except Exception:
            recent_type_dist = {}

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
            "trades_today_kr": int(getattr(self, "trades_today_kr", 0) or 0),
            "trades_today_us": int(getattr(self, "trades_today_us", 0) or 0),
            "max_trades_per_day": int(self.max_trades_per_day),
            "max_trades_per_day_kr": int(getattr(self, "max_trades_per_day_kr", self.max_trades_per_day) or self.max_trades_per_day),
            "max_trades_per_day_us": int(getattr(self, "max_trades_per_day_us", self.max_trades_per_day) or self.max_trades_per_day),
            "max_position_qty": int(self.max_position_qty),
            "max_concurrent_positions": int(self.max_concurrent_positions),
            "active_positions_count": int(len(positions_payload)),
            "position": pos.to_dict() if pos else None,
            "positions": positions_payload,
            "trades_today_by_symbol": dict(self.trades_today_by_symbol),
            "symbols": list(self.target_symbols),
            "universe": universe_summary,
            "institutional": {
                "oms_enabled": bool(self.oms is not None),
                "ledger_enabled": bool(self.ledger is not None),
                "reconcile_enabled": bool(
                    os.environ.get("KIS_INSTITUTIONAL_RECONCILE", "0") == "1"
                ),
                "reconcile_interval_sec": int(
                    getattr(self, "reconcile_interval_sec", 0) or 0
                ),
                "reconcile_last": dict(getattr(self, "_reconcile_last", {}) or {}),
                "position_snapshot_interval_sec": int(
                    getattr(self, "position_snapshot_interval_sec", 0) or 0
                ),
            },
            "errors": {
                "event_store_error_count": int(
                    getattr(self.event_store, "error_count", 0) or 0
                ),
                "engine_error_counts": dict(self._error_counts),
            },
            "events": {
                "base_dir": events_dir,
                "recent_counts": recent_counts,
                "recent_type_distribution": recent_type_dist,
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
                "health state=%s ws=%s live=%s kill=%s trades(KR)=%s/%s trades(US)=%s/%s",
                status["state"],
                status["ws_connected"],
                status["live_ordering_active"],
                status["kill_switch"],
                status.get("trades_today_kr", 0),
                status.get("max_trades_per_day_kr", status.get("max_trades_per_day", 0)),
                status.get("trades_today_us", 0),
                status.get("max_trades_per_day_us", status.get("max_trades_per_day", 0)),
            )
            await asyncio.sleep(self.healthcheck_interval_sec)

    async def position_snapshot_loop(self) -> None:
        """Periodically emit PositionSnapshot events (best-effort).

        This is OFF by default; enable by setting:
            KIS_POSITION_SNAPSHOT_INTERVAL_SEC=60

        If Ledger is enabled, periodic snapshots will include derived cash.
        """
        interval = float(self.position_snapshot_interval_sec)
        while True:
            try:
                for pos in self._active_positions().values():
                    self._record_position_snapshot(str(pos.symbol), trigger="periodic")
            except Exception:
                try:
                    self._error_counts["position_snapshot_loop"] += 1
                except Exception:
                    pass
            await asyncio.sleep(interval)

    async def reconcile_loop(self) -> None:
        """Best-effort reconcile loop (Phase5 skeleton).

        Enabled when:
            KIS_INSTITUTIONAL_RECONCILE=1
            KIS_RECONCILE_INTERVAL_SEC>0

        Compares internal OMS view vs broker open-orders query (if available).
        Also checks position mismatch and sets kill-switch for critical issues.
        """
        interval = float(self.reconcile_interval_sec)
        while True:
            issues = []
            try:
                broker_orders = await self.rest.get_open_orders()
                internal: dict[str, dict[str, Any]] = {}
                if self.oms is not None:
                    # Access is best-effort; OMS is an in-memory helper.
                    for k, rec in (
                        getattr(self.oms, "_orders_by_key", {}) or {}
                    ).items():
                        internal[str(k)] = {
                            "symbol": getattr(rec, "symbol", None),
                            "side": getattr(rec, "side", None),
                            "qty": getattr(rec, "qty", None),
                            "state": str(getattr(rec, "state", "")),
                            "broker_order_id": getattr(rec, "broker_order_id", None),
                            "filled_qty": getattr(rec, "filled_qty", None),
                        }

                # Check open orders
                order_issues = reconcile_open_orders(
                    internal_orders=internal, broker_orders=broker_orders
                )
                issues.extend(order_issues)

                # Check positions if ledger is enabled
                if self.ledger is not None:
                    broker_positions = await self.rest.get_all_positions()
                    internal_positions: dict[str, dict[str, Any]] = {}
                    for sym, pos in self.ledger.positions.items():
                        internal_positions[sym] = {
                            "qty": pos.qty,
                            "avg_price": pos.avg_price,
                        }
                    position_issues = reconcile_positions(
                        internal=internal_positions, broker=broker_positions
                    )
                    issues.extend(position_issues)

                # Classify and take action based on severity
                if should_block_new_entries(issues):
                    # Create kill-switch flag file for critical issues
                    flag_path = os.path.join(self.base_dir, "STOP_TRADING.flag")
                    try:
                        with open(flag_path, "w") as f:
                            f.write(
                                f"reconcile_critical:{now_local(self.tz).isoformat()}\n"
                            )
                    except Exception:
                        pass
                    self.logger.error(
                        "CRITICAL reconcile issues detected: blocking new entries. issues=%s",
                        len(issues),
                    )
                    self._reconcile_last = {
                        "ts": now_local(self.tz).isoformat(),
                        "issue_count": len(issues),
                        "blocked": True,
                        "severity": "critical",
                    }
                else:
                    self._reconcile_last = {
                        "ts": now_local(self.tz).isoformat(),
                        "issue_count": len(issues),
                        "blocked": False,
                    }
                    if issues:
                        self.logger.warning(
                            "reconcile issues=%s sample=%s", len(issues), issues[0].kind
                        )
            except Exception as e:
                self._error_counts["reconcile_loop"] += 1
                self._reconcile_last = {
                    "ts": now_local(self.tz).isoformat(),
                    "issue_count": -1,
                    "error": str(e),
                }

            await asyncio.sleep(interval)

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


# Bind extracted responsibilities to keep TradingEngine modularized without behavior drift.
TradingEngine.aclose = io_adapters_impl.aclose
TradingEngine.on_ws_status = io_adapters_impl.on_ws_status
TradingEngine.kill_switch_on = io_adapters_impl.kill_switch_on
TradingEngine.live_ordering_enabled = io_adapters_impl.live_ordering_enabled

TradingEngine._active_positions = state_impl._active_positions
TradingEngine._append_event = state_impl._append_event
TradingEngine._record_position_snapshot = state_impl._record_position_snapshot
TradingEngine._build_risk_context = state_impl._build_risk_context
TradingEngine._make_idempotency_key = state_impl._make_idempotency_key
TradingEngine._make_correlation_id = state_impl._make_correlation_id
TradingEngine.handle_entry = state_impl.handle_entry
TradingEngine.handle_exit = state_impl.handle_exit
TradingEngine.update_state_by_time = state_impl.update_state_by_time
TradingEngine.check_kill_switch = state_impl.check_kill_switch
TradingEngine.check_force_exit = state_impl.check_force_exit

TradingEngine.on_tick = strategy_impl.on_tick
TradingEngine.on_book = strategy_impl.on_book
TradingEngine.on_bar_close = strategy_impl.on_bar_close
TradingEngine.check_tp_sl = strategy_impl.check_tp_sl
TradingEngine._check_tp_sl_for_symbol = strategy_impl._check_tp_sl_for_symbol
TradingEngine._load_prev_closes_if_needed = strategy_impl._load_prev_closes_if_needed
TradingEngine._bootstrap_us_prev_day_candles = strategy_impl._bootstrap_us_prev_day_candles
TradingEngine._maybe_refresh_news_score = strategy_impl._maybe_refresh_news_score


def _acquire_singleton_lock(base_dir: str) -> IO[str]:
    """Acquire an exclusive singleton lock file for this bot process.

    Args:
        base_dir: Project base directory where lock file is stored.

    Raises:
        RuntimeError: If lock acquisition still fails after recovery attempts.

    Returns:
        IO[str]: Open lock-file handle that must stay alive while running.
    """
    lock_name = os.environ.get("KIS_LOCK_FILE", ".kis_bot.lock").strip() or ".kis_bot.lock"
    lock_path = os.path.join(base_dir, lock_name)

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
    from modules.kr_swing import KRSwingModule
    from modules.us_swing import USSwingModule
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

    acct_no_raw = str(config.get("account.account_no", ""))
    acct_prdt_raw = str(config.get("account.account_product_code", ""))

    env_acct_no = os.getenv("KIS_ACCOUNT_NO", "").strip()
    env_acct_prdt = os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "").strip()
    if (not acct_no_raw) or (str(acct_no_raw).upper() == "YOUR_ACCOUNT_NO"):
        if env_acct_no:
            acct_no_raw = env_acct_no
    if (not acct_prdt_raw) or (str(acct_prdt_raw).upper() in {"YOUR_ACCOUNT_PRODUCT_CODE", ""}):
        if env_acct_prdt:
            acct_prdt_raw = env_acct_prdt
    acct_no = "".join(ch for ch in acct_no_raw if ch.isdigit())
    acct_prdt = "".join(ch for ch in acct_prdt_raw if ch.isdigit())
    if len(acct_no) > 8 and not acct_prdt:
        acct_prdt = acct_no[8:10]
        acct_no = acct_no[:8]
    account = AccountInfo(account_no=acct_no, product_code=acct_prdt)

    await auth.fetch_token()

    modules_config = config.get("modules", {})
    modules: Dict[str, base.BaseTradingModule] = {}

    kukjang_cfg = modules_config.get("kukjang", {})
    if kukjang_cfg.get("enabled", True):
        logger.info("Loading Kukjang module...")
        rest_client = KISRestOrders(rest_base_url, auth, account, logger)

        scanner = KisScanner(auth, rest_base_url)

        time_rules, _early_exit = build_time_rules(
            config.get("time_rules", {}),
            logger,
            log_prefix="[ModuleLoader:kukjang]",
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

    kr_swing_cfg = modules_config.get("kr_swing", {})
    if kr_swing_cfg.get("enabled", False):
        logger.info("Loading KR Swing module...")

        rest_client = KISRestOrders(rest_base_url, auth, account, logger)
        scanner = KisScanner(auth, rest_base_url)

        ctx = ModuleContext(
            name="kr_swing",
            enabled=True,
            supports_trading=True,
            symbols=kr_swing_cfg.get("symbols", []),
            config=kr_swing_cfg,
        )

        kr_swing = KRSwingModule(ctx, logger, rest_client, config.get("trading", {}))
        kr_swing.set_scanner(scanner)
        modules["kr_swing"] = kr_swing
        logger.info("KR Swing module loaded")

    us_swing_cfg = modules_config.get("us_swing", {})
    if us_swing_cfg.get("enabled", False):
        logger.info("Loading US Swing module...")

        overseas_client = KISOverseasRestOrders(
            rest_base_url,
            auth,
            account,
            logger,
            exchange=us_swing_cfg.get("exchange", "NASD"),
        )

        ctx = ModuleContext(
            name="us_swing",
            enabled=True,
            supports_trading=True,
            symbols=us_swing_cfg.get("symbols", ["AAPL", "MSFT"]),
            exchange=us_swing_cfg.get("exchange", "NASD"),
            config=us_swing_cfg,
        )

        us_swing = USSwingModule(ctx, logger, overseas_client, config.get("trading", {}))
        us_swing_scanner = USStockScanner(auth, rest_base_url, logger)
        us_swing.set_scanner(us_swing_scanner)
        modules["us_swing"] = us_swing
        logger.info("US Swing module loaded")

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
    engine: Optional[TradingEngine] = None
    try:
        engine = TradingEngine(base_dir)
        asyncio.run(engine.start())
    except KeyboardInterrupt:
        pass
    finally:
        # Ensure aiohttp sessions are closed to avoid resource leaks.
        if engine is not None:
            try:
                asyncio.run(engine.aclose())
            except Exception:
                pass
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
