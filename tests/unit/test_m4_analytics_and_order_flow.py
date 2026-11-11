"""Unit tests for M4 Analytics Core & Deep Order-Flow / Anti-Spoofing Mechanics."""

from __future__ import annotations

from datetime import datetime

from engine.analytics import (
    ChainGEXNode,
    OHLCBar,
    OIBuildupQuadrant,
    OptionPositionLeg,
    OrderFlowMechanic,
    RollingOrderFlowTracker,
    StopPoolKind,
    StopPoolLevel,
    StrikeOINode,
    StrikePairQuote,
    analyze_book_microstructure,
    black76_price,
    build_vol_surface_slice,
    check_put_call_parity,
    classify_futures_oi_buildup,
    compute_black76_greeks,
    compute_cks_ofi_step,
    compute_dealer_gex_profile,
    compute_dual_clock,
    compute_forward_vol,
    compute_portfolio_rupee_greeks,
    compute_real_execution_ratio,
    compute_realized_vol_suite,
    compute_stress_grid,
    compute_synthetic_forward,
    compute_volume_profile,
    compute_vrp_snapshot,
    compute_vwap_bands,
    compute_wilder_atr,
    detect_equal_highs_lows,
    evaluate_anti_spoofing,
    evaluate_stop_pool_sweep,
    extract_risk_neutral_density,
    implied_vol_black76,
    rv_intraday_from_prices,
    summarize_chain_positioning,
)
from engine.core.rules import IST


def test_synthetic_forward_and_parity() -> None:
    pairs = [
        StrikePairQuote(23900.0, 210.0, 212.0, 62.0, 64.0),
        StrikePairQuote(24000.0, 145.0, 147.0, 96.0, 98.0),
        StrikePairQuote(24100.0, 92.0, 94.0, 143.0, 145.0),
    ]
    res = compute_synthetic_forward(pairs, spot=24000.0, t_cal_years=7.0 / 365.0)
    assert 24045.0 <= res.forward <= 24055.0
    assert res.implied_basis > 0
    assert len(check_put_call_parity(pairs, forward=res.forward, t_cal_years=7.0 / 365.0)) == 0

    bad_pair = [StrikePairQuote(24000.0, 200.0, 202.0, 50.0, 52.0)]
    viols = check_put_call_parity(bad_pair, forward=24050.0, t_cal_years=7.0 / 365.0)
    assert len(viols) == 1
    assert viols[0].excess_pts > 0


def test_dual_clock_black76_iv_surface_and_density() -> None:
    now = datetime(2026, 11, 10, 10, 15, tzinfo=IST)
    exp = datetime(2026, 11, 17, 15, 40, tzinfo=IST)
    clk = compute_dual_clock(now, exp, trading_days_between=5)
    assert clk.t_cal > 0 and clk.t_var > 0

    f = 24050.0
    for k, is_call, true_iv in [
        (23600.0, False, 0.165),
        (24050.0, True, 0.135),
        (24500.0, True, 0.148),
    ]:
        px = black76_price(f, k, clk.t_var, true_iv, is_call=is_call, t_cal=clk.t_cal)
        solved = implied_vol_black76(px, f, k, clk.t_var, is_call=is_call, t_cal=clk.t_cal)
        assert solved is not None
        assert abs(solved - true_iv) < 1e-5

    smile_inputs = [
        (23400.0, 0.175),
        (23700.0, 0.155),
        (24050.0, 0.135),
        (24400.0, 0.142),
        (24700.0, 0.152),
    ]
    surf = build_vol_surface_slice(smile_inputs, forward=f, t_var=clk.t_var, t_cal=clk.t_cal)
    assert abs(surf.atm_iv - 0.135) < 0.005
    assert surf.risk_reversal_25d < 0  # Put skew > Call skew
    assert surf.butterfly_25d > 0
    assert surf.atm_straddle_pts > 0

    fwd_v = compute_forward_vol(0.13, 5.0 / 252.0, 0.145, 20.0 / 252.0)
    assert fwd_v is not None and fwd_v > 0.145

    rnd = extract_risk_neutral_density(surf, t_cal=clk.t_cal)
    assert len(rnd.strikes) == 41
    assert 0.0 <= rnd.prob_below_minus_2pct <= 1.0


def test_1st_2nd_3rd_order_greeks_stress_and_gex() -> None:
    g = compute_black76_greeks(24000.0, 24000.0, 7.0 / 252.0, 0.14, is_call=True)
    assert 0.48 <= g.delta <= 0.55
    assert g.gamma > 0
    assert g.vega_per_1pct > 0
    assert g.theta_per_day < 0
    assert g.speed != 0.0
    assert g.zomma != 0.0

    legs = [
        OptionPositionLeg("NIFTY24000CE", 24000.0, True, -65, 150.0, 150.0, 0.14, 7.0 / 252.0),
        OptionPositionLeg("NIFTY24300CE", 24300.0, True, 65, 45.0, 45.0, 0.145, 7.0 / 252.0),
    ]
    port = compute_portfolio_rupee_greeks(legs, forward=24000.0)
    assert port.net_delta_units < 0

    grid = compute_stress_grid(
        legs, forward=24000.0, spot_shocks_pct=(-2.0, 0.0, 2.0), iv_shocks_pts=(0.0, 5.0)
    )
    assert len(grid) == 6

    gex = compute_dealer_gex_profile(
        [
            ChainGEXNode(23800.0, 50_000, 250_000, 0.15, 0.16),
            ChainGEXNode(24000.0, 180_000, 180_000, 0.14, 0.14),
            ChainGEXNode(24200.0, 300_000, 40_000, 0.14, 0.15),
        ],
        forward=24050.0,
        t_var=5.0 / 252.0,
    )
    assert gex.max_positive_gamma_strike == 24200.0
    assert gex.max_negative_gamma_strike == 23800.0


