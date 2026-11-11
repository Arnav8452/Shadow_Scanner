"""Macro Event Blackout Window & Constituent Earnings Event-Risk Score (SPEC §3 L8, §5.1, PDF §7.5).

Why this module is critical to both Version 1 (Shadow Scanner) and Version 2 (Bots):
1. **Order-Book Blackout Windows (PDF §7.5)**:
   - During RBI Monetary Policy (MPC) announcements (10:00 AM IST on policy days), Union Budget
     speeches, CPI/IIP releases, or overnight US FOMC / CPI releases, market makers pull up to 80%
     of resting limit orders. L1–L30 OBI and Air-Pocket signals become erratic.
   - Any time ``now`` falls within ``[event.start_ts - pre_blackout_min, event.end_ts + post_blackout_min]``,
     order-flow and short-vega entries are **blocked**.
2. **Constituent Earnings Event-Risk Score (SPEC §5.1)**:
   - Sums the Nifty 50 index weights of all constituents reporting quarterly earnings within the
     next 24 hours.
   - If the reporting constituent weight sum exceeds ``earnings_weight_threshold_pct`` (default 8.0%,
     e.g., HDFCBANK or RELIANCE reporting), short-vega Iron Condors are scaled down by 50% or skipped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class EventCategory(StrEnum):
    RBI_MPC = "RBI_MPC"
    UNION_BUDGET = "UNION_BUDGET"
    INDIA_MACRO = "INDIA_MACRO"  # CPI, IIP, GDP
    US_MACRO = "US_MACRO"  # FOMC, US CPI, NFP
    CONSTITUENT_EARNINGS = "CONSTITUENT_EARNINGS"
    ELECTION_EXIT_POLL = "ELECTION_EXIT_POLL"


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    title: str
    category: EventCategory
    start_ts: datetime
    end_ts: datetime
    symbol: str | None = None  # For constituent earnings (e.g. 'HDFCBANK')
    index_weight_pct: float = 0.0
    pre_blackout_min: int = 15
    post_blackout_min: int = 30


@dataclass(frozen=True, slots=True)
class EventBlackoutStatus:
    in_macro_blackout: bool
    active_blackout_events: tuple[str, ...]
    earnings_weight_24h_pct: float
    reporting_symbols_24h: tuple[str, ...]
    short_vega_scale_factor: float  # 0.0 (blocked), 0.5 (half size), or 1.0 (normal)
    order_flow_allowed: bool


def evaluate_event_risk(
    events: Sequence[ScheduledEvent],
    now: datetime,
    *,
    earnings_weight_half_size_pct: float = 6.0,
    earnings_weight_block_pct: float = 12.0,
) -> EventBlackoutStatus:
    """Evaluate macro blackout status and 24-hour constituent earnings weight at ``now``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    active_macro: list[str] = []
    reporting_syms: list[str] = []
    earnings_weight = 0.0
    horizon_24h = now + timedelta(hours=24)

    for ev in events:
        if ev.category is EventCategory.CONSTITUENT_EARNINGS:
            if now <= ev.start_ts <= horizon_24h:
                if ev.symbol:
                    reporting_syms.append(ev.symbol)
                earnings_weight += ev.index_weight_pct
            # Also block direct single-stock order flow during earnings announcement window
            win_start = ev.start_ts - timedelta(minutes=ev.pre_blackout_min)
            win_end = ev.end_ts + timedelta(minutes=ev.post_blackout_min)
            if win_start <= now <= win_end and ev.index_weight_pct >= earnings_weight_block_pct:
                active_macro.append(f"{ev.title} ({ev.symbol})")
        else:
            win_start = ev.start_ts - timedelta(minutes=ev.pre_blackout_min)
            win_end = ev.end_ts + timedelta(minutes=ev.post_blackout_min)
            if win_start <= now <= win_end:
                active_macro.append(ev.title)

    in_blackout = len(active_macro) > 0
    if in_blackout or earnings_weight >= earnings_weight_block_pct:
        vega_scale = 0.0
    elif earnings_weight >= earnings_weight_half_size_pct:
        vega_scale = 0.5
    else:
        vega_scale = 1.0

    return EventBlackoutStatus(
        in_macro_blackout=in_blackout,
        active_blackout_events=tuple(active_macro),
        earnings_weight_24h_pct=round(earnings_weight, 2),
        reporting_symbols_24h=tuple(reporting_syms),
        short_vega_scale_factor=vega_scale,
        order_flow_allowed=not in_blackout,
    )
