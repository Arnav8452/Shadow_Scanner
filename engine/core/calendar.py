"""Exchange trading-day calendar built from ``config/rules/holidays.csv``.

A year with no holiday rows is treated as *not covered*: asking about it raises
:class:`CalendarCoverageError` instead of silently treating every weekday as a trading day.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from engine.core.rules import RuleBook


class CalendarCoverageError(LookupError):
    """The holiday list for this year has not been loaded."""


class TradingCalendar:
    def __init__(
        self,
        holidays: Iterable[date],
        *,
        covered_years: Iterable[int] | None = None,
        special_sessions: Iterable[date] = (),
        name: str = "NSE",
    ) -> None:
        self._holidays = frozenset(holidays)
        self._special = frozenset(special_sessions)
        years = covered_years if covered_years is not None else {d.year for d in self._holidays}
        self.covered_years = frozenset(years)
        self.name = name

    @classmethod
    def from_rulebook(
        cls,
        book: RuleBook,
        *,
        exchange: str = "NSE",
        segment: str = "cm",
        allow_unverified: bool = False,
    ) -> TradingCalendar:
        """Rows with unit ``holiday`` close a weekday; ``special_session`` opens a weekend day
        (e.g. Muhurat trading)."""
        rows = [r for r in book["holidays"] if r.keys == {"exchange": exchange, "segment": segment}]
        if not allow_unverified:
            bad = [r for r in rows if not r.verified]
            if bad:
                raise ValueError(f"{len(bad)} UNVERIFIED holiday rows for {exchange}/{segment}")
        unknown = {r.unit for r in rows} - {"holiday", "special_session"}
        if unknown:
            raise ValueError(f"holidays.csv: unknown unit(s) {sorted(unknown)}")
        holidays = [r.effective_from for r in rows if r.unit == "holiday"]
        special = [r.effective_from for r in rows if r.unit == "special_session"]
        return cls(holidays, special_sessions=special, name=f"{exchange}/{segment}")

    def _check(self, d: date) -> None:
        if d.year not in self.covered_years:
            raise CalendarCoverageError(
                f"{self.name}: holidays for {d.year} not loaded "
                "(add them to config/rules/holidays.csv from the exchange circular)"
            )

    def is_trading_day(self, d: date) -> bool:
        if d in self._special:
            return True
        self._check(d)
        return d.weekday() < 5 and d not in self._holidays

    def previous_trading_day(self, d: date) -> date:
        cur = d - timedelta(days=1)
        while not self.is_trading_day(cur):
            cur -= timedelta(days=1)
        return cur

    def next_trading_day(self, d: date) -> date:
        cur = d + timedelta(days=1)
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur

    def add_trading_days(self, d: date, n: int) -> date:
        cur = d
        step = self.next_trading_day if n >= 0 else self.previous_trading_day
        for _ in range(abs(n)):
            cur = step(cur)
        return cur

    def trading_days_between(self, start: date, end: date) -> int:
        """Trading days in the half-open interval (start, end]."""
        if end <= start:
            return 0
        count, cur = 0, start
        while cur < end:
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                count += 1
        return count

    def last_cum_date(self, record_date: date, settlement_days: int = 1) -> date:
        """Last day to BUY and still be on the register on ``record_date``.

        Under T+1 a purchase settles the next trading day, so the last buy day is the
        trading day ``settlement_days`` before the record date (ex-date == record date).
        """
        return self.add_trading_days(record_date, -settlement_days)
