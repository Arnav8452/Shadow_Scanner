"""Unit tests for M1 Trade Mining, M5 Execution/Costs, M6 Backtest/DSR, M8 Risk/Kelly, and M7/M10-M13 Strategies."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from engine.analytics import (
    ChainGEXNode,
    FuturesOIBuildup,
    OIBuildupQuadrant,
    StrikeOINode,
    compute_dealer_gex_profile,
    compute_vrp_snapshot,
    summarize_chain_positioning,
)
from engine.backtest import (
    SimulatedTrade,
    compute_deflated_sharpe,
    evaluate_backtest_with_stress,
    evaluate_walk_forward,
    fno_charges,
    run_block_bootstrap_mc,
)
from engine.confluence import InterconnectedConfluencePacket
from engine.core.config import EngineVersion, load_settings
from engine.core.rules import IST, RuleBook
from engine.data.constituents import compute_constituent_breadth, default_nifty50_heavyweights
from engine.data.eod import DeliveryBias
from engine.data.events import evaluate_event_risk
from engine.execution import (
    OPSRateLimiter,
    OrderRecord,
    OrderState,
    PaperBroker,
    SpreadLegIntent,
    cap_short_legs_to_filled_hedges,
    reconcile_positions,
    resolve_execution_route,
    sequence_entry_legs,
    sequence_exit_legs,
)
from engine.risk import (
    AccountRiskState,
    attribute_option_pnl,
    build_kill_switch_plan,
    check_pre_trade_risk_gate,
    compute_dynamic_quarter_kelly,
    evaluate_dynamic_microstructure_stops,
)
from engine.shadow.models import (
    ConfidenceBreakdown,
    ShadowSignal,
    ShadowSignalStatus,
    SignalGrade,
    SignalWave,
)
from engine.strategies import (
    evaluate_calendar_spread,
    evaluate_cash_swing_delivery,
    evaluate_directional_intraday,
    evaluate_expiry_gamma_breakout,
    evaluate_iron_condor,
    evaluate_opening_breakout,
)
from engine.trade_mining import (
    analyze_trade_journal,
    parse_pdftotext_report,
    profile_mined_trader,
    redact_pii_text,
    validate_parsed_report,
)


def test_m1_trade_mining_validation_and_self_review() -> None:
    sample_layout = """2026-11-09 True Equity   Daily Net: ₹ 1,187.00
09:18:10
 1234 NIFTY24000CE BUY 65 INTRADAY 100.00 120.00 ₹ 1,300.00 ₹ 1,242.00
09:24:40 LIMIT
09:26:00
 1234 NIFTY24000PE BUY 65 INTRADAY 80.00 79.00 ₹ -65.00 ₹ -115.00
09:29:15 SL_M
09:35:00
 1234 NIFTY24100CE BUY 65 INTRADAY 90.00 91.85 ₹ 120.25 ₹ 60.00
