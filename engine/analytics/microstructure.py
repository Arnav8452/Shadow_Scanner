"""L1–L30 Order-Book Microstructure: Microprice, Exp-Weighted OBI, OFI, and Structural Walls.

Implements SPEC §4.7 and PDF §2.1–2.6, §4.1–4.3, §8.1:
1. **Size-Weighted Microprice**:
   ``P_micro = (V_bid_1 * P_ask_1 + V_ask_1 * P_bid_1) / (V_bid_1 + V_ask_1)``
2. **Exponentially Weighted Order Book Imbalance (OBI)**:
   ``w_i = exp(-0.5 * i)`` for ``i = 1..N`` (both ``N = 5`` and ``N = 30``).
3. **Cont-Kukanov-Stoikov Order Flow Imbalance (OFI)**:
   Tracks tick-to-tick net limit order arrivals/cancellations at the best bid and best ask.
4. **L6–L25 vs L1–L5 Overhead / Underfoot Air-Pocket Ratio**:
   Detects thin-book liquidity vacuums behind the top 5 levels (``ratio < 0.55``) using Upstox V3
   ``full_d30`` 30-level depth.
5. **Structural Wall Locator (L1–L30)**:
   Finds the strongest resting bid wall and ask wall (and second-tier L10–L30 target walls) so
   Version 1 (Shadow Scanner) and Version 2 (Bots) can place stops 1–2 ticks *behind* real
   institutional liquidity and take profits 1–2 ticks *in front of* opposing walls.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from engine.data.adapters.base import Level


@dataclass(frozen=True, slots=True)
class StructuralWall:
    side: str  # "BID" or "ASK"
    level_index: int  # 1-based depth level (1..30)
    price: float
    qty: int
    orders: int
    multiple_of_avg: float  # Wall qty / average level qty


@dataclass(frozen=True, slots=True)
class BookMicrostructureSnapshot:
    mid_price: float
    microprice: float
    microprice_divergence: float  # microprice - mid_price
    spread_pts: float
    spread_bps: float
    obi_l1: float
    obi_l5: float  # Exponentially weighted L1-L5 OBI in [-1, +1]
    obi_l30: float  # Exponentially weighted L1-L30 OBI in [-1, +1]
    overhead_air_pocket_ratio: float  # Mean(L6-L25 Ask) / Mean(L1-L5 Ask); < 0.55 = vacuum above
    underfoot_air_pocket_ratio: float  # Mean(L6-L25 Bid) / Mean(L1-L5 Bid); < 0.55 = vacuum below
    strongest_bid_wall: StructuralWall | None  # L1-L15 support wall for stop-loss anchor
    strongest_ask_wall: StructuralWall | None  # L1-L15 resistance wall for stop-loss anchor
    deep_bid_wall_l10_l30: StructuralWall | None  # Downside target magnet
    deep_ask_wall_l10_l30: StructuralWall | None  # Upside target magnet


def compute_microprice(bid_px: float, bid_qty: int, ask_px: float, ask_qty: int) -> float:
    """Compute size-weighted microprice ``(V_bid * P_ask + V_ask * P_bid) / (V_bid + V_ask)``."""
    tot = max(0, bid_qty) + max(0, ask_qty)
    if tot <= 0:
        return 0.5 * (bid_px + ask_px)
    return (max(0, bid_qty) * ask_px + max(0, ask_qty) * bid_px) / tot


def compute_exp_weighted_obi(
    bids: Sequence[Level],
    asks: Sequence[Level],
    *,
    max_levels: int = 5,
    decay: float = 0.5,
) -> float:
    """Compute exponentially weighted Order Book Imbalance ``w_i = exp(-decay * i)``."""
    n_b = min(len(bids), max_levels)
    n_a = min(len(asks), max_levels)
    n = max(n_b, n_a)
    if n == 0:
        return 0.0

    w_bid = 0.0
    w_ask = 0.0
    for i in range(n):
        w = math.exp(-decay * (i + 1))
        if i < n_b:
            w_bid += w * max(0, bids[i][1])
        if i < n_a:
            w_ask += w * max(0, asks[i][1])

    denom = w_bid + w_ask
    if denom <= 0:
        return 0.0
    return (w_bid - w_ask) / denom


def compute_cks_ofi_step(
    prev_bid_px: float,
    prev_bid_qty: int,
    prev_ask_px: float,
    prev_ask_qty: int,
    curr_bid_px: float,
    curr_bid_qty: int,
    curr_ask_px: float,
    curr_ask_qty: int,
) -> float:
    """Cont-Kukanov-Stoikov (2014) single-step Order Flow Imbalance ``e_bid - e_ask``."""
    if curr_bid_px > prev_bid_px:
        e_bid = float(curr_bid_qty)
    elif curr_bid_px == prev_bid_px:
        e_bid = float(curr_bid_qty - prev_bid_qty)
    else:
        e_bid = -float(prev_bid_qty)

    if curr_ask_px < prev_ask_px:
        e_ask = float(curr_ask_qty)
    elif curr_ask_px == prev_ask_px:
        e_ask = float(curr_ask_qty - prev_ask_qty)
    else:
        e_ask = -float(prev_ask_qty)

    return e_bid - e_ask


def compute_air_pocket_ratio(levels: Sequence[Level]) -> float:
    """Ratio of average depth in L6–L25 to average depth in L1–L5 (PDF §2.4 & §8.1.3).

    - Ratio < 0.55 indicates a **Liquidity Vacuum / Air Pocket** behind the top 5 levels.
    - If fewer than 6 levels are available (e.g., 5-level feed), compares L3–L5 against L1–L2.
    """
    if not levels:
        return 1.0
    if len(levels) >= 6:
        top = [max(0, lv[1]) for lv in levels[:5]]
        deep = [max(0, lv[1]) for lv in levels[5:25]]
    elif len(levels) >= 3:
        top = [max(0, lv[1]) for lv in levels[:2]]
        deep = [max(0, lv[1]) for lv in levels[2:]]
    else:
        return 1.0

    avg_top = sum(top) / len(top) if top else 0.0
    avg_deep = sum(deep) / len(deep) if deep else 0.0
    if avg_top <= 0:
        return 1.0
    return avg_deep / avg_top


def _find_strongest_wall(
    levels: Sequence[Level],
    *,
    side: str,
    start_idx: int = 0,
    end_idx: int = 15,
) -> StructuralWall | None:
    sub = levels[start_idx : min(len(levels), end_idx)]
    if not sub:
        return None
    all_qtys = [max(0, lv[1]) for lv in levels if lv[1] > 0]
    avg_qty = (sum(all_qtys) / len(all_qtys)) if all_qtys else 1.0

    best_rel_i = max(range(len(sub)), key=lambda i: sub[i][1])
    lv = sub[best_rel_i]
    abs_idx = start_idx + best_rel_i + 1
    mult = lv[1] / max(avg_qty, 1.0)
    return StructuralWall(
        side=side,
        level_index=abs_idx,
        price=lv[0],
        qty=lv[1],
        orders=lv[2] or 1,
        multiple_of_avg=round(mult, 2),
    )


def analyze_book_microstructure(
    bids: Sequence[Level],
    asks: Sequence[Level],
) -> BookMicrostructureSnapshot:
    """Analyze L1–L30 order book into microprice, exp-OBI, air pockets, and structural walls."""
    if not bids or not asks:
        return BookMicrostructureSnapshot(
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, None, None, None, None
        )

    b1_px, b1_q, _ = bids[0]
    a1_px, a1_q, _ = asks[0]
    mid = 0.5 * (b1_px + a1_px)
    micro = compute_microprice(b1_px, b1_q, a1_px, a1_q)
    spread = max(0.0, a1_px - b1_px)
    spread_bps = (spread / mid * 10_000.0) if mid > 0 else 0.0

    obi_1 = compute_exp_weighted_obi(bids, asks, max_levels=1)
    obi_5 = compute_exp_weighted_obi(bids, asks, max_levels=5)
    obi_30 = compute_exp_weighted_obi(bids, asks, max_levels=30)

    air_ask = compute_air_pocket_ratio(asks)
    air_bid = compute_air_pocket_ratio(bids)

    wall_bid_near = _find_strongest_wall(bids, side="BID", start_idx=0, end_idx=15)
    wall_ask_near = _find_strongest_wall(asks, side="ASK", start_idx=0, end_idx=15)
    wall_bid_deep = (
        _find_strongest_wall(bids, side="BID", start_idx=9, end_idx=30)
        if len(bids) >= 10
        else _find_strongest_wall(bids, side="BID", start_idx=2, end_idx=5)
    )
    wall_ask_deep = (
        _find_strongest_wall(asks, side="ASK", start_idx=9, end_idx=30)
        if len(asks) >= 10
        else _find_strongest_wall(asks, side="ASK", start_idx=2, end_idx=5)
    )

    return BookMicrostructureSnapshot(
        mid_price=round(mid, 4),
        microprice=round(micro, 4),
        microprice_divergence=round(micro - mid, 4),
        spread_pts=round(spread, 4),
        spread_bps=round(spread_bps, 2),
        obi_l1=round(obi_1, 4),
        obi_l5=round(obi_5, 4),
        obi_l30=round(obi_30, 4),
        overhead_air_pocket_ratio=round(air_ask, 4),
        underfoot_air_pocket_ratio=round(air_bid, 4),
        strongest_bid_wall=wall_bid_near,
        strongest_ask_wall=wall_ask_near,
        deep_bid_wall_l10_l30=wall_bid_deep,
        deep_ask_wall_l10_l30=wall_ask_deep,
    )
