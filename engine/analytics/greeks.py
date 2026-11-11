"""1st, 2nd, and 3rd Order Black-76 Option Greeks (SPEC §4.3).

Computes:
- **First Order**: Delta (Δ), Gamma (Γ), Theta (Θ per calendar day), Vega (V per 1% IV), Rho (ρ).
- **Second Order**:
  - **Vanna** (``dDelta / dSigma``): critical before/after IV crushes (RBI MPC, Union Budget).
  - **Charm** (``-dDelta / dTau``): delta bleed over time; drives Tuesday/Thursday expiry pinning.
  - **Vomma / Volga** (``dVega / dSigma``): convexity of vega on OTM wings.
  - **Veta** (``dVega / dTau``): rate of vega decay over time.
- **Third Order**:
  - **Speed** (``dGamma / dF``): how fast gamma accelerates during a stop-run / gamma breakout.
  - **Zomma** (``dGamma / dSigma``): sensitivity of gamma to volatility spikes.
  - **Color** (``dGamma / dTau``): rate of gamma expansion as expiry afternoon approaches.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from engine.analytics.iv import norm_cdf, norm_pdf


@dataclass(frozen=True, slots=True)
class OptionGreeks:
    # 1st order
    delta: float
    gamma: float
    theta_per_day: float
    vega_per_1pct: float
    rho_per_1pct: float
    # 2nd order
    vanna: float
    charm_per_day: float
    vomma: float
    veta: float
    # 3rd order
    speed: float
    zomma: float
    color_per_day: float


def compute_black76_greeks(
    forward: float,
    strike: float,
    t_var: float,
    sigma: float,
    *,
    is_call: bool,
    t_cal: float | None = None,
    risk_free_rate: float = 0.068,
) -> OptionGreeks:
    """Compute analytical 1st, 2nd, and 3rd order Black-76 Greeks."""
    if forward <= 0 or strike <= 0:
        raise ValueError("forward and strike must be > 0")
    t_eff = max(t_var, 1e-6)
    t_disc = t_eff if t_cal is None else max(t_cal, 1e-6)
    sig = max(sigma, 1e-4)
    df = math.exp(-risk_free_rate * t_disc)
    sqrt_t = math.sqrt(t_eff)
    vol_sqrt_t = sig * sqrt_t

    d1 = (math.log(forward / strike) + 0.5 * sig * sig * t_eff) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    nd1 = norm_pdf(d1)
    cdf_d1 = norm_cdf(d1)
    cdf_d2 = norm_cdf(d2)

    # 1st order
    delta = df * cdf_d1 if is_call else -df * norm_cdf(-d1)
    gamma = df * nd1 / (forward * vol_sqrt_t)
    vega_raw = df * forward * nd1 * sqrt_t
    vega_1pct = vega_raw / 100.0

    # Annual theta -> per day (/ 365)
    option_val = (
        df * (forward * cdf_d1 - strike * cdf_d2)
        if is_call
        else df * (strike * norm_cdf(-d2) - forward * norm_cdf(-d1))
    )
    theta_annual = -(df * forward * nd1 * sig) / (2.0 * sqrt_t) + risk_free_rate * option_val
    theta_day = theta_annual / 365.0
    rho_1pct = (-t_disc * option_val) / 100.0

    # 2nd order
    vanna = -df * nd1 * (d2 / sig)
    charm_annual = df * nd1 * (d2 / (2.0 * t_eff)) - risk_free_rate * delta
    charm_day = charm_annual / 365.0
    vomma = vega_raw * (d1 * d2 / sig)
    veta = (
        df
        * forward
        * nd1
        * sqrt_t
        * (risk_free_rate + (d1 * d2) / (2.0 * t_eff) - 1.0 / (2.0 * t_eff))
    )

    # 3rd order
    speed = -(gamma / forward) * (1.0 + d1 / vol_sqrt_t)
    zomma = gamma * ((d1 * d2 - 1.0) / sig)
    color_annual = -gamma * (risk_free_rate + (1.0 - d1 * d2) / (2.0 * t_eff))
    color_day = color_annual / 365.0

    return OptionGreeks(
        delta=round(delta, 6),
        gamma=round(gamma, 8),
        theta_per_day=round(theta_day, 4),
        vega_per_1pct=round(vega_1pct, 4),
        rho_per_1pct=round(rho_1pct, 4),
        vanna=round(vanna, 6),
        charm_per_day=round(charm_day, 6),
        vomma=round(vomma, 4),
        veta=round(veta, 4),
        speed=round(speed, 10),
        zomma=round(zomma, 8),
        color_per_day=round(color_day, 8),
    )
