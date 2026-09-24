"""Conservative automatic-order windows for Shanghai-listed securities."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


def market_timezone(market: str) -> ZoneInfo:
    normalized = str(market).upper()
    if normalized == "CN":
        return SHANGHAI
    if normalized == "US":
        return NEW_YORK
    raise ValueError(f"unsupported trading market: {market}")


def market_now(market: str) -> datetime:
    return datetime.now(market_timezone(market))


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def is_submission_window(moment: datetime, market: str = "CN") -> bool:
    if moment.tzinfo is None:
        raise ValueError("submission clock must be timezone-aware")
    normalized = str(market).upper()
    clock = moment.astimezone(market_timezone(normalized)).time().replace(tzinfo=None)
    if normalized == "US":
        return time(9, 30) <= clock < time(16)
    morning = time(9, 30) <= clock <= time(11, 30)
    afternoon = time(13, 0) <= clock < time(14, 57)
    return morning or afternoon
