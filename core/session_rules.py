"""Session/time rule helpers for engine orchestration.

This module centralizes:
- time_rules parsing/defaulting from config
- exit/force-liquidation phase evaluation
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Dict

from utils_time import TimeRules, is_after, parse_time


DEFAULT_TIME_RULES: dict[str, str] = {
    "observe_start": "08:59:00",
    "or_start": "09:00:00",
    "or_end": "09:05:00",
    "entry_start": "09:05:05",
    "force_exit": "15:15:00",
    "early_exit": "15:00:00",
}


def _parse_time_rule_with_default(
    tr_cfg: Dict[str, Any],
    key: str,
    logger: Any,
    *,
    log_prefix: str,
) -> tuple[dt_time, bool]:
    raw = tr_cfg.get(key)
    default_raw = DEFAULT_TIME_RULES[key]
    used_default = False
    if raw is None or str(raw).strip() == "":
        raw = default_raw
        used_default = True
    try:
        return parse_time(str(raw)), used_default
    except Exception:
        logger.warning(
            "%s invalid time_rules.%s=%r; using default=%s",
            log_prefix,
            key,
            raw,
            default_raw,
        )
        return parse_time(default_raw), True


def build_time_rules(
    tr_cfg: Dict[str, Any] | None,
    logger: Any,
    *,
    log_prefix: str,
) -> tuple[TimeRules, dt_time]:
    """Resolve validated TimeRules and early_exit time from config."""
    tr = tr_cfg or {}
    defaults_used: list[str] = []

    observe_start, used = _parse_time_rule_with_default(tr, "observe_start", logger, log_prefix=log_prefix)
    if used:
        defaults_used.append("observe_start")
    or_start, used = _parse_time_rule_with_default(tr, "or_start", logger, log_prefix=log_prefix)
    if used:
        defaults_used.append("or_start")
    or_end, used = _parse_time_rule_with_default(tr, "or_end", logger, log_prefix=log_prefix)
    if used:
        defaults_used.append("or_end")
    entry_start, used = _parse_time_rule_with_default(tr, "entry_start", logger, log_prefix=log_prefix)
    if used:
        defaults_used.append("entry_start")
    force_exit, used = _parse_time_rule_with_default(tr, "force_exit", logger, log_prefix=log_prefix)
    if used:
        defaults_used.append("force_exit")
    early_exit, used = _parse_time_rule_with_default(tr, "early_exit", logger, log_prefix=log_prefix)
    if used:
        defaults_used.append("early_exit")

    rules = TimeRules(
        observe_start=observe_start,
        or_start=or_start,
        or_end=or_end,
        entry_start=entry_start,
        force_exit=force_exit,
    )
    logger.info(
        "%s time_rules resolved: observe_start=%s or_start=%s or_end=%s entry_start=%s force_exit=%s early_exit=%s",
        log_prefix,
        rules.observe_start,
        rules.or_start,
        rules.or_end,
        rules.entry_start,
        rules.force_exit,
        early_exit,
    )
    if defaults_used:
        logger.warning("%s time_rules defaults used for: %s", log_prefix, ", ".join(defaults_used))
    return rules, early_exit


def exit_phase(now_dt: datetime, early_exit: dt_time, force_exit: dt_time) -> tuple[str | None, dt_time]:
    """Return current exit phase and final emergency cutoff time.

    Phases:
    - early: at/after early_exit and before force_exit
    - force: at/after force_exit and before force_exit+3m
    - emergency: at/after force_exit+3m
    """
    force_exit_dt = now_dt.replace(
        hour=force_exit.hour,
        minute=force_exit.minute,
        second=force_exit.second,
        microsecond=0,
    )
    final_kill_dt = force_exit_dt + timedelta(minutes=3)
    final_kill_time = final_kill_dt.time()

    if is_after(final_kill_time, now_dt):
        return "emergency", final_kill_time
    if is_after(force_exit, now_dt):
        return "force", final_kill_time
    if is_after(early_exit, now_dt):
        return "early", final_kill_time
    return None, final_kill_time


@dataclass(frozen=True)
class EntryRiskBounds:
    """Explicit portfolio/symbol exposure bounds used by entry gating."""

    max_total_position_pct: float
    max_symbol_position_pct: float


def resolve_entry_risk_bounds(trading_cfg: Dict[str, Any] | None) -> EntryRiskBounds:
    """Resolve + clamp entry exposure boundaries from trading config."""
    tcfg = trading_cfg or {}
    max_total_position_pct = 0.60
    max_symbol_position_pct = 0.08
    try:
        max_total_position_pct = float(tcfg.get("max_total_position_pct", 0.60) or 0.60)
    except Exception:
        max_total_position_pct = 0.60
    try:
        max_symbol_position_pct = float(tcfg.get("max_symbol_position_pct", 0.08) or 0.08)
    except Exception:
        max_symbol_position_pct = 0.08

    return EntryRiskBounds(
        max_total_position_pct=max(0.05, min(0.95, max_total_position_pct)),
        max_symbol_position_pct=max(0.01, min(0.50, max_symbol_position_pct)),
    )
