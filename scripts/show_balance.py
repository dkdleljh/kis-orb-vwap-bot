"""Show current KR/US account balance (best-effort).

Prints:
- KR: available order cash (psbl cash) + positions (qty/avg)
- US: available order cash (psamount) + positions (if any)

This script is read-only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from logger import setup_logger  # noqa: E402
from kis_auth import KISAuth, load_auth_from_env  # noqa: E402
from kis_rest_orders import AccountInfo, KISRestOrders  # noqa: E402
from kis_rest_overseas import KISOverseasRestOrders  # noqa: E402


def _load_dotenv(path: Path) -> None:
    try:
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
    except Exception:
        return


async def main() -> int:
    _load_dotenv(ROOT / ".env")

    app_key, app_secret, _ = load_auth_from_env()
    acct_no = (os.environ.get("KIS_ACCOUNT_NO") or "").strip()
    acct_prdt = (os.environ.get("KIS_ACCOUNT_PRODUCT_CODE") or "").strip()

    if not acct_no or not acct_prdt:
        print(json.dumps({"ok": False, "error": "missing KIS_ACCOUNT_NO / KIS_ACCOUNT_PRODUCT_CODE"}, ensure_ascii=False))
        return 2

    logger = setup_logger(str(ROOT / "logs" / "balance"), "Asia/Seoul")
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, logger)
    account = AccountInfo(account_no=acct_no, product_code=acct_prdt)

    rest_kr = KISRestOrders("https://openapi.koreainvestment.com:9443", auth, account, logger)
    # We'll query multiple exchanges (best-effort) because some assets/cash may be mapped differently.
    rest_us_nasd = KISOverseasRestOrders("https://openapi.koreainvestment.com:9443", auth, account, logger, exchange="NASD")
    rest_us_nyse = KISOverseasRestOrders("https://openapi.koreainvestment.com:9443", auth, account, logger, exchange="NYSE")

    try:
        kr_cash = await rest_kr.get_cash_available("069500", 0)
        kr_pos = await rest_kr.get_all_positions()
    finally:
        await rest_kr.aclose()

    async def _fetch_us(rest: KISOverseasRestOrders, label: str) -> dict:
        try:
            cash = await rest.get_cash_available("AAPL", price=100.0)
            raw = await rest.get_balance_raw()
        finally:
            await rest.aclose()
        return {"exchange": label, "cash_available": cash, "raw": raw}

    us_nasd = await _fetch_us(rest_us_nasd, "NASD")
    us_nyse = await _fetch_us(rest_us_nyse, "NYSE")

    # summarize positions (KR)
    kr_positions = []
    for sym, info in (kr_pos or {}).items():
        kr_positions.append({"symbol": sym, "qty": info.get("qty"), "avg_price": info.get("avg_price")})

    def _summarize_us(one: dict) -> dict:
        raw = one.get("raw")
        positions = []
        summary = {}
        if isinstance(raw, dict):
            out1 = raw.get("output1")
            if isinstance(out1, list):
                for row in out1:
                    try:
                        qty = int(float(row.get("ovrs_hldg_qty", 0) or 0))
                        if qty <= 0:
                            continue
                        positions.append(
                            {
                                "symbol": row.get("ovrs_pdno"),
                                "qty": qty,
                                "avg_price": float(row.get("pchs_amt", 0) or 0),
                            }
                        )
                    except Exception:
                        continue
            out2 = raw.get("output2")
            if isinstance(out2, dict):
                keep = [
                    "frcr_pchs_amt1",
                    "frcr_buy_amt_smtl1",
                    "tot_evlu_pfls_amt",
                    "tot_pftrt",
                    "ovrs_tot_pfls",
                    "rlzt_erng_rt",
                ]
                summary = {k: out2.get(k) for k in keep if k in out2}
            summary["msg1"] = raw.get("msg1")
            summary["rt_cd"] = raw.get("rt_cd")

        return {
            "exchange": one.get("exchange"),
            "cash_available": one.get("cash_available"),
            "positions": positions,
            "summary": summary,
        }

    # Also try AMEX best-effort
    rest_us_amex = KISOverseasRestOrders("https://openapi.koreainvestment.com:9443", auth, account, logger, exchange="AMEX")
    us_amex = await _fetch_us(rest_us_amex, "AMEX")

    us = {
        "NASD": _summarize_us(us_nasd),
        "NYSE": _summarize_us(us_nyse),
        "AMEX": _summarize_us(us_amex),
    }

    # Integrated margin estimate (display-only): KR cash -> USD using USD/KRW
    # This does NOT guarantee broker-side availability; for diagnostics only.
    try:
        import urllib.request, json as _json

        # exchangerate.host may be limited; use open.er-api.com as primary
        urls = [
            "https://open.er-api.com/v6/latest/USD",
            "https://api.exchangerate.host/latest?base=USD&symbols=KRW",
        ]
        usdkrw = 0.0
        for u in urls:
            try:
                with urllib.request.urlopen(u, timeout=10) as r:
                    ex = _json.loads(r.read().decode("utf-8", errors="replace"))
                rates = ex.get("rates") or (ex.get("conversion_rates") if isinstance(ex.get("conversion_rates"), dict) else {})
                v = (rates or {}).get("KRW")
                if v:
                    usdkrw = float(v)
                    break
            except Exception:
                continue
        if usdkrw > 0:
            us["ESTIMATE_FROM_KR_CASH"] = {
                "usdkrw": usdkrw,
                "kr_cash_available": kr_cash,
                "usd_equiv": round(float(kr_cash) / usdkrw, 2),
                "note": "표시용 추정치(통합증거금 적용 가정). 실제 주문 가능 여부는 KIS 해외 psamount가 우선입니다.",
            }
    except Exception:
        pass

    print(
        json.dumps(
            {
                "ok": True,
                "kr": {"cash_available": kr_cash, "positions": kr_positions},
                "us": us,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