09:48:00 LIMIT
"""
    assert redact_pii_text("PAN ABCDE1234F") == "PAN REDACTED_PAN"
    parsed = parse_pdftotext_report(sample_layout)
    assert len(parsed.rows) == 3
    assert parsed.rows[0].account_anon.startswith("ACCT-")
    assert "1234" not in parsed.rows[0].account_anon

    val = validate_parsed_report(parsed)
    assert val.passed

    rev = analyze_trade_journal(parsed.rows)
    assert rev.total_trades == 3
    assert rev.revenge.post_loss_trade_count == 1
    assert "+2_ticks" in rev.slippage_stress_net_pnl

    prof = profile_mined_trader(parsed.rows)
    assert prof.opening_15m_share_pct > 50.0
    assert prof.inferred_style == "OPENING_RANGE_SCALPER"


def test_m5_fno_costs_order_fsm_sequencer_and_paper_broker(repo_root: Path) -> None:
    book = RuleBook.load(repo_root / "config" / "rules")
    d = date(2026, 11, 10)
    opt_sell = fno_charges(book, d, segment="options", side="sell", turnover_inr=100_000.0)
    assert opt_sell.brokerage == 20.0
    assert opt_sell.stt == 150.0  # 0.15% of 100,000 from Apr 2026

    fut_sell = fno_charges(book, d, segment="futures", side="sell", turnover_inr=1_500_000.0)
    assert fut_sell.brokerage == 20.0
    assert fut_sell.stt == 750.0  # 0.05% of 1,500,000

    # Plain MARKET order is forbidden
    with pytest.raises(ValueError, match="Forbidden order_type"):
        OrderRecord("ORD-BAD", "NIFTY", "BUY", "MARKET", 65, 100.0)

    limiter = OPSRateLimiter(max_orders_per_second=3)
    assert limiter.allow(1_000_000_000)
    assert limiter.allow(1_100_000_000)
    assert limiter.allow(1_200_000_000)
    assert not limiter.allow(1_300_000_000)

    # Hedge-first entry + partial-fill cap + short-first exit
    legs = [
        SpreadLegIntent("NIFTY24200CE", "SELL", 130, 120.0, False),
        SpreadLegIntent("NIFTY24400CE", "BUY", 130, 40.0, True),
    ]
    st1, st2 = sequence_entry_legs(legs)
    assert st1[0].symbol == "NIFTY24400CE"
    assert st2[0].symbol == "NIFTY24200CE"
    capped = cap_short_legs_to_filled_hedges(st2, [replace(st1[0], qty=65)], lot_size=65)
    assert capped[0].qty == 65
    ex1, ex2 = sequence_exit_legs(legs)
    assert ex1[0].symbol == "NIFTY24200CE" and ex1[0].side == "BUY"
    assert ex2[0].symbol == "NIFTY24400CE" and ex2[0].side == "SELL"

    # PaperBroker depth walking & pre-trade liquidity gate
    broker = PaperBroker(book)
    now = datetime(2026, 11, 10, 10, 30, tzinfo=IST)
    buy_ord = OrderRecord("ORD-1", "NIFTY24000CE", "BUY", "LIMIT", 130, 152.0)
    res = broker.execute_against_depth(
        buy_ord,
        bids=((149.5, 500, 5),),
        asks=((150.0, 65, 3), (151.0, 400, 8)),
        now=now,
        trade_date=d,
    )
    assert res.order.state is OrderState.FILLED
    assert res.order.avg_fill_price == 150.5
    assert res.slippage_pts == 0.5

    rec = reconcile_positions({"NIFTY": 65}, {"NIFTY": 0})
    assert not rec.matched and rec.drifts[0].diff_qty == -65

    settings_v1 = load_settings(root=repo_root, version="v1_shadow", environ={})
    assert resolve_execution_route(settings_v1, today=d).target == "SHADOW_ONLY"


def test_m6_backtest_wfo_mc_and_deflated_sharpe() -> None:
    trades = [
        SimulatedTrade("NIFTY", "LONG", 65, 100.0, 122.0, 0.20, 55.0),
        SimulatedTrade("NIFTY", "LONG", 65, 105.0, 125.0, 0.20, 55.0),
        SimulatedTrade("NIFTY", "LONG", 65, 110.0, 104.0, 0.20, 55.0),
        SimulatedTrade("NIFTY", "SHORT", 65, 130.0, 108.0, 0.20, 55.0),
        SimulatedTrade("NIFTY", "LONG", 65, 98.0, 119.0, 0.20, 55.0),
        SimulatedTrade("NIFTY", "LONG", 65, 102.0, 121.0, 0.20, 55.0),
    ]
    ev = evaluate_backtest_with_stress(trades)
    assert ev.passes_2x_slippage_gate
    assert ev.survives_50ms_retail_latency

    daily_rets = [450.0, 520.0, -120.0, 610.0, 390.0, -80.0, 490.0, 530.0] * 8
    wfo = evaluate_walk_forward(daily_rets, train_size=24, test_size=8)
    assert wfo.passed

    mc = run_block_bootstrap_mc(daily_rets, n_paths=100)
    assert mc.p99_max_drawdown_inr >= mc.median_max_drawdown_inr

    dsr = compute_deflated_sharpe(daily_rets, n_trials=4)
    assert dsr.observed_sharpe_annual > 2.0
    assert dsr.passed


def test_m8_kelly_risk_gates_stops_kill_switch_and_attribution(repo_root: Path) -> None:
    pnls = [
        1200.0,
        -500.0,
        1450.0,
        980.0,
        -480.0,
        1350.0,
        1100.0,
        -520.0,
        1500.0,
        1250.0,
        -490.0,
        1400.0,
    ]
    kelly = compute_dynamic_quarter_kelly(
        pnls, capital_inr=200_000.0, stop_distance_pts=15.0, lot_size=65
    )
    assert 0.25 <= kelly.final_risk_pct <= 1.50
    assert kelly.recommended_lots >= 1

    settings = load_settings(root=repo_root, environ={})
    now = datetime(2026, 11, 10, 10, 45, tzinfo=IST)
    open_ts = datetime(2026, 11, 10, 9, 15, tzinfo=IST)

    # 3 consecutive losses -> scales size by 0.5x; 4 consecutive losses -> blocks entry
    gate_3 = check_pre_trade_risk_gate(
        settings.risk,
        AccountRiskState(consecutive_losses_today=3),
        proposed_trade_max_loss_inr=2000.0,
        proposed_margin_inr=30_000.0,
        proposed_lots=1,
        now=now,
        session_open=open_ts,
    )
    assert gate_3.allowed and gate_3.size_scale_factor == 0.5

    gate_4 = check_pre_trade_risk_gate(
        settings.risk,
        AccountRiskState(consecutive_losses_today=4),
        proposed_trade_max_loss_inr=2000.0,
        proposed_margin_inr=30_000.0,
        proposed_lots=1,
        now=now,
        session_open=open_ts,
    )
    assert not gate_4.allowed

    # Delta-Flip stop & Momentum Half-Life time-decay stop
    stop_flip = evaluate_dynamic_microstructure_stops(
        direction="LONG",
        entry_price=24000.0,
        current_price=23992.0,
        hard_stop_loss=23970.0,
        max_favorable_price=24004.0,
        atr=25.0,
        elapsed_seconds=45.0,
        obi_l5=-0.72,
        cvd_z_score=-2.1,
    )
    assert stop_flip.should_exit and stop_flip.exit_type == "DELTA_FLIP_INVALIDATION"

    stop_time = evaluate_dynamic_microstructure_stops(
        direction="LONG",
        entry_price=24000.0,
        current_price=24003.0,
        hard_stop_loss=23970.0,
        max_favorable_price=24005.0,
        atr=30.0,
        elapsed_seconds=240.0,
        obi_l5=0.10,
        cvd_z_score=0.2,
    )
    assert stop_time.should_exit and stop_time.exit_type == "TIME_DECAY_HALF_LIFE"

    # Emergency Kill Switch: short-first ordering from broker positions
    plan = build_kill_switch_plan({"NIFTY24300CE": 65, "NIFTY24000CE": -65})
    assert plan[0].symbol == "NIFTY24000CE" and plan[0].side == "BUY" and plan[0].stage == 1
    assert plan[1].symbol == "NIFTY24300CE" and plan[1].side == "SELL" and plan[1].stage == 2

    attr = attribute_option_pnl(
        actual_net_pnl_inr=1200.0,
        net_delta_units=32.5,
        net_gamma_units=0.08,
        vega_inr_per_1pt=450.0,
        theta_inr_per_day=-320.0,
        vanna_units=1.2,
        charm_units_per_day=-0.5,
        underlying_change_pts=30.0,
        iv_change_vol_pts=0.8,
        days_elapsed=0.25,
        charges_inr=58.0,
    )
    assert attr.delta_pnl_inr == 975.0


def test_m7_m10_m11_m13_strategies() -> None:
    now = datetime(2026, 11, 10, 10, 30, tzinfo=IST)
    from engine.analytics.order_flow import OrderFlowMechanic

    sig = ShadowSignal(
        signal_id="SHDW-1",
        created_at=now,
        symbol="NIFTY",
        direction="LONG",
        wave=SignalWave.WALL_BREAKOUT_SQUEEZE,
        mechanic=OrderFlowMechanic.STOP_RUN_SHORT_SQUEEZE,
        status=ShadowSignalStatus.PENDING_TRIGGER,
        grade=SignalGrade.A_PLUS,
        confidence_score=86.0,
        breakdown=ConfidenceBreakdown(23.0, 28.0, 20.0, 15.0),
        ltp_at_signal=24050.0,
        entry_price=24051.0,
        stop_loss=24020.0,
        target_1=24105.0,
        target_2=24140.0,
        rr_t1=1.74,
        rr_t2=2.87,
        sl_wall_info="L3 Bid Wall",
        target_wall_info="L15 Ask Wall",
        real_execution_ratio=0.88,
        obi_l5=0.62,
        air_pocket_ratio=0.42,
        cvd_z_score=2.6,
        futures_oi_quadrant="SHORT_COVERING",
        recommended_risk_pct=0.75,
        recommended_qty=65,
        rationale="Breakout squeeze",
    )
    vrp = compute_vrp_snapshot(0.16, 0.11, [0.10, 0.12, 0.14, 0.15, 0.18, 0.20])
    gex_pos = compute_dealer_gex_profile(
        [ChainGEXNode(24100.0, 250_000, 80_000, 0.14, 0.15)], forward=24050.0, t_var=5.0 / 252.0
    )
    pos = summarize_chain_positioning(
        [StrikeOINode(24050.0, 200_000, 180_000, 50_000, 50_000, call_oi_change=-20_000)],
        spot=24055.0,
    )
    breadth = compute_constituent_breadth(default_nifty50_heavyweights(), {"HDFCBANK": 1.0})
    ev_ok = evaluate_event_risk([], now)

    pkt = InterconnectedConfluencePacket(
        timestamp=now,
        symbol="NIFTY",
        engine_version=EngineVersion.V2_BOT,
        delivery_bias=DeliveryBias.LONG_ACCUMULATION,
        participant_bias="BULLISH",
        event_status=ev_ok,
        vrp=vrp,
        gex=gex_pos,
        futures_buildup=FuturesOIBuildup(
            OIBuildupQuadrant.SHORT_COVERING, 0.35, -1.2, 0.15, 18.0, 0.0, "Squeeze"
        ),
        chain_positioning=pos,
        derivative_leads_stock_direction="BULLISH_LEAD",
        breadth=breadth,
        shadow_signal=sig,
        recommended_strategies=("iron_condor", "directional_intraday", "opening_breakout"),
        blocked_strategies=(),
        interconnected_summary="OK",
    )

    ic = evaluate_iron_condor(
        pkt,
        short_put_strike=23800.0,
        long_put_strike=23600.0,
        short_call_strike=24300.0,
        long_call_strike=24500.0,
        short_put_px=45.0,
        long_put_px=15.0,
        short_call_px=42.0,
        long_call_px=14.0,
    )
    assert ic.triggered and len(ic.stage1_hedge_legs) == 2 and len(ic.stage2_risk_legs) == 2

    dir_dec = evaluate_directional_intraday(
        pkt, atm_strike=24050.0, otm_hedge_strike=24250.0, atm_option_px=140.0, otm_option_px=55.0
    )
    assert dir_dec.triggered and dir_dec.is_executable_v2_order

    orb_dec = evaluate_opening_breakout(pkt, option_symbol="NIFTY24050CE", option_ltp=140.0)
    assert orb_dec.triggered

    cal_dec = evaluate_calendar_spread(
        pkt,
        strike=24050.0,
        near_iv=0.165,
        near_t_var=5.0 / 252.0,
        far_iv=0.140,
        far_t_var=25.0 / 252.0,
        near_px=110.0,
        far_px=240.0,
    )
    assert cal_dec.triggered

    swing_dec = evaluate_cash_swing_delivery(pkt, anchor_vwap=24010.0, current_price=24050.0)
    assert swing_dec.triggered

    # Flip GEX to negative for expiry gamma breakout
    gex_neg = compute_dealer_gex_profile(
        [ChainGEXNode(24000.0, 50_000, 350_000, 0.14, 0.16)], forward=24055.0, t_var=1.0 / 252.0
    )
    from dataclasses import replace

    pkt_neg = replace(pkt, gex=gex_neg)
    gamma_dec = evaluate_expiry_gamma_breakout(
        pkt_neg, is_expiry_afternoon=True, option_symbol="NIFTY24050CE", option_ltp=85.0
    )
    assert gamma_dec.triggered
