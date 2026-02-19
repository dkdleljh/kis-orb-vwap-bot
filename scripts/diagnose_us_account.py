"""Diagnose US(overseas) account/balance visibility issues (read-only).

Checks:
- dayornight (ledger selection)
- psamount (buying power)
- inquire-balance raw (output1/2)
- try multiple exchanges

This helps explain why US balance appears as 0.
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
from kis_rest_orders import AccountInfo  # noqa: E402
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


def _pick(d: dict, keys: list[str]) -> dict:
    return {k: d.get(k) for k in keys if k in d}


async def _one(exchange: str, auth, account, logger) -> dict:
    rest = KISOverseasRestOrders("https://openapi.koreainvestment.com:9443", auth, account, logger, exchange=exchange)
    try:
        daynight = await rest.get_day_or_night()
        cash = await rest.get_cash_available("AAPL", price=100.0)
        raw = await rest.get_balance_raw()
    finally:
        await rest.aclose()

    out2 = raw.get("output2") if isinstance(raw, dict) else None
    out2 = out2 if isinstance(out2, dict) else {}

    summary_keys = [
        "frcr_pchs_amt1",
        "frcr_buy_amt_smtl1",
        "tot_evlu_pfls_amt",
        "tot_pftrt",
        "ovrs_tot_pfls",
        "rlzt_erng_rt",
    ]

    return {
        "exchange": exchange,
        "day_or_night": daynight,
        "cash_available": cash,
        "balance_rt_cd": raw.get("rt_cd") if isinstance(raw, dict) else None,
        "balance_msg1": raw.get("msg1") if isinstance(raw, dict) else None,
        "output1_len": len(raw.get("output1") or []) if isinstance(raw, dict) and isinstance(raw.get("output1"), list) else None,
        "output2": _pick(out2, summary_keys),
    }


async def main() -> int:
    _load_dotenv(ROOT / ".env")

    app_key, app_secret, _ = load_auth_from_env()
    acct_no = (os.environ.get("KIS_ACCOUNT_NO") or "").strip()
    acct_prdt = (os.environ.get("KIS_ACCOUNT_PRODUCT_CODE") or "").strip()

    logger = setup_logger(str(ROOT / "logs" / "us_diagnose"), "Asia/Seoul")
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, logger)
    account = AccountInfo(account_no=acct_no, product_code=acct_prdt)

    results = []
    for ex in ("NASD", "NYSE", "AMEX"):
        results.append(await _one(ex, auth, account, logger))

    print(json.dumps({"ok": True, "account_product": acct_prdt, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
