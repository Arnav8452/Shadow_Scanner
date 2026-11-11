"""Base models for Version 1 (Shadow Scanner) and Version 2 (Automated Execution Bot) strategies."""

from __future__ import annotations

from dataclasses import dataclass

from engine.core.config import EngineVersion
from engine.execution.leg_sequencer import SpreadLegIntent
from engine.shadow.models import ShadowSignal


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    strategy_name: str
    symbol: str
    engine_version: EngineVersion
    triggered: bool
    direction: str  # "LONG", "SHORT", "NEUTRAL_DEFINED_RISK", or "FLAT"
    shadow_signal: ShadowSignal | None
    stage1_hedge_legs: tuple[SpreadLegIntent, ...]
    stage2_risk_legs: tuple[SpreadLegIntent, ...]
    max_loss_inr: float
    target_profit_inr: float
    rationale: str

    @property
    def is_executable_v2_order(self) -> bool:
        return (
            self.triggered
            and self.engine_version is EngineVersion.V2_BOT
            and (len(self.stage1_hedge_legs) > 0 or len(self.stage2_risk_legs) > 0)
        )
