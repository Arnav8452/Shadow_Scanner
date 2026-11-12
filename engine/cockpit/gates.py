"""Entry gates and exit managers shared by the playbook strategies (owner's notes, 12 Nov 2026).

Charges come from ``config/rules`` through ``fno_charges`` (never the notes' hard-coded rates):

- ``breakeven``: the exit price at which a long option bought at ``entry`` nets zero after
  the round trip's charges, plus a slippage buffer, rounded up to the tick ("true breakeven").
- ``impulse_ok`` (Gate 1): the option's expected 3-minute move, ``ATR_1m * sqrt(3) * |delta|
  + 0.5 * gamma * (ATR_1m * sqrt(3))^2``, must be at least ``mult`` x the breakeven points.
  This is what lets a short-hold ("scalp") strategy trade at all: owner rule, scalping only
  where the expected move clears charges by a margin.
- ``BreakevenRatchet`` (Gate 2): initial stop (12% of premium or the rupee cap), stop moved
  to true breakeven once the bid clears 2x breakeven points, then trailed 1.5x breakeven
  points behind the bid; a scratch exit if still below breakeven after ``scratch_s``.
- ``model_price``: Black-76 fair value from the forward and a smoothed IV, so a stop is not
  triggered by a quote air-pocket the index never made (breaking_retail.md).
- ``absorption`` and ``off_cycle``: effort-vs-result on the trigger bar, and no entries in the
  first seconds of round minutes when retail algorithms fire.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from engine.analytics.iv import black76_price
from engine.backtest.costs import fno_charges
from engine.core.rules import IST, RuleBook


def round_up_tick(px: float, tick: float = 0.05) -> float:
    return math.ceil(px / tick - 1e-9) * tick


def breakeven(
    book: RuleBook,
    broker: str,
    day: date,
    entry: float,
    qty: int,
    *,
    slippage_pts: float = 1.0,
    tick: float = 0.05,
) -> tuple[float, float]:
    """(true breakeven exit price, breakeven points) for a long option of ``qty`` units."""
    buy = fno_charges(
        book, day, segment="options", side="buy", turnover_inr=entry * qty, broker=broker
    ).total
    px = entry + buy / qty
    for _ in range(4):  # sell charges depend on the exit price: a few fixed-point steps
        sell = fno_charges(
            book, day, segment="options", side="sell", turnover_inr=px * qty, broker=broker
        ).total
        px = entry + (buy + sell) / qty
    true_be = round(round_up_tick(px + slippage_pts, tick), 2)
    return true_be, round(true_be - entry, 2)


def expected_impulse(atr_1m: float, delta: float, gamma: float, minutes: float = 3.0) -> float:
    move = atr_1m * math.sqrt(minutes)
    return abs(delta) * move + 0.5 * abs(gamma) * move * move


def impulse_ok(
    atr_1m: float | None, delta: float | None, gamma: float | None, be_points: float, mult: float
) -> tuple[bool, float]:
    if not atr_1m or delta is None or gamma is None or be_points <= 0:
        return False, 0.0
    imp = expected_impulse(atr_1m, delta, gamma)
    return imp >= mult * be_points, round(imp, 2)


@dataclass
class BreakevenRatchet:
    entry: float
    true_be: float
    be_points: float
    stop: float
    opened_at: datetime
    scratch_s: float = 180.0
    lock_mult: float = 2.0
    trail_mult: float = 1.5
    state: str = "INITIAL_RISK"

    @classmethod
    def start(
        cls,
        entry: float,
        qty: int,
        true_be: float,
        be_points: float,
        opened_at: datetime,
        *,
        stop_pct: float = 12.0,
        max_risk_inr: float = 1200.0,
        scratch_s: float = 180.0,
    ) -> BreakevenRatchet:
        stop = round(entry - min(entry * stop_pct / 100.0, max_risk_inr / max(qty, 1)), 2)
        return cls(entry, true_be, be_points, stop, opened_at, scratch_s)

    def on_bid(self, bid: float, now: datetime) -> str | None:
        """Updates the stop; returns an exit reason when the trade should close now."""
        if bid <= self.stop:
            return f"stop ({self.state.lower().replace('_', ' ')})"
        held = (now - self.opened_at).total_seconds()
        if self.state == "INITIAL_RISK" and held >= self.scratch_s and bid < self.true_be:
            return f"scratch: below charges breakeven after {self.scratch_s:.0f} s"
        if self.state == "INITIAL_RISK" and bid >= self.entry + self.lock_mult * self.be_points:
            self.stop, self.state = self.true_be, "BREAKEVEN_LOCKED"
        elif self.state == "BREAKEVEN_LOCKED":
            self.stop = max(self.stop, round(bid - self.trail_mult * self.be_points, 2))
        return None


def model_price(
    forward: float, strike: float, t_var: float, iv: float, *, is_call: bool, t_cal: float, r: float
) -> float | None:
    if forward <= 0 or strike <= 0 or t_var <= 0 or iv <= 0:
        return None
    return black76_price(forward, strike, t_var, iv, is_call=is_call, t_cal=t_cal, risk_free_rate=r)


def off_cycle(
    now: datetime, minutes: tuple[int, ...] = (0, 15, 20, 30, 45), quiet_s: int = 45
) -> bool:
    """False in the first ``quiet_s`` seconds of the round minutes retail algorithms use."""
    local = now.astimezone(IST)
    return not (local.minute in minutes and local.second < quiet_s)


def absorption(bar: Any, rvol: float | None, atr: float | None, bullish: bool) -> str | None:
    """Effort without result on a 1-minute bar: big volume, small body or a rejection wick."""
    if rvol is None or atr is None or atr <= 0:
        return None
    rng = max(bar.high - bar.low, 1e-9)
    body = abs(bar.close - bar.open)
    if rvol >= 2.5 and body / atr < 0.35:
        return f"absorption: {rvol:.1f}x volume, body {body / atr:.2f} ATR"
    wick = (
        (bar.high - max(bar.open, bar.close)) if bullish else (min(bar.open, bar.close) - bar.low)
    )
    if rvol >= 2.0 and wick / rng > 0.55:
        return f"absorption: rejection wick {wick / rng:.0%} of the bar"
    return None


def heavyweights_aligned(
    day_returns: dict[str, float] | None, bullish: bool, need: int = 4
) -> bool | None:
    """Proxy for the notes' 'top-6 heavyweight cash CVD' check: at least ``need`` of the six
    largest Nifty members moving the trade's way today (we have no tick data for the stocks).
    None when breadth data is missing."""
    if not day_returns:
        return None
    six = ("HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "LT", "TCS")
    vals = [day_returns[s] for s in six if s in day_returns]
    if len(vals) < 4:
        return None
    return sum(1 for v in vals if (v > 0) == bullish and v != 0) >= need
