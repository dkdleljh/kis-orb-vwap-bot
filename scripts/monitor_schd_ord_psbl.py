"""Monitor SCHD orderable qty (ord_psbl_qty) during US regular hours.

- Read-only: no orders.
- Queries overseas balance (inquire-balance output1) across NASD/NYSE/AMEX.
- Tracks state in data/schd_ord_psbl_state.json to avoid spam.

Exit behavior:
- If weekend/US holiday: prints single line starting with "SKIP" and exits 0.
- If no state change: prints nothing and exits 0.
- If ord_psbl_qty changed OR key fields changed: prints JSON summary to stdout.

This script is intended to be called by an OpenClaw cron agent which can decide
whether to notify the user and/or restart US modules.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from logger import setup_logger  # noqa: E402
from kis_auth import KISAuth, load_auth_from_env  # noqa: E402
from kis_rest_orders import AccountInfo  # noqa: E402
from kis_rest_overseas import KISOverseasRestOrders  # noqa: E402
from modules.mijang import is_us_market_holiday, get_us_holiday_name  # noqa: E402


STATE_PATH = ROOT / "data" / "schd_ord_psbl_state.json"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _to_int(v) -> int:
    try:
        if v is None or v == "":
            return 0
        return int(float(v))
    except Exception:
        return 0


def _read_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


async def _fetch_schd_row(rest: KISOverseasRestOrders, exchange: str) -> dict | None:
    raw = await rest.get_balance_raw(exchange=exchange)
    if not isinstance(raw, dict):
        return None
    out1 = raw.get("output1")
    if not isinstance(out1, list):
        return None
    for row in out1:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("ovrs_pdno", "") or "").strip().upper()
        if sym == "SCHD":
            return row
    return None


async def main() -> int:
    # Holiday/weekend check in US/Eastern
    # NOTE: This script may be called every 5 minutes; to avoid spam on closed days,
    # we only emit a single "SKIP ..." line once per ET date+reason.
    now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    skip_reason: str | None = None
    if now_et.weekday() >= 5:
        skip_reason = "Weekend"
    elif is_us_market_holiday(now_et.date()):
        skip_reason = get_us_holiday_name(now_et.date()) or "Holiday"

    if skip_reason is not None:
        prev = _read_state()
        key = f"{now_et.date().isoformat()}::{skip_reason}"
        if prev.get("last_skip_key") != key:
            state = {
                **(prev if isinstance(prev, dict) else {}),
                "last_skip_key": key,
                "last_seen_at": datetime.now(tz=ZoneInfo("Asia/Seoul")).isoformat(),
            }
            _write_state(state)
            print(f"SKIP {skip_reason}")
        return 0

    _load_dotenv(ROOT / ".env")

    app_key, app_secret, _ = load_auth_from_env()
    acct_no = (os.environ.get("KIS_ACCOUNT_NO") or "").strip()
    acct_prdt = (os.environ.get("KIS_ACCOUNT_PRODUCT_CODE") or "").strip()

    logger = setup_logger(str(ROOT / "logs" / "schd_ord_psbl"), "Asia/Seoul")
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, logger)
    account = AccountInfo(account_no=acct_no, product_code=acct_prdt)

    found_row = None
    found_ex = None

    # Try each exchange bucket in order
    for ex in ("NASD", "NYSE", "AMEX"):
        rest = KISOverseasRestOrders("https://openapi.koreainvestment.com:9443", auth, account, logger, exchange=ex)
        try:
            row = await _fetch_schd_row(rest, ex)
        finally:
            await rest.aclose()
        if row is not None:
            found_row = row
            found_ex = ex
            break

    if not found_row:
        # No SCHD row found: still update last_seen_at but don't spam
        prev = _read_state()
        state = {
            **prev,
            "last_seen_at": datetime.now(tz=ZoneInfo("Asia/Seoul")).isoformat(),
            "found": False,
        }
        _write_state(state)
        return 0

    ovrs_cblc_qty = _to_int(found_row.get("ovrs_cblc_qty"))
    ord_psbl_qty = _to_int(found_row.get("ord_psbl_qty"))
    ovrs_excg_cd = str(found_row.get("ovrs_excg_cd") or found_ex or "").strip().upper()
    loan_type_cd = str(found_row.get("loan_type_cd") or "").strip()

    now_kst = datetime.now(tz=ZoneInfo("Asia/Seoul")).isoformat()

    prev = _read_state()
    prev_ord = _to_int(prev.get("ord_psbl_qty"))
    prev_ovrs = _to_int(prev.get("ovrs_cblc_qty"))
    prev_ex = str(prev.get("ovrs_excg_cd") or "").strip().upper()
    prev_loan = str(prev.get("loan_type_cd") or "").strip()

    changed = (prev_ord != ord_psbl_qty) or (prev_ovrs != ovrs_cblc_qty) or (prev_ex != ovrs_excg_cd) or (prev_loan != loan_type_cd)

    state = {
        "last_seen_at": now_kst,
        "found": True,
        "ovrs_cblc_qty": ovrs_cblc_qty,
        "ord_psbl_qty": ord_psbl_qty,
        "ovrs_excg_cd": ovrs_excg_cd,
        "loan_type_cd": loan_type_cd,
    }
    _write_state(state)

    if not changed:
        return 0

    payload = {
        "ok": True,
        "changed": True,
        "prev": {
            "ovrs_cblc_qty": prev_ovrs,
            "ord_psbl_qty": prev_ord,
            "ovrs_excg_cd": prev_ex,
            "loan_type_cd": prev_loan,
        },
        "curr": state,
        "transition": {
            "ord_psbl_0_to_pos": (prev_ord == 0 and ord_psbl_qty > 0 and ovrs_cblc_qty > 0),
        },
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
