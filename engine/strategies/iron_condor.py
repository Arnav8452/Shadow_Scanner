"""Strategy v1: Defined-Risk Iron Condor & Credit Spreads with Event-Risk Filter (M7, SPEC §5.1).

Interconnected Confluence Requirements:
- ``vrp.short_vol_favorable`` is True (``VRP >= 1.5 vol pts`` and ``25 <= IVP <= 85``).
- ``gex.regime == "POSITIVE_GAMMA_PIN"`` (dealers dampen intraday excursions).
- ``event_status.short_vega_scale_factor > 0`` (not inside an RBI MPC / Budget blackout or heavy
  Nifty constituent earnings cluster).
- Strictly hedge-first leg sequencing: buys ~6Δ–8Δ protective wings in Stage 1 *before* selling
  ~16Δ–20Δ short strikes in Stage 2.
"""

from __future__ import annotations

from engine.confluence.engine import InterconnectedConfluencePacket
from engine.core.config import EngineVersion
from engine.execution.leg_sequencer import SpreadLegIntent, sequence_entry_legs
from engine.strategies.base import StrategyDecision


def evaluate_iron_condor(
    packet: InterconnectedConfluencePacket,
    *,
    short_put_strike: float,
    long_put_strike: float,
    short_call_strike: float,
    long_call_strike: float,
    short_put_px: float,
    long_put_px: float,
    short_call_px: float,
    long_call_px: float,
    lot_size: int = 65,
    lots: int = 1,
    use_event_risk_filter: bool = True,
) -> StrategyDecision:
    """Evaluate Iron Condor entry using VRP, GEX, and Event-Risk Filter (A/B testable)."""
    ver = packet.engine_version
    if use_event_risk_filter and packet.event_status.short_vega_scale_factor <= 0.0:
        return StrategyDecision(
            strategy_name="iron_condor",
            symbol=packet.symbol,
            engine_version=ver,
            triggered=False,
            direction="FLAT",
            shadow_signal=packet.shadow_signal,
            stage1_hedge_legs=(),
            stage2_risk_legs=(),
            max_loss_inr=0.0,
            target_profit_inr=0.0,
            rationale="Blocked by Event-Risk Filter (macro blackout or heavy 24h earnings)",
        )

    if not packet.vrp.short_vol_favorable or packet.gex.regime != "POSITIVE_GAMMA_PIN":
        return StrategyDecision(
            strategy_name="iron_condor",
            symbol=packet.symbol,
            engine_version=ver,
            triggered=False,
            direction="FLAT",
            shadow_signal=packet.shadow_signal,
            stage1_hedge_legs=(),
            stage2_risk_legs=(),
            max_loss_inr=0.0,
            target_profit_inr=0.0,
            rationale=(
                f"Regime unfavorable for Iron Condor: VRP={packet.vrp.vrp_vol_pts:+.1f}pts, "
                f"GEX={packet.gex.regime}"
            ),
        )

    scale = packet.event_status.short_vega_scale_factor if use_event_risk_filter else 1.0
    eff_lots = int(lots * scale)  # never round a reduced size back up to a full lot
    if eff_lots < 1:
        return StrategyDecision(
            strategy_name="iron_condor",
            symbol=packet.symbol,
            engine_version=ver,
            triggered=False,
            direction="FLAT",
            shadow_signal=packet.shadow_signal,
            stage1_hedge_legs=(),
            stage2_risk_legs=(),
            max_loss_inr=0.0,
            target_profit_inr=0.0,
            rationale=f"Event-risk scale {scale:.2f} leaves less than one lot",
        )
    qty = eff_lots * max(1, lot_size)
    net_credit_pts = (short_put_px - long_put_px) + (short_call_px - long_call_px)
    wing_width_pts = max(short_put_strike - long_put_strike, long_call_strike - short_call_strike)
    max_loss_pts = max(0.05, wing_width_pts - net_credit_pts)

    raw_legs = [
        SpreadLegIntent(f"{packet.symbol}_{int(long_put_strike)}PE", "BUY", qty, long_put_px, True),
        SpreadLegIntent(
            f"{packet.symbol}_{int(long_call_strike)}CE", "BUY", qty, long_call_px, True
        ),
        SpreadLegIntent(
            f"{packet.symbol}_{int(short_put_strike)}PE", "SELL", qty, short_put_px, False
        ),
        SpreadLegIntent(
            f"{packet.symbol}_{int(short_call_strike)}CE", "SELL", qty, short_call_px, False
        ),
    ]
    st1, st2 = sequence_entry_legs(raw_legs)

    return StrategyDecision(
        strategy_name="iron_condor",
        symbol=packet.symbol,
        engine_version=ver,
        triggered=True,
        direction="NEUTRAL_DEFINED_RISK",
        shadow_signal=packet.shadow_signal,
        stage1_hedge_legs=tuple(st1) if ver is EngineVersion.V2_BOT else (),
        stage2_risk_legs=tuple(st2) if ver is EngineVersion.V2_BOT else (),
        max_loss_inr=round(max_loss_pts * qty, 2),
        target_profit_inr=round(0.50 * net_credit_pts * qty, 2),
        rationale=(
            f"Iron Condor triggered: VRP={packet.vrp.vrp_vol_pts:+.1f}pts, "
            f"GEX={packet.gex.regime}, Credit={net_credit_pts:.2f}pts, "
            f"MaxLoss=₹{max_loss_pts * qty:.0f}"
        ),
    )
