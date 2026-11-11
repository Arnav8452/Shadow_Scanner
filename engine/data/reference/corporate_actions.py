"""Corporate actions (dividends, splits, bonuses) and F&O Ban List / MWPL gate (SPEC §3 L2, §6).

1. **Discrete Cash Dividends**: Supplies discounted present value of known upcoming ex-dividends
   between ``as_of`` and ``expiry`` for accurate synthetic forward & Black-76 option pricing.
2. **Split / Bonus Adjustment Factors**: Adjusts historical strikes, lot sizes, and prices across
   ex-dates so backtests never manufacture phantom P&L jumps.
3. **F&O Ban List & MWPL Tracker**: Blocks fresh positions in stock F&O when Market-Wide Position
   Limit (MWPL) crosses 95% (ban entry) or alerts when approaching 85% early-warning threshold.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class DividendAction:
    symbol: str
    ex_date: date
    dividend_inr: float
    announced_on: date
    is_special: bool = False


@dataclass(frozen=True, slots=True)
class SplitBonusAction:
    symbol: str
    ex_date: date
    numerator: int  # e.g., 2-for-1 split -> numerator=2, denominator=1 (factor = 0.5 on strike)
    denominator: int
    action_type: str = "SPLIT"  # SPLIT | BONUS

    @property
    def price_strike_factor(self) -> float:
        """Multiply old strike/price by this factor on ex-date."""
        return self.denominator / self.numerator

    @property
    def qty_lot_factor(self) -> float:
        """Multiply old lot size / quantity by this factor on ex-date."""
        return self.numerator / self.denominator


@dataclass(frozen=True, slots=True)
class MWPLStatus:
    trade_date: date
    symbol: str
    mwpl_pct: float
    in_ban: bool

    @property
    def allow_new_fno_entry(self) -> bool:
        """Block fresh F&O entries when in ban or MWPL >= 95% (SPEC §3 L2)."""
        return not self.in_ban and self.mwpl_pct < 95.0

    @property
    def early_warning(self) -> bool:
        return self.mwpl_pct >= 85.0


def pv_discrete_dividends(
    dividends: Sequence[DividendAction],
    *,
    symbol: str,
    as_of: date,
    expiry: date,
    risk_free_rate: float = 0.068,
) -> float:
    """Present value of known cash dividends with ``as_of < ex_date <= expiry``."""
    pv = 0.0
    for d in dividends:
        if d.symbol != symbol:
            continue
        if d.announced_on > as_of:
            continue  # Point-in-time: ignore dividends not yet announced on as_of
        if as_of < d.ex_date <= expiry:
            t_cal = (d.ex_date - as_of).days / 365.0
            pv += d.dividend_inr * math.exp(-risk_free_rate * t_cal)
    return pv


def adjust_strike_and_lot(
    strike: float,
    lot_size: int,
    actions: Sequence[SplitBonusAction],
    *,
    symbol: str,
    from_date: date,
    to_date: date,
) -> tuple[float, int]:
    """Adjust strike and lot size across any split/bonus ex-dates in ``(from_date, to_date]``."""
    adj_strike = strike
    adj_lot = float(lot_size)
    for act in sorted(actions, key=lambda a: a.ex_date):
        if act.symbol == symbol and from_date < act.ex_date <= to_date:
            adj_strike = round(adj_strike * act.price_strike_factor, 2)
            adj_lot = adj_lot * act.qty_lot_factor
    return adj_strike, max(1, round(adj_lot))
