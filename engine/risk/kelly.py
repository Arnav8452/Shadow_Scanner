"""Dynamic Fractional Quarter-Kelly Position Sizer & Volatility-Targeting Overlay (M8, PDF §7.1).

Implements:
1. **Rolling Empirical Quarter-Kelly Formula (PDF §7.1)**:
   ``f* = p_hat - (1 - p_hat) / b_hat``
   ``risk_pct = min(max_risk_pct, max(0.0, kelly_fraction * f* * 100.0))``
   - Uses ``kelly_fraction = 0.25`` (Quarter-Kelly) and clamps ``risk_pct <= 1.5%`` to protect
     against fat-tailed Indian F&O gap risk and parameter estimation error.
2. **Volatility-Targeting Overlay (SPEC §5.7)**:
   ``vol_scale = min(max_leverage, target_vol_pa / max(realized_vol_yz, 0.05))``
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KellySizingResult:
    win_rate: float
    payoff_ratio: float
    full_kelly_pct: float
    quarter_kelly_pct: float
    vol_target_scale: float
    final_risk_pct: float
    risk_budget_inr: float
    recommended_lots: int
    recommended_qty: int


def compute_dynamic_quarter_kelly(
    recent_net_pnls: Sequence[float],
    *,
    capital_inr: float,
    stop_distance_pts: float,
    lot_size: int = 1,
    kelly_fraction: float = 0.25,
    max_risk_pct: float = 1.5,
    cold_start_risk_pct: float = 0.50,
    min_trades_for_empirical: int = 10,
    target_vol_pa: float = 0.15,
    realized_vol_yz: float = 0.15,
    max_lots_cap: int = 1,  # CLAUDE.md rule 5: one lot until 20-30 live trades
) -> KellySizingResult:
    """Compute position size combining Empirical Quarter-Kelly and Volatility-Targeting."""
    vol_scale = min(1.5, max(0.25, target_vol_pa / max(realized_vol_yz, 0.05)))

    if len(recent_net_pnls) < min_trades_for_empirical:
        base_pct = cold_start_risk_pct
        win_rate = 0.5
        payoff = 1.8
        full_k = 2.0
    else:
        wins = [p for p in recent_net_pnls if p > 0]
        losses = [abs(p) for p in recent_net_pnls if p <= 0]
        if not wins:
            win_rate = 0.0
            payoff = 0.0
            full_k = 0.0
            base_pct = 0.0
        elif not losses:
            win_rate = 1.0
            payoff = 3.0
            full_k = max_risk_pct / max(kelly_fraction, 0.01)
            base_pct = max_risk_pct
        else:
            win_rate = len(wins) / len(recent_net_pnls)
            avg_w = sum(wins) / len(wins)
            avg_l = sum(losses) / len(losses)
            payoff = avg_w / max(avg_l, 1e-6)
            f_star = win_rate - (1.0 - win_rate) / payoff
            full_k = max(0.0, f_star * 100.0)
            base_pct = max(0.0, kelly_fraction * full_k)

    final_pct = min(max_risk_pct, base_pct * vol_scale)
    risk_inr = capital_inr * (final_pct / 100.0)
    per_lot_risk = max(0.05, stop_distance_pts) * max(1, lot_size)
    raw_lots = int(risk_inr // per_lot_risk) if final_pct > 0 else 0
    # Never round up: if one lot risks more than the budget, the honest size is zero.
    lots = max(0, min(max_lots_cap, raw_lots))

    return KellySizingResult(
        win_rate=round(win_rate, 4),
        payoff_ratio=round(payoff, 4),
        full_kelly_pct=round(full_k, 3),
        quarter_kelly_pct=round(min(max_risk_pct, base_pct), 3),
        vol_target_scale=round(vol_scale, 3),
        final_risk_pct=round(final_pct, 3),
        risk_budget_inr=round(risk_inr, 2),
        recommended_lots=lots,
        recommended_qty=lots * max(1, lot_size),
    )
