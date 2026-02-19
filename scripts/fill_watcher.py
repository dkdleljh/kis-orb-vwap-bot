"""Fill watcher: backfill Fill events by polling broker executions.

Why:
- Our Decision/order_result events capture *intent* and broker order ids (ODNO).
- To make the audit trail institution-like, we also append explicit Fill events.

This script is read-only.

Strategy:
- Read today's Decision events from logs/events/<UTC_ymd>/events.jsonl
- Extract order_result entries that contain ODNO (broker order id)
- For KR: use KISRestOrders.get_fills() and match by order_id
- For US: call overseas inquire-ccnl (TTTS3035R) and match by odno in output
- De-duplicate using a local state file logs/fill_watcher_state.json

Env:
- KIS_ACCOUNT_NO / KIS_ACCOUNT_PRODUCT_CODE / KIS_APP_KEY / KIS_APP_SECRET

Recommended schedule:
- every 5 minutes on weekdays.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.event_store import EventStore
from core.events import Fill, Event
from kis_auth import KISAuth, load_auth_from_env
from kis_rest_orders import AccountInfo, KISRestOrders
from logger import setup_logger

STATE_PATH = ROOT / "logs" / "fill_watcher_state.json"


def _utc_ymd() -> str:
    return datetime.utcnow().strftime("%Y%m%d")


def _events_path(ymd: str) -> Path:
    return ROOT / "logs" / "events" / ymd / "events.jsonl"


def _load_state() -> dict[str, Any]:
    try:
        if STATE_PATH.exists():
            j = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return j if isinstance(j, dict) else {}
    except Exception:
        return {}
    return {}


def _save_state(st: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _load_dotenv_like(path: Path) -> None:
    try:
        if not path.exists():
            return
        for raw in path.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        return


def _iter_orders_for_fill_watch(ymd: str) -> list[dict[str, Any]]:
    """Collect broker order-ids(ODNO) to watch for fills.

    Sources:
    - Decision(kind=order_result) with extra.odno
    - OrderSubmitted/OrderAck events with broker_order_id

    Returns:
      list of {symbol, market, side, correlation_id, odno}
    """

    path = _events_path(ymd)
    if not path.exists():
        return []

    out: list[dict[str, Any]] = []

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue

        et = e.get("type")
        p = e.get("payload") or {}
        if not isinstance(p, dict):
            p = {}

        # 1) Decision(order_result)
        if et == "Decision" and p.get("kind") == "order_result":
            extra = p.get("extra") or {}
            if isinstance(extra, dict):
                odno = extra.get("odno")
                if odno:
                    out.append(
                        {
                            "ts": e.get("ts"),
                            "symbol": e.get("symbol"),
                            "market": p.get("market"),
                            "style": p.get("style"),
                            "side": p.get("side"),
                            "correlation_id": p.get("correlation_id") or "",
                            "odno": str(odno),
                        }
                    )
            continue

        # 2) Engine order events
        if et in {"OrderSubmitted", "OrderAck"}:
            odno = p.get("broker_order_id")
            if odno:
                out.append(
                    {
                        "ts": e.get("ts"),
                        "symbol": e.get("symbol"),
                        "market": None,
                        "style": None,
                        "side": None,
                        "correlation_id": p.get("correlation_id") or "",
                        "odno": str(odno),
                    }
                )

    # de-dup by odno+symbol+corr
    seen = set()
    uniq: list[dict[str, Any]] = []
    for r in out:
        key = (str(r.get("symbol") or ""), str(r.get("odno") or ""), str(r.get("correlation_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    return uniq


async def _us_inquire_ccnl(auth: KISAuth, account: AccountInfo, *, excg: str, ord_dt: str, max_pages: int = 5) -> list[dict[str, Any]]:
    """Overseas order/execution list (best-effort, paged).

    Uses CTX_AREA_NK200/FK200 when provided by the broker.
    """
    import aiohttp

    url = f"{auth.base_url}/uapi/overseas-stock/v1/trading/inquire-ccnl"
    headers = auth.auth_headers()
    headers.update({"custtype": "P", "tr_id": "TTTS3035R"})

    all_rows: list[dict[str, Any]] = []
    nk = ""
    fk = ""

    async with aiohttp.ClientSession() as s:
        for _ in range(max_pages):
            params = {
                "CANO": account.account_no,
                "ACNT_PRDT_CD": account.product_code,
                "PDNO": "%",
                "ORD_STRT_DT": ord_dt,
                "ORD_END_DT": ord_dt,
                "SLL_BUY_DVSN": "00",
                "CCLD_NCCS_DVSN": "00",
                "OVRS_EXCG_CD": excg,
                "SORT_SQN": "DS",
                "ORD_DT": "",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "CTX_AREA_NK200": nk,
                "CTX_AREA_FK200": fk,
            }

            async with s.get(url, headers=headers, params=params, timeout=20) as r:
                j = await r.json()

            outs = j.get("output") or []
            if isinstance(outs, dict):
                outs = [outs]
            if isinstance(outs, list) and outs:
                all_rows.extend(outs)

            # Paging keys (best-effort)
            nk = str(j.get("ctx_area_nk200") or "")
            fk = str(j.get("ctx_area_fk200") or "")
            tr_cont = str(j.get("tr_cont") or j.get("tr_cont_yn") or "")

            # Stop conditions: no next key or no continuation signal.
            if not nk and not fk:
                break
            if tr_cont and tr_cont not in {"M", "F", "Y", "N"}:
                break

    return all_rows


def _make_store() -> EventStore:
    return EventStore(str(ROOT / "logs" / "events"), enabled=True, run_id=os.environ.get("KIS_RUN_ID") or None)


async def main() -> int:
    _load_dotenv_like(ROOT / ".env")

    ymd = _utc_ymd()
    orders = _iter_orders_for_fill_watch(ymd)
    if not orders:
        print(json.dumps({"ok": True, "ymd": ymd, "orders": 0, "fills_added": 0}, ensure_ascii=False))
        return 0

    st = _load_state()
    filled_keys = set((st.get("filled") or {}).keys()) if isinstance(st.get("filled"), dict) else set()
    status_keys = set((st.get("status") or {}).keys()) if isinstance(st.get("status"), dict) else set()

    app_key, app_secret, _ = load_auth_from_env()
    logger = setup_logger(str(ROOT / "logs" / "tmp"), "Asia/Seoul")
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, logger)
    await auth.fetch_token(force=True)

    account = AccountInfo(
        account_no=os.environ["KIS_ACCOUNT_NO"],
        product_code=os.environ["KIS_ACCOUNT_PRODUCT_CODE"],
    )

    store = _make_store()

    fills_added = 0

    # KR fills cache (paged, best-effort)
    rest_kr = KISRestOrders(auth.base_url, auth, account, logger)
    try:
        kr_fills: list[dict[str, Any]] = []
        for page in range(1, 6):
            xs = await rest_kr.get_fills(
                start_date=(datetime.now() - timedelta(days=2)).strftime("%Y%m%d"),
                page=page,
            )
            if not xs:
                break
            kr_fills.extend(xs)
    finally:
        await rest_kr.aclose()

    kr_by_odno: dict[str, list[dict[str, Any]]] = {}
    for f in kr_fills or []:
        odno = str(f.get("order_id") or "")
        if not odno:
            continue
        kr_by_odno.setdefault(odno, []).append(f)

    # US inquire cache (today + yesterday, exchanges)
    kst_today = datetime.now().strftime("%Y%m%d")
    kst_yday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    us_rows: list[dict[str, Any]] = []
    for ord_dt in [kst_today, kst_yday]:
        for excg in ["AMEX", "NASD", "NYSE"]:
            try:
                us_rows.extend(await _us_inquire_ccnl(auth, account, excg=excg, ord_dt=ord_dt))
            except Exception:
                continue

    # normalize US rows by odno (lower-case keys)
    us_by_odno: dict[str, dict[str, Any]] = {}
    for r in us_rows:
        try:
            odno = str(r.get("odno") or r.get("ODNO") or "")
        except Exception:
            odno = ""
        if odno:
            us_by_odno[odno] = r

    # Process each order_result Decision
    for o in orders:
        odno = str(o.get("odno"))
        market = str(o.get("market") or "")
        sym = str(o.get("symbol") or "")
        corr = str(o.get("correlation_id") or "")
        key = f"{market}:{sym}:{odno}:{corr}" if corr else f"{market}:{sym}:{odno}"
        if key in filled_keys:
            continue

        if market == "KR":
            hits = kr_by_odno.get(odno) or []
            if not hits:
                # best-effort status event (once)
                if key not in status_keys:
                    try:
                        store.append(
                            Event.make(
                                type="OrderStatus",
                                payload={
                                    "broker_order_id": odno,
                                    "status": "NO_FILL_YET",
                                    "market": "KR",
                                    "correlation_id": corr,
                                },
                                symbol=sym,
                                run_id=store.run_id,
                            )
                        )
                        st.setdefault("status", {})[key] = {"ts": datetime.utcnow().isoformat() + "Z"}
                        status_keys.add(key)
                    except Exception:
                        pass
                continue
            # append fills (could be multiple partials)
            for f in hits[:10]:
                try:
                    ev = Fill(
                        symbol=str(f.get("symbol") or sym),
                        side=str(f.get("side") or ""),
                        qty=int(f.get("qty") or 0),
                        price=float(f.get("price") or 0.0),
                        broker_order_id=str(odno),
                        correlation_id=corr,
                    ).to_event(run_id=store.run_id)
                    store.append(ev)
                    fills_added += 1
                except Exception:
                    pass
            st.setdefault("filled", {})[key] = {"ts": datetime.utcnow().isoformat() + "Z"}

        elif market == "US":
            r = us_by_odno.get(odno)
            if not r:
                if key not in status_keys:
                    try:
                        store.append(
                            Event.make(
                                type="OrderStatus",
                                payload={
                                    "broker_order_id": odno,
                                    "status": "NO_ROW_YET",
                                    "market": "US",
                                    "correlation_id": corr,
                                },
                                symbol=sym,
                                run_id=store.run_id,
                            )
                        )
                        st.setdefault("status", {})[key] = {"ts": datetime.utcnow().isoformat() + "Z"}
                        status_keys.add(key)
                    except Exception:
                        pass
                continue
            # determine filled qty/price
            try:
                fill_qty = int(float(r.get("ft_ccld_qty") or 0))
            except Exception:
                fill_qty = 0
            if fill_qty <= 0:
                if key not in status_keys:
                    try:
                        store.append(
                            Event.make(
                                type="OrderStatus",
                                payload={
                                    "broker_order_id": odno,
                                    "status": "NO_FILL_YET",
                                    "market": "US",
                                    "correlation_id": corr,
                                },
                                symbol=sym,
                                run_id=store.run_id,
                            )
                        )
                        st.setdefault("status", {})[key] = {"ts": datetime.utcnow().isoformat() + "Z"}
                        status_keys.add(key)
                    except Exception:
                        pass
                continue
            try:
                fill_px = float(r.get("ft_ccld_unpr3") or 0)
            except Exception:
                fill_px = 0.0
            side_cd = str(r.get("sll_buy_dvsn_cd") or "")
            side = "BUY" if side_cd == "02" else "SELL" if side_cd == "01" else ""

            try:
                ev = Fill(
                    symbol=str(r.get("pdno") or sym),
                    side=side or "",
                    qty=int(fill_qty),
                    price=float(fill_px or 0.0),
                    broker_order_id=str(odno),
                    correlation_id=corr,
                ).to_event(run_id=store.run_id)
                store.append(ev)
                fills_added += 1
                st.setdefault("filled", {})[key] = {"ts": datetime.utcnow().isoformat() + "Z"}
            except Exception:
                pass

    _save_state(st)
    print(json.dumps({"ok": True, "ymd": ymd, "orders": len(orders), "fills_added": fills_added}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
