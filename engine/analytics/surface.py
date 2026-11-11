"""Volatility Smile, Natural Cubic Spline in Log-Moneyness, 25Δ Skew/BF, and Term Structure.

Computes:
1. Natural cubic spline interpolation of implied volatility ``sigma(k)`` over log-moneyness
   ``k = ln(K / F)`` using OTM options (puts for ``K < F``, calls for ``K >= F``).
2. **ATM IV** (``k = 0``), **25Δ Risk Reversal** (``IV_25d_call - IV_25d_put``), and
   **25Δ Butterfly** (``0.5 * (IV_25d_call + IV_25d_put) - IV_ATM``).
3. **Term Structure Slope** (``IV_monthly - IV_weekly``), **Forward Volatility** between two
   expiries ``T1 < T2``: ``sqrt((sigma2^2 * T2 - sigma1^2 * T1) / (T2 - T1))``, and
   **ATM Straddle Implied Move** (pts and %).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from engine.analytics.iv import black76_price, norm_cdf


@dataclass(frozen=True, slots=True)
class SmileNode:
    strike: float
    log_moneyness: float
    iv: float
    delta: float


@dataclass(frozen=True, slots=True)
class VolSurfaceSlice:
    forward: float
    t_var: float
    atm_iv: float
    call_25d_iv: float
    put_25d_iv: float
    risk_reversal_25d: float  # IV(25d Call) - IV(25d Put); negative when put skew dominates
    butterfly_25d: float  # 0.5 * (IV(25d Call) + IV(25d Put)) - IV(ATM)
    atm_straddle_pts: float
    implied_move_1sd_pct: float
    implied_move_straddle80_pts: float
    nodes: tuple[SmileNode, ...]

    def iv_at_strike(self, strike: float) -> float:
        if strike <= 0 or not self.nodes:
            return self.atm_iv
        k = math.log(strike / self.forward)
        ks = [n.log_moneyness for n in self.nodes]
        ivs = [n.iv for n in self.nodes]
        return _eval_piecewise_cubic(ks, ivs, k)


def _eval_piecewise_cubic(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    """Evaluate natural cubic spline (with flat wing extrapolation) at ``x``."""
    n = len(xs)
    if n == 0:
        return 0.0
    if n == 1 or x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    if n == 2:
        t = (x - xs[0]) / max(xs[1] - xs[0], 1e-9)
        return ys[0] + t * (ys[1] - ys[0])

    # Solve tridiagonal system for natural cubic spline second derivatives M_i
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    alpha = [0.0] * n
    for i in range(1, n - 1):
        if h[i - 1] <= 1e-12 or h[i] <= 1e-12:
            continue
        alpha[i] = (3.0 / h[i]) * (ys[i + 1] - ys[i]) - (3.0 / h[i - 1]) * (ys[i] - ys[i - 1])

    l_arr = [1.0] * n
    mu = [0.0] * n
    z = [0.0] * n
    for i in range(1, n - 1):
        denom = 2.0 * (xs[i + 1] - xs[i - 1]) - h[i - 1] * mu[i - 1]
        if abs(denom) <= 1e-12:
            denom = 1e-12
        l_arr[i] = denom
        mu[i] = h[i] / denom
        z[i] = (alpha[i] - h[i - 1] * z[i - 1]) / denom

    c_coeff = [0.0] * n
    b_coeff = [0.0] * (n - 1)
    d_coeff = [0.0] * (n - 1)
    for j in range(n - 2, -1, -1):
        c_coeff[j] = z[j] - mu[j] * c_coeff[j + 1]
        hj = max(h[j], 1e-12)
        b_coeff[j] = (ys[j + 1] - ys[j]) / hj - hj * (c_coeff[j + 1] + 2.0 * c_coeff[j]) / 3.0
        d_coeff[j] = (c_coeff[j + 1] - c_coeff[j]) / (3.0 * hj)

    # Locate interval
    idx = 0
    for i in range(n - 1):
        if xs[i] <= x <= xs[i + 1]:
            idx = i
            break
    dx = x - xs[idx]
    val = ys[idx] + b_coeff[idx] * dx + c_coeff[idx] * dx * dx + d_coeff[idx] * dx * dx * dx
    return max(1e-4, val)


def build_vol_surface_slice(
    strike_ivs: Sequence[tuple[float, float]],
    *,
    forward: float,
    t_var: float,
    t_cal: float | None = None,
    risk_free_rate: float = 0.068,
) -> VolSurfaceSlice:
    """Build a fitted volatility smile slice from ``(strike, iv)`` pairs."""
    if forward <= 0 or t_var <= 0:
        raise ValueError("forward and t_var must be > 0")
    t_disc = t_var if t_cal is None else t_cal
    df = math.exp(-risk_free_rate * max(t_disc, 0.0))

    dedup: dict[float, float] = {}
    for k_strike, iv in strike_ivs:
        if k_strike > 0 and iv > 0:
            dedup[float(k_strike)] = float(iv)
    if not dedup:
        raise ValueError("strike_ivs must contain at least one positive (strike, iv) pair")

    sorted_pairs = sorted(dedup.items(), key=lambda item: item[0])
    ks = [math.log(k / forward) for k, _ in sorted_pairs]
    ivs = [iv for _, iv in sorted_pairs]
    atm_iv = _eval_piecewise_cubic(ks, ivs, 0.0)

    nodes: list[SmileNode] = []
    sqrt_t = math.sqrt(t_var)
    for (strike, iv), k in zip(sorted_pairs, ks, strict=True):
        d1 = (-k + 0.5 * iv * iv * t_var) / (iv * sqrt_t)
        is_call = strike >= forward
        delta = df * norm_cdf(d1) if is_call else -df * norm_cdf(-d1)
        nodes.append(
            SmileNode(
                strike=strike,
                log_moneyness=round(k, 6),
                iv=round(iv, 6),
                delta=round(delta, 4),
            )
        )

    # Locate 25-delta Call (+0.25) and 25-delta Put (-0.25)
    call_nodes = [n for n in nodes if n.strike >= forward]
    put_nodes = [n for n in nodes if n.strike <= forward]
    call_25d_iv = min(call_nodes, key=lambda n: abs(n.delta - 0.25)).iv if call_nodes else atm_iv
    put_25d_iv = min(put_nodes, key=lambda n: abs(n.delta - (-0.25))).iv if put_nodes else atm_iv

    rr_25 = call_25d_iv - put_25d_iv
    bf_25 = 0.5 * (call_25d_iv + put_25d_iv) - atm_iv

    atm_c = black76_price(
        forward, forward, t_var, atm_iv, is_call=True, t_cal=t_disc, risk_free_rate=risk_free_rate
    )
    atm_p = black76_price(
        forward, forward, t_var, atm_iv, is_call=False, t_cal=t_disc, risk_free_rate=risk_free_rate
    )
    straddle = atm_c + atm_p
    move_1sd_pct = atm_iv * sqrt_t * 100.0

    return VolSurfaceSlice(
        forward=round(forward, 4),
        t_var=t_var,
        atm_iv=round(atm_iv, 6),
        call_25d_iv=round(call_25d_iv, 6),
        put_25d_iv=round(put_25d_iv, 6),
        risk_reversal_25d=round(rr_25, 6),
        butterfly_25d=round(bf_25, 6),
        atm_straddle_pts=round(straddle, 2),
        implied_move_1sd_pct=round(move_1sd_pct, 3),
        implied_move_straddle80_pts=round(0.8 * straddle, 2),
        nodes=tuple(nodes),
    )


def compute_forward_vol(sigma1: float, t1: float, sigma2: float, t2: float) -> float | None:
    """Compute implied forward volatility between ``t1`` and ``t2`` (``t2 > t1 > 0``).

    Returns ``None`` if calendar arbitrage is present (``sigma2^2 * t2 < sigma1^2 * t1``).
    """
    if t2 <= t1 or t1 <= 0 or sigma1 <= 0 or sigma2 <= 0:
        return None
    total_var_diff = (sigma2 * sigma2 * t2) - (sigma1 * sigma1 * t1)
    if total_var_diff < 0:
        return None
    return math.sqrt(total_var_diff / (t2 - t1))
