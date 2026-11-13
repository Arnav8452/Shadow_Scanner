"""Per-index minute tape for the playbook strategies (owner's notes, 12 Nov 2026).

Built from what the engine already has every step: the index spot, the front future's last
price, cumulative volume and touch, the ATM call and put books, our live IVs, and the
recorder's 1-minute option chain. Everything is computed here; nothing is taken from a
broker's analytics.

Per 1-minute bar: index OHLC, futures close, futures volume split into aggressive buys and
sells by the quote rule (last trade at/above the ask = buy, at/below the bid = sell, else by
the side of the mid), and price x volume for the session VWAP. Derived measures: ATR, CVD and
its z-score, relative volume, the 30-minute opening range, realized volatility, the ATM
straddle and its VWAP, IV changes, per-strike OI changes, 15-minute bars and Bollinger width,
and VWAP whipsaws. Sampling is the cockpit step (~0.1-0.2 s), coarser than tick data.
"""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from engine.core.rules import IST


def trade_side(
    vol: float, px: float, bid: float, ask: float, prev_px: float = 0.0
) -> tuple[float, float]:
    """(aggressive buy, aggressive sell) volume for ``vol`` that printed at ``px``, judged
    against the quote that stood BEFORE it (Lee-Ready): at/above that ask is a buy, at/below
    that bid a sell, otherwise by the side of that mid; exactly at the mid, by the tick
    (up = buy, down = sell, unchanged = split). Using the post-trade quote instead misreads a
    sweep that lifted the touch as selling."""
    if vol <= 0:
        return 0.0, 0.0
    if ask > 0 and px >= ask:
        return vol, 0.0
    if bid > 0 and px <= bid:
        return 0.0, vol
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2
        if px > mid:
            return vol, 0.0
        if px < mid:
            return 0.0, vol
    if prev_px > 0 and px != prev_px:
        return (vol, 0.0) if px > prev_px else (0.0, vol)
    return vol / 2, vol / 2


@dataclass
class MinuteBar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    fut_close: float = 0.0
    vol: float = 0.0
    buy: float = 0.0
    sell: float = 0.0
    pv: float = 0.0
    straddle: float = 0.0
    straddle_vol: float = 0.0

    @property
    def delta(self) -> float:
        return self.buy - self.sell


