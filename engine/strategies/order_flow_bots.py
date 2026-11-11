"""Unified Version 1 (Shadow Scanner) <-> Version 2 (Automated Bot) Order-Flow Strategy Controller.

Bridges the 5 Order-Flow & Stop-Hunt Waves between Version 1 and Version 2:
- **Version 1 (``EngineVersion.V1_SHADOW``)**:
  Emits a read-only :class:`StrategyDecision` containing the ranked :class:`ShadowSignal` (Entry,
  Microstructure SL behind L1–L15 wall, T1/T2 in front of L10–L30 wall, Quarter-Kelly size) with
  **zero executable order legs** (Human-in-the-Loop).
- **Version 2 (``EngineVersion.V2_BOT``)**:
  Converts the exact same validated :class:`ShadowSignal` (when ``confidence_score >= min_score``
  and `` check_pre_trade_risk_gate`` passes) into executable ``LIMIT`` / ``MARKET_PROTECTED`` legs
  for :class:`PaperBroker` or gated :class:`LiveBroker`.
"""

from __future__ import annotations

import re
from datetime import datetime

from engine.confluence.engine import InterconnectedConfluencePacket
from engine.core.config import EngineVersion, RiskLimits
from engine.execution.leg_sequencer import SpreadLegIntent
from engine.risk.limits import AccountRiskState, check_pre_trade_risk_gate
from engine.shadow.models import ShadowSignalStatus
from engine.strategies.base import StrategyDecision

# Option symbols end in <digits>CE|PE (optionally a tag like _FAR); Upstox keys (NSE_FO|12345)
# carry no type, so they are resolved through the instrument master before reaching V2.
_OPTION_SYMBOL_RE = re.compile(r"\d(CE|PE)(?:_[A-Z0-9]+)?$")


