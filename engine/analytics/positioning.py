"""Chain Positioning (PCR, Call/Put OI Walls, Max Pain) & Lag-Adjusted 4-Quad Futures OI Buildup.

Implements SPEC §4.9 and PDF §3.2, §4.4, §5.1.4:
1. **Option Chain Positioning**:
   - ``pcr_oi``: Total Put OI / Total Call OI.
   - ``pcr_volume``: Total Put Volume / Total Call Volume.
   - ``call_oi_wall``: Strike with highest Call OI (overhead resistance & stop-hunt magnet).
   - ``put_oi_wall``: Strike with highest Put OI (underfoot support & stop-hunt magnet).
   - ``max_pain_strike``: Strike minimizing aggregate intrinsic payout to option holders at expiry.
2. **Lag-Adjusted 4-Quadrant Futures Price × OI Buildup (PDF §4.4)**:
   - Evaluates rolling 3-to-5-minute ``delta_price_pct`` and ``delta_oi_pct`` (not tick-by-tick OI,
     because NSE broadcasts Open Interest snapshots with a 1–3 minute lag):
     - ``LONG_BUILDUP``: ``Price ↑`` and ``OI ↑`` -> institutional longs entering; breakout.
     - ``SHORT_COVERING``: ``Price ↑`` and ``OI ↓`` -> trapped shorts forced to buy back; fuels fast
       short-squeeze cascades above ORH / PDH / Call OI Wall.
     - ``SHORT_BUILDUP``: ``Price ↓`` and ``OI ↑`` -> institutional shorts pressing; breakdown.
     - ``LONG_LIQUIDATION``: ``Price ↓`` and ``OI ↓`` -> trapped retail longs hitting stop-losses;
       classic Stage 2 signature of an institutional Wyckoff Spring stop-hunt below PDL / ORL!
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class OIBuildupQuadrant(StrEnum):
    LONG_BUILDUP = "LONG_BUILDUP"
    SHORT_COVERING = "SHORT_COVERING"
    SHORT_BUILDUP = "SHORT_BUILDUP"
    LONG_LIQUIDATION = "LONG_LIQUIDATION"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True, slots=True)
class StrikeOINode:
    strike: float
    call_oi: int
    put_oi: int
    call_volume: int = 0
    put_volume: int = 0
    call_oi_change: int = 0
    put_oi_change: int = 0


@dataclass(frozen=True, slots=True)
class ChainPositioningSummary:
    pcr_oi: float
    pcr_volume: float
    call_oi_wall: float
    call_oi_wall_qty: int
    put_oi_wall: float
    put_oi_wall_qty: int
    max_pain_strike: float
    call_writers_trapped: bool  # True when spot breaks above Call OI Wall while Call OI drops


@dataclass(frozen=True, slots=True)
class FuturesOIBuildup:
    quadrant: OIBuildupQuadrant
    price_change_pct: float
    oi_change_pct: float
    basis_pct: float  # (Futures - Spot) / Spot * 100
    confluence_pts_long: float  # [0, 20] contribution to 100-pt confidence score for LONG
    confluence_pts_short: float  # [0, 20] contribution to 100-pt confidence score for SHORT
    interpretation: str


def summarize_chain_positioning(
    nodes: Sequence[StrikeOINode],
    *,
    spot: float,
) -> ChainPositioningSummary:
    if not nodes:
        return ChainPositioningSummary(1.0, 1.0, spot, 0, spot, 0, spot, False)

    tot_call_oi = sum(max(0, n.call_oi) for n in nodes)
    tot_put_oi = sum(max(0, n.put_oi) for n in nodes)
    tot_call_vol = sum(max(0, n.call_volume) for n in nodes)
    tot_put_vol = sum(max(0, n.put_volume) for n in nodes)

    pcr_oi = (tot_put_oi / tot_call_oi) if tot_call_oi > 0 else 1.0
    pcr_vol = (tot_put_vol / tot_call_vol) if tot_call_vol > 0 else 1.0

    call_wall_node = max(nodes, key=lambda n: n.call_oi)
    put_wall_node = max(nodes, key=lambda n: n.put_oi)

    # Max Pain: strike S_exp minimizing aggregate option intrinsic payout at expiry
    best_strike = spot
    min_payout = float("inf")
    for candidate in nodes:
        s_exp = candidate.strike
        payout = sum(
            n.call_oi * max(0.0, s_exp - n.strike) + n.put_oi * max(0.0, n.strike - s_exp)
            for n in nodes
        )
        if payout < min_payout:
            min_payout = payout
            best_strike = s_exp

    trapped_calls = spot >= call_wall_node.strike and call_wall_node.call_oi_change < 0

    return ChainPositioningSummary(
        pcr_oi=round(pcr_oi, 4),
        pcr_volume=round(pcr_vol, 4),
        call_oi_wall=call_wall_node.strike,
        call_oi_wall_qty=call_wall_node.call_oi,
        put_oi_wall=put_wall_node.strike,
        put_oi_wall_qty=put_wall_node.put_oi,
        max_pain_strike=best_strike,
        call_writers_trapped=trapped_calls,
    )


def classify_futures_oi_buildup(
    *,
    price_now: float,
    price_3m_ago: float,
    oi_now: int,
    oi_3m_ago: int,
    spot_now: float | None = None,
    min_price_move_pct: float = 0.08,
    min_oi_move_pct: float = 0.25,
    is_stop_hunt_reclaim: bool = False,
) -> FuturesOIBuildup:
    """Classify rolling 3-to-5-minute Futures Price × OI behavior into 4 institutional quadrants."""
    if price_3m_ago <= 0 or oi_3m_ago <= 0:
        return FuturesOIBuildup(
            OIBuildupQuadrant.NEUTRAL, 0.0, 0.0, 0.0, 5.0, 5.0, "Insufficient 3m history"
        )

    dp_pct = (price_now - price_3m_ago) / price_3m_ago * 100.0
    doi_pct = (oi_now - oi_3m_ago) / oi_3m_ago * 100.0
    basis_pct = ((price_now - spot_now) / spot_now * 100.0) if (spot_now and spot_now > 0) else 0.0

    if abs(dp_pct) < min_price_move_pct and abs(doi_pct) < min_oi_move_pct:
        return FuturesOIBuildup(
            OIBuildupQuadrant.NEUTRAL,
            round(dp_pct, 3),
            round(doi_pct, 3),
            round(basis_pct, 3),
            5.0,
            5.0,
            "Neutral price & OI drift",
        )

    if dp_pct >= min_price_move_pct and doi_pct >= min_oi_move_pct:
        quad = OIBuildupQuadrant.LONG_BUILDUP
        pts_l, pts_s = 20.0, 0.0
        desc = f"Long Buildup (Price +{dp_pct:.2f}%, OI +{doi_pct:.2f}%)"
    elif dp_pct >= min_price_move_pct and doi_pct <= -min_oi_move_pct:
        quad = OIBuildupQuadrant.SHORT_COVERING
        pts_l, pts_s = 18.0, 0.0
        desc = f"Short Covering Squeeze (Price +{dp_pct:.2f}%, OI {doi_pct:.2f}%)"
    elif dp_pct <= -min_price_move_pct and doi_pct >= min_oi_move_pct:
        quad = OIBuildupQuadrant.SHORT_BUILDUP
        pts_l = 0.0
        pts_s = 20.0
        desc = f"Short Buildup (Price {dp_pct:.2f}%, OI +{doi_pct:.2f}%)"
    elif dp_pct <= -min_price_move_pct and doi_pct <= -min_oi_move_pct:
        quad = OIBuildupQuadrant.LONG_LIQUIDATION
        # Long liquidation during a sweep below PDL/ORL that reclaims is a Wyckoff Spring!
        pts_l = 16.0 if is_stop_hunt_reclaim else 2.0
        pts_s = 14.0 if not is_stop_hunt_reclaim else 4.0
        desc = f"Long Liquidation Flush (Price {dp_pct:.2f}%, OI {doi_pct:.2f}%)"
    else:
        quad = OIBuildupQuadrant.NEUTRAL
        pts_l, pts_s = 8.0, 8.0
        desc = f"Mixed buildup (Price {dp_pct:+.2f}%, OI {doi_pct:+.2f}%)"

    return FuturesOIBuildup(
        quadrant=quad,
        price_change_pct=round(dp_pct, 3),
        oi_change_pct=round(doi_pct, 3),
        basis_pct=round(basis_pct, 3),
        confluence_pts_long=pts_l,
        confluence_pts_short=pts_s,
        interpretation=desc,
    )
