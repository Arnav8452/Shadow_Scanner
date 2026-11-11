"""Point-in-time Nifty 50 constituents, weights, and intraday breadth/dispersion (SPEC §3 L9).

How this interconnects with the rest of the system:
- Index breakouts or stop-hunt reversals on NIFTY / BANKNIFTY require constituent alignment:
  if NIFTY sweeps PDL and reclaims, AND top index heavyweights (HDFCBANK, RELIANCE, ICICIBANK,
  INFY, TCS, BHARTIARTL, SBIN, LT) flip positive in weighted breadth, the index signal gets
  maximum cross-asset confluence points (+20/20 in the 100-point scorer).
- Conversely, if constituent return dispersion is high and weighted advance-decline is flat,
  index moves are pinned by cross-currents -> favors Iron Condor / Credit Spreads over breakouts.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class IndexConstituent:
    symbol: str
    sector: str
    weight_pct: float
    effective_from: date
    effective_to: date | None = None


@dataclass(frozen=True, slots=True)
class ConstituentBreadthSnapshot:
    advancing_count: int
    declining_count: int
    unchanged_count: int
    weighted_breadth_pct: float  # [-100, +100]: net weight of advancing minus declining members
    weighted_index_return_pct: float
    cross_sectional_dispersion_pct: float
    implied_correlation_proxy: float  # [0, 1]
    top_sector_leader: str
    top_sector_laggard: str


_DEFAULT_HEAVYWEIGHTS: tuple[IndexConstituent, ...] = (
    IndexConstituent("HDFCBANK", "BANKS", 11.8, date(2020, 1, 1)),
    IndexConstituent("RELIANCE", "ENERGY", 9.4, date(2020, 1, 1)),
    IndexConstituent("ICICIBANK", "BANKS", 8.1, date(2020, 1, 1)),
    IndexConstituent("INFY", "IT", 5.8, date(2020, 1, 1)),
    IndexConstituent("TCS", "IT", 4.1, date(2020, 1, 1)),
    IndexConstituent("BHARTIARTL", "TELECOM", 4.0, date(2020, 1, 1)),
    IndexConstituent("LT", "INDUSTRIALS", 3.9, date(2020, 1, 1)),
    IndexConstituent("ITC", "FMCG", 3.8, date(2020, 1, 1)),
    IndexConstituent("SBIN", "BANKS", 3.1, date(2020, 1, 1)),
    IndexConstituent("AXISBANK", "BANKS", 3.0, date(2020, 1, 1)),
    IndexConstituent("KOTAKBANK", "BANKS", 2.6, date(2020, 1, 1)),
    IndexConstituent("M&M", "AUTO", 2.4, date(2020, 1, 1)),
)


def default_nifty50_heavyweights() -> tuple[IndexConstituent, ...]:
    """Top 12 Nifty 50 constituents (~62% of index weight) used for real-time breadth & event risk."""
    return _DEFAULT_HEAVYWEIGHTS


def point_in_time_constituents(
    universe: Sequence[IndexConstituent], on: date
) -> list[IndexConstituent]:
    """Return only constituents that were members of the index on ``on`` (no survivorship bias)."""
    active = [
        c
        for c in universe
        if c.effective_from <= on and (c.effective_to is None or on < c.effective_to)
    ]
    active.sort(key=lambda c: c.weight_pct, reverse=True)
    return active


def compute_constituent_breadth(
    constituents: Sequence[IndexConstituent],
    returns_pct: Mapping[str, float],
) -> ConstituentBreadthSnapshot:
    """Compute free-float-weight-adjusted breadth, dispersion, and implied correlation proxy."""
    adv = 0
    dec = 0
    unch = 0
    total_w = 0.0
    net_adv_w = 0.0
    weighted_ret = 0.0
    sector_weighted_ret: dict[str, float] = {}
    sector_weights: dict[str, float] = {}

    present: list[tuple[float, float]] = []  # (weight, ret)
    for c in constituents:
        if c.symbol not in returns_pct:
            continue
        r = returns_pct[c.symbol]
        w = c.weight_pct
        total_w += w
        weighted_ret += w * r
        present.append((w, r))
        sector_weighted_ret[c.sector] = sector_weighted_ret.get(c.sector, 0.0) + w * r
        sector_weights[c.sector] = sector_weights.get(c.sector, 0.0) + w
        if r > 0.05:
            adv += 1
            net_adv_w += w
        elif r < -0.05:
            dec += 1
            net_adv_w -= w
        else:
            unch += 1

    if total_w <= 0 or not present:
        return ConstituentBreadthSnapshot(0, 0, 0, 0.0, 0.0, 0.0, 0.0, "NONE", "NONE")

    mean_ret = weighted_ret / total_w
    weighted_breadth = (net_adv_w / total_w) * 100.0
    var_cross = sum(w * ((r - mean_ret) ** 2) for w, r in present) / total_w
    dispersion = math.sqrt(max(0.0, var_cross))

    # Ratio of index return magnitude to constituent dispersion gives a fast correlation proxy
    denom = abs(mean_ret) + dispersion
    corr_proxy = (abs(mean_ret) / denom) if denom > 1e-6 else 0.0

    sector_avgs = {
        sec: sector_weighted_ret[sec] / sector_weights[sec]
        for sec in sector_weights
        if sector_weights[sec] > 0
    }
    leader = max(sector_avgs, key=lambda k: sector_avgs[k]) if sector_avgs else "NONE"
    laggard = min(sector_avgs, key=lambda k: sector_avgs[k]) if sector_avgs else "NONE"

    return ConstituentBreadthSnapshot(
        advancing_count=adv,
        declining_count=dec,
        unchanged_count=unch,
        weighted_breadth_pct=round(weighted_breadth, 2),
        weighted_index_return_pct=round(mean_ret, 3),
        cross_sectional_dispersion_pct=round(dispersion, 3),
        implied_correlation_proxy=round(corr_proxy, 3),
        top_sector_leader=leader,
        top_sector_laggard=laggard,
    )
