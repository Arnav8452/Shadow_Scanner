"""Broker Position Reconciler & Live Execution Gate Router (M5, M12, SPEC §7).

1. **Position Reconciliation**:
   - Every 30–60s in live/paper sessions, compares ``local_positions`` against ``broker_positions``.
   - Any non-zero drift triggers a reconciliation alert and blocks new entries until resolved.
2. **Version-Aware Execution Router**:
   - In ``EngineVersion.V1_SHADOW`` (Shadow Order Book Scanner), **zero orders** are ever routed to
     a broker; signals go exclusively to the Shadow Dashboard & Shadow Ledger.
   - In ``EngineVersion.V2_BOT``, orders route to :class:`PaperBroker` unless :func:`live_gate`
     passes all 5 preconditions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from engine.core.config import EngineVersion, LiveGate, Settings, live_gate
from engine.core.rules import IST


@dataclass(frozen=True, slots=True)
class PositionDrift:
    symbol: str
    local_qty: int
    broker_qty: int
    diff_qty: int


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    matched: bool
    drifts: tuple[PositionDrift, ...]


@dataclass(frozen=True, slots=True)
class ExecutionRouteDecision:
    target: str  # "SHADOW_ONLY" | "PAPER_BROKER" | "LIVE_BROKER"
    reasons: tuple[str, ...]


def reconcile_positions(
    local_positions: Mapping[str, int],
    broker_positions: Mapping[str, int],
) -> ReconciliationResult:
    """Compare local position book against broker position book."""
    all_syms = sorted(set(local_positions) | set(broker_positions))
    drifts: list[PositionDrift] = []
    for sym in all_syms:
        lq = local_positions.get(sym, 0)
        bq = broker_positions.get(sym, 0)
        if lq != bq:
            drifts.append(
                PositionDrift(
                    symbol=sym,
                    local_qty=lq,
                    broker_qty=bq,
                    diff_qty=bq - lq,
                )
            )
    return ReconciliationResult(matched=(len(drifts) == 0), drifts=tuple(drifts))


def resolve_execution_route(
    settings: Settings,
    *,
    static_ip_whitelisted: bool = False,
    session_2fa_on: date | None = None,
    today: date | None = None,
    owner_confirmed: bool = False,
) -> ExecutionRouteDecision:
    """Decide whether a signal routes to Shadow-Only (V1), PaperBroker (V2), or LiveBroker (V2)."""
    if settings.engine_version is EngineVersion.V1_SHADOW:
        return ExecutionRouteDecision(
            target="SHADOW_ONLY",
            reasons=("engine_version is v1_shadow (Human-in-the-Loop Shadow Scanner)",),
        )

    eff_today = today or datetime.now(IST).date()
    gate: LiveGate = live_gate(
        settings,
        static_ip_whitelisted=static_ip_whitelisted,
        session_2fa_on=session_2fa_on,
        today=eff_today,
        owner_confirmed=owner_confirmed,
    )
    if gate.allowed:
        return ExecutionRouteDecision(target="LIVE_BROKER", reasons=())
    return ExecutionRouteDecision(target="PAPER_BROKER", reasons=gate.reasons)
