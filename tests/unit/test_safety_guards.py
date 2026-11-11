"""Regression tests for the 10 Nov 2026 audit (docs/REVIEW_2026-11-10.md): defined risk, lot caps and honest sizing."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from engine.core.config import load_settings
from engine.core.rules import IST
from engine.execution.leg_sequencer import (
    NakedShortError,
    SpreadLegIntent,
    cap_short_legs_to_filled_hedges,
    emergency_flatten_filled_hedges,
    sequence_entry_legs,
    sequence_exit_legs,
)
from engine.risk.kelly import compute_dynamic_quarter_kelly
from engine.risk.limits import AccountRiskState, check_pre_trade_risk_gate

NOW = datetime(2026, 11, 11, 11, 0, tzinfo=IST)
OPEN = datetime(2026, 11, 11, 9, 15, tzinfo=IST)


def condor(qty: int = 130) -> list[SpreadLegIntent]:
    return [
        SpreadLegIntent("NSE:NIFTY26N1724600PE", "BUY", qty, 20.0, True),
        SpreadLegIntent("NSE:NIFTY26N1725400CE", "BUY", qty, 18.0, True),
        SpreadLegIntent("NSE:NIFTY26N1724800PE", "SELL", qty, 45.0, False),
        SpreadLegIntent("NSE:NIFTY26N1725200CE", "SELL", qty, 40.0, False),
    ]


def test_sell_leg_can_never_be_a_hedge() -> None:
    with pytest.raises(ValueError, match="never be a protective hedge"):
        SpreadLegIntent("NSE:NIFTY26N1725000CE", "SELL", 65, 100.0, True)
    with pytest.raises(ValueError, match="BUY or SELL"):
        SpreadLegIntent("NSE:NIFTY26N1725000CE", "SHORT", 65, 100.0, False)
    with pytest.raises(ValueError, match="qty"):
        SpreadLegIntent("NSE:NIFTY26N1725000CE", "BUY", 0, 100.0, True)


def test_uncovered_or_cross_type_shorts_are_refused() -> None:
    naked = [SpreadLegIntent("NSE:NIFTY26N1725000CE", "SELL", 65, 100.0, False)]
    with pytest.raises(NakedShortError):
        sequence_entry_legs(naked)
    put_covers_call = [
        SpreadLegIntent("NSE:NIFTY26N1724500PE", "BUY", 65, 20.0, True),
        SpreadLegIntent("NSE:NIFTY26N1725000CE", "SELL", 65, 100.0, False),
    ]
    with pytest.raises(NakedShortError, match="CE"):
        sequence_entry_legs(put_covers_call)
    short_future = [SpreadLegIntent("NSE:NIFTY26NOVFUT", "SELL", 65, 25000.0, False)]
    with pytest.raises(NakedShortError, match="non-option"):
        sequence_entry_legs(short_future)
    under_hedged = [
        SpreadLegIntent("NSE:NIFTY26N1725400CE", "BUY", 65, 18.0, True),
        SpreadLegIntent("NSE:NIFTY26N1725200CE", "SELL", 130, 40.0, False),
    ]
    with pytest.raises(NakedShortError):
        sequence_entry_legs(under_hedged)


def test_condor_partial_fill_never_leaves_a_naked_short_call() -> None:
    buys, shorts = sequence_entry_legs(condor(130))
    put_wing, call_wing = buys
    # Put wing fully filled (2 lots), call wing not filled at all.
    filled = [replace(put_wing, qty=130)]
    capped = cap_short_legs_to_filled_hedges(shorts, filled, lot_size=65)
    assert [(c.symbol, c.qty) for c in capped] == [("NSE:NIFTY26N1724800PE", 130)]
    # Odd partial fill rounds down to whole lots.
    filled = [replace(put_wing, qty=100), replace(call_wing, qty=65)]
    capped = cap_short_legs_to_filled_hedges(shorts, filled, lot_size=65)
    assert {(c.symbol, c.qty) for c in capped} == {
        ("NSE:NIFTY26N1724800PE", 65),
        ("NSE:NIFTY26N1725200CE", 65),
    }


def test_exit_and_emergency_legs_are_valid_orders() -> None:
    legs = condor(65)
    first, second = sequence_exit_legs(legs)
    assert {lg.side for lg in first} == {"BUY"} and {lg.side for lg in second} == {"SELL"}
    assert not any(lg.is_protective_hedge for lg in first + second)
    flat = emergency_flatten_filled_hedges(legs[:2])
    assert {(lg.side, lg.order_type) for lg in flat} == {("SELL", "MARKET_PROTECTED")}


def test_tagged_symbols_are_recognised() -> None:
    cal = [
        SpreadLegIntent("NIFTY_25000CE_FAR", "BUY", 65, 210.0, True),
        SpreadLegIntent("NIFTY_25000CE_NEAR", "SELL", 65, 150.0, False),
    ]
    buys, shorts = sequence_entry_legs(cal)
    assert len(buys) == 1 and len(shorts) == 1
    # "RELIANCE" contains "CE" but is not an option.
    assert SpreadLegIntent("NSE:RELIANCE-EQ", "BUY", 1, 1300.0, False).opt_type is None


def test_gate_enforces_one_lot_per_strategy(repo_root: Path) -> None:
    risk = load_settings(root=repo_root, environ={}).risk
    assert risk.max_lots_per_strategy == 1

    def gate(lots: int) -> tuple[bool, tuple[str, ...]]:
        d = check_pre_trade_risk_gate(
            risk,
            AccountRiskState(),
            proposed_trade_max_loss_inr=1000.0,
            proposed_margin_inr=10_000.0,
            proposed_lots=lots,
            now=NOW,
            session_open=OPEN,
        )
        return d.allowed, d.reasons

    assert gate(1)[0]
    ok, why = gate(2)
    assert not ok and "max_lots_per_strategy_exceeded" in why[0]
    assert not gate(0)[0]
    with pytest.raises(ValueError, match="no per-trade loss limit"):
        check_pre_trade_risk_gate(
            risk,
            AccountRiskState(),
            proposed_trade_max_loss_inr=1.0,
            proposed_margin_inr=1.0,
            proposed_lots=1,
            strategy_kind="lottery",
            now=NOW,
            session_open=OPEN,
        )


def test_kelly_never_rounds_up_to_a_lot_and_caps_at_one() -> None:
    wins_losses = [1200.0, -500.0, 1450.0, 980.0, -480.0, 1350.0, 1100.0, -520.0, 1500.0, 1250.0]
    # 40-point stop on a 65-lot = ₹2,600 per lot, above the ₹1,000 cold-start budget.
    k = compute_dynamic_quarter_kelly(
        [], capital_inr=200_000.0, stop_distance_pts=40.0, lot_size=65
    )
    assert k.risk_budget_inr == pytest.approx(1000.0) and k.recommended_lots == 0
    # A tight stop would allow many lots, but the default cap is one.
    k = compute_dynamic_quarter_kelly(
        wins_losses, capital_inr=200_000.0, stop_distance_pts=2.0, lot_size=65
    )
    assert k.recommended_lots == 1 and k.recommended_qty == 65


def test_upstox_feed_reports_auth_failure_instead_of_connected() -> None:
    from engine.core.env import Secret
    from engine.data.adapters.base import FeedStatus, StatusKind
    from engine.data.adapters.upstox import MODE_FULL, UpstoxFeed

    def refused(url: str, headers: object) -> bytes:
        raise OSError("HTTP Error 401: Unauthorized")

    events: list[object] = []
    UpstoxFeed(
        token=Secret("UPSTOX_ANALYTICS_TOKEN", "tok"),
        mode=MODE_FULL,
        http_get=refused,
        reconnect=False,
    ).start(events.append)
    assert len(events) == 1 and isinstance(events[0], FeedStatus)
    assert events[0].kind is StatusKind.AUTH_FAILED


def test_synthetic_tasks_refuse_without_demo(repo_root: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("tasks_mod", repo_root / "tasks.py")
    assert spec is not None and spec.loader is not None
    tasks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tasks)
    for name in ("task_shadow_scan", "task_report", "task_backtest", "task_paper"):
        assert getattr(tasks, name)({}) == 2, name
    assert tasks.task_self_review({}) == 2


def _signal(symbol: str, direction: str, entry: float, stop: float) -> object:
    from engine.analytics.order_flow import OrderFlowMechanic
    from engine.shadow.models import (
        ConfidenceBreakdown,
        ShadowSignal,
        ShadowSignalStatus,
        SignalGrade,
        SignalWave,
    )

    return ShadowSignal(
        signal_id="T-1",
        created_at=NOW,
        symbol=symbol,
        direction=direction,
        wave=SignalWave.WALL_BREAKOUT_SQUEEZE,
        mechanic=OrderFlowMechanic.STOP_RUN_SHORT_SQUEEZE,
        status=ShadowSignalStatus.PENDING_TRIGGER,
        grade=SignalGrade.A_PLUS,
        confidence_score=86.0,
        breakdown=ConfidenceBreakdown(23.0, 28.0, 20.0, 15.0),
        ltp_at_signal=entry,
        entry_price=entry,
        stop_loss=stop,
        target_1=entry + 2 * (entry - stop),
        target_2=entry + 3 * (entry - stop),
        rr_t1=2.0,
        rr_t2=3.0,
        sl_wall_info="",
        target_wall_info="",
        real_execution_ratio=0.88,
        obi_l5=0.62,
        air_pocket_ratio=0.42,
        cvd_z_score=2.6,
        futures_oi_quadrant="SHORT_COVERING",
        recommended_risk_pct=0.75,
        recommended_qty=130,  # 2 lots suggested; V2 must cap to 1
        rationale="test",
    )


def test_v2_trades_only_long_options_one_lot(repo_root: Path) -> None:
    from types import SimpleNamespace

    from engine.core.config import EngineVersion
    from engine.strategies.order_flow_bots import dispatch_shadow_or_bot_signal

    risk = load_settings(root=repo_root, environ={}).risk

    def run(symbol: str, direction: str = "LONG", entry: float = 120.0, stop: float = 105.0):
        packet = SimpleNamespace(
            engine_version=EngineVersion.V2_BOT,
            symbol=symbol,
            shadow_signal=_signal(symbol, direction, entry, stop),
        )
        return dispatch_shadow_or_bot_signal(
            packet,  # type: ignore[arg-type]
            risk_limits=risk,
            account_state=AccountRiskState(),
            now=NOW,
            session_open=OPEN,
            lot_size=65,
        )

    ok = run("NSE:NIFTY26N1725000CE")
    assert ok.triggered and ok.is_executable_v2_order
    (leg,) = ok.stage1_hedge_legs
    assert (leg.side, leg.qty, leg.is_protective_hedge) == ("BUY", 65, False)
    fut = run("NSE:NIFTY26NOVFUT", entry=25000.0, stop=24980.0)
    assert not fut.triggered and "only options" in fut.rationale
    short = run("NSE:NIFTY26N1725000PE", direction="SHORT")
    assert not short.triggered and "selling to open is not allowed" in short.rationale


def test_kill_switch_disconnect_never_flattens_blindly() -> None:
    from engine.risk.kill_switch import build_kill_switch_plan

    positions = {"NSE:NIFTY26N1725200CE": -65, "NSE:NIFTY26N1725400CE": 65}
    # When quotes are missing on a feed disconnect, do not fire blind flatten orders.
    assert build_kill_switch_plan(positions, quotes_available=False) == []
    # When quotes are available, short legs are closed in stage 1 before long legs in stage 2.
    plan = build_kill_switch_plan(
        positions,
        quotes_available=True,
        reference_prices={"NSE:NIFTY26N1725200CE": 42.0, "NSE:NIFTY26N1725400CE": 16.0},
    )
    assert [(p.stage, p.symbol, p.side, p.reference_price) for p in plan] == [
        (1, "NSE:NIFTY26N1725200CE", "BUY", 42.0),
        (2, "NSE:NIFTY26N1725400CE", "SELL", 16.0),
    ]


def test_risk_numbers_and_ops_cap_come_from_config(repo_root: Path) -> None:
    from dataclasses import replace as dc_replace

    from engine.execution.ops_limiter import OPSRateLimiter

    risk = load_settings(root=repo_root, environ={}).risk
    assert (
        risk.margin_headroom_max_used_pct,
        risk.loss_streak_size_cut_after,
        risk.loss_streak_halt_after,
    ) == (50.0, 3, 4)

    def gate(r: object, losses: int, margin: float) -> object:
        return check_pre_trade_risk_gate(
            r,  # type: ignore[arg-type]
            AccountRiskState(consecutive_losses_today=losses),
            proposed_trade_max_loss_inr=1000.0,
            proposed_margin_inr=margin,
            proposed_lots=1,
            now=NOW,
            session_open=OPEN,
        )

    stricter = dc_replace(risk, margin_headroom_max_used_pct=10.0, loss_streak_halt_after=2)
    assert gate(risk, 0, 50_000.0).allowed  # 25% of ₹2L used
    assert not gate(stricter, 0, 50_000.0).allowed  # same trade, 10% cap from config
    assert not gate(stricter, 2, 1_000.0).allowed  # halt after 2 losses from config
    ops = OPSRateLimiter.from_rules(repo_root, NOW.date(), 5)
    with pytest.raises(ValueError, match="SEBI < 10"):
        OPSRateLimiter.from_rules(repo_root, NOW.date(), 10)
    assert ops.max_ops == 5
