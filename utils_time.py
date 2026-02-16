from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo


@dataclass
class TimeRules:
    observe_start: time
    or_start: time
    or_end: time
    entry_start: time
    force_exit: time


def parse_time(s: str) -> time:
    return datetime.strptime(s, "%H:%M:%S").time()


def now_local(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def is_after(t: time, now_dt: datetime) -> bool:
    return now_dt.time() >= t


def is_between(start: time, end: time, now_dt: datetime) -> bool:
    return start <= now_dt.time() < end
