"""Point-in-time market state from 1-minute candles.

Convention: a decision is taken when bar ``t`` completes (``t + 1 min``) and may use only bars
that started at or before ``t``; its orders fill at the open of the first bar that starts
after ``t``. Nothing here reads a later bar for a decision.

IV follows CLAUDE.md: synthetic forward ``F = K + e^(rT)(C - P)`` as the median over 3-5
near-ATM strikes, Black-76 on the variance clock (``expiry_clock``), ``r`` from the T-bill
table. Prices are last trades (no mids exist), taken only when both legs of a strike traded
within ``stale_iv_min`` minutes of the decision.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from engine.analytics.expiry_clock import expiry_clock
from engine.analytics.forward import StrikePairQuote, compute_synthetic_forward
from engine.analytics.greeks import compute_black76_greeks
from engine.analytics.iv import implied_vol_black76
from engine.backtest.candles.data import Bar, CandleSource, Contract
from engine.cockpit.term_backfill import risk_free
from engine.core.rules import IST, RuleBook

MINUTE = timedelta(minutes=1)


class Series:
    """One contract's bars with minute lookups."""

    def __init__(self, bars: Sequence[Bar]) -> None:
        self.bars = list(bars)
        self.starts = [b.start for b in self.bars]

    def last(self, at: datetime, max_age_min: float | None = None) -> Bar | None:
        """The latest bar that started at or before ``at`` (optionally not older than)."""
        i = bisect.bisect_right(self.starts, at) - 1
        if i < 0:
            return None
        b = self.bars[i]
        if max_age_min is not None and (at - b.start) > timedelta(minutes=max_age_min):
            return None
        return b

    def next_after(self, at: datetime, within_min: float | None = None) -> Bar | None:
        """The first bar that starts after ``at`` (optionally within ``within_min``)."""
        i = bisect.bisect_right(self.starts, at)
        if i >= len(self.bars):
            return None
        b = self.bars[i]
        if within_min is not None and (b.start - at) > timedelta(minutes=within_min):
            return None
        return b

    def between(self, after: datetime, upto: datetime) -> list[Bar]:
        i = bisect.bisect_right(self.starts, after)
        j = bisect.bisect_right(self.starts, upto)
        return self.bars[i:j]


@dataclass(frozen=True)
class FwdIv:
    at: datetime
    expiry: date
    spot: float
    forward: float
    atm_strike: float
    atm_iv: float  # fraction
    t_var: float
    t_cal: float
    r: float
    strikes_used: int
    approx_clock: bool


