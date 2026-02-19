import asyncio

from universe_builder import build_universe, resolve_universe_config


class USScannerStub:
    def __init__(self):
        self.calls = []

    async def scan(self, top_n=0, exchanges=None, min_price=0.0):
        self.calls.append({
            "top_n": top_n,
            "exchanges": exchanges,
            "min_price": min_price,
        })
        return ["aapl", "TSLA", "005930", "BAD*", "TSLA", "msft"]


class KRScannerStub:
    def __init__(self):
        self.calls = []

    async def get_top_trading_value(self, limit=0):
        self.calls.append({"limit": limit})
        return [
            "005930",
            "000660",
            "AAPL",
            "12345",
            "005930",
            "114800",
        ]


class KRScannerWithMetaStub:
    async def get_top_trading_value(self, limit=0):
        return [
            {"mksc_shrn_iscd": "123456", "hts_kor_isnm": "테스트스팩"},
            {"mksc_shrn_iscd": "654321", "hts_kor_isnm": "정상종목"},
        ]


def run(coro):
    return asyncio.run(coro)


def test_us_validation_dedup_always_include_and_cap():
    scanner = USScannerStub()
    config = {
        "always_include": ["qqq", "TSLA"],
        "cap": 4,
        "top_n": 10,
        "exchanges": ["NASD", "NYSE"],
        "min_price_usd": 3,
    }

    result = run(
        build_universe(
            market="US",
            style="SCALP",
            scanner=scanner,
            base_symbols=["nvda", "MSFT"],
            config=config,
        )
    )

    assert result == ["QQQ", "TSLA", "AAPL", "005930"]


def test_kr_validation_dedup_always_include_and_cap():
    scanner = KRScannerStub()
    config = {
        "always_include": ["122630", "114800"],
        "cap": 3,
        "top_n": 10,
        "exclude_spac": True,
    }

    result = run(
        build_universe(
            market="KR",
            style="SCALP",
            scanner=scanner,
            base_symbols=["233740"],
            config=config,
        )
    )

    assert result == ["122630", "114800", "005930"]


def test_kr_exclude_spac_best_effort_with_meta():
    result = run(
        build_universe(
            market="KR",
            style="SCALP",
            scanner=KRScannerWithMetaStub(),
            base_symbols=[],
            config={"exclude_spac": True},
        )
    )
    assert result == ["654321"]


def test_fallback_to_base_symbols_when_scanner_missing():
    result = run(
        build_universe(
            market="US",
            style="SCALP",
            scanner=None,
            base_symbols=["aapl", "bad*", "MSFT", "MSFT"],
            config={"cap": 3},
        )
    )
    assert result == ["AAPL", "MSFT"]


def test_style_param_diff_applied_to_scanner_calls():
    us_scalp_scanner = USScannerStub()
    us_swing_scanner = USScannerStub()
    kr_scalp_scanner = KRScannerStub()
    kr_swing_scanner = KRScannerStub()

    scalp_cfg = resolve_universe_config("SCALP", {})
    swing_cfg = resolve_universe_config("SWING", {})

    run(build_universe("US", "SCALP", us_scalp_scanner, [], scalp_cfg))
    run(build_universe("US", "SWING", us_swing_scanner, [], swing_cfg))
    run(build_universe("KR", "SCALP", kr_scalp_scanner, [], scalp_cfg))
    run(build_universe("KR", "SWING", kr_swing_scanner, [], swing_cfg))

    assert us_scalp_scanner.calls[0]["top_n"] == 60
    assert us_scalp_scanner.calls[0]["min_price"] == 5.0
    assert us_swing_scanner.calls[0]["top_n"] == 120
    assert us_swing_scanner.calls[0]["min_price"] == 3.0

    assert kr_scalp_scanner.calls[0]["limit"] == 60
    assert kr_swing_scanner.calls[0]["limit"] == 120
