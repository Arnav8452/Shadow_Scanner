"""Strategy kill rules (Definition of Done step: "kill rules written" before any live lot).

A strategy is switched off when its own record says the edge is gone, not when the account's
daily limits trip (that is the kill switch). Evaluated on the strategy's closed trades in order:

- **Drawdown:** the cumulative net P&L falls ``max_drawdown_inr`` below its running peak within
  the last ``window_trades`` trades (default Rs 10,000 over 15 trades, the plan's example);
- **Losing streak:** ``max_consecutive_losses`` losing trades in a row (default 5).

On paper the verdict is reported (the scorecard), never acted on: paper keeps collecting data.
Live, a killed strategy places no new entries until the owner re-enables it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class KillRules:
    max_drawdown_inr: float = 10_000.0
    window_trades: int = 15
    max_consecutive_losses: int = 5


@dataclass(frozen=True)
class KillVerdict:
    killed: bool
    reasons: tuple[str, ...]
    drawdown_inr: float
    losing_streak: int


def evaluate_kill_rules(
    net_per_trade: Sequence[float], rules: KillRules | None = None
) -> KillVerdict:
    """``net_per_trade``: each closed trade's net P&L in rupees, oldest first."""
    rules = rules or KillRules()
    window = (
        list(net_per_trade)[-rules.window_trades :]
        if rules.window_trades > 0
        else list(net_per_trade)
    )
    run = peak = 0.0
    dd = 0.0
    for x in window:
        run += x
        peak = max(peak, run)
        dd = max(dd, peak - run)
    streak = best = 0
    for x in net_per_trade:
        streak = streak + 1 if x < 0 else 0
        best = max(best, streak)
    reasons = []
    if rules.max_drawdown_inr > 0 and dd >= rules.max_drawdown_inr:
        reasons.append(
            f"drawdown Rs {dd:,.0f} >= Rs {rules.max_drawdown_inr:,.0f} within the last "
            f"{len(window)} trades"
        )
    if rules.max_consecutive_losses > 0 and best >= rules.max_consecutive_losses:
        reasons.append(f"{best} losing trades in a row (limit {rules.max_consecutive_losses})")
    return KillVerdict(bool(reasons), tuple(reasons), round(dd, 2), best)


__all__ = ["KillRules", "KillVerdict", "evaluate_kill_rules"]
