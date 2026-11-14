"""Fills, charges and margin for the candle backtest.

**Fills.** A candle has no bid or ask. Each leg pays a half-spread ``h = mult x max(min_inr,
pct% of price)`` on every fill: buys at ``price + h``, sells at ``price - h`` (never below one
tick). The price is the NEXT bar's open after the decision bar, never the decision bar's close.
Default ``max(₹0.10, 0.5%)``: about 2-3x the median half-spread our recorder measured on Nifty
weekly options (12-13 Nov 2026) and at or above its 90th percentile. ``mult = 2`` is the stress
rerun; ``max(₹1.50, 1.5%)`` (the first brief's value, 12-60x the measured spread) is kept as an
extreme scenario. ``table`` uses measured spreads per price bucket instead.

**Charges.** ``fno_charges`` per executed order from ``config/rules``. ``costs_on: run_date``
(default) prices every trade at the rules in force when the backtest runs: the question is what
the rule would earn now (STT on option sales is 0.15% since 1 Apr 2026, 0.10% before; the
Upstox Plus ₹30 row starts on 11 Nov 2026). ``trade_date`` uses each trade's own date and falls
back to ``fallback_broker`` where the plan's row did not exist yet.

**Margin.** ``margin_rules.yaml`` ``paper_margin_per_lot`` (Upstox quotes of 13 Nov 2026 + 15%),
looked up on the run date: a margin estimate, not market data. Historical margin differed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal

from engine.backtest.costs import fno_charges
from engine.core.rules import RuleBook


@dataclass(frozen=True)
class Haircut:
    min_inr: float = 0.10
    pct: float = 0.5
    mult: float = 1.0
    tick: float = 0.05
    # Measured spreads (``spreads.haircut_table``): ((lower price edge, half-spread % of
    # price), ...); when set it replaces ``min_inr``/``pct`` (floor: half a tick).
    table: tuple[tuple[float, float], ...] = ()

    def half(self, px: float) -> float:
        if self.table:
            rel = self.table[0][1]
            for lo, r in self.table:
                if abs(px) >= lo:
                    rel = r
            return self.mult * max(self.tick / 2, abs(px) * rel / 100.0)
        return self.mult * max(self.min_inr, abs(px) * self.pct / 100.0)

    def describe(self) -> str:
        core = (
            "recorded half-spreads by price bucket"
            if self.table
            else f"max(₹{self.min_inr:g}, {self.pct:g}% of price)"
        )
        return core if self.mult == 1 else f"{self.mult:g}x {core}"

    def buy(self, px: float) -> float:
        return round(px + self.half(px), 2)

    def sell(self, px: float) -> float:
        return max(self.tick, round(px - self.half(px), 2))

    def fill(self, side: str, px: float) -> float:
        return self.buy(px) if side == "BUY" else self.sell(px)

    def scaled(self, mult: float) -> Haircut:
        return replace(self, mult=mult)


@dataclass
class Costs:
    book: RuleBook
    run_date: date
    broker: str = "upstox_plus"
    fallback_broker: str = "upstox"
    costs_on: Literal["run_date", "trade_date"] = "run_date"
    charges_mult: float = 1.0  # the stress run doubles every order's charges
    unverified: set[str] = field(default_factory=set)
    fallbacks: int = 0

    def order(self, side: str, px: float, qty: int, day: date) -> float:
        """Charges in ₹ for one executed option order of ``qty`` units at ``px``."""
        on = self.run_date if self.costs_on == "run_date" else day
        s: Literal["buy", "sell"] = "buy" if side == "BUY" else "sell"
        turnover = max(0.0, px) * qty
        try:
            ch = fno_charges(
                self.book, on, segment="options", side=s, turnover_inr=turnover, broker=self.broker
            )
        except LookupError:
            self.fallbacks += 1
            ch = fno_charges(
                self.book,
                on,
                segment="options",
                side=s,
                turnover_inr=turnover,
                broker=self.fallback_broker,
            )
        self.unverified |= set(ch.unverified)
        return ch.total * self.charges_mult


MARGIN_CLASS = {
    "IRON_CONDOR": "four_leg",
    "IRON_FLY": "four_leg",
    "BULL_PUT": "credit_2leg",
    "BEAR_CALL": "credit_2leg",
    "DEBIT_2LEG": "debit_2leg",
}


def table_margin(book: RuleBook, structure: str, lots: int, on: date) -> float | None:
    """Broker-margin estimate per ``margin_rules.yaml`` (None when no row is in force)."""
    try:
        row = book.lookup("margin_rules", on, allow_unverified=True, name="paper_margin_per_lot")
    except LookupError:
        return None
    params = row.params
    cls = MARGIN_CLASS.get(structure, "unknown")
    per_lot = params.get(cls) or params.get("unknown")
    return float(per_lot) * max(1, lots) if per_lot else None


__all__ = ["MARGIN_CLASS", "Costs", "Haircut", "table_margin"]
