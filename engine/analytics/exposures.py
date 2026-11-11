"""Portfolio Rupee Exposures, Spot × IV × Time Stress Grid, and Dealer Gamma Exposure (GEX).

Implements SPEC §4.3, §4.9, §6, and PDF §3.2:
1. **Rupee Greeks**:
   - ``delta_inr``: ₹ change per 1% underlying move (or per point).
   - ``gamma_inr_1pct``: ₹ delta change for a 1% underlying move.
   - ``vega_inr_1pt``: ₹ P&L for a +1 vol point (+0.01) IV shift.
   - ``theta_inr_day``: ₹ P&L for 1 day of time decay.
2. **Spot × IV × Time Stress Matrix**:
   - Evaluates exact Black-76 repricing across configurable spot shocks (e.g., ±1%, ±2%, ±3%, ±5%)
     and IV shocks (e.g., -3, 0, +3, +5, +10 vol pts).
3. **Dealer Gamma Exposure (GEX) & Gamma-Flip Estimator**:
   - Computes strike-by-strike GEX under explicit sign assumptions (standard assumption: customers
     are net buyers of OTM puts and net sellers/buyers of calls, configurable via
     ``call_dealer_sign`` and ``put_dealer_sign``).
   - Locates the **Gamma-Flip Strike** where net dealer gamma crosses zero:
     - Spot > Gamma-Flip (Positive Dealer Gamma): dealers buy dips & sell rips -> pins price,
       favoring Liquidity Absorption Reversals & Iron Condors.
     - Spot < Gamma-Flip (Negative Dealer Gamma): dealers hedge in the direction of the move ->
       amplifies Stop-Runs, Short-Squeeze Cascades, and ORB breakouts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from engine.analytics.greeks import compute_black76_greeks
from engine.analytics.iv import black76_price


@dataclass(frozen=True, slots=True)
class OptionPositionLeg:
    symbol: str
    strike: float
    is_call: bool
    signed_qty: int  # +qty for long, -qty for short (in total shares/units = lots * lot_size)
    entry_price: float
    mark_price: float
    iv: float
    t_var: float
    t_cal: float | None = None


@dataclass(frozen=True, slots=True)
class PortfolioRupeeGreeks:
    net_delta_units: float
    delta_inr_1pct: float
    gamma_inr_1pct: float
    vega_inr_1pt: float
    theta_inr_day: float
    vanna_inr: float
    charm_units_day: float


@dataclass(frozen=True, slots=True)
class StressScenarioResult:
    spot_shock_pct: float
    iv_shock_pts: float
    pnl_inr: float


@dataclass(frozen=True, slots=True)
class ChainGEXNode:
    strike: float
    call_oi: int
    put_oi: int
    call_iv: float
    put_iv: float


@dataclass(frozen=True, slots=True)
class GEXProfile:
    forward: float
    net_gex_inr_per_1pct: float
    gamma_flip_level: float
    max_positive_gamma_strike: float
    max_negative_gamma_strike: float
    regime: str  # POSITIVE_GAMMA_PIN | NEGATIVE_GAMMA_ACCELERATION


def compute_portfolio_rupee_greeks(
    legs: Sequence[OptionPositionLeg],
    *,
    forward: float,
    risk_free_rate: float = 0.068,
) -> PortfolioRupeeGreeks:
    """Aggregate portfolio Greeks in ₹ terms across all option legs."""
    net_delta = 0.0
    net_gamma = 0.0
    vega_inr = 0.0
    theta_inr = 0.0
    vanna_sum = 0.0
    charm_sum = 0.0

    for leg in legs:
        g = compute_black76_greeks(
            forward,
            leg.strike,
            leg.t_var,
            leg.iv,
            is_call=leg.is_call,
            t_cal=leg.t_cal,
            risk_free_rate=risk_free_rate,
        )
        q = float(leg.signed_qty)
        net_delta += q * g.delta
        net_gamma += q * g.gamma
        vega_inr += q * g.vega_per_1pct
        theta_inr += q * g.theta_per_day
        vanna_sum += q * g.vanna
        charm_sum += q * g.charm_per_day

    move_1pct = forward * 0.01
    delta_inr_1pct = net_delta * move_1pct
    gamma_inr_1pct = 0.5 * net_gamma * (move_1pct**2)

    return PortfolioRupeeGreeks(
        net_delta_units=round(net_delta, 4),
        delta_inr_1pct=round(delta_inr_1pct, 2),
        gamma_inr_1pct=round(gamma_inr_1pct, 2),
        vega_inr_1pt=round(vega_inr, 2),
        theta_inr_day=round(theta_inr, 2),
        vanna_inr=round(vanna_sum * move_1pct * 0.01, 2),
        charm_units_day=round(charm_sum, 4),
    )


def compute_stress_grid(
    legs: Sequence[OptionPositionLeg],
    *,
    forward: float,
    spot_shocks_pct: Sequence[float] = (-5.0, -3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 5.0),
    iv_shocks_pts: Sequence[float] = (-3.0, 0.0, 3.0, 5.0, 10.0),
    days_elapsed: float = 0.0,
    risk_free_rate: float = 0.068,
) -> list[StressScenarioResult]:
    """Full nonlinear Black-76 repricing stress grid across Spot × IV × Time shocks."""
    results: list[StressScenarioResult] = []
    dt_years = max(0.0, days_elapsed) / 365.0
    for s_pct in spot_shocks_pct:
        f_shocked = forward * (1.0 + s_pct / 100.0)
        for v_pts in iv_shocks_pts:
            pnl = 0.0
            for leg in legs:
                t_var_new = max(1e-5, leg.t_var - dt_years)
                t_cal_new = max(1e-5, (leg.t_cal or leg.t_var) - dt_years)
                iv_new = max(0.01, leg.iv + v_pts / 100.0)
                new_px = black76_price(
                    f_shocked,
                    leg.strike,
                    t_var_new,
                    iv_new,
                    is_call=leg.is_call,
                    t_cal=t_cal_new,
                    risk_free_rate=risk_free_rate,
                )
                pnl += leg.signed_qty * (new_px - leg.mark_price)
            results.append(
                StressScenarioResult(
                    spot_shock_pct=s_pct,
                    iv_shock_pts=v_pts,
                    pnl_inr=round(pnl, 2),
                )
            )
    return results


def compute_dealer_gex_profile(
    nodes: Sequence[ChainGEXNode],
    *,
    forward: float,
    t_var: float,
    call_dealer_sign: float = 1.0,
    put_dealer_sign: float = -1.0,
    risk_free_rate: float = 0.068,
) -> GEXProfile:
    """Compute Dealer Gamma Exposure (GEX) and the Gamma-Flip level across the option chain."""
    if not nodes or forward <= 0:
        return GEXProfile(forward, 0.0, forward, forward, forward, "POSITIVE_GAMMA_PIN")

    move_1pct = forward * 0.01
    strike_gex: dict[float, float] = {}
    for node in nodes:
        cg = compute_black76_greeks(
            forward,
            node.strike,
            t_var,
            max(node.call_iv, 0.05),
            is_call=True,
            risk_free_rate=risk_free_rate,
        ).gamma
        pg = compute_black76_greeks(
            forward,
            node.strike,
            t_var,
            max(node.put_iv, 0.05),
            is_call=False,
            risk_free_rate=risk_free_rate,
        ).gamma
        gex_inr = (call_dealer_sign * node.call_oi * cg + put_dealer_sign * node.put_oi * pg) * (
            move_1pct * forward
        )
        strike_gex[node.strike] = gex_inr

    net_gex = sum(strike_gex.values())
    max_pos_strike = max(strike_gex, key=lambda k: strike_gex[k])
    max_neg_strike = min(strike_gex, key=lambda k: strike_gex[k])

    # Estimate zero-crossing strike between max negative and max positive gamma strikes
    sorted_strikes = sorted(strike_gex)
    flip = forward
    for i in range(len(sorted_strikes) - 1):
        k1, k2 = sorted_strikes[i], sorted_strikes[i + 1]
        g1, g2 = strike_gex[k1], strike_gex[k2]
        if g1 <= 0 <= g2 and (g2 - g1) > 1e-9:
            flip = k1 + (-g1 / (g2 - g1)) * (k2 - k1)
            break

    regime = "POSITIVE_GAMMA_PIN" if net_gex >= 0 else "NEGATIVE_GAMMA_ACCELERATION"
    return GEXProfile(
        forward=round(forward, 2),
        net_gex_inr_per_1pct=round(net_gex, 2),
        gamma_flip_level=round(flip, 2),
        max_positive_gamma_strike=max_pos_strike,
        max_negative_gamma_strike=max_neg_strike,
        regime=regime,
    )
