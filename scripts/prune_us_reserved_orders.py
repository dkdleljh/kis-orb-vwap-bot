#!/usr/bin/env python3
"""Prune duplicate US reserved orders (safe, best-effort).

Goal:
- Prevent multiple identical reserved sell orders from accumulating (e.g., SCHD TP).

Policy (recommended default):
- Only targets SYMBOL=SCHD unless overridden.
- Groups rows by (symbol, exchange, side, qty, price).
- Keeps the newest row in each group (by receipt date+time when available).
- Cancels the rest using order-resv-ccnl.

Safety gates:
- Requires environment variable ALLOW_AUTO_CANCEL=YES
- Respects STOP_TRADING.flag and KIS_KILL_SWITCH=1 (won't cancel when kill-switch is on)

Usage:
  ALLOW_AUTO_CANCEL=YES ./venv/bin/python scripts/prune_us_reserved_orders.py --exchange AMEX --symbol SCHD

Exit codes:
- 0: ok / nothing to do
- 2: refused by safety gate
- 3: partial failure
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import aiohttp
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kis_auth import KISAuth, load_auth_from_env
from kis_rest_orders import AccountInfo
from kis_rest_overseas import KISOverseasRestOrders
from logger import setup_logger


def _load_dotenv_like(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _kill_switch_active() -> bool:
    return (ROOT / "STOP_TRADING.flag").exists() or os.environ.get("KIS_KILL_SWITCH", "0") == "1"


def _ymd(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def _norm(v: Any) -> str:
    return str(v or "").strip()


def _to_int(v: Any) -> int:
    try:
        if v is None or v == "":
            return 0
        return int(float(v))
    except Exception:
        return 0


@dataclass
class Row:
    symbol: str
    exchange: str
    side: str
    qty: int
    price: str
    rcit_dt: str
    rcit_tmd: str
    odno: str
    raw: dict

    @property
    def group_key(self) -> tuple[str, str, str, int, str]:
        return (self.symbol, self.exchange, self.side, self.qty, self.price)

    @property
    def sort_key(self) -> str:
        # newer first: YYYYMMDDHHMMSS
        return f"{self.rcit_dt}{self.rcit_tmd}".ljust(14, "0")


def _parse_rows(rows: list[dict], *, symbol_filter: str = "") -> list[Row]:
    out: list[Row] = []
    symf = symbol_filter.strip().upper()
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = _norm(r.get("pdno") or r.get("PDNO")).upper()
        if symf and sym != symf:
            continue

        ex = _norm(r.get("ovrs_excg_cd") or r.get("OVRS_EXCG_CD") or r.get("tr_mket_name")).upper()
        side = _norm(r.get("sll_buy_dvsn_cd_name") or r.get("sll_buy_dvsn_cd") or r.get("SLL_BUY_DVSN_CD"))
        qty = _to_int(r.get("ft_ord_qty") or r.get("FT_ORD_QTY") or r.get("ord_qty") or r.get("ORD_QTY"))
        px = _norm(r.get("ft_ord_unpr3") or r.get("FT_ORD_UNPR3") or r.get("ovrs_ord_unpr") or r.get("OVRS_ORD_UNPR"))
        rcit_dt = _norm(r.get("rsvn_ord_rcit_dt") or r.get("RSVN_ORD_RCIT_DT"))
        rcit_tmd = _norm(r.get("ord_rcit_tmd") or r.get("ORD_RCIT_TMD"))
        odno = _norm(r.get("ovrs_rsvn_odno") or r.get("OVRS_RSVN_ODNO") or r.get("odno") or r.get("ODNO"))

        if not sym or not rcit_dt or not odno:
            continue
        out.append(Row(sym, ex, side, qty, px, rcit_dt, rcit_tmd, odno, r))
    return out


async def _retry(label: str, fn, *, tries: int = 4, base_sleep: float = 0.8):
    """Retry for transient broker/network failures.

    Returns:
        The awaited result of fn(). Raises last exception if all retries fail.
    """
    last_exc: Exception | None = None
    for i in range(tries):
        try:
            return await fn()
        except (
            aiohttp.client_exceptions.ServerDisconnectedError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientOSError,
            aiohttp.ClientResponseError,
            asyncio.TimeoutError,
        ) as e:
            last_exc = e
            await asyncio.sleep(base_sleep * (2**i))
    assert last_exc is not None
    raise last_exc


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", default="AMEX", help="NASD/NYSE/AMEX")
    ap.add_argument("--symbol", default="SCHD")
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()

    if os.environ.get("ALLOW_AUTO_CANCEL", "") != "YES":
        print("REFUSED: set ALLOW_AUTO_CANCEL=YES")
        return 2
    if _kill_switch_active():
        print("REFUSED: kill_switch_on")
        return 2

    _load_dotenv_like(ROOT / ".env")

    logger = setup_logger(str(ROOT / "logs" / "tmp"), "Asia/Seoul")
    app_key, app_secret, _ = load_auth_from_env()
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, logger)
    try:
        await _retry("fetch_token", lambda: auth.fetch_token(force=True), tries=3, base_sleep=0.8)
    except Exception as e:
        # Do not alarm on transient broker issues; skip this run.
        print(f"OK: broker unavailable ({type(e).__name__})")
        return 0

    account = AccountInfo(
        account_no=os.environ.get("KIS_ACCOUNT_NO", ""),
        product_code=os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", ""),
    )

    end = datetime.now()
    start = end - timedelta(days=max(1, args.days))

    rest = KISOverseasRestOrders(auth.base_url, auth, account, logger, exchange=args.exchange)
    try:
        try:
            rows_raw = await _retry(
                "order_resv_list_us",
                lambda: rest.order_resv_list_us(
                    exchange=args.exchange,
                    inqr_strt_dt=_ymd(start),
                    inqr_end_dt=_ymd(end),
                ),
                tries=4,
                base_sleep=0.8,
            )
        except Exception as e:
            print(f"OK: broker unavailable ({type(e).__name__})")
            return 0

        # Keep only active(received) + not-cancelled rows when those flags exist.
        filtered: list[dict] = []
        for rr in rows_raw:
            if not isinstance(rr, dict):
                continue
            if _norm(rr.get("cncl_yn") or rr.get("CNCL_YN")).upper() == "Y":
                continue
            filtered.append(rr)

        rows = _parse_rows(filtered, symbol_filter=args.symbol)

        # group
        groups: dict[tuple[str, str, str, int, str], list[Row]] = {}
        for r in rows:
            groups.setdefault(r.group_key, []).append(r)

        to_cancel: list[Row] = []
        for key, items in groups.items():
            if len(items) <= 1:
                continue
            # keep newest
            items_sorted = sorted(items, key=lambda x: x.sort_key, reverse=True)
            keep = items_sorted[0]
            to_cancel.extend(items_sorted[1:])
            print(f"KEEP {keep.symbol} {keep.exchange} qty={keep.qty} px={keep.price} rcit={keep.rcit_dt} {keep.rcit_tmd} odno={keep.odno}")

        if not to_cancel:
            print("OK: no duplicates")
            return 0

        # de-dup cancel list by (rcit_dt, odno)
        uniq: dict[tuple[str, str], Row] = {}
        for r in to_cancel:
            uniq[(r.rcit_dt, r.odno)] = r
        to_cancel = list(uniq.values())

        failed = 0
        canceled = 0
        for r in to_cancel:
            if _kill_switch_active():
                print("ABORT: kill_switch_on")
                return 3
            print(f"CANCEL {r.symbol} rcit_dt={r.rcit_dt} odno={r.odno} qty={r.qty} px={r.price}")
            try:
                resp = await _retry(
                    "order_resv_ccnl_us",
                    lambda: rest.order_resv_ccnl_us(rsvn_ord_rcit_dt=r.rcit_dt, ovrs_rsvn_odno=r.odno),
                    tries=3,
                    base_sleep=0.8,
                )
            except Exception as e:
                failed += 1
                print(f"CANCEL_FAIL odno={r.odno} exc={type(e).__name__}")
                continue
            rt_cd = str((resp or {}).get("rt_cd"))
            msg1 = str((resp or {}).get("msg1") or "")
            if rt_cd == "0" or "이미 취소" in msg1:
                canceled += 1
                continue
            failed += 1
            print(f"CANCEL_FAIL odno={r.odno} rt_cd={rt_cd} msg={msg1}")

        if failed:
            print(f"DONE_WITH_ERRORS canceled={canceled} failed={failed}")
            return 3

        print(f"DONE canceled={canceled}")
        return 0
    finally:
        await rest.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
