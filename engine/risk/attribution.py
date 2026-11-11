"""Daily Greek & Cost P&L Attribution (M8, SPEC §4.3, §6).

Decomposes realized + mark-to-market P&L into:
    Actual_PnL = Delta_PnL + Gamma_PnL + Vega_PnL + Theta_PnL + Vanna_Charm_PnL - Charges + Residual
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PnLAttributionBreakdown:
    actual_net_pnl_inr: float
    delta_pnl_inr: float
    gamma_pnl_inr: float
    vega_pnl_inr: float
    theta_pnl_inr: float
    vanna_charm_pnl_inr: float
    charges_inr: float
    unexplained_residual_inr: float


def attribute_option_pnl(
    *,
    actual_net_pnl_inr: float,
    net_delta_units: float,
    net_gamma_units: float,
    vega_inr_per_1pt: float,
    theta_inr_per_day: float,
    vanna_units: float,
    charm_units_per_day: float,
    underlying_change_pts: float,
    iv_change_vol_pts: float,
    days_elapsed: float,
    charges_inr: float,
) -> PnLAttributionBreakdown:
    """Compute Taylor-expansion Greek P&L attribution and unexplained residual."""
    df = underlying_change_pts
    dsig = iv_change_vol_pts
    dt = days_elapsed

    delta_pnl = net_delta_units * df
    gamma_pnl = 0.5 * net_gamma_units * (df * df)
    vega_pnl = vega_inr_per_1pt * dsig
    theta_pnl = theta_inr_per_day * dt
    vanna_charm_pnl = (vanna_units * df * (dsig / 100.0)) + (charm_units_per_day * df * dt)

    explained_net = delta_pnl + gamma_pnl + vega_pnl + theta_pnl + vanna_charm_pnl - charges_inr
    residual = actual_net_pnl_inr - explained_net

    return PnLAttributionBreakdown(
        actual_net_pnl_inr=round(actual_net_pnl_inr, 2),
        delta_pnl_inr=round(delta_pnl, 2),
        gamma_pnl_inr=round(gamma_pnl, 2),
        vega_pnl_inr=round(vega_pnl, 2),
        theta_pnl_inr=round(theta_pnl, 2),
        vanna_charm_pnl_inr=round(vanna_charm_pnl, 2),
        charges_inr=round(charges_inr, 2),
        unexplained_residual_inr=round(residual, 2),
    )
