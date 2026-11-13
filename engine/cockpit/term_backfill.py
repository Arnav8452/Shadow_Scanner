"""Backfill the calendar strategy's term-spread history (spec C) from synchronous intraday
option candles, instead of waiting 10 sessions (owner, 13 Nov 2026).

The bhavcopy route was dropped because a far monthly's daily close can be hours stale. Here each
past session uses Upstox 1-minute candles of the near and far expiries' ATM calls and puts:

- **Near:** the first Nifty expiry on or after the session. **Far:** the first monthly expiry
  (the last of its calendar month) at least 14 days away, which is what the live chain's
  14-day rule picks among the recorded expiries.
- **Prices:** for every minute from 14:30 to 15:25 in which both the call and the put at a
  strike traded, that minute's closes. Strikes: the three nearest the index at 15:00.
- **IV:** synthetic forward ``F = K + e^(rT)(C - P)`` at the strike where C and P are closest,
  Black-76 on the variance clock, mean of call and put; the session's value is the median over
  the minutes. Spread = near ATM IV - far ATM IV, in vol points.
- **Contracts:** expired ones through ``/v2/expired-instruments`` (Upstox Plus), live ones
  through the v3 historical candle API.

Last trades are not mids: a minute's close can sit anywhere in its spread. The median over ~55
minutes and the parity strike keep that noise small; the live strategy keeps using mids.
"""

from __future__ import annotations

import math
import statistics
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from engine.analytics.expiry_clock import expiry_clock
from engine.analytics.iv import implied_vol_black76
from engine.core.rules import IST, RuleBook

Candles = list[list[Any]]
API = "https://api.upstox.com"


def monthly_expiries(expiries: Sequence[date]) -> list[date]:
    """The last expiry of each calendar month."""
    last: dict[tuple[int, int], date] = {}
    for e in sorted(expiries):
        last[(e.year, e.month)] = e
    return sorted(last.values())


def pick_expiries(
    day: date, expiries: Sequence[date], min_far_days: int = 14
) -> tuple[date, date] | None:
    near = next((e for e in sorted(expiries) if e >= day), None)
    far = next((e for e in monthly_expiries(expiries) if (e - day).days >= min_far_days), None)
    return (near, far) if near is not None and far is not None and far > near else None


def _closes(candles: Candles, start: time, end: time) -> dict[time, float]:
    out: dict[time, float] = {}
    for c in candles:
        t = datetime.fromisoformat(str(c[0])).astimezone(IST).time()
        if start <= t <= end and float(c[4]) > 0 and float(c[5] or 0) > 0:
            out[t] = float(c[4])
    return out


def atm_iv(
    day: date,
    expiry: date,
    pairs: Mapping[float, tuple[Candles, Candles]],
    *,
    book: RuleBook,
    calendar: Any,
    r: float,
    start: time = time(14, 30),
    end: time = time(15, 25),
) -> float | None:
    """Median ATM IV (%) over the window from per-strike (call candles, put candles)."""
    per_min: dict[time, dict[float, tuple[float, float]]] = {}
    for k, (cc, pc) in pairs.items():
        cm, pm = _closes(cc, start, end), _closes(pc, start, end)
        for t in cm.keys() & pm.keys():
            per_min.setdefault(t, {})[k] = (cm[t], pm[t])
    ivs: list[float] = []
    for t, quotes in sorted(per_min.items()):
        k, (c, p) = min(quotes.items(), key=lambda x: abs(x[1][0] - x[1][1]))
        at = datetime.combine(day, t, tzinfo=IST) + timedelta(seconds=59)
        try:
            clk = expiry_clock(at, expiry, book=book, calendar=calendar)
        except Exception:
            continue
        f = k + math.exp(r * clk.t_cal) * (c - p)
        kw: dict[str, Any] = {"t_cal": clk.t_cal, "risk_free_rate": r}
        a = implied_vol_black76(c, f, k, clk.t_var, is_call=True, **kw)
        b = implied_vol_black76(p, f, k, clk.t_var, is_call=False, **kw)
        got = [x for x in (a, b) if x]
        if got:
            ivs.append(statistics.fmean(got) * 100)
    return statistics.median(ivs) if len(ivs) >= 5 else None