def dispatch_shadow_or_bot_signal(
    packet: InterconnectedConfluencePacket,
    *,
    risk_limits: RiskLimits,
    account_state: AccountRiskState,
    now: datetime,
    session_open: datetime,
    lot_size: int,
    min_confidence_score: float = 75.0,
) -> StrategyDecision:
    """Route an InterconnectedConfluencePacket to V1 Shadow Radar or V2 Automated Bot."""
    ver = packet.engine_version
    sig = packet.shadow_signal
    strat_name = f"order_flow_{sig.wave.value.lower()}" if sig else "order_flow_none"

    if sig is None:
        return StrategyDecision(
            strategy_name=strat_name,
            symbol=packet.symbol,
            engine_version=ver,
            triggered=False,
            direction="FLAT",
            shadow_signal=None,
            stage1_hedge_legs=(),
            stage2_risk_legs=(),
            max_loss_inr=0.0,
            target_profit_inr=0.0,
            rationale="No microstructure shadow signal present",
        )

    if sig.status is ShadowSignalStatus.BLOCKED_SPOOF:
        return StrategyDecision(
            strategy_name=strat_name,
            symbol=packet.symbol,
            engine_version=ver,
            triggered=False,
            direction="FLAT",
            shadow_signal=sig,
            stage1_hedge_legs=(),
            stage2_risk_legs=(),
            max_loss_inr=0.0,
            target_profit_inr=0.0,
            rationale=(
                f"Blocked by Anti-Spoofing Filter (RER={sig.real_execution_ratio:.2f} < 0.20)"
            ),
        )

    if sig.confidence_score < min_confidence_score:
        return StrategyDecision(
            strategy_name=strat_name,
            symbol=packet.symbol,
            engine_version=ver,
            triggered=False,
            direction="FLAT",
            shadow_signal=sig,
            stage1_hedge_legs=(),
            stage2_risk_legs=(),
            max_loss_inr=0.0,
            target_profit_inr=0.0,
            rationale=(
                f"Confidence score {sig.confidence_score:.1f} "
                f"below threshold {min_confidence_score:.1f}"
            ),
        )

    unit_risk = abs(sig.entry_price - sig.stop_loss)
    max_loss_inr = unit_risk * sig.recommended_qty
    target_profit_inr = abs(sig.target_1 - sig.entry_price) * sig.recommended_qty

    # Version 1: Human-in-the-Loop Shadow Scanner — never generates broker order legs!
    if ver is EngineVersion.V1_SHADOW:
        return StrategyDecision(
            strategy_name=strat_name,
            symbol=packet.symbol,
            engine_version=ver,
            triggered=True,
            direction=sig.direction,
            shadow_signal=sig,
            stage1_hedge_legs=(),
            stage2_risk_legs=(),
            max_loss_inr=round(max_loss_inr, 2),
            target_profit_inr=round(target_profit_inr, 2),
            rationale=f"[V1 SHADOW RADAR ONLY] {sig.rationale}",
        )

    def blocked(why: str) -> StrategyDecision:
        return StrategyDecision(
            strategy_name=strat_name,
            symbol=packet.symbol,
            engine_version=ver,
            triggered=False,
            direction="FLAT",
            shadow_signal=sig,
            stage1_hedge_legs=(),
            stage2_risk_legs=(),
            max_loss_inr=0.0,
            target_profit_inr=0.0,
            rationale=why,
        )

    # Version 2 never sells to open: a SHORT view needs a defined-risk structure (long put or
    # bear put spread), which this dispatcher does not build (CLAUDE.md rule 1).
    if sig.direction != "LONG":
        return blocked(
            "[V2 BOT BLOCKED] short entries need a defined-risk structure (long put / bear put "
            "spread); selling to open is not allowed"
        )
    # Owner decision 11 Nov 2026: no futures in V2 at ₹2L capital (undefined risk, ~8x leverage).
    # Futures stay in V1 as a lead indicator for defined-risk option structures.
    if not _OPTION_SYMBOL_RE.search(sig.symbol.upper()):
        return blocked(
            "[V2 BOT BLOCKED] only options may be traded in V2; futures and cash are signals only"
        )
    lots = min(sig.recommended_qty // max(1, lot_size), risk_limits.max_lots_per_strategy)
    qty = lots * max(1, lot_size)
    unit_risk_qty = unit_risk * qty
    # Margin for a long position: options pay full premium; futures margin is not modelled here,
    # so use the full notional as a deliberately conservative stand-in.
    proposed_margin = sig.entry_price * qty
    gate = check_pre_trade_risk_gate(
        risk_limits,
        account_state,
        proposed_trade_max_loss_inr=unit_risk_qty,
        proposed_margin_inr=proposed_margin,
        proposed_lots=lots,
        strategy_kind="directional",
        is_opening_range_strategy=False,
        now=now,
        session_open=session_open,
    )
    if not gate.allowed:
        return blocked(f"[V2 BOT BLOCKED BY RISK GATE] {', '.join(gate.reasons)}")
    scaled_lots = int(lots * gate.size_scale_factor)  # 0.5x after 3 losses rounds 1 lot to 0
    if scaled_lots < 1:
        return blocked("[V2 BOT BLOCKED] size scaled below one lot after consecutive losses")
    scaled_qty = scaled_lots * max(1, lot_size)
    intent = SpreadLegIntent(
        symbol=sig.symbol,
        side="BUY",
        qty=scaled_qty,
        limit_price=sig.entry_price,
        is_protective_hedge=False,
    )
    return StrategyDecision(
        strategy_name=strat_name,
        symbol=packet.symbol,
        engine_version=ver,
        triggered=True,
        direction=sig.direction,
        shadow_signal=sig,
        stage1_hedge_legs=(intent,),
        stage2_risk_legs=(),
        max_loss_inr=round(unit_risk * scaled_qty, 2),
        target_profit_inr=round(abs(sig.target_1 - sig.entry_price) * scaled_qty, 2),
        rationale=f"[V2 BOT EXECUTABLE] {sig.rationale}",
    )
