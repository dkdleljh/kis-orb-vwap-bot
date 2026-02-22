import asyncio
import json
import os
import signal
import time
from types import FrameType
from typing import Dict, IO, Optional

import fcntl

from config import load_config
from kis_auth import KISAuth, load_auth_from_env
from kis_rest_orders import AccountInfo, KISRestOrders
from logger import setup_logger
from core.reconcile import (
    reconcile_enabled,
    reconcile_open_orders,
    reconcile_positions,
    should_block_new_entries,
)
from utils_time import now_local
from engine.core_engine import TradingEngine
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
    from core.session_rules import build_time_rules
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

    # Stagger module initialization to avoid short request bursts to KIS OpenAPI
    # (EGW00201 = per-second rate limit), especially right after restart.
    for idx, (name, mod) in enumerate(modules.items()):
        await mod.initialize()
        if idx < len(modules) - 1:
            await asyncio.sleep(1.0)

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