class UpstoxOptions:
    """Contract lookup and 1-minute candles for live and expired Nifty options."""

    def __init__(
        self,
        token: str,
        http: Callable[[str, str], dict[str, Any]],
        live: Mapping[date, Mapping[tuple[float, str], str]],
        index_key: str = "NSE_INDEX|Nifty 50",
    ) -> None:
        self.token, self.http, self.live = token, http, live
        self.key = urllib.parse.quote(index_key, safe="")
        self._expired: dict[date, dict[tuple[float, str], str]] = {}

    def expired_expiries(self) -> list[date]:
        doc = self.http(
            f"{API}/v2/expired-instruments/expiries?instrument_key={self.key}", self.token
        )
        return [date.fromisoformat(str(x)) for x in doc.get("data") or []]

    def contracts(self, expiry: date) -> Mapping[tuple[float, str], str]:
        if expiry in self.live:
            return self.live[expiry]
        if expiry not in self._expired:
            doc = self.http(
                f"{API}/v2/expired-instruments/option/contract?instrument_key={self.key}"
                f"&expiry_date={expiry.isoformat()}",
                self.token,
            )
            self._expired[expiry] = {
                (float(c["strike_price"]), str(c["instrument_type"])): str(c["instrument_key"])
                for c in doc.get("data") or []
            }
        return self._expired[expiry]

    def candles(self, expiry: date, key: str, day: date) -> Candles:
        k, d = urllib.parse.quote(key, safe=""), day.isoformat()
        if expiry in self.live:
            url = f"{API}/v3/historical-candle/{k}/minutes/1/{d}/{d}"
        else:
            url = f"{API}/v2/expired-instruments/historical-candle/{k}/1minute/{d}/{d}"
        return list((self.http(url, self.token).get("data") or {}).get("candles") or [])


def risk_free(book: RuleBook, at: datetime) -> float:
    """91-day T-bill rate as a fraction (config/rules), as the live analytics use."""
    for allow in (False, True):
        try:
            row = book.lookup("risk_free_rate", at, allow_unverified=allow, tenor="91d_tbill")
            return float(row.as_float()) / 100.0
        except Exception:
            continue
    return 0.0


def spot_at(candles: Candles, at: time) -> float | None:
    """The index's 1-minute close at ``at`` (or the last one before it)."""
    best = None
    for c in sorted(candles, key=lambda c: str(c[0])):
        if datetime.fromisoformat(str(c[0])).astimezone(IST).time() <= at:
            best = float(c[4])
    return best


def term_spread_day(
    day: date,
    spot_at_3: float,
    src: UpstoxOptions,
    expiries: Sequence[date],
    *,
    book: RuleBook,
    calendar: Any,
    r: float,
) -> dict[str, Any] | None:
    pick = pick_expiries(day, expiries)
    if pick is None:
        return None
    ivs: list[float] = []
    for exp in pick:
        cons = src.contracts(exp)
        strikes = sorted({k for k, _ in cons}, key=lambda k: abs(k - spot_at_3))[:3]
        pairs = {}
        for k in strikes:
            ce, pe = cons.get((k, "CE")), cons.get((k, "PE"))
            if ce and pe:
                pairs[k] = (src.candles(exp, ce, day), src.candles(exp, pe, day))
        iv = atm_iv(day, exp, pairs, book=book, calendar=calendar, r=r)
        if iv is None:
            return None
        ivs.append(iv)
    return {
        "day": day.isoformat(),
        "spread": round(ivs[0] - ivs[1], 2),
        "near": pick[0].isoformat(),
        "far": pick[1].isoformat(),
        "near_iv": round(ivs[0], 2),
        "far_iv": round(ivs[1], 2),
        "source": "upstox_1m_14:30-15:25",
    }


__all__ = [
    "UpstoxOptions",
    "atm_iv",
    "monthly_expiries",
    "pick_expiries",
    "risk_free",
    "spot_at",
    "term_spread_day",
]
