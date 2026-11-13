"""Time to expiry from the real session times and holiday calendar (CLAUDE.md IV rule).

* ``t_cal`` — calendar years to the expiry-day close, for discounting.
* ``t_var`` — variance time to the expiry-day close (SPEC §4): trading minutes, plus each
  overnight and each weekend/holiday day weighted by the variance it carries in units of one
  session (``set_variance_weights``; calibrated from index daily candles in
  ``engine/analytics/variance_clock.py``), divided by a year measured the same way
  (252 sessions with their overnights + ~113 non-trading days). With both weights 0 it is the
  plain trading-minute clock.

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
NON_TRADING_DAYS_PER_YEAR = 113.0  # ~104 weekend days + ~9-12 exchange holidays

# (overnight, non-trading day) variance, each in units of one trading session's variance.
_WEIGHTS: tuple[float, float] = (0.0, 0.0)


def set_variance_weights(overnight: float, non_trading_day: float) -> None:
    """Set the variance clock (settings ``analytics.variance_clock``); 0, 0 = trading minutes."""
    global _WEIGHTS
    if overnight < 0 or non_trading_day < 0:
        raise ValueError("variance weights must be >= 0")
    _WEIGHTS = (float(overnight), float(non_trading_day))


def variance_weights() -> tuple[float, float]:
    return _WEIGHTS


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
    nights = 0  # overnights still ahead (after each trading day's close, before the expiry day)
    off_days = 0  # weekend / holiday days still ahead
    d = now.date()
    while d <= expiry:
        trading, guessed = _is_trading_day(calendar, d)
        approx |= guessed
        if trading:
            s = market_session(book, exchange, segment, d, allow_unverified=allow)
            start = max(now, s.open_dt) if d == now.date() else s.open_dt
            minutes += max(0.0, (s.close_dt - start).total_seconds() / 60.0)
            if d < expiry:
                nights += 1
        elif d < expiry:
            off_days += 1
        d += timedelta(days=1)
    minutes = max(minutes, min_minutes)
    session_len = (exp_sess.close_dt - exp_sess.open_dt).total_seconds() / 60.0
    w_on, w_nt = _WEIGHTS
    eff = minutes + session_len * (w_on * nights + w_nt * off_days)
    year = session_len * (TRADING_DAYS_PER_YEAR * (1.0 + w_on) + NON_TRADING_DAYS_PER_YEAR * w_nt)
    t_var = eff / year
    return ExpiryClock(t_cal, t_var, minutes, close, approx)
