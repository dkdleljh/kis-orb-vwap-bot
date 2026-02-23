"""Notify new execution fills.

This script is designed to be called by the OpenClaw cron job "Trade Fill Notifier".

Previous implementation relied on a local queue file (logs/fill_alerts.jsonl). That
missed fills produced outside the main engine (e.g., manual orders).

New approach (recommended): query broker fills directly (KIS inquire-ccnl) and keep
an idempotent cursor locally.

Behavior:
- If cursor does not exist: bootstrap cursor to latest fill and print nothing.
- If no new fills since cursor: print nothing.
- If new fills exist: print formatted messages.

No secrets are printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Project root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CURSOR_PATH = ROOT / "logs" / "fill_cursor.json"
KST = ZoneInfo("Asia/Seoul")


def _load_dotenv(path: Path) -> None:
    try:
        if not path.exists():
            return
        for raw in path.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
    except Exception:
        return


def _parse_kis_ts(ts: str) -> datetime | None:
    """Parse KIS execution timestamp.

    Often comes as YYYYMMDDHHMMSS (local time). Best-effort.
    """
    ts = (ts or "").strip()
    if not ts:
        return None
    try:
        if len(ts) >= 14 and ts[:14].isdigit():
            dt = datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
            return dt.replace(tzinfo=KST)
        if len(ts) == 8 and ts.isdigit():
            dt = datetime.strptime(ts, "%Y%m%d")
            return dt.replace(tzinfo=KST)
    except Exception:
        return None
    return None


def _fmt_currency(x: float) -> str:
    try:
        return f"{int(round(float(x))):,}"
    except Exception:
        return str(x)


def _read_cursor() -> dict:
    try:
        if not CURSOR_PATH.exists():
            return {}
        return json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cursor(data: dict) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass
class FillMsg:
    dt: datetime
    symbol: str
    side: str
    qty: int
    price: float
    fill_id: str


async def _fetch_fills() -> list[dict]:
    # Local imports to avoid module overhead when called by cron
    from kis_auth import KISAuth, load_auth_from_env
    from kis_rest_orders import AccountInfo, KISRestOrders
    from logger import setup_logger

    _load_dotenv(ROOT / ".env")

    logger = setup_logger(str(ROOT / "logs" / "notify_fills"), "Asia/Seoul")

    base_url = os.environ.get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
    app_key, app_secret, _ = load_auth_from_env()

    acct_no = (os.environ.get("KIS_ACCOUNT_NO") or "").strip()
    acct_prdt = (os.environ.get("KIS_ACCOUNT_PRODUCT_CODE") or "").strip()

    if not acct_no or not acct_prdt:
        return []

    auth = KISAuth(base_url, app_key, app_secret, logger)
    account = AccountInfo(account_no=acct_no, product_code=acct_prdt)
    rest = KISRestOrders(base_url, auth, account, logger)

    try:
        end = datetime.now(KST).strftime("%Y%m%d")
        start = (datetime.now(KST) - timedelta(days=2)).strftime("%Y%m%d")
        fills = await rest.get_fills(start_date=start, end_date=end)
        return fills or []
    finally:
        try:
            await rest.aclose()
        except Exception:
            pass


def _to_messages(fills: list[dict]) -> list[FillMsg]:
    out: list[FillMsg] = []
    for f in fills:
        dt = _parse_kis_ts(str(f.get("ts") or ""))
        if not dt:
            continue
        out.append(
            FillMsg(
                dt=dt,
                symbol=str(f.get("symbol") or ""),
                side=str(f.get("side") or ""),
                qty=int(f.get("qty") or 0),
                price=float(f.get("price") or 0.0),
                fill_id=str(f.get("fill_id") or ""),
            )
        )
    out.sort(key=lambda x: x.dt)
    return out


def _format_msg(m: FillMsg) -> str:
    t = m.dt.strftime("%H:%M:%S")
    side_kr = "매수" if m.side.upper() == "BUY" else "매도"
    return (
        f"[{side_kr} 체결] {m.symbol} | {t} | {m.qty}주 @ {_fmt_currency(m.price)}원"
    )


async def main() -> int:
    cursor = _read_cursor()
    last_seen_iso = cursor.get("last_seen_ts")
    last_seen_dt = None
    try:
        if last_seen_iso:
            last_seen_dt = datetime.fromisoformat(last_seen_iso)
            if last_seen_dt.tzinfo is None:
                last_seen_dt = last_seen_dt.replace(tzinfo=KST)
    except Exception:
        last_seen_dt = None

    fills = await _fetch_fills()
    msgs = _to_messages(fills)

    if not msgs:
        # Nothing to do; keep cursor as-is.
        return 0

    newest = msgs[-1].dt

    # Bootstrap: don't spam historical fills on first run.
    if not last_seen_dt:
        _write_cursor({"last_seen_ts": newest.isoformat(), "bootstrapped_at": datetime.now(KST).isoformat()})
        return 0

    new_msgs = [m for m in msgs if m.dt > last_seen_dt]

    # Update cursor regardless (idempotent)
    _write_cursor({"last_seen_ts": newest.isoformat(), "updated_at": datetime.now(KST).isoformat()})

    if not new_msgs:
        return 0

    # Print messages (cron will deliver output)
    print("\n".join(_format_msg(m) for m in new_msgs))
    return 0


if __name__ == "__main__":
    os.chdir(str(ROOT))
    raise SystemExit(asyncio.run(main()))
