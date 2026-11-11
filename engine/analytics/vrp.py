"""Variance Risk Premium (VRP), IV Rank (IVR), and IV Percentile (IVP) (SPEC §4.5, §5.1).

Computes:
- ``vrp_pts = (iv_atm - rv_yz) * 100`` (in vol points): positive when options price more movement
  than realized volatility delivers (structural edge for Iron Condor when ``VRP > 2.0``).
- ``iv_rank``: ``(iv - min_1y) / (max_1y - min_1y) * 100``
- ``iv_percentile``: percentage of historical observations over the lookback window strictly below
  current ``iv``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VRPSnapshot:
    iv_atm: float
    rv_yz: float
    vrp: float  # iv_atm - rv_yz (decimal)
    vrp_vol_pts: float  # (iv_atm - rv_yz) * 100
    iv_rank_pct: float
    iv_percentile_pct: float
    short_vol_favorable: bool  # True when VRP > 0 and IVP in [25, 85]


def compute_vrp_snapshot(
    iv_atm: float,
    rv_yz: float,
    historical_ivs: Sequence[float] = (),
) -> VRPSnapshot:
    vrp = iv_atm - rv_yz
    vrp_pts = vrp * 100.0
    if historical_ivs:
        lo = min(historical_ivs)
        hi = max(historical_ivs)
        ivr = ((iv_atm - lo) / (hi - lo) * 100.0) if hi > lo else 50.0
        ivr = max(0.0, min(100.0, ivr))
        below = sum(1 for x in historical_ivs if x < iv_atm)
        ivp = (below / len(historical_ivs)) * 100.0
    else:
        ivr = 50.0
        ivp = 50.0

    favorable = vrp_pts >= 1.5 and 25.0 <= ivp <= 85.0
    return VRPSnapshot(
        iv_atm=round(iv_atm, 6),
        rv_yz=round(rv_yz, 6),
        vrp=round(vrp, 6),
        vrp_vol_pts=round(vrp_pts, 2),
        iv_rank_pct=round(ivr, 2),
        iv_percentile_pct=round(ivp, 2),
        short_vol_favorable=favorable,
    )
