"""Breeden-Litzenberger Risk-Neutral Density Extractor (SPEC §4.2, §4.6).

Extracts the risk-neutral probability density ``q(K)`` from a fitted :class:`VolSurfaceSlice`:
    q(K) = exp(r * T_cal) * d^2 C(K) / dK^2
Computes left/right tail probabilities and implied skewness to quantify crash-fear pricing before
major macro/earnings events.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from engine.analytics.iv import black76_price
from engine.analytics.surface import VolSurfaceSlice


@dataclass(frozen=True, slots=True)
class RiskNeutralDensity:
    forward: float
    strikes: tuple[float, ...]
    densities: tuple[float, ...]
    prob_below_minus_2pct: float
    prob_above_plus_2pct: float
    implied_skewness: float


def extract_risk_neutral_density(
    surface: VolSurfaceSlice,
    *,
    t_cal: float | None = None,
    risk_free_rate: float = 0.068,
    n_grid: int = 41,
    span_pct: float = 0.08,
) -> RiskNeutralDensity:
    """Extract normalized Breeden-Litzenberger density across ``forward * (1 ± span_pct)``."""
    t_disc = surface.t_var if t_cal is None else t_cal
    df_inv = math.exp(risk_free_rate * max(t_disc, 0.0))
    f = surface.forward
    k_min = f * (1.0 - span_pct)
    k_max = f * (1.0 + span_pct)
    dk = (k_max - k_min) / max(2, n_grid - 1)

    strikes: list[float] = []
    raw_q: list[float] = []

    for i in range(n_grid):
        k = k_min + i * dk
        k_dn = max(1.0, k - dk)
        k_up = k + dk
        c_dn = black76_price(
            f,
            k_dn,
            surface.t_var,
            surface.iv_at_strike(k_dn),
            is_call=True,
            t_cal=t_disc,
            risk_free_rate=risk_free_rate,
        )
        c_mid = black76_price(
            f,
            k,
            surface.t_var,
            surface.iv_at_strike(k),
            is_call=True,
            t_cal=t_disc,
            risk_free_rate=risk_free_rate,
        )
        c_up = black76_price(
            f,
            k_up,
            surface.t_var,
            surface.iv_at_strike(k_up),
            is_call=True,
            t_cal=t_disc,
            risk_free_rate=risk_free_rate,
        )
        d2c = (c_up - 2.0 * c_mid + c_dn) / (dk * dk)
        strikes.append(round(k, 2))
        raw_q.append(max(0.0, df_inv * d2c))

    area = sum(raw_q) * dk
    norm_q = [q / area if area > 0 else 1.0 / (k_max - k_min) for q in raw_q]

    p_left = sum(q * dk for k, q in zip(strikes, norm_q, strict=True) if k <= f * 0.98)
    p_right = sum(q * dk for k, q in zip(strikes, norm_q, strict=True) if k >= f * 1.02)

    mean_k = sum(k * q * dk for k, q in zip(strikes, norm_q, strict=True))
    var_k = sum(((k - mean_k) ** 2) * q * dk for k, q in zip(strikes, norm_q, strict=True))
    sd_k = math.sqrt(max(var_k, 1e-9))
    skew = (
        sum((((k - mean_k) / sd_k) ** 3) * q * dk for k, q in zip(strikes, norm_q, strict=True))
        if sd_k > 1e-4
        else 0.0
    )

    return RiskNeutralDensity(
        forward=f,
        strikes=tuple(strikes),
        densities=tuple(round(q, 8) for q in norm_q),
        prob_below_minus_2pct=round(min(1.0, max(0.0, p_left)), 4),
        prob_above_plus_2pct=round(min(1.0, max(0.0, p_right)), 4),
        implied_skewness=round(skew, 4),
    )
