"""Pre-Trade & Intraday Risk Gatekeeper + Consecutive-Loss Circuit Breaker (M8, SPEC §6, PDF §7.4).

The 50% margin headroom, the 0.5x size cut after 3 consecutive losses (no trade at one lot) and
the session halt after 4 losses were APPROVED by the owner on 11 Nov 2026 (docs/DECISIONS.md).
They are stricter than SPEC §6; changing them needs the owner's OK.

Enforces:
1. Per-trade max loss % (2.5% directional, 5.0% defined-risk spread).
2. Daily loss limit (2.0% of capital) & Weekly loss limit (5.0% of capital).
3. Max open risk % (10.0% of capital) & Margin utilization cap (≤ 50% of available margin so ≥ 50%
   headroom remains for intraday SPAN/ELM expansion).
4. **Consecutive-Loss Circuit Breaker (PDF §7.4)**:
   - After 2 consecutive losses: enforces ``cooloff_after_loss_min`` (15 min) pause.
   - After 3 consecutive losses in a session: cuts allowed position size by 50%.
   - After 4 consecutive losses in a session: halts new entries for the rest of the trading day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from engine.core.config import RiskLimits


@dataclass(frozen=True, slots=True)
class AccountRiskState:
    realized_daily_pnl_inr: float = 0.0
    realized_weekly_pnl_inr: float = 0.0
    open_risk_inr: float = 0.0
    margin_used_inr: float = 0.0
    trades_taken_today: int = 0
    consecutive_losses_today: int = 0
    last_loss_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PreTradeGateDecision:
    allowed: bool
    size_scale_factor: float  # 1.0, 0.5 (after 3 consecutive losses), or 0.0 (blocked)
    reasons: tuple[str, ...]


def check_pre_trade_risk_gate(
    limits: RiskLimits,
    state: AccountRiskState,
    *,
    proposed_trade_max_loss_inr: float,
    proposed_margin_inr: float,
    proposed_lots: int,
    strategy_kind: str = "directional",  # "directional" or "defined_risk_spread"
    is_opening_range_strategy: bool = False,
    now: datetime,
    session_open: datetime,
) -> PreTradeGateDecision:
    """Evaluate all hard risk rules before allowing any paper or live order."""
    reasons: list[str] = []
    cap = limits.capital_inr

    # 0. One lot per strategy until 20-30 live trades (CLAUDE.md rule 5); whole lots only
    if proposed_lots < 1:
        reasons.append(f"no_whole_lot ({proposed_lots})")
    elif proposed_lots > limits.max_lots_per_strategy:
        reasons.append(
            f"max_lots_per_strategy_exceeded ({proposed_lots} > {limits.max_lots_per_strategy})"
        )

    # 1. Daily & Weekly loss circuit breakers
    day_lim = limits.inr(limits.daily_loss_limit_pct)
    wk_lim = limits.inr(limits.weekly_loss_limit_pct)
    if state.realized_daily_pnl_inr <= -day_lim:
        reasons.append(
            f"daily_loss_limit_hit ({state.realized_daily_pnl_inr:.0f} <= -{day_lim:.0f})"
        )
    if state.realized_weekly_pnl_inr <= -wk_lim:
        reasons.append(
            f"weekly_loss_limit_hit ({state.realized_weekly_pnl_inr:.0f} <= -{wk_lim:.0f})"
        )

    # 2. Max trades per day & Consecutive-Loss Circuit Breaker (PDF §7.4)
    if state.trades_taken_today >= limits.max_trades_per_day:
        reasons.append(f"max_trades_per_day_reached ({state.trades_taken_today})")
    if state.consecutive_losses_today >= limits.loss_streak_halt_after:
        reasons.append(
            f"consecutive_loss_circuit_breaker ({state.consecutive_losses_today} losses -> "
            "session halt)"
        )

    # 3. Cool-off timer after loss
    if state.last_loss_at is not None and limits.cooloff_after_loss_min > 0:
        elapsed_min = (now - state.last_loss_at).total_seconds() / 60.0
        if elapsed_min < limits.cooloff_after_loss_min:
            reasons.append(
                f"in_loss_cooloff ({elapsed_min:.1f}m < {limits.cooloff_after_loss_min}m)"
            )

    # 4. Opening 15-minute block (except ORB strategy) and afternoon directional cutoff
    mins_since_open = (now - session_open).total_seconds() / 60.0
    if not is_opening_range_strategy and 0 <= mins_since_open < limits.block_first_minutes:
        reasons.append(f"blocked_first_{limits.block_first_minutes}_minutes")

    if strategy_kind == "directional":
        hhmm = now.strftime("%H:%M")
        if hhmm > limits.directional_no_entry_after:
            reasons.append(
                f"after_directional_cutoff ({hhmm} > {limits.directional_no_entry_after})"
            )

    # 5. Per-trade max loss & Open risk cap
    if strategy_kind not in limits.per_trade_max_loss_pct:
        raise ValueError(f"no per-trade loss limit configured for {strategy_kind!r}")
    max_trade_inr = limits.inr(limits.per_trade_max_loss_pct[strategy_kind])
    if proposed_trade_max_loss_inr > max_trade_inr:
        reasons.append(
            f"per_trade_risk_exceeded ({proposed_trade_max_loss_inr:.0f} > {max_trade_inr:.0f})"
        )

    if state.open_risk_inr + proposed_trade_max_loss_inr > limits.inr(limits.max_open_risk_pct):
        reasons.append("max_open_risk_exceeded")

    # 6. 50% Margin headroom rule (SPEC §6)
    tot_margin = state.margin_used_inr + proposed_margin_inr
    max_used = limits.margin_headroom_max_used_pct / 100.0 * cap
    if tot_margin > max_used:
        reasons.append(
            f"margin_headroom_violated (used+proposed {tot_margin:.0f} > "
            f"{limits.margin_headroom_max_used_pct:g}% of {cap:.0f})"
        )

    scale = 0.5 if state.consecutive_losses_today >= limits.loss_streak_size_cut_after else 1.0
    if reasons:
        scale = 0.0
    return PreTradeGateDecision(
        allowed=(len(reasons) == 0),
        size_scale_factor=scale,
        reasons=tuple(reasons),
    )
