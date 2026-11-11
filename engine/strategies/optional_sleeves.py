"""M13 Optional Strategies: Calendar Spread, Cash Swing, and Expiry Gamma Breakout (SPEC §5)."""

from __future__ import annotations

from engine.analytics.surface import compute_forward_vol
from engine.confluence.engine import InterconnectedConfluencePacket
from engine.core.config import EngineVersion
from engine.data.eod.bhavcopy import DeliveryBias
from engine.execution.leg_sequencer import SpreadLegIntent
from engine.strategies.base import StrategyDecision


def evaluate_calendar_spread(
    packet: InterconnectedConfluencePacket,
    *,
    strike: float,
    near_iv: float,
    near_t_var: float,
    far_iv: float,
    far_t_var: float,
    near_px: float,
    far_px: float,
    lot_size: int = 65,
) -> StrategyDecision:
    """SPEC §5.4: Long back-month, short front-week calendar spread when front IV is rich."""
    ver = packet.engine_version
    fwd_vol = compute_forward_vol(near_iv, near_t_var, far_iv, far_t_var)
    term_richness = near_iv - far_iv
    if fwd_vol is None or term_richness < 0.015 or not packet.event_status.order_flow_allowed:
        return StrategyDecision(
            "calendar_spread",
            packet.symbol,
            ver,
            False,
            "FLAT",
            packet.shadow_signal,
            (),
            (),
            0.0,
            0.0,
            "Front-week IV not sufficiently rich vs back-month",
        )

    long_far = SpreadLegIntent(
        f"{packet.symbol}_{int(strike)}CE_FAR", "BUY", lot_size, far_px, True
    )
    short_near = SpreadLegIntent(
        f"{packet.symbol}_{int(strike)}CE_NEAR", "SELL", lot_size, near_px, False
    )
    net_debit = max(1.0, far_px - near_px) * lot_size
    return StrategyDecision(
        "calendar_spread",
        packet.symbol,
        ver,
        True,
        "NEUTRAL_DEFINED_RISK",
        packet.shadow_signal,
        (long_far,) if ver is EngineVersion.V2_BOT else (),
        (short_near,) if ver is EngineVersion.V2_BOT else (),
        round(net_debit, 2),
        round(0.35 * net_debit, 2),
        f"Calendar Spread: Front IV {near_iv:.2%} vs Back IV {far_iv:.2%} (FwdVol={fwd_vol:.2%})",
    )


def evaluate_cash_swing_delivery(
    packet: InterconnectedConfluencePacket,
    *,
    anchor_vwap: float,
    current_price: float,
    qty: int = 10,
) -> StrategyDecision:
    """SPEC §5.8 & PDF §6: 3-5d Institutional Delivery Spike stock reclaiming anchor VWAP."""
    ver = packet.engine_version
    sig = packet.shadow_signal
    dist_vwap_pct = abs(current_price - anchor_vwap) / max(anchor_vwap, 1.0) * 100.0
    ok = (
        packet.delivery_bias is DeliveryBias.LONG_ACCUMULATION
        and current_price >= anchor_vwap
        and dist_vwap_pct <= 1.2
        and sig is not None
        and sig.confidence_score >= 75.0
    )
    if not ok or sig is None:
        return StrategyDecision(
            "swing_delivery_momentum",
            packet.symbol,
            ver,
            False,
            "FLAT",
            sig,
            (),
            (),
            0.0,
            0.0,
            "Requires EOD LONG_ACCUMULATION + pullback within 1.2% above Anchor VWAP + Score ≥ 75",
        )

    leg = SpreadLegIntent(packet.symbol, "BUY", qty, current_price, True)
    risk = max(1.0, current_price - sig.stop_loss) * qty
    return StrategyDecision(
        "swing_delivery_momentum",
        packet.symbol,
        ver,
        True,
        "LONG",
        sig,
        (leg,) if ver is EngineVersion.V2_BOT else (),
        (),
        round(risk, 2),
        round(risk * sig.rr_t2, 2),
        (
            f"Swing Delivery Reclaim: AnchorVWAP={anchor_vwap:.2f}, "
            f"Score={sig.confidence_score:.1f}/100"
        ),
    )


def evaluate_expiry_gamma_breakout(
    packet: InterconnectedConfluencePacket,
    *,
    is_expiry_afternoon: bool,
    option_symbol: str,
    option_ltp: float,
    lot_size: int = 65,
) -> StrategyDecision:
    """SPEC §5.10: Expiry-afternoon gamma squeeze when GEX is negative and Call writers unwind."""
    ver = packet.engine_version
    sig = packet.shadow_signal
    ok = (
        is_expiry_afternoon
        and packet.gex.regime == "NEGATIVE_GAMMA_ACCELERATION"
        and packet.chain_positioning.call_writers_trapped
        and sig is not None
        and sig.confidence_score >= 80.0
    )
    if not ok or sig is None:
        return StrategyDecision(
            "expiry_gamma_breakout",
            packet.symbol,
            ver,
            False,
            "FLAT",
            sig,
            (),
            (),
            0.0,
            0.0,
            "Requires Expiry Afternoon + Negative GEX + Trapped Call Writers + A+ Score ≥ 80",
        )

    leg = SpreadLegIntent(option_symbol, "BUY", lot_size, option_ltp, True)
    risk = 0.35 * option_ltp * lot_size
    return StrategyDecision(
        "expiry_gamma_breakout",
        packet.symbol,
        ver,
        True,
        sig.direction,
        sig,
        (leg,) if ver is EngineVersion.V2_BOT else (),
        (),
        round(risk, 2),
        round(risk * 2.5, 2),
        (
            f"Expiry Gamma Squeeze ({option_symbol}): Negative GEX + Call Wall Unwind, "
            f"Score={sig.confidence_score:.1f}"
        ),
    )
