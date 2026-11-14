"""IST time helpers, an injectable clock, and market-session times from config/rules.

Session times are exchange rules, so they live in ``config/rules/market_sessions.csv``
(effective-dated, sourced) — never as constants in code. Example: NSE F&O moved its normal
close from 15:30 to 15:40 on 3 Aug 2026 (NSE/FAOP/74467) and to 15:45 on 7 Dec 2026
(NSE/FAOP/77356), when expiring index contracts also began to stop at 15:30 on their expiry day.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
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
    # Expiring index derivatives stop trading at this time on their expiry day (15:30 from
    # 7 Dec 2026); None where no rule row exists, meaning the normal close.
    index_expiry_close: time | None = None
    # Expiry-day final settlement averages the underlying over this window (15:00-15:30 from
    # 7 Dec 2026); None where settlement is not a VWAP (3 Aug-6 Dec 2026: CAS closing price).
    settle_vwap_start: time | None = None
    settle_vwap_end: time | None = None
    # Start of the daily close-price VWAP window, which ends at the normal close.
    close_vwap_start: time | None = None

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

    def expiry_close(self, *, index: bool) -> time:
        """When a contract expiring on ``day`` stops trading (index or stock derivative)."""
        if index and self.index_expiry_close is not None:
            return self.index_expiry_close
        return self.normal_close

    def expiry_close_dt(self, *, index: bool) -> datetime:
        return self.at(self.expiry_close(index=index))

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
        index_expiry_close=get("index_expiry_close", False),
        settle_vwap_start=get("settle_vwap_start", False),
        settle_vwap_end=get("settle_vwap_end", False),
        close_vwap_start=get("close_vwap_start", False),
    )


def expiry_close_fn(
    book: RuleBook, segment: str = "fo", *, allow_unverified: bool = True
) -> Callable[[str, date, bool], time]:
    """``(exchange, expiry, index) -> time`` a contract expiring that day stops trading.

    For instrument masters: each contract takes the rule in force on its own expiry date
    (an index contract on its expiry day stops at 15:30 from 7 Dec 2026, a stock one at 15:45).
    UNVERIFIED rows are allowed by default (BSE's session rows are broker-master only).
    """
    cache: dict[tuple[str, date, bool], time] = {}

    def close_for(exchange: str, expiry: date, index: bool) -> time:
        key = (exchange, expiry, index)
        if key not in cache:
            s = market_session(book, exchange, segment, expiry, allow_unverified=allow_unverified)
            cache[key] = s.expiry_close(index=index)
        return cache[key]

    return close_for


def recording_window(
    sessions: list[MarketSession], *, pre_buffer_min: int, post_buffer_min: int
) -> tuple[datetime, datetime]:
    """Union of the sessions, widened by buffers. Recording too long is always safe."""
    if not sessions:
        raise ValueError("no sessions")
    start = min(s.first_dt for s in sessions) - timedelta(minutes=pre_buffer_min)
    end = max(s.close_dt for s in sessions) + timedelta(minutes=post_buffer_min)
    return start, end
