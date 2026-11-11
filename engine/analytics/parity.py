"""Put-Call Parity & Box-Spread Sanity Monitor (SPEC §4.1).

Not a trading strategy on retail execution (HFT colocation arbitrages true box violations in
microseconds); used as a **real-time data-quality & stale-quote detector**:
- Flags strike pairs where ``|(C_mid - P_mid) - exp(-r * T_cal) * (F - K)|`` exceeds the combined
  half-spread plus a configurable noise tolerance.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from engine.analytics.forward import StrikePairQuote


@dataclass(frozen=True, slots=True)
class ParityViolation:
    strike: float
    residual_pts: float
    half_spread_pts: float
    excess_pts: float
    reason: str


def check_put_call_parity(
    pairs: Sequence[StrikePairQuote],
    *,
    forward: float,
    t_cal_years: float,
    risk_free_rate: float = 0.068,
    tolerance_pts: float = 1.5,
) -> list[ParityViolation]:
    """Return any strikes whose call/put mid prices violate Put-Call Parity beyond spread + tol."""
    df = math.exp(-risk_free_rate * max(t_cal_years, 0.0))
    violations: list[ParityViolation] = []
    for p in pairs:
        if p.call_ask < p.call_bid or p.put_ask < p.put_bid:
            violations.append(
                ParityViolation(
                    strike=p.strike,
                    residual_pts=0.0,
                    half_spread_pts=0.0,
                    excess_pts=0.0,
                    reason="inverted_bid_ask",
                )
            )
            continue
        theo_diff = df * (forward - p.strike)
        obs_diff = p.call_mid - p.put_mid
        residual = abs(obs_diff - theo_diff)
        half_spread = 0.5 * ((p.call_ask - p.call_bid) + (p.put_ask - p.put_bid))
        excess = residual - (half_spread + tolerance_pts)
        if excess > 0:
            violations.append(
                ParityViolation(
                    strike=p.strike,
                    residual_pts=round(residual, 4),
                    half_spread_pts=round(half_spread, 4),
                    excess_pts=round(excess, 4),
                    reason="parity_residual_exceeds_spread",
                )
            )
    return violations