def ema(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = statistics.fmean(values[:n])
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


@dataclass
class IndexTape:
    und: str
    bars: list[MinuteBar] = field(default_factory=list)
    day: Any = None
    last_cum: float | None = None
    straddle_last_cum: float | None = None
    # The futures quote and price at the previous sample: volume that traded since then hit
    # THAT book (a sweep moves the touch before the next sample arrives).
    prev_bid: float = 0.0
    prev_ask: float = 0.0
    prev_px: float = 0.0
    atm_strike: float | None = None
    iv: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict)
    oi: list[tuple[datetime, dict[tuple[float, str], int]]] = field(default_factory=list)

    # ------------------------------------------------------------------ input

    def on_sample(
        self,
        now: datetime,
        *,
        spot: float,
        fut: Any = None,
        straddle: float | None = None,
        straddle_cum_vol: float | None = None,
        ivs: dict[str, float] | None = None,
    ) -> None:
        local = now.astimezone(IST)
        self._roll(local.date())
        if spot <= 0:
            return
        start = local.replace(second=0, microsecond=0)
        if not self.bars or self.bars[-1].start != start:
            self.bars.append(MinuteBar(start, spot, spot, spot, spot))
        b = self.bars[-1]
        b.high, b.low, b.close = max(b.high, spot), min(b.low, spot), spot
        if fut is not None and float(getattr(fut, "ltp", 0.0) or 0.0) > 0:
            b.fut_close = float(fut.ltp)
            cum = float(getattr(fut, "cum_volume", 0) or 0)
            vol = 0.0
            if cum > 0:
                if self.last_cum is not None and cum >= self.last_cum:
                    vol = cum - self.last_cum
                self.last_cum = cum
            bid = fut.bids[0][0] if fut.bids else 0.0
            ask = fut.asks[0][0] if fut.asks else 0.0
            px = float(fut.ltp)
            if vol > 0:
                buy, sell = trade_side(
                    vol, px, self.prev_bid or bid, self.prev_ask or ask, self.prev_px
                )
                b.buy += buy
                b.sell += sell
                b.vol += vol
                b.pv += vol * px
            self.prev_bid, self.prev_ask, self.prev_px = bid, ask, px
        if straddle:
            b.straddle = straddle
            if straddle_cum_vol is not None:
                if (
                    self.straddle_last_cum is not None
                    and straddle_cum_vol >= self.straddle_last_cum
                ):
                    b.straddle_vol += straddle_cum_vol - self.straddle_last_cum
                self.straddle_last_cum = straddle_cum_vol
        for k, v in (ivs or {}).items():
            if v:
                series = self.iv.setdefault(k, [])
                if not series or series[-1][0] != start:
                    series.append((start, float(v)))
                else:
                    series[-1] = (start, float(v))

    def _roll(self, day: Any) -> None:
        """A new session clears yesterday's data (the first call of the day only sets it)."""
        if self.day is not None and self.day != day:
            self.__init__(self.und)  # type: ignore[misc]
        self.day = day

    def on_chain(self, now: datetime, rows: list[dict[str, Any]]) -> None:
        self._roll(now.astimezone(IST).date())
        start = now.astimezone(IST).replace(second=0, microsecond=0)
        snap: dict[tuple[float, str], int] = {}
        for r in rows:
            for key, kind in (("ce", "CE"), ("pe", "PE")):
                side = r.get(key) or {}
                if side.get("oi") is not None:
                    snap[(float(r["k"]), kind)] = int(side["oi"])
        if snap and (not self.oi or self.oi[-1][0] != start):
            self.oi.append((start, snap))
            self.oi = self.oi[-120:]

    # ------------------------------------------------------------------ derived

    def done(self) -> list[MinuteBar]:
        """Completed bars (the last one is still forming)."""
        return self.bars[:-1]

    def atr(self, n: int = 14) -> float | None:
        bars = self.done()
        if len(bars) < 2:
            return None
        trs = []
        for prev, b in itertools.pairwise(bars):
            trs.append(max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close)))
        trs = trs[-n:]
        return statistics.fmean(trs) if trs else None

    def cvd(self) -> float:
        return sum(b.delta for b in self.bars)

    def cvd_z(self, n: int = 20) -> float | None:
        bars = self.done()
        if len(bars) < n + 1:
            return None
        hist = [b.delta for b in bars[-n - 1 : -1]]
        sd = statistics.pstdev(hist)
        if sd <= 0:
            return None
        return (bars[-1].delta - statistics.fmean(hist)) / sd

    def rvol(self, n: int = 20) -> float | None:
        bars = self.done()
        if len(bars) < n + 1:
            return None
        avg = statistics.fmean(b.vol for b in bars[-n - 1 : -1])
        return bars[-1].vol / avg if avg > 0 else None

    def vwap(self) -> float | None:
        v = sum(b.vol for b in self.bars)
        return sum(b.pv for b in self.bars) / v if v > 0 else None

    def opening_range(self, minutes: int = 30) -> tuple[float, float] | None:
        if not self.bars:
            return None
        first = self.bars[0].start.replace(hour=9, minute=15)
        end = first + timedelta(minutes=minutes)
        inside = [b for b in self.bars if first <= b.start < end]
        if not inside or self.bars[-1].start < end:
            return None
        if inside[0].start > first + timedelta(minutes=1):
            return None  # the tape started late (a restart): a partial range is not the range
        return max(b.high for b in inside), min(b.low for b in inside)

    def realized_vol(self, minutes: int = 30, from_open: bool = True) -> float | None:
        """Annualized realized volatility of 1-minute index returns (375 minutes, 252 days)."""
        bars = self.done()
        sel = bars[: minutes + 1] if from_open else bars[-minutes - 1 :]
        if len(sel) < 10:
            return None
        rets = [math.log(b.close / a.close) for a, b in itertools.pairwise(sel) if a.close > 0]
        if len(rets) < 5:
            return None
        return statistics.pstdev(rets) * math.sqrt(375 * 252)

    def iv_change(self, key: str, minutes: int = 3) -> float | None:
        s = self.iv.get(key) or []
        if len(s) < 2:
            return None
        cut = s[-1][0] - timedelta(minutes=minutes)
        base = next((v for t, v in reversed(s) if t <= cut), None)
        return s[-1][1] - base if base is not None else None

    def oi_change_pct(self, strike: float, kind: str, minutes: int) -> float | None:
        if len(self.oi) < 2:
            return None
        now_t, now_s = self.oi[-1]
        cut = now_t - timedelta(minutes=minutes)
        base = next((s for t, s in reversed(self.oi) if t <= cut), None)
        if base is None:
            return None
        a, b = base.get((strike, kind)), now_s.get((strike, kind))
        if not a or b is None:
            return None
        return (b - a) / a * 100

    def straddle_stats(self) -> tuple[float, float, float] | None:
        """(VWAP, standard deviation, last close) of the ATM straddle over the session."""
        pts = [(b.straddle, b.straddle_vol or 1.0) for b in self.done() if b.straddle]
        if len(pts) < 5:
            return None
        w = sum(v for _, v in pts)
        vwap = sum(p * v for p, v in pts) / w
        sd = math.sqrt(sum(v * (p - vwap) ** 2 for p, v in pts) / w)
        return vwap, sd, pts[-1][0]

    def bars_n(self, n: int) -> list[MinuteBar]:
        """Completed n-minute bars aggregated from the 1-minute bars."""
        out: list[MinuteBar] = []
        for b in self.done():
            m = (b.start.hour * 60 + b.start.minute) // n * n
            start = b.start.replace(hour=m // 60, minute=m % 60)
            if not out or out[-1].start != start:
                out.append(MinuteBar(start, b.open, b.high, b.low, b.close, b.fut_close))
            o = out[-1]
            o.high, o.low, o.close, o.fut_close = (
                max(o.high, b.high),
                min(o.low, b.low),
                b.close,
                b.fut_close,
            )
            o.vol += b.vol
            o.buy += b.buy
            o.sell += b.sell
            o.pv += b.pv
        return out

    def bb_width(self, n: int = 15, period: int = 20) -> float | None:
        bars = self.bars_n(n)
        closes = [b.close for b in bars[-period:]]
        if len(closes) < 5:
            return None
        m = statistics.fmean(closes)
        return 4 * statistics.pstdev(closes) / m if m > 0 else None

    def whipsaws(self) -> int:
        """Times the futures close crossed the session VWAP so far."""
        n = 0
        side = 0
        cum_v = cum_pv = 0.0
        for b in self.done():
            cum_v += b.vol
            cum_pv += b.pv
            if cum_v <= 0 or not b.fut_close:
                continue
            s = 1 if b.fut_close > cum_pv / cum_v else -1
            if side and s != side:
                n += 1
            side = s
        return n

    def box(self, minutes: int = 60) -> tuple[float, float] | None:
        bars = self.done()[-minutes:]
        if len(bars) < minutes // 2:
            return None
        return max(b.high for b in bars), min(b.low for b in bars)


@dataclass
class OptionTape:
    """1-minute closes, volume and VWAP of one option (for EMA trails and writer VWAP)."""

    closes: list[tuple[datetime, float]] = field(default_factory=list)
    last_cum: float | None = None
    pv: float = 0.0
    vol: float = 0.0
    open_920: float | None = None

    def on_sample(self, now: datetime, ltp: float, cum_volume: float) -> None:
        if ltp <= 0:
            return
        local = now.astimezone(IST)
        start = local.replace(second=0, microsecond=0)
        if not self.closes or self.closes[-1][0] != start:
            self.closes.append((start, ltp))
        else:
            self.closes[-1] = (start, ltp)
        if self.open_920 is None and local.hour == 9 and local.minute >= 20:
            self.open_920 = ltp
        if cum_volume > 0:
            if self.last_cum is not None and cum_volume >= self.last_cum:
                v = cum_volume - self.last_cum
                self.vol += v
                self.pv += v * ltp
            self.last_cum = cum_volume

    def vwap(self) -> float | None:
        return self.pv / self.vol if self.vol > 0 else None

    def ema(self, n: int = 9) -> float | None:
        return ema([c for _, c in self.closes[:-1]], n)
