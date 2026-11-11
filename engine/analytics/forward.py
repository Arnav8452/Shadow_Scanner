"""Synthetic forward & Put-Call Parity implied basis (SPEC §4.1).

Computes the risk-neutral synthetic forward price from the 3–5 liquid strikes closest to ATM:
    F_i = K_i + exp(r * T_cal) * (C_mid(K_i) - P_mid(K_i))
    F   = median(F_i)
Why we never use raw spot ``S`` directly in Black-76:
- NSE index options price off the forward ``F`` (which embeds the futures cost-of-carry and any
  expected discrete dividends before expiry). Using ``S`` shifts ATM by the basis ``F - S`` and
  corrupts call vs put IVs in opposite directions.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StrikePairQuote:
    strike: float
    call_bid: float
    call_ask: float
    put_bid: float
    put_ask: float

    @property
    def call_mid(self) -> float:
        return 0.5 * (self.call_bid + self.call_ask)

    @property
    def put_mid(self) -> float:
        return 0.5 * (self.put_bid + self.put_ask)

    @property
    def call_spread_pct(self) -> float:
        mid = self.call_mid
        return ((self.call_ask - self.call_bid) / mid * 100.0) if mid > 0 else 999.0

    @property
    def put_spread_pct(self) -> float:
        mid = self.put_mid
        return ((self.put_ask - self.put_bid) / mid * 100.0) if mid > 0 else 999.0


@dataclass(frozen=True, slots=True)
class SyntheticForwardResult:
    forward: float
    spot: float
    implied_basis: float
    implied_carry_rate_pa: float
    strikes_used: tuple[float, ...]
    per_strike_forwards: tuple[float, ...]
    dispersion_pts: float


def compute_synthetic_forward(
    pairs: Sequence[StrikePairQuote],
    *,
    spot: float,
    t_cal_years: float,
    risk_free_rate: float = 0.068,
    max_strikes: int = 5,
    max_rel_spread_pct: float = 15.0,
) -> SyntheticForwardResult:
    """Estimate synthetic forward ``F`` from near-ATM call/put mid prices via Put-Call Parity."""
    if spot <= 0:
        raise ValueError("spot must be > 0")
    t_eff = max(t_cal_years, 1e-6)
    df_inv = math.exp(risk_free_rate * t_eff)

    valid = [
        p
        for p in pairs
        if p.call_bid > 0
        and p.call_ask >= p.call_bid
        and p.put_bid > 0
        and p.put_ask >= p.put_bid
        and p.call_spread_pct <= max_rel_spread_pct
        and p.put_spread_pct <= max_rel_spread_pct
    ]
    if not valid:
        # Fallback to cost-of-carry forward if no clean strike pairs pass liquidity filter
        f_fallback = spot * df_inv
        return SyntheticForwardResult(
            forward=round(f_fallback, 4),
            spot=spot,
            implied_basis=round(f_fallback - spot, 4),
            implied_carry_rate_pa=risk_free_rate,
            strikes_used=(),
            per_strike_forwards=(),
            dispersion_pts=0.0,
        )

    valid.sort(key=lambda p: abs(p.strike - spot))
    chosen = valid[: max(1, max_strikes)]
    fwds = [p.strike + df_inv * (p.call_mid - p.put_mid) for p in chosen]
    f_med = float(statistics.median(fwds))
    disp = float(statistics.pstdev(fwds)) if len(fwds) > 1 else 0.0
    carry = math.log(max(f_med, 1e-6) / spot) / t_eff

    return SyntheticForwardResult(
        forward=round(f_med, 4),
        spot=spot,
        implied_basis=round(f_med - spot, 4),
        implied_carry_rate_pa=round(carry, 6),
        strikes_used=tuple(p.strike for p in chosen),
        per_strike_forwards=tuple(round(x, 4) for x in fwds),
        dispersion_pts=round(disp, 4),
    )