def test_realized_vol_vrp_positioning_and_technicals() -> None:
    bars = [
        OHLCBar(
            24000 + i * 15, 24060 + i * 15, 23960 + i * 15, 24020 + i * 15, 24000 + (i - 1) * 15
        )
        for i in range(1, 22)
    ]
    suite = compute_realized_vol_suite(bars)
    assert suite.yang_zhang > 0
    assert suite.parkinson > 0
    assert suite.garman_klass > 0
    assert suite.rogers_satchell > 0
    assert rv_intraday_from_prices([24000.0, 24015.0, 24005.0, 24030.0]) > 0

    vrp = compute_vrp_snapshot(0.16, 0.11, [0.10, 0.12, 0.14, 0.15, 0.18, 0.20])
    assert vrp.vrp_vol_pts == 5.0
    assert vrp.short_vol_favorable

    pos = summarize_chain_positioning(
        [
            StrikeOINode(23900.0, 50_000, 300_000, 20_000, 90_000),
            StrikeOINode(24000.0, 150_000, 200_000, 80_000, 85_000),
            StrikeOINode(24100.0, 350_000, 60_000, 110_000, 30_000, call_oi_change=-25_000),
        ],
        spot=24120.0,
    )
    assert pos.call_oi_wall == 24100.0
    assert pos.put_oi_wall == 23900.0
    assert pos.call_writers_trapped

    buildup = classify_futures_oi_buildup(
        price_now=24120.0, price_3m_ago=24050.0, oi_now=11_800_000, oi_3m_ago=12_100_000
    )
    assert buildup.quadrant is OIBuildupQuadrant.SHORT_COVERING

    vwap = compute_vwap_bands([24000.0, 24020.0, 24040.0], [1000, 2000, 3000])
    assert vwap.vwap > 24020.0 and vwap.upper_1sd > vwap.vwap
    assert (
        compute_wilder_atr([105.0, 108.0, 107.0], [100.0, 102.0, 101.0], [103.0, 106.0, 104.0]) > 0
    )

    eq_pools = detect_equal_highs_lows([24200.0, 24205.0], [23900.0, 23895.0])
    assert len(eq_pools) == 2

    sweep = evaluate_stop_pool_sweep(
        ltp=24005.0,
        session_low=23955.0,
        session_high=24080.0,
        pools=[StopPoolLevel(StopPoolKind.PDL, 24000.0, is_downside_pool=True)],
        direction="LONG",
    )
    assert sweep.reclaimed_level
    assert sweep.structural_score_pts == 25.0

    vp = compute_volume_profile([100.0, 101.0, 101.2, 102.0, 105.0], [500, 5000, 4500, 800, 200])
    assert 100.5 <= vp.poc <= 102.0


def test_deep_book_microstructure_anti_spoofing_and_6_mechanics() -> None:
    # 30-level depth with strong L1-L5 bids and thin L6-L25 ask air pocket
    bids = tuple((24000.0 - i * 0.5, 5000 if i < 5 else 800, 15) for i in range(30))
    asks = tuple((24000.5 + i * 0.5, 1000 if i < 5 else 250, 4) for i in range(30))
    snap = analyze_book_microstructure(bids, asks)
    assert snap.microprice > snap.mid_price
    assert snap.obi_l5 > 0.50
    assert snap.overhead_air_pocket_ratio < 0.55
    assert snap.strongest_bid_wall is not None
    assert compute_cks_ofi_step(100.0, 500, 100.5, 500, 100.0, 900, 100.5, 200) == 700.0

    # Anti-spoofing RER check: 2000 qty pulled from wall with only 100 qty traded -> RER = 0.05 (SPOOF!)
    assert compute_real_execution_ratio(traded_volume_delta=100, book_wall_reduction=2000) == 0.05
    spoof_eval = evaluate_anti_spoofing(
        traded_volume_delta=100, book_wall_reduction=2000, cvd_z_score=1.5
    )
    assert spoof_eval.is_spoof_pull
    assert spoof_eval.flow_score_pts == 0.0

    # Genuine execution: 1800 traded out of 2000 wall reduction -> RER = 0.90 (CONFIRMED!)
    real_eval = evaluate_anti_spoofing(
        traded_volume_delta=1800, book_wall_reduction=2000, cvd_z_score=2.4, wall_age_ms=1200
    )
    assert real_eval.is_genuine_execution
    assert real_eval.flow_score_pts == 30.0

    tracker = RollingOrderFlowTracker()
    det = tracker.update(
        recv_ns=1_000_000_000,
        ltp=24001.0,
        cum_volume=10_000,
        ltq=2500,
        bids=bids,
        asks=asks,
        at_resistance_break=True,
    )
    assert det.primary_mechanic is OrderFlowMechanic.STOP_RUN_SHORT_SQUEEZE
    assert OrderFlowMechanic.LIQUIDITY_VACUUM in det.active_mechanics
