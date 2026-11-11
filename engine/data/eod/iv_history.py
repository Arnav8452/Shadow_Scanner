"""Daily ATM implied-volatility history from NSE F&O bhavcopies, plus India VIX closes.

Why: the IV-percentile filters (SPEC §5.1, §5.8) need a year of IV history that the recorder
does not have yet. This backfill is **fidelity tier C** — end-of-day close prices (NSE's close is
the VWAP of the last 30 minutes), not recorded bid/ask mids — and every row says so.

Method per day and underlying (CLAUDE.md IV rule, adapted to EOD data):
1. Expiry: the nearest listed expiry at least ``min_trading_days`` trading days ahead (skips the
   expiry-day and day-before noise).
2. Synthetic forward: median of ``K + e^(r·t_cal)·(C − P)`` over the ``n_parity`` strikes nearest
   the underlying, using only strikes where both call and put traded that day.
3. ATM strike nearest the forward; Black-76 IV of the call and put on close prices with
   ``t_var`` from the real session clock; ATM IV = their average (each must solve).
Rows are known at 19:30 IST on the trade date (next-session use only).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from engine.analytics.expiry_clock import expiry_clock
from engine.analytics.iv import implied_vol_black76
from engine.core.calendar import TradingCalendar
from engine.core.clock import market_session
from engine.core.rules import IST, RuleBook
from engine.data.eod.bhavcopy import FnOBhavcopyRow, parse_fno_bhavcopy_csv
from engine.data.eod.download import eod_path, read_eod_text


@dataclass(frozen=True)
class DailyIV:
    trade_date: date
    underlying: str
    expiry: date
    trading_minutes: float
    spot: float
    forward: float
    atm_strike: float
    call_iv: float
    put_iv: float
    atm_iv: float
    n_parity_strikes: int
    r: float
    clock_approx: bool
    known_at: datetime
    tier: str = "C_eod_close"


def _trading_days_ahead(cal: TradingCalendar | None, start: date, end: date) -> int:
    from engine.analytics.expiry_clock import _is_trading_day

    n = 0
    d = start
    while d < end:
        d = date.fromordinal(d.toordinal() + 1)
        if _is_trading_day(cal, d)[0]:
            n += 1
    return n


def atm_iv_for_day(
    rows: Sequence[FnOBhavcopyRow],
    *,
    underlying: str,
    book: RuleBook,
    calendar: TradingCalendar | None,
    r: float,
    min_trading_days: int = 2,
    n_parity: int = 5,
) -> DailyIV | None:
    opts = [
        x
        for x in rows
        if x.symbol == underlying
        and x.option_type in ("CE", "PE")
        and x.instrument in ("IDO", "OPTIDX")
    ]
    if not opts:
        return None
    day = opts[0].trade_date
    expiries = sorted({x.expiry for x in opts if x.expiry > day})
    expiry = next(
        (e for e in expiries if _trading_days_ahead(calendar, day, e) >= min_trading_days), None
    )
    if expiry is None:
        return None
    chain = [x for x in opts if x.expiry == expiry]
    spot = next((x.underlying_price for x in chain if x.underlying_price > 0), 0.0)
    if spot <= 0:
        return None
    calls = {
        x.strike: x for x in chain if x.option_type == "CE" and x.contracts > 0 and x.close > 0
    }
    puts = {x.strike: x for x in chain if x.option_type == "PE" and x.contracts > 0 and x.close > 0}
    both = sorted(set(calls) & set(puts), key=lambda k: abs(k - spot))[:n_parity]
    if len(both) < 3:
        return None
    close_dt = market_session(book, "NSE", "fo", day).close_dt
    clk = expiry_clock(close_dt, expiry, book=book, calendar=calendar)
    growth = math.exp(r * clk.t_cal)
    forward = statistics.median(k + growth * (calls[k].close - puts[k].close) for k in both)
    atm = min(both, key=lambda k: abs(k - forward))
    kw = {"t_cal": clk.t_cal, "risk_free_rate": r}
    civ = implied_vol_black76(calls[atm].close, forward, atm, clk.t_var, is_call=True, **kw)
    piv = implied_vol_black76(puts[atm].close, forward, atm, clk.t_var, is_call=False, **kw)
    if civ is None or piv is None:
        return None
    return DailyIV(
        trade_date=day,
        underlying=underlying,
        expiry=expiry,
        trading_minutes=round(clk.trading_minutes, 1),
        spot=spot,
        forward=round(forward, 2),
        atm_strike=atm,
        call_iv=round(civ, 5),
        put_iv=round(piv, 5),
        atm_iv=round((civ + piv) / 2, 5),
        n_parity_strikes=len(both),
        r=r,
        clock_approx=clk.approx,
        # EOD files are published ~18:00-19:30: never usable before 19:30 on the trade date
        known_at=max(opts[0].known_at, datetime.combine(day, time(19, 30), IST)),
    )


def vix_close(indices_csv: str) -> float | None:
    for line in indices_csv.splitlines():
        cells = line.split(",")
        if cells and cells[0].strip().lower() == "india vix" and len(cells) > 5:
            try:
                return float(cells[5])
            except ValueError:
                return None
    return None


def build_iv_history(
    raw_dir: Path,
    days: Iterable[date],
    *,
    underlyings: Sequence[str],
    book: RuleBook,
    calendar: TradingCalendar | None,
) -> tuple[list[DailyIV], dict[date, float]]:
    ivs: list[DailyIV] = []
    vix: dict[date, float] = {}
    for d in days:
        r = book.lookup("risk_free_rate", d, allow_unverified=True, tenor="91d_tbill").as_float()
        fo = eod_path(raw_dir, "fo", d)
        if fo.exists():
            rows = parse_fno_bhavcopy_csv(read_eod_text(fo))
            for u in underlyings:
                iv = atm_iv_for_day(rows, underlying=u, book=book, calendar=calendar, r=r / 100)
                if iv is not None:
                    ivs.append(iv)
        idx = eod_path(raw_dir, "indices", d)
        if idx.exists():
            v = vix_close(read_eod_text(idx))
            if v is not None:
                vix[d] = v
    return ivs, vix


def write_iv_history(path: Path, ivs: Sequence[DailyIV], vix: dict[date, float]) -> Path:
    rows = [asdict(x) | {"vix_close": vix.get(x.trade_date)} for x in ivs]
    for d, v in vix.items():  # VIX on days without an IV row (e.g. parse failures)
        if not any(x.trade_date == d for x in ivs):
            rows.append({"trade_date": d, "underlying": "INDIAVIX", "vix_close": v})
    table = pa.Table.from_pylist(sorted(rows, key=lambda r: (r["trade_date"], r["underlying"])))
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return path


def percentile_rank(history: Sequence[float], value: float) -> float:
    """Share of ``history`` strictly below ``value`` (0-100). Use only past values (as_of)."""
    if not history:
        raise ValueError("empty history")
    return 100.0 * sum(1 for h in history if h < value) / len(history)


def iv_known_by(ivs: Sequence[DailyIV], as_of: datetime) -> list[DailyIV]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    return [x for x in ivs if x.known_at <= as_of.astimezone(IST)]
