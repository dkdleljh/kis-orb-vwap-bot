from __future__ import annotations

import inspect
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


_US_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
_KR_SYMBOL_RE = re.compile(r"^\d{6}$")

_STYLE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "SCALP": {
        "top_n": 60,
        "cap": 30,
        "scan_interval_sec": 300,
        "min_price_usd": 5.0,
        "min_price": 5.0,
        "min_liquidity": 1_000_000,
        "volatility_preference": "high",
    },
    "SWING": {
        "top_n": 120,
        "cap": 60,
        "scan_interval_sec": 600,
        "min_price_usd": 3.0,
        "min_price": 3.0,
        "min_liquidity": 500_000,
        "volatility_preference": "stable_trend",
    },
}


def resolve_universe_config(style: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    style_key = (style or "SCALP").upper()
    merged = dict(_STYLE_DEFAULTS.get(style_key, _STYLE_DEFAULTS["SCALP"]))
    merged.update(config or {})
    if "max_symbols" in merged and "cap" not in merged:
        merged["cap"] = merged["max_symbols"]
    if "cap" in merged and "max_symbols" not in merged:
        merged["max_symbols"] = merged["cap"]
    return merged


def style_defaults(style: str) -> Dict[str, Any]:
    return dict(_STYLE_DEFAULTS.get((style or "SCALP").upper(), _STYLE_DEFAULTS["SCALP"]))


def _extract_symbol_and_meta(candidate: Any) -> Tuple[str, Dict[str, Any]]:
    if isinstance(candidate, str):
        return candidate, {}
    if isinstance(candidate, dict):
        symbol = (
            candidate.get("symbol")
            or candidate.get("pdno")
            or candidate.get("mksc_shrn_iscd")
            or candidate.get("code")
            or ""
        )
        return str(symbol or ""), candidate
    return str(candidate or ""), {}


def _is_valid_symbol(market: str, symbol: str, meta: Dict[str, Any], exclude_spac: bool) -> bool:
    if market == "US":
        s = symbol.strip().upper()
        return bool(_US_SYMBOL_RE.match(s))

    s = symbol.strip()
    if not _KR_SYMBOL_RE.match(s):
        return False
    if exclude_spac:
        name = str(meta.get("name") or meta.get("hts_kor_isnm") or "")
        if "스팩" in name or "SPAC" in name.upper():
            return False
    return True


def _normalize_symbol(market: str, symbol: str) -> str:
    s = symbol.strip()
    if market == "US":
        return s.upper()
    return s


def _quality_pass(market: str, meta: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    min_liquidity = float(cfg.get("min_liquidity", 0) or 0)
    min_price = float(cfg.get("min_price_usd" if market == "US" else "min_price", 0) or 0)

    if min_price > 0:
        price_fields = ("price", "last", "lastxch", "stck_prpr", "stck_prpr_unpr", "prpr")
        price = 0.0
        for k in price_fields:
            v = meta.get(k)
            if v in (None, ""):
                continue
            try:
                price = float(v)
                break
            except (TypeError, ValueError):
                continue
        if price > 0 and price < min_price:
            return False

    if min_liquidity > 0:
        liquidity_fields = (
            "trading_value",
            "trade_value",
            "acml_tr_pbmn",
            "acc_trdval",
            "volume_value",
            "volume",
            "acml_vol",
        )
        liquidity = 0.0
        for k in liquidity_fields:
            v = meta.get(k)
            if v in (None, ""):
                continue
            try:
                liquidity = float(v)
                break
            except (TypeError, ValueError):
                continue
        if liquidity > 0 and liquidity < min_liquidity:
            return False

    return True


def _volatility_score(meta: Dict[str, Any], preference: str) -> float:
    volatility_fields = ("volatility", "atr_pct", "change_pct", "rate", "chg_rate")
    v = 0.0
    for k in volatility_fields:
        raw = meta.get(k)
        if raw in (None, ""):
            continue
        try:
            v = abs(float(raw))
            break
        except (TypeError, ValueError):
            continue

    trend_fields = ("trend_score", "trend_stability", "stability", "ma_alignment")
    trend = 0.0
    for k in trend_fields:
        raw = meta.get(k)
        if raw in (None, ""):
            continue
        try:
            trend = float(raw)
            break
        except (TypeError, ValueError):
            continue

    if preference == "stable_trend":
        return trend - (v * 0.25)
    return v + (trend * 0.1)


async def _scan_candidates(
    market: str,
    scanner: Any,
    cfg: Dict[str, Any],
) -> List[Any]:
    if scanner is None:
        return []

    top_n = int(cfg.get("top_n", 0) or 0)

    method = None
    kwargs: Dict[str, Any] = {}
    if market == "US" and hasattr(scanner, "scan"):
        method = scanner.scan
        kwargs = {
            "top_n": top_n,
            "exchanges": cfg.get("exchanges", ["NASD", "NYSE"]),
            "min_price": float(cfg.get("min_price_usd", 0) or 0),
        }
    elif hasattr(scanner, "get_top_trading_value"):
        method = scanner.get_top_trading_value
        kwargs = {
            "limit": top_n,
            "market_div_code": cfg.get("market_div_code", "J"),
        }

    if method is None:
        return []

    sig = inspect.signature(method)
    call_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    result = method(**call_kwargs)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, list):
        return result
    return []


async def build_universe(
    market: str,
    style: str,
    scanner: Any,
    base_symbols: Iterable[str],
    config: Optional[Dict[str, Any]],
) -> List[str]:
    market_key = (market or "").upper()
    style_key = (style or "SCALP").upper()
    cfg = resolve_universe_config(style_key, config)
    exclude_spac = bool(cfg.get("exclude_spac", False))

    scanned = await _scan_candidates(market_key, scanner, cfg)
    candidates: List[Any] = list(scanned) if scanned else list(base_symbols or [])

    # (a)(b) 후보 생성 + 정규화/필터
    normalized: List[Tuple[str, Dict[str, Any]]] = []
    for raw in candidates:
        symbol_raw, meta = _extract_symbol_and_meta(raw)
        if not _is_valid_symbol(market_key, symbol_raw, meta, exclude_spac):
            continue
        symbol = _normalize_symbol(market_key, symbol_raw)
        if not _quality_pass(market_key, meta, cfg):
            continue
        normalized.append((symbol, meta))

    preference = str(cfg.get("volatility_preference", "high" if style_key == "SCALP" else "stable_trend"))
    normalized.sort(key=lambda item: _volatility_score(item[1], preference), reverse=True)

    # (c) always_include 우선
    always_include = cfg.get("always_include", []) or []
    prioritized: List[Tuple[str, Dict[str, Any]]] = []
    for raw in always_include:
        symbol_raw, meta = _extract_symbol_and_meta(raw)
        if not _is_valid_symbol(market_key, symbol_raw, meta, exclude_spac):
            continue
        symbol = _normalize_symbol(market_key, symbol_raw)
        prioritized.append((symbol, meta))

    prioritized.extend(normalized)

    # (d) 중복 제거
    deduped: List[str] = []
    seen = set()
    for symbol, _ in prioritized:
        if symbol in seen:
            continue
        seen.add(symbol)
        deduped.append(symbol)

    # (e) cap 적용
    cap = int(cfg.get("cap", cfg.get("max_symbols", 0)) or 0)
    if cap > 0:
        deduped = deduped[:cap]

    # (f) 품질 필터는 상단 quality_pass로 best-effort 적용
    return deduped
