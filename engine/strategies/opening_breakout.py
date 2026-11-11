"""Strategy v3: 'Log-2-style' Opening-Range Breakout (ORB) with Anti-Spoofing Filter (M11).

Combines 5m/15m Opening Range Breakout with the PDF's Stop-Run & Short-Squeeze Cascade rules:
- Requires price breaking above ``ORH_15M`` (or below ``ORL_15M``) with:
  1. ``real_execution_ratio >= 0.75`` (confirming the opening wall was genuinely consumed).
  2. ``obi_l5 >= 0.50`` (or ``<= -0.50`` for downside breakdown).
  3. Overhead/underfoot L6–L25 ``air_pocket_ratio < 0.55`` (liquidity vacuum for rapid expansion).
"""

from __future__ import annotations

from engine.confluence.engine import InterconnectedConfluencePacket
from engine.core.config import EngineVersion
from engine.execution.leg_sequencer import SpreadLegIntent
from engine.shadow.models import SignalWave
from engine.strategies.base import StrategyDecision


def evaluate_opening_breakout(
    packet: InterconnectedConfluencePacket,
    *,
    option_symbol: str,
    option_ltp: float,
    lot_size: int = 65,
    lots: int = 1,
) -> StrategyDecision:
    """Evaluate Opening-Range Breakout (ORB) gated by L1–L30 Air-Pocket and Anti-Spoofing RER."""
    ver = packet.engine_version
    sig = packet.shadow_signal

    if (
        sig is None
        or sig.confidence_score < 75.0
        or sig.real_execution_ratio < 0.75
        or sig.air_pocket_ratio >= 0.60
        or sig.wave not in {SignalWave.WALL_BREAKOUT_SQUEEZE, SignalWave.STOP_HUNT_RECLAIM}
    ):
        return StrategyDecision(
            strategy_name="opening_breakout",
            symbol=packet.symbol,
            engine_version=ver,
            triggered=False,
            direction="FLAT",
            shadow_signal=sig,
            stage1_hedge_legs=(),
            stage2_risk_legs=(),
            max_loss_inr=0.0,
            target_profit_inr=0.0,
            rationale="ORB requires score ≥ 75, RER ≥ 0.75, and L6-L25 Air-Pocket < 0.60",
        )

    qty = max(1, lots) * max(1, lot_size)
    leg = SpreadLegIntent(
        symbol=option_symbol,
        side="BUY",
        qty=qty,
        limit_price=round(option_ltp * 1.005, 2),
        is_protective_hedge=True,
    )
    risk_inr = 0.25 * option_ltp * qty

    return StrategyDecision(
        strategy_name="opening_breakout",
        symbol=packet.symbol,
        engine_version=ver,
        triggered=True,
        direction=sig.direction,
        shadow_signal=sig,
        stage1_hedge_legs=(leg,) if ver is EngineVersion.V2_BOT else (),
        stage2_risk_legs=(),
        max_loss_inr=round(risk_inr, 2),
        target_profit_inr=round(risk_inr * sig.rr_t1, 2),
        rationale=(
            f"ORB Squeeze ({sig.direction}): Score={sig.confidence_score:.1f}/100, "
            f"RER={sig.real_execution_ratio:.2f}, AirPocket={sig.air_pocket_ratio:.2f}"
        ),
    )