@dataclass
class Market:
    src: CandleSource
    book: RuleBook
    calendar: Any
    stale_iv_min: float = 2.0
    min_parity_strikes: int = 3
    _series: dict[str, Series] = field(default_factory=dict)
    _daily_iv: dict[date, float | None] = field(default_factory=dict)

    # ---------------------------------------------------------------- lookups
    def series(self, c: Contract) -> Series:
        s = self._series.get(c.key)
        if s is None:
            s = Series(self.src.option_bars(c))
            self._series[c.key] = s
        return s

    def nearest_expiry(self, day: date) -> date | None:
        return next((e for e in self.src.expiries() if e >= day), None)

    def strikes(self, expiry: date, kind: str) -> list[float]:
        return sorted(k for k, kd in self.src.contracts(expiry) if kd == kind)

    def contract(self, expiry: date, strike: float, kind: str) -> Contract | None:
        return self.src.contracts(expiry).get((float(strike), kind))

    def near(self, expiry: date, kind: str, level: float, n: int) -> list[float]:
        return sorted(self.strikes(expiry, kind), key=lambda k: abs(k - level))[:n]

    def close(self, c: Contract, at: datetime, max_age_min: float | None) -> float | None:
        b = self.series(c).last(at, max_age_min)
        return b.close if b is not None and b.close > 0 else None

    # ------------------------------------------------------------- forward/IV
    def forward_iv(self, at: datetime, expiry: date, spot: float, n: int = 5) -> FwdIv | None:
        """Synthetic forward and ATM IV from the strikes nearest ``spot`` at bar ``at``."""
        cons = self.src.contracts(expiry)
        strikes = sorted({k for k, _ in cons}, key=lambda k: abs(k - spot))[: n + 2]
        try:
            clk = expiry_clock(
                at + timedelta(seconds=59), expiry, book=self.book, calendar=self.calendar
            )
        except Exception:
            return None
        r = risk_free(self.book, at)
        pairs = []
        for k in strikes:
            ce, pe = cons.get((k, "CE")), cons.get((k, "PE"))
            if ce is None or pe is None:
                continue
            c = self.close(ce, at, self.stale_iv_min)
            p = self.close(pe, at, self.stale_iv_min)
            if c and p:
                pairs.append(StrikePairQuote(k, c, c, p, p))
        if len(pairs) < self.min_parity_strikes:
            return None
        fwd = compute_synthetic_forward(
            pairs, spot=spot, t_cal_years=clk.t_cal, risk_free_rate=r, max_strikes=n
        )
        if len(fwd.strikes_used) < self.min_parity_strikes:
            return None
        f = fwd.forward
        atm = min(pairs, key=lambda q: abs(q.strike - f))
        kw: dict[str, Any] = {"t_cal": clk.t_cal, "risk_free_rate": r}
        ivs = [
            v
            for v in (
                implied_vol_black76(atm.call_mid, f, atm.strike, clk.t_var, is_call=True, **kw),
                implied_vol_black76(atm.put_mid, f, atm.strike, clk.t_var, is_call=False, **kw),
            )
            if v
        ]
        if not ivs:
            return None
        return FwdIv(
            at, expiry, spot, f, atm.strike, statistics.fmean(ivs), clk.t_var, clk.t_cal, r,
            len(fwd.strikes_used), clk.approx,
        )  # fmt: skip

    def iv_delta(
        self, c: Contract, at: datetime, fi: FwdIv, max_age_min: float
    ) -> tuple[float, float, float] | None:
        """(price, IV, delta) of ``c`` from its own last trade at ``at``."""
        px = self.close(c, at, max_age_min)
        if not px:
            return None
        is_call = c.kind == "CE"
        iv = implied_vol_black76(
            px, fi.forward, c.strike, fi.t_var, is_call=is_call, t_cal=fi.t_cal, risk_free_rate=fi.r
        )
        if not iv:
            return None
        g = compute_black76_greeks(
            fi.forward, c.strike, fi.t_var, iv, is_call=is_call, t_cal=fi.t_cal, risk_free_rate=fi.r
        )
        return px, iv, g.delta

    def delta_strike_estimate(self, fi: FwdIv, kind: str, target: float) -> float:
        """Strike whose Black-76 |delta| is ``target`` at the ATM IV (a start for the search)."""
        sd = fi.atm_iv * math.sqrt(max(fi.t_var, 1e-9))
        # call: N(d1) = target; put: N(d1) - 1 = -target; ln(F/K) = d1 sd - sd^2 / 2
        p = target if kind == "CE" else 1.0 - target
        d1 = statistics.NormalDist().inv_cdf(min(max(p, 1e-6), 1 - 1e-6))
        return fi.forward * math.exp(-d1 * sd + 0.5 * sd * sd)

    def pick_by_delta(
        self,
        expiry: date,
        kind: str,
        target: float,
        fi: FwdIv,
        at: datetime,
        *,
        max_age_min: float,
        n: int = 7,
        otm_only: bool = False,
    ) -> tuple[Contract, float, float] | None:
        """Contract of ``kind`` whose own-IV |delta| is closest to ``target``: (contract,
        price, delta). Candidates: the ``n`` listed strikes nearest the Black-76 estimate."""
        est = self.delta_strike_estimate(fi, kind, target)
        best: tuple[float, Contract, float, float] | None = None
        for k in self.near(expiry, kind, est, n):
            if otm_only and (
                (kind == "PE" and k >= fi.forward) or (kind == "CE" and k <= fi.forward)
            ):
                continue
            c = self.contract(expiry, k, kind)
            if c is None:
                continue
            got = self.iv_delta(c, at, fi, max_age_min)
            if got is None:
                continue
            px, _iv, d = got
            err = abs(abs(d) - target)
            if best is None or err < best[0]:
                best = (err, c, px, d)
        return (best[1], best[2], best[3]) if best else None

    def daily_atm_iv(
        self,
        day: date,
        *,
        start: time = time(14, 45),
        end: time = time(15, 15),
        step_min: int = 5,
    ) -> float | None:
        """The session's closing ATM IV (median over ``start``-``end``), nearest expiry."""
        if day in self._daily_iv:
            return self._daily_iv[day]
        self._daily_iv[day] = self._daily_atm_iv(day, start, end, step_min)
        return self._daily_iv[day]

    def _daily_atm_iv(self, day: date, start: time, end: time, step_min: int) -> float | None:
        exp = self.nearest_expiry(day)
        bars = self.src.index_bars(day)
        if exp is None or not bars:
            return None
        idx = Series(bars)
        ivs = []
        t = datetime.combine(day, start, tzinfo=IST)
        stop = datetime.combine(day, end, tzinfo=IST)
        while t <= stop:
            b = idx.last(t, 2)
            if b is not None:
                fi = self.forward_iv(t, exp, b.close)
                if fi is not None:
                    ivs.append(fi.atm_iv)
            t += timedelta(minutes=step_min)
        return statistics.median(ivs) if len(ivs) >= 3 else None


@dataclass
class IvHistory:
    """Closing ATM IVs by session; ``percentile`` uses only sessions before ``day``."""

    values: dict[date, float] = field(default_factory=dict)
    lookback: int = 252
    min_history: int = 20

    def add(self, day: date, iv: float | None) -> None:
        if iv is not None:
            self.values[day] = iv

    def percentile(self, day: date, iv: float) -> tuple[float | None, int]:
        hist = [v for d, v in sorted(self.values.items()) if d < day][-self.lookback :]
        if len(hist) < self.min_history:
            return None, len(hist)
        return 100.0 * sum(1 for h in hist if h < iv) / len(hist), len(hist)


def in_window(at: datetime, window: tuple[str, str]) -> bool:
    h = at.astimezone(IST).strftime("%H:%M")
    return window[0] <= h <= window[1]


def hhmm(at: datetime) -> str:
    return at.astimezone(IST).strftime("%H:%M")


__all__ = ["MINUTE", "FwdIv", "IvHistory", "Market", "Series", "hhmm", "in_window"]
