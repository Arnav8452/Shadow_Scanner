"""Reverse-Engineering Entry Features for Reference Traders (Log #3 / Log #2 archetypes, SPEC §9.1).

Extracts strike moneyness, opening-window concentration, holding-time profile, and stop/target
asymmetry from mined trade logs to parameterize Strategy v2 (``directional_intraday``) and
Strategy v3 (``opening_breakout``).
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from engine.trade_mining.parse_report import MinedTradeRow

_OPT_STRIKE_RE = re.compile(r"(\d{4,5})(CE|PE)$")


@dataclass(frozen=True, slots=True)
class TraderArchetypeProfile:
    total_trades: int
    opening_15m_share_pct: float  # % of trades entered between 09:15 and 09:30
    call_share_pct: float
    put_share_pct: float
    median_holding_seconds: float
    sl_m_exit_share_pct: float
    qty_mismatch_count: int
    breach_count: int
    inferred_style: str  # "OPENING_RANGE_SCALPER" | "INTRADAY_MOMENTUM_BUYER" | "OPTION_SELLER"


def profile_mined_trader(rows: Sequence[MinedTradeRow]) -> TraderArchetypeProfile:
    """Summarize entry timing, option type, holding duration, and exit style of a mined trader."""
    if not rows:
        return TraderArchetypeProfile(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, "UNKNOWN")

    n = len(rows)
    open_15m = sum(1 for r in rows if r.time_in and "09:15:00" <= r.time_in <= "09:30:59")
    calls = sum(
        1 for r in rows if _OPT_STRIKE_RE.search(r.instrument) and r.instrument.endswith("CE")
    )
    puts = sum(
        1 for r in rows if _OPT_STRIKE_RE.search(r.instrument) and r.instrument.endswith("PE")
    )
    holds = [h for r in rows if (h := r.holding_seconds) is not None]
    med_hold = float(statistics.median(holds)) if holds else 0.0
    sl_m = sum(1 for r in rows if r.exit_order_type == "SL_M")
    q_mis = sum(1 for r in rows if r.qty_mismatch)
    breaches = sum(1 for r in rows if r.breach_inr > 0)
    sells = sum(1 for r in rows if r.side.upper() == "SELL")

    open_pct = open_15m / n * 100.0
    if open_pct >= 45.0:
        style = "OPENING_RANGE_SCALPER"
    elif sells / n >= 0.60:
        style = "OPTION_SELLER"
    else:
        style = "INTRADAY_MOMENTUM_BUYER"

    return TraderArchetypeProfile(
        total_trades=n,
        opening_15m_share_pct=round(open_pct, 2),
        call_share_pct=round(calls / n * 100.0, 2),
        put_share_pct=round(puts / n * 100.0, 2),
        median_holding_seconds=round(med_hold, 1),
        sl_m_exit_share_pct=round(sl_m / n * 100.0, 2),
        qty_mismatch_count=q_mis,
        breach_count=breaches,
        inferred_style=style,
    )
