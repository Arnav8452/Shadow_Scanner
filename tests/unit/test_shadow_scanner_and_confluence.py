"""Unit tests for Version 1 (Shadow Order Book Scanner) vs Version 2 (Bots) & Confluence Engine."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from engine.analytics import (
    ChainGEXNode,
    StrikeOINode,
    classify_futures_oi_buildup,
    compute_dealer_gex_profile,
    compute_vrp_snapshot,
    summarize_chain_positioning,
)
from engine.confluence import evaluate_interconnected_confluence
from engine.core.config import EngineVersion, load_settings
from engine.core.rules import IST
from engine.data.adapters.base import Depth, Quote
from engine.data.constituents import compute_constituent_breadth, default_nifty50_heavyweights
from engine.data.eod import DeliveryBias
from engine.data.events import evaluate_event_risk
from engine.risk import AccountRiskState
from engine.shadow import (
    InstrumentMicrostructureState,
    ShadowOrderBookScanner,
    ShadowSignalStatus,
    SignalGrade,
    SignalWave,
    render_shadow_dashboard_html,
    write_shadow_dashboard_and_ledger,
)
from engine.strategies import dispatch_shadow_or_bot_signal


def test_switchable_v1_shadow_and_v2_bot_config(repo_root: Path) -> None:
    s_default = load_settings(root=repo_root, environ={})
    assert s_default.engine_version is EngineVersion.V1_SHADOW

    s_v2 = load_settings(root=repo_root, environ={"ENGINE_VERSION": "v2_bot"})
    assert s_v2.engine_version is EngineVersion.V2_BOT

    s_v1_alias = load_settings(root=repo_root, version="v1", environ={})
    assert s_v1_alias.engine_version is EngineVersion.V1_SHADOW


def test_shadow_scanner_100pt_scoring_ledger_and_v1_v2_dispatch(
    repo_root: Path, tmp_path: Path
) -> None:
    settings_v1 = load_settings(root=repo_root, version="v1_shadow", environ={})
    settings_v2 = load_settings(root=repo_root, version="v2_bot", environ={})

    scanner = ShadowOrderBookScanner(settings_v1.shadow_scanner, capital_inr=200_000.0)
    st = InstrumentMicrostructureState(
        symbol="NSE:NIFTY50-FUT",
        tick_size=0.05,
        lot_size=65,
        pdh=24250.0,
        pdl=24000.0,
        prev_close=24080.0,
        orh_15m=24150.0,
        orl_15m=24010.0,
        delivery_bias=DeliveryBias.LONG_ACCUMULATION,
    )
    scanner.register_instrument(st)

    now = datetime(2026, 11, 10, 10, 30, tzinfo=IST)
    now_ns = int(now.timestamp() * 1_000_000_000)

    # Stage 1: Sweep below PDL (24000 -> low 23965) then reclaim 24008 on high volume & OI drop
    scanner.on_quote(
        Quote(
            symbol="NSE:NIFTY50-FUT",
            recv_ns=now_ns - 180_000_000_000,
            ltp=23980.0,
            ltq=650,
            volume=100_000,
            oi=12_000_000,
            open=24040.0,
            high=24050.0,
            low=23965.0,
        )
    )
    scanner.on_quote(
        Quote(
            symbol="NSE:NIFTY50-FUT",
            recv_ns=now_ns,
            ltp=24008.0,
            ltq=3900,
            volume=120_000,
            oi=11_800_000,
            open=24040.0,
            high=24050.0,
            low=23965.0,
        )
    )

    bids = tuple((24008.0 - i * 0.5, 5200 if i < 5 else 700, 14) for i in range(30))
    asks = tuple((24008.5 + i * 0.5, 850 if i < 5 else 220, 4) for i in range(30))
    sig = scanner.on_depth(Depth("NSE:NIFTY50-FUT", now_ns, bids, asks), now=now)

    assert sig is not None
    assert sig.confidence_score >= 80.0
    assert sig.grade is SignalGrade.A_PLUS
    assert sig.wave is SignalWave.STOP_HUNT_RECLAIM
    assert sig.direction == "LONG"
    assert sig.stop_loss < sig.entry_price < sig.target_1 < sig.target_2
    assert sig.rr_t1 >= 1.5

    # Interconnected Confluence Packet evaluation
    vrp = compute_vrp_snapshot(0.15, 0.115, [0.11, 0.12, 0.13, 0.14, 0.16])
    gex = compute_dealer_gex_profile(
        [ChainGEXNode(24000.0, 90_000, 180_000, 0.14, 0.15)],
        forward=24008.0,
        t_var=5.0 / 252.0,
    )
    fut_oi = classify_futures_oi_buildup(
        price_now=24008.0,
        price_3m_ago=23980.0,
        oi_now=11_800_000,
        oi_3m_ago=12_000_000,
        is_stop_hunt_reclaim=True,
    )
    pos = summarize_chain_positioning(
        [StrikeOINode(24000.0, 90_000, 220_000, 50_000, 95_000)],
        spot=24008.0,
    )
    breadth = compute_constituent_breadth(
        default_nifty50_heavyweights(),
        {"HDFCBANK": 0.9, "RELIANCE": 0.8, "ICICIBANK": 1.0},
    )
    ev_status = evaluate_event_risk([], now)

    packet_v1 = evaluate_interconnected_confluence(
        timestamp=now,
        symbol="NSE:NIFTY50-FUT",
        engine_version=EngineVersion.V1_SHADOW,
        scanner=scanner,
        state=st,
        vrp=vrp,
        gex=gex,
        futures_buildup=fut_oi,
        chain_positioning=pos,
        breadth=breadth,
        event_status=ev_status,
    )
    session_open = datetime(2026, 11, 10, 9, 15, tzinfo=IST)
    dec_v1 = dispatch_shadow_or_bot_signal(
        packet_v1,
        risk_limits=settings_v1.risk,
        account_state=AccountRiskState(),
        now=now,
        session_open=session_open,
        lot_size=65,
    )
    # V1 Shadow triggers radar alert but NEVER emits executable legs
    assert dec_v1.triggered
    assert not dec_v1.is_executable_v2_order
    assert len(dec_v1.stage1_hedge_legs) == 0

    # Switch to V2 Bot on the exact same signal -> emits executable LIMIT leg
    packet_v2 = evaluate_interconnected_confluence(
        timestamp=now,
        symbol="NSE:NIFTY50-FUT",
        engine_version=EngineVersion.V2_BOT,
        scanner=scanner,
        state=st,
        vrp=vrp,
        gex=gex,
        futures_buildup=fut_oi,
        chain_positioning=pos,
        breadth=breadth,
        event_status=ev_status,
    )
    dec_v2 = dispatch_shadow_or_bot_signal(
        packet_v2,
        risk_limits=settings_v2.risk,
        account_state=AccountRiskState(),
        now=now,
        session_open=session_open,
        lot_size=65,
    )
    # One Nifty future is ~₹15.6L notional: on ₹2L capital the V2 bot must refuse it
    # (the old code under-stated margin as min(12% of notional, 4 x stop risk) and let it through).
    assert not dec_v2.triggered and not dec_v2.is_executable_v2_order
    assert "only options may be traded in V2" in dec_v2.rationale  # owner decision 11 Nov

    # Forward-test the Shadow Ledger: trigger entry then hit Target 1
    active_sig = scanner.active_signals["NSE:NIFTY50-FUT"]
    scanner.on_quote(
        Quote("NSE:NIFTY50-FUT", now_ns + 1_000_000_000, ltp=active_sig.entry_price + 1.0)
    )
    scanner.on_depth(Depth("NSE:NIFTY50-FUT", now_ns + 1_000_000_000, bids, asks), now=now)
    assert scanner.active_signals["NSE:NIFTY50-FUT"].status is ShadowSignalStatus.ACTIVE

    entry_fill = scanner.active_signals["NSE:NIFTY50-FUT"].entry_price
    assert entry_fill == asks[0][0]  # long entry fills at the ask, not at LTP
    # The book moves up with the price; the exit fills at the bid, not at the target price.
    t1 = active_sig.target_1
    bids_up = tuple((t1 + 1.5 - i * 0.5, 5200, 14) for i in range(30))
    asks_up = tuple((t1 + 2.0 + i * 0.5, 850, 4) for i in range(30))
    scanner.on_quote(Quote("NSE:NIFTY50-FUT", now_ns + 2_000_000_000, ltp=t1 + 2.0))
    scanner.on_depth(Depth("NSE:NIFTY50-FUT", now_ns + 2_000_000_000, bids_up, asks_up), now=now)
    assert len(scanner.closed_outcomes) == 1
    out = scanner.closed_outcomes[0]
    assert out.status is ShadowSignalStatus.HIT_T1
    assert out.exit_price == bids_up[0][0]
    assert out.gross_pnl_inr == pytest.approx((bids_up[0][0] - entry_fill) * out.qty)
    # Charges come from config/rules: futures sell-side STT alone is 0.05% of exit turnover.
    assert out.charges_inr > 0.0005 * out.exit_price * out.qty
    assert out.net_pnl_inr == pytest.approx(out.gross_pnl_inr - out.charges_inr)

    # Verify XSS-safe HTML dashboard & JSONL ledger writing
    html_out = render_shadow_dashboard_html(
        scanner, session_date=date(2026, 11, 10), data_feed="<script>alert(1)</script>"
    )
    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out

    report_path = write_shadow_dashboard_and_ledger(
        scanner,
        session_date=date(2026, 11, 10),
        report_dir=tmp_path / "reports",
        ledger_file=tmp_path / "ledger.jsonl",
    )
    assert report_path.is_file()
    assert (tmp_path / "ledger.jsonl").is_file()
