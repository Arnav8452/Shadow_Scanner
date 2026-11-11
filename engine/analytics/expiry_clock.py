"""Time to expiry from the real session times and holiday calendar (CLAUDE.md IV rule).

* ``t_cal`` — calendar years to the expiry-day close, for discounting.
* ``t_var`` — trading minutes left until the expiry-day close, divided by a trading year of
  ``252 x`` the expiry day's session length. Overnight, weekend and holiday time carries no
  variance in this clock (the simplest variance clock; SPEC v2 may weight overnight).

Session times come from ``config/rules/market_sessions.csv``, so the 15:30 → 15:40 close change
on 3 Aug 2026 (NSE/FAOP/74467) is applied by date. Days in a year whose holidays are not loaded
fall back to weekdays-only and the result is flagged ``approx``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from engine.core.calendar import CalendarCoverageError, TradingCalendar
from engine.core.clock import market_session
from engine.core.rules import RuleBook

TRADING_DAYS_PER_YEAR = 252.0


@dataclass(frozen=True)
class ExpiryClock:
    t_cal: float
    t_var: float
    trading_minutes: float
    expiry_close: datetime
    approx: bool  # True when a holiday calendar was missing for some day


def _is_trading_day(cal: TradingCalendar | None, d: date) -> tuple[bool, bool]:
    """(is trading day, approximated)."""
    if cal is not None:
        try:
            return cal.is_trading_day(d), False
        except CalendarCoverageError:
            pass
    return d.weekday() < 5, True


def expiry_clock(
    now: datetime,
    expiry: date,
    *,
    book: RuleBook,
    calendar: TradingCalendar | None,
    exchange: str = "NSE",
    segment: str = "fo",
    min_minutes: float = 15.0,
) -> ExpiryClock:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    allow = exchange != "NSE"  # BSE session rows are UNVERIFIED; they only shape the clock
    exp_sess = market_session(book, exchange, segment, expiry, allow_unverified=allow)
    close = exp_sess.close_dt
    t_cal = max((close - now).total_seconds(), min_minutes * 60) / (365.0 * 86400.0)
    approx = False
    minutes = 0.0
    d = now.date()
    while d <= expiry:
        trading, guessed = _is_trading_day(calendar, d)
        approx |= guessed
        if trading:
            s = market_session(book, exchange, segment, d, allow_unverified=allow)
            start = max(now, s.open_dt) if d == now.date() else s.open_dt
            minutes += max(0.0, (s.close_dt - start).total_seconds() / 60.0)
        d += timedelta(days=1)
    minutes = max(minutes, min_minutes)
    session_len = (exp_sess.close_dt - exp_sess.open_dt).total_seconds() / 60.0
    t_var = minutes / (TRADING_DAYS_PER_YEAR * session_len)
    return ExpiryClock(t_cal, t_var, minutes, close, approx)
