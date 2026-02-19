import logging
from typing import List

import aiohttp

LOGGER = logging.getLogger(__name__)


class KisScanner:
    def __init__(self, auth, base_url: str):
        self.auth = auth
        self.base_url = base_url
        self.last_error: str = ""

    async def get_top_trading_value(self, limit: int = 30, market_div_code: str = "J") -> List[str]:
        """거래대금 상위 종목 발굴 (가장 확실한 주도주)

        실운영에서 자주 발생한 문제:
        - HTTP 200인데 rt_cd!=0 (msg1에 에러 원인)
        - 환경/계정에 따라 스크리너 파라미터 키가
          FID_COND_SCR_GRP_CODE vs FID_COND_SCR_DIV_CODE로 갈리는 케이스
        """
        self.last_error = ""

        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"

        headers = self.auth.auth_headers()
        headers["tr_id"] = "FHPST01710000"  # 거래대금 순위 TR ID
        headers["custtype"] = "P"

        mkt = (market_div_code or "J").strip().upper()
        if mkt not in {"J", "Y", "K"}:
            mkt = "J"

        base_params = {
            "FID_COND_MRKT_DIV_CODE": mkt,  # J: 전체, Y: 코스피, K: 코스닥
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "111111111",  # 필터링 (ETF/ETN/스팩 등 포함여부)
            # KIS 문서/계정/환경에 따라 키가 EXLS vs EXCLS 로 갈리는 케이스가 있어 둘 다 넣음.
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_TRGT_EXCLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        }

        params_variants = [
            ("FID_COND_SCR_DIV_CODE", {**base_params, "FID_COND_SCR_DIV_CODE": "20171"}),
            ("FID_COND_SCR_GRP_CODE", {**base_params, "FID_COND_SCR_GRP_CODE": "20171"}),
        ]

        try:
            async with aiohttp.ClientSession() as session:
                last_err = ""
                for key_name, params in params_variants:
                    async with session.get(url, headers=headers, params=params, timeout=10) as resp:
                        try:
                            data = await resp.json()
                        except Exception:
                            text = await resp.text()
                            last_err = f"non-json resp status={resp.status} text={text[:200]}"
                            LOGGER.error(f"Scanner fetch failed({key_name}): {last_err}")
                            continue

                    rt_cd = str(data.get("rt_cd", ""))
                    if resp.status != 200 or rt_cd != "0":
                        msg_cd = data.get("msg_cd", "")
                        msg1 = data.get("msg1", "")
                        msg = msg1 or msg_cd or "Unknown"
                        last_err = f"status={resp.status} rt_cd={rt_cd} msg_cd={msg_cd} msg={msg}"

                        # 특정 입력필드 오류면 다음 variant 시도
                        if "INPUT" in msg or "FIELD" in msg or "FID_COND_SCR" in msg:
                            LOGGER.error(f"Scanner fetch failed({key_name}): {last_err} -> trying next")
                            continue

                        LOGGER.error(f"Scanner fetch failed({key_name}): {last_err}")
                        break

                    output = data.get("output", [])
                    symbols: List[str] = []
                    for item in output:
                        sym = item.get("mksc_shrn_iscd")
                        name = item.get("hts_kor_isnm", "")

                        price_candidates = ["stck_prpr", "stck_prpr_unpr", "prpr"]
                        price = 0.0
                        for k in price_candidates:
                            v = item.get(k)
                            if v:
                                price = float(v)
                                break

                        if price < 1000:
                            continue
                        if not sym or not sym.isdigit():
                            continue
                        if "스팩" in name:
                            continue

                        symbols.append(sym)
                        if len(symbols) >= limit:
                            break

                    LOGGER.info(f"Scanned Top {len(symbols)} stocks ({key_name}): {symbols}")
                    return symbols

                self.last_error = last_err
                if last_err:
                    LOGGER.error(f"Scanner fetch failed after variants: {last_err}")
                return []

        except aiohttp.ClientError as e:
            self.last_error = str(e)
            LOGGER.error(f"Scanner network error: {e}")
            return []
        except Exception as e:
            self.last_error = str(e)
            LOGGER.error(f"Scanner error: {e}")
            return []
