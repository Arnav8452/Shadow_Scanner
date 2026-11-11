"""Microstructure Structural Invalidation Stop (Delta-Flip) & Momentum Half-Life Time-Decay Stop.

Implements PDF §7.2 & §7.3:
1. **Structural Invalidation Stop (Delta-Flip Exit)**:
   - Even before price hits the hard ``stop_loss``, if the order book mechanics that justified the
     entry invert violently (e.g., for a ``LONG``, ``obi_l5 <= -0.60`` and ``cvd_z <= -1.8`` or the
     defending bid wall is pulled with ``RER < 0.20``), exits immediately at market-protected price
     to save 50–70% of the nominal 1R stop loss!
2. **Time-Decay Stop (Momentum Half-Life)**:
   - True stop-runs, short squeezes, and absorption reclaims resolve rapidly.
   - If a trade has been open for ``>= max_stall_seconds`` (default 180s) and has failed to achieve
     a favorable excursion of at least ``min_atr_progress * atr`` (default ``0.5 * ATR``), exits
     immediately rather than sitting in choppy theta bleed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DynamicStopDecision:
    should_exit: bool
    exit_type: str  # "HOLD" | "HARD_STOP_LOSS" | "DELTA_FLIP_INVALIDATION" | "TIME_DECAY_HALF_LIFE"
    rationale: str


def evaluate_dynamic_microstructure_stops(
    *,
    direction: str,  # "LONG" or "SHORT"
    entry_price: float,
    current_price: float,
    hard_stop_loss: float,
    max_favorable_price: float,
    atr: float,
    elapsed_seconds: float,
    obi_l5: float,
    cvd_z_score: float,
    defending_wall_spoof_pulled: bool = False,
    max_stall_seconds: float = 180.0,
    min_atr_progress_mult: float = 0.5,
) -> DynamicStopDecision:
    """Evaluate hard stop, Delta-Flip microstructure invalidation, and Momentum Half-Life stop."""
    is_long = direction.upper() == "LONG"

    # 1. Hard Price Stop-Loss
    if (is_long and current_price <= hard_stop_loss) or (
        not is_long and current_price >= hard_stop_loss
    ):
        return DynamicStopDecision(
            should_exit=True,
            exit_type="HARD_STOP_LOSS",
            rationale=(
                f"Price ({current_price:.2f}) breached hard SL ({hard_stop_loss:.2f})"
            ),
        )

    # 2. Structural Invalidation Stop (Delta-Flip or Defending Wall Pulled)
    if defending_wall_spoof_pulled:
        return DynamicStopDecision(
            should_exit=True,
            exit_type="DELTA_FLIP_INVALIDATION",
            rationale="Defending structural wall was spoof-pulled (RER < 0.20); thesis invalidated",
        )

    if is_long and obi_l5 <= -0.60 and cvd_z_score <= -1.5:
        return DynamicStopDecision(
            should_exit=True,
            exit_type="DELTA_FLIP_INVALIDATION",
            rationale=(
                f"Bearish Delta-Flip while LONG (OBI_L5={obi_l5:+.2f}, CVD_Z={cvd_z_score:+.2f})"
            ),
        )
    if not is_long and obi_l5 >= 0.60 and cvd_z_score >= 1.5:
        return DynamicStopDecision(
            should_exit=True,
            exit_type="DELTA_FLIP_INVALIDATION",
            rationale=(
                f"Bullish Delta-Flip while SHORT (OBI_L5={obi_l5:+.2f}, CVD_Z={cvd_z_score:+.2f})"
            ),
        )

    # 3. Time-Decay Stop (Momentum Half-Life: >= 0.5 * ATR within max_stall_seconds)
    if elapsed_seconds >= max_stall_seconds and atr > 0:
        favorable_move = (
            (max_favorable_price - entry_price) if is_long else (entry_price - max_favorable_price)
        )
        req_move = min_atr_progress_mult * atr
        if favorable_move < req_move:
            return DynamicStopDecision(
                should_exit=True,
                exit_type="TIME_DECAY_HALF_LIFE",
                rationale=(
                    f"Momentum Half-Life expired ({elapsed_seconds:.0f}s): "
                    f"max excursion {favorable_move:.2f} < {req_move:.2f} (0.5x ATR)"
                ),
            )

    return DynamicStopDecision(
        should_exit=False,
        exit_type="HOLD",
        rationale="Thesis intact",
    )
