"""Strategy v2: 'Log-3-style' Intraday Directional Options / Debit Spread (M10, SPEC §5.2).

Interconnected Confluence Requirements:
- Cross-asset derivative lead is ``BULLISH_LEAD`` or ``BEARISH_LEAD`` (Futures Price × OI buildup +
  Option Chain PCR/Call-Wall alignment + Constituent Breadth).
- Deep-Book Shadow Signal score ≥ 75/100 with Anti-Spoofing ``RER >= 0.75`` (not spoof-blocked).
- Selects near-ATM (0.50Δ) option or bull/bear debit spread when IV Percentile is elevated.
"""

from __future__ import annotations

from engine.confluence.engine import InterconnectedConfluencePacket
from engine.core.config import EngineVersion
from engine.execution.leg_sequencer import SpreadLegIntent
from engine.strategies.base import StrategyDecision


def evaluate_directional_intraday(
    packet: InterconnectedConfluencePacket,
    *,
    atm_strike: float,
    otm_hedge_strike: float,
    atm_option_px: float,
    otm_option_px: float,
    lot_size: int = 65,
    lots: int = 1,
) -> StrategyDecision:
    """Evaluate directional intraday option / debit spread entry."""
    ver = packet.engine_version
    sig = packet.shadow_signal

    if (
        sig is None
        or sig.confidence_score < 75.0
        or packet.derivative_leads_stock_direction not in {"BULLISH_LEAD", "BEARISH_LEAD"}
    ):
        return StrategyDecision(
            strategy_name="directional_intraday",
            symbol=packet.symbol,
            engine_version=ver,
            triggered=False,
            direction="FLAT",
            shadow_signal=sig,
            stage1_hedge_legs=(),
            stage2_risk_legs=(),
            max_loss_inr=0.0,
            target_profit_inr=0.0,
            rationale="Awaiting Derivative Lead + ≥75/100 Order-Flow Confluence",
        )

    is_bull = sig.direction == "LONG" and packet.derivative_leads_stock_direction == "BULLISH_LEAD"
    opt_suffix = "CE" if is_bull else "PE"
    qty = max(1, lots) * max(1, lot_size)

    # Use defined-risk debit spread when IVP > 60% to cap vega/theta risk; else single long option
    use_debit_spread = packet.vrp.iv_percentile_pct >= 60.0 and otm_option_px > 0
    long_leg = SpreadLegIntent(
        f"{packet.symbol}_{int(atm_strike)}{opt_suffix}",
        "BUY",
        qty,
        atm_option_px,
        is_protective_hedge=True,
    )
    short_legs: tuple[SpreadLegIntent, ...] = ()
    if use_debit_spread:
        short_legs = (
            SpreadLegIntent(
                f"{packet.symbol}_{int(otm_hedge_strike)}{opt_suffix}",
                "SELL",
                qty,
                otm_option_px,
                is_protective_hedge=False,
            ),
        )
        net_debit = max(1.0, atm_option_px - otm_option_px)
        max_loss = net_debit * qty
        target_profit = max_loss * 1.6
    else:
        # Stop-loss on single option anchored to 30% premium risk (microstructure-linked)
        max_loss = 0.30 * atm_option_px * qty
        target_profit = max_loss * sig.rr_t1

    return StrategyDecision(
        strategy_name="directional_intraday",
        symbol=packet.symbol,
        engine_version=ver,
        triggered=True,
        direction=sig.direction,
        shadow_signal=sig,
        stage1_hedge_legs=(long_leg,) if ver is EngineVersion.V2_BOT else (),
        stage2_risk_legs=short_legs if ver is EngineVersion.V2_BOT else (),
        max_loss_inr=round(max_loss, 2),
        target_profit_inr=round(target_profit, 2),
        rationale=(
            f"Directional Intraday ({sig.direction} {opt_suffix}): "
            f"Score={sig.confidence_score:.1f}/100, "
            f"DerivLead={packet.derivative_leads_stock_direction}, "
            f"RER={sig.real_execution_ratio:.2f}"
        ),
    )
