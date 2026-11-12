"""Trend sleeve (SPEC §5.6): 12-month time-series momentum, held through the NIFTYBEES ETF.

Signal at each month's last trading day ``M`` (after NSE's index close file, 19:30 IST):
long when the Nifty 50's price return from the last trading day 12 months earlier to ``M``
exceeds the risk-free return over the year (91-day T-bill from ``config/rules``); otherwise
flat (cash). Acted on at the next session's open. No futures: SPEC keeps Nifty futures for
≈₹15L+ of notional. Paper only; allocation is a HYPOTHESIS for the owner to set.

Purpose (SPEC): it tends to gain in sustained declines when short-vol loses. The sample of
monthly signals is tiny, so it is a structural hedge, not a measured edge.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from engine.core.calendar import TradingCalendar
from engine.data.eod.download import eod_path, read_eod_text

ETF = "NIFTYBEES"
INDEX_NAME = "nifty 50"


@dataclass(frozen=True)
class TrendSignal:
    month_end: date
    base_day: date
    index_close: float
    base_close: float
    ret_12m_pct: float
    rf_pct: float
    long: bool

    @property
    def text(self) -> str:
        side = "LONG NIFTYBEES" if self.long else "FLAT (cash)"
        return (
            f"{side}: Nifty 50 {self.ret_12m_pct:+.1f}% over 12 months to {self.month_end} "
            f"vs T-bill {self.rf_pct:.1f}%"
        )


def last_trading_day_of_month(cal: TradingCalendar, year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return cal.previous_trading_day(nxt)


def is_month_end(cal: TradingCalendar, day: date) -> bool:
    return cal.is_trading_day(day) and last_trading_day_of_month(cal, day.year, day.month) == day


def index_close(raw_dir: Path, day: date, name: str = INDEX_NAME) -> float | None:
    p = eod_path(raw_dir, "indices", day)
    if not p.exists():
        return None
    for line in read_eod_text(p).splitlines()[1:]:
        cells = [c.strip() for c in line.split(",")]
        if len(cells) > 5 and cells[0].lower() == name:
            try:
                return float(cells[5])
            except ValueError:
                return None
    return None


def tsmom_signal(
    raw_dir: Path,
    month_end: date,
    *,
    cal: TradingCalendar,
    rf_pct: float,
    fetch: Callable[[date], None] | None = None,
) -> TrendSignal | None:
    """Signal at ``month_end``; ``fetch(day)`` downloads a missing index file first."""
    y, m = (month_end.year - 1, month_end.month)
    base = last_trading_day_of_month(cal, y, m)
    for d in (month_end, base):
        if fetch is not None and index_close(raw_dir, d) is None:
            fetch(d)
    now, then = index_close(raw_dir, month_end), index_close(raw_dir, base)
    if not now or not then:
        return None
    ret = (now / then - 1.0) * 100.0
    return TrendSignal(month_end, base, now, then, round(ret, 2), rf_pct, ret > rf_pct)
