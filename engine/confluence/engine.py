"""Interconnected Cross-Asset Confluence Engine: Derivative Momentum <-> Stock <-> Strategy.

This module connects every analytical layer in the repository into a single living graph:
1. **Multi-Day EOD Institutional Context**:
   - Relative Delivery % Spike Watchlist (``LONG_ACCUMULATION`` vs ``RETAIL_CHURN_FADE``).
   - Participant OI Regime (FII + Pro vs Client net index futures & options delta).
   - Macro & Constituent Earnings Blackout Gate (``EventBlackoutStatus``).
2. **Options Surface, Skew & Variance Risk Premium (VRP)**:
   - Synthetic Forward ``F`` & implied basis ``F - S``.
   - Black-76 ATM IV, 25Δ Risk Reversal, 25Δ Butterfly, Yang-Zhang RV, and VRP.
   - Dealer GEX Profile & Gamma-Flip Strike (``POSITIVE_GAMMA_PIN`` vs
     ``NEGATIVE_GAMMA_ACCELERATION``).
3. **Futures Price × OI 4-Quadrant Buildup & Option Chain Walls**:
   - Lag-adjusted 3-to-5-minute Futures Price × OI quadrant (``LONG_BUILDUP``, ``SHORT_COVERING``,
     ``SHORT_BUILDUP``, ``LONG_LIQUIDATION``).
   - Call OI Wall, Put OI Wall, PCR, and Max Pain.
   - Derivative-to-Cash Lead-Lag: detects when Futures basis expands + Call Wall OI drops +
     GEX is negative -> triggers stock/index momentum acceleration!
4. **Index Constituent Breadth & Sector Alignment**:
   - Free-float-weighted Nifty 50 constituent advance/decline breadth & implied correlation proxy.
5. **L1–L30 Order-Book Microstructure & Anti-Spoofing Entry Trigger**:
   - Feeds all upstream context directly into :class:`ShadowOrderBookScanner` (for **Version 1**
     Human-in-the-Loop Shadow Signals) AND emits strategy regime authorizations for **Version 2**
     Automated Execution Bots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from engine.analytics.exposures import GEXProfile
from engine.analytics.positioning import (
    ChainPositioningSummary,
    FuturesOIBuildup,
    OIBuildupQuadrant,
)
from engine.analytics.vrp import VRPSnapshot
from engine.core.config import EngineVersion
from engine.data.constituents.nifty_members import ConstituentBreadthSnapshot
from engine.data.eod.bhavcopy import DeliveryBias
from engine.data.eod.participant_oi import ParticipantRegime
from engine.data.events.event_calendar import EventBlackoutStatus
from engine.shadow.models import ShadowSignal
from engine.shadow.scanner import InstrumentMicrostructureState, ShadowOrderBookScanner


@dataclass(frozen=True, slots=True)
class InterconnectedConfluencePacket:
    timestamp: datetime
    symbol: str
    engine_version: EngineVersion
    # Layer 1: Multi-day EOD & Macro Event Gate
    delivery_bias: DeliveryBias
    participant_bias: str
    event_status: EventBlackoutStatus
    # Layer 2: Vol Surface, VRP & Dealer GEX
    vrp: VRPSnapshot
    gex: GEXProfile
    # Layer 3: Futures 4-Quad OI & Option Chain Positioning
    futures_buildup: FuturesOIBuildup
    chain_positioning: ChainPositioningSummary
    derivative_leads_stock_direction: str  # "BULLISH_LEAD", "BEARISH_LEAD", "PINNED", "NEUTRAL"
    # Layer 4: Constituent Breadth
    breadth: ConstituentBreadthSnapshot
    # Layer 5: Deep-Book L1-L30 Shadow Signal & Version 2 Strategy Routing
    shadow_signal: ShadowSignal | None
    recommended_strategies: tuple[str, ...]
    blocked_strategies: tuple[str, ...]
    interconnected_summary: str
    synthetic_forward_basis_pts: float = 0.0

    @property
    def recommended_vehicle(self) -> str:
        """Auto-select optimal trade vehicle based on IV/VRP percentile, GEX, and Delivery bias."""
        if not self.event_status.order_flow_allowed or self.event_status.in_macro_blackout:
            return "FLAT_BLACKOUT"
        if (
            self.delivery_bias is DeliveryBias.LONG_ACCUMULATION
            and self.vrp.iv_percentile_pct >= 75.0
        ):
            return "CASH_EQUITY_SWING"
        if self.vrp.short_vol_favorable and self.gex.regime == "POSITIVE_GAMMA_PIN":
            return "DEFINED_RISK_CREDIT_SPREAD"
        if self.vrp.iv_percentile_pct < 40.0 and self.gex.regime == "NEGATIVE_GAMMA_ACCELERATION":
            return "ATM_OPTION_CONVEX"
        return "DEFINED_RISK_DEBIT_SPREAD"


def evaluate_interconnected_confluence(
    *,
    timestamp: datetime,
    symbol: str,
    engine_version: EngineVersion,
    scanner: ShadowOrderBookScanner,
    state: InstrumentMicrostructureState,
    vrp: VRPSnapshot,
    gex: GEXProfile,
    futures_buildup: FuturesOIBuildup,
    chain_positioning: ChainPositioningSummary,
    breadth: ConstituentBreadthSnapshot,
    event_status: EventBlackoutStatus,
    participant_regime: ParticipantRegime | None = None,
    synthetic_forward_basis_pts: float = 0.0,
) -> InterconnectedConfluencePacket:
    """Synthesize all 5 layers into a single cross-asset decision packet for V1 or V2."""
    # 1. Determine how Derivative Momentum is driving Stock/Index Movement
    fut_quad = futures_buildup.quadrant
    breadth_bull = breadth.weighted_breadth_pct >= 20.0
    breadth_bear = breadth.weighted_breadth_pct <= -20.0

    if (
        fut_quad in {OIBuildupQuadrant.LONG_BUILDUP, OIBuildupQuadrant.SHORT_COVERING}
        and (
            chain_positioning.call_writers_trapped
            or chain_positioning.pcr_oi >= 1.05
            or synthetic_forward_basis_pts >= 3.0
        )
        and breadth_bull
    ):
        deriv_lead = "BULLISH_LEAD"
    elif (
        fut_quad in {OIBuildupQuadrant.SHORT_BUILDUP, OIBuildupQuadrant.LONG_LIQUIDATION}
        and (chain_positioning.pcr_oi <= 0.90 or synthetic_forward_basis_pts <= -3.0)
        and breadth_bear
    ):
        deriv_lead = "BEARISH_LEAD"
    elif gex.regime == "POSITIVE_GAMMA_PIN" and abs(breadth.weighted_breadth_pct) < 20.0:
        deriv_lead = "PINNED"
    else:
        deriv_lead = "NEUTRAL"

    # 2. Propagate cross-asset context into the instrument's microstructure state before scoring
    state.call_oi_wall = chain_positioning.call_oi_wall
    state.put_oi_wall = chain_positioning.put_oi_wall
    state.gex_regime = gex.regime
    state.breadth_aligned = (
        (deriv_lead == "BULLISH_LEAD" and state.ltp >= state.session_open)
        or (deriv_lead == "BEARISH_LEAD" and state.ltp <= state.session_open)
        or abs(breadth.weighted_breadth_pct) >= 25.0
    )
    if symbol not in scanner.states:
        scanner.register_instrument(state)

    shadow_sig = (
        scanner.evaluate_instrument(symbol, now=timestamp)
        if event_status.order_flow_allowed
        else None
    )

    # 3. Determine recommended vs blocked strategies across the whole system
    recommended: list[str] = []
    blocked: list[str] = []

    if not event_status.order_flow_allowed or event_status.in_macro_blackout:
        blocked.extend(
            [
                "iron_condor",
                "credit_spread",
                "directional_intraday",
                "opening_breakout",
                "order_flow_stop_hunt",
                "order_flow_squeeze",
            ]
        )
    else:
        # Short-Vol Sleeve (Iron Condor / Credit Spreads): requires VRP > 0, Positive Gamma Pin
        if (
            vrp.short_vol_favorable
            and gex.regime == "POSITIVE_GAMMA_PIN"
            and event_status.short_vega_scale_factor > 0
            and deriv_lead in {"PINNED", "NEUTRAL"}
        ):
            recommended.append("iron_condor")
            recommended.append("credit_spread")
        else:
            blocked.append("iron_condor")

        # Directional & Order-Flow Breakout / Stop-Hunt Sleeves
        if (
            shadow_sig is not None
            and shadow_sig.confidence_score >= scanner.config.min_confidence_score
        ):
            recommended.append(f"shadow_{shadow_sig.wave.value.lower()}")
            if deriv_lead in {"BULLISH_LEAD", "BEARISH_LEAD"}:
                recommended.append("directional_intraday")
                if gex.regime == "NEGATIVE_GAMMA_ACCELERATION":
                    recommended.append("opening_breakout")
                    recommended.append("expiry_gamma_breakout")

        # Multi-day Swing / Trend Sleeve
        if state.delivery_bias is DeliveryBias.LONG_ACCUMULATION and deriv_lead == "BULLISH_LEAD":
            recommended.append("swing_delivery_momentum")
            recommended.append("trend_sleeve")

    part_bias = participant_regime.institutional_bias if participant_regime else "NEUTRAL"
    sig_str = (
        f"{shadow_sig.grade.value} {shadow_sig.direction} ({shadow_sig.confidence_score:.1f}/100)"
        if shadow_sig
        else "NONE"
    )
    summary = (
        f"[{engine_version.value}] {symbol}: DerivLead={deriv_lead} "
        f"(Fut={fut_quad.value}, PCR={chain_positioning.pcr_oi:.2f}, GEX={gex.regime}) | "
        f"VRP={vrp.vrp_vol_pts:+.1f}pts | Breadth={breadth.weighted_breadth_pct:+.1f}% | "
        f"Delivery={state.delivery_bias.value} | Participant={part_bias} | "
        f"ShadowSignal={sig_str}"
    )

    return InterconnectedConfluencePacket(
        timestamp=timestamp,
        symbol=symbol,
        engine_version=engine_version,
        delivery_bias=state.delivery_bias,
        participant_bias=part_bias,
        event_status=event_status,
        vrp=vrp,
        gex=gex,
        futures_buildup=futures_buildup,
        chain_positioning=chain_positioning,
        derivative_leads_stock_direction=deriv_lead,
        breadth=breadth,
        shadow_signal=shadow_sig,
        recommended_strategies=tuple(recommended),
        blocked_strategies=tuple(blocked),
        interconnected_summary=summary,
        synthetic_forward_basis_pts=synthetic_forward_basis_pts,
    )
