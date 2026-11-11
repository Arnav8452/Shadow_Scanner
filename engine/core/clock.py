"""IST time helpers, an injectable clock, and market-session times from config/rules.

Session times are exchange rules, so they live in ``config/rules/market_sessions.csv``
(effective-dated, sourced) — never as constants in code. Example: NSE F&O moved its normal
close from 15:30 to 15:40 on 3 Aug 2026 (NSE/FAOP/74467).
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol

from engine.core.rules import IST, RuleBook, RuleNotFoundError

NS_PER_S = 1_000_000_000


def to_ns(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime {dt!r}; pass a timezone-aware value")
    return int(dt.timestamp()) * NS_PER_S + dt.microsecond * 1000


def from_ns(ns: int) -> datetime:
    """Epoch nanoseconds -> IST-aware datetime (microsecond precision)."""
    dt = datetime.fromtimestamp(ns // NS_PER_S, UTC).replace(microsecond=(ns % NS_PER_S) // 1000)
    return dt.astimezone(IST)


def ist_date(ns: int) -> date:
    return from_ns(ns).date()


class Clock(Protocol):
    def now_ns(self) -> int: ...


class SystemClock:
    def now_ns(self) -> int:
        return _time.time_ns()


class ManualClock:
    """Deterministic clock for tests and replays."""

    def __init__(self, start: datetime) -> None:
        self._ns = to_ns(start)

    def now_ns(self) -> int:
        return self._ns

    def set(self, dt: datetime) -> None:
        self._ns = to_ns(dt)

    def advance(self, seconds: float) -> int:
        self._ns += int(seconds * NS_PER_S)
        return self._ns


def _hhmm(value: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except ValueError as exc:
        raise ValueError(f"session time must be HH:MM, got {value!r}") from exc


@dataclass(frozen=True)
class MarketSession:
    exchange: str
    segment: str
    day: date
    normal_open: time
    normal_close: time
    pre_open_start: time | None
    pre_open_end: time | None
    verified: bool  # every row used was primary/secondary

    def at(self, t: time) -> datetime:
        return datetime.combine(self.day, t, IST)

    @property
    def open_dt(self) -> datetime:
        return self.at(self.normal_open)

    @property
    def close_dt(self) -> datetime:
        return self.at(self.normal_close)

    @property
    def first_dt(self) -> datetime:
        return self.at(self.pre_open_start or self.normal_open)

    def in_normal(self, ns: int) -> bool:
        return to_ns(self.open_dt) <= ns < to_ns(self.close_dt)


def market_session(
    book: RuleBook,
    exchange: str,
    segment: str,
    day: date,
    *,
    allow_unverified: bool = False,
) -> MarketSession:
    """Session times in force on ``day`` (pre-open fields are None where none exists)."""
    table = book["market_sessions"]
    verified = True

    def get(session: str, required: bool) -> time | None:
        nonlocal verified
        try:
            row = table.lookup(
                day,
                allow_unverified=allow_unverified,
                exchange=exchange,
                segment=segment,
                session=session,
            )
        except RuleNotFoundError:
            if required:
                raise
            return None
        verified &= row.verified
        return _hhmm(row.value)

    normal_open = get("normal_open", True)
    normal_close = get("normal_close", True)
    assert normal_open is not None and normal_close is not None
    return MarketSession(
        exchange=exchange,
        segment=segment,
        day=day,
        normal_open=normal_open,
        normal_close=normal_close,
        pre_open_start=get("pre_open_start", False),
        pre_open_end=get("pre_open_end", False),
        verified=verified,
    )


def recording_window(
    sessions: list[MarketSession], *, pre_buffer_min: int, post_buffer_min: int
) -> tuple[datetime, datetime]:
    """Union of the sessions, widened by buffers. Recording too long is always safe."""
    if not sessions:
        raise ValueError("no sessions")
    start = min(s.first_dt for s in sessions) - timedelta(minutes=pre_buffer_min)
    end = max(s.close_dt for s in sessions) + timedelta(minutes=post_buffer_min)
    return start, end
