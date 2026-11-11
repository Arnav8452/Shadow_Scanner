"""Unit tests for M3 & M9 Data Modules and Upstox V3 `full_d30` 30-Level Depth Adapter."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from engine.core.env import Secret
from engine.core.rules import IST
from engine.data.adapters.base import Depth, Quote
from engine.data.adapters.upstox import (
    MODE_FULL_D30,
    UpstoxFeed,
    build_upstox_sub_frame,
    parse_upstox_v3_feed,
    resolve_upstox_token,
)
from engine.data.constituents import (
    IndexConstituent,
    compute_constituent_breadth,
    default_nifty50_heavyweights,
    point_in_time_constituents,
)
from engine.data.eod import (
    DeliveryBias,
    ParticipantCategory,
    parse_cash_bhavcopy_csv,
    parse_fno_bhavcopy_csv,
    parse_participant_oi_csv,
    scan_relative_delivery_spikes,
    summarize_participant_regime,
)
from engine.data.events import EventCategory, ScheduledEvent, evaluate_event_risk
from engine.data.reference.corporate_actions import (
    DividendAction,
    MWPLStatus,
    SplitBonusAction,
    adjust_strike_and_lot,
    pv_discrete_dividends,
)


def test_upstox_analytics_token_precedence() -> None:
    env = {
        "UPSTOX_ANALYTICS_TOKEN": "analytics-1yr-token",
        "UPSTOX_ACCESS_TOKEN": "daily-access-token",
    }
    tok = resolve_upstox_token(env)
    assert tok.reveal() == "analytics-1yr-token"
    assert "analytics-1yr-token" not in repr(tok)


def test_upstox_v3_full_d30_parser_and_adapter() -> None:
    raw_quotes = [
        {
            "bidP": 24000.0 - i * 0.5,
            "bidQ": 1000 + i * 50,
            "bidO": 5 + i,
            "askP": 24000.5 + i * 0.5,
            "askQ": 800 + i * 30,
            "askO": 4 + i,
        }
        for i in range(30)
    ]
    payload = {
        "type": "live_feed",
        "currentTs": "1762766100000",
        "feeds": {
            "NSE_FO|NIFTY26NOV24000CE": {
                "fullFeed": {
                    "marketFF": {
                        "ltpc": {"ltp": 215.5, "ltt": "1762766100000", "ltq": 65, "cp": 190.0},
                        "marketLevel": {"bidAskQuote": raw_quotes},
                        "optionGreeks": {
                            "iv": 0.142,
                            "delta": 0.52,
                            "gamma": 0.0012,
                            "theta": -14.5,
                            "vega": 11.2,
                            "rho": 3.1,
                        },
                        "marketOHLC": {
                            "ohlc": [{"interval": "1d", "open": 195.0, "high": 220.0, "low": 188.0}]
                        },
                        "eFeedDetails": {
                            "atp": 208.4,
                            "vtt": 650000,
                            "oi": 2400000,
                            "tbq": 180000,
                            "tsq": 140000,
                            "iep": 216.0,
                            "iiqTotal": 4500,
                        },
                    }
                }
            }
        },
    }
    events = parse_upstox_v3_feed(payload, recv_ns=1_000_000_000)
    assert len(events) == 2
    q, d = events[0], events[1]
    assert isinstance(q, Quote)
    assert isinstance(d, Depth)
    assert q.ltp == 215.5
    assert q.delta == 0.52
    assert q.iep == 216.0
    assert q.iiq_total == 4500
    assert len(d.bids) == 30
    assert len(d.asks) == 30
    assert d.bids[0][0] == 24000.0
    assert d.asks[0][0] == 24000.5

    sub_bytes = build_upstox_sub_frame(["NSE_FO|12345"], mode=MODE_FULL_D30)
    assert b"full_d30" in sub_bytes

    feed = UpstoxFeed(token=Secret("UPSTOX_ANALYTICS_TOKEN", "tok"), mode=MODE_FULL_D30)
    received: list[object] = []
    feed.start(received.append)
    feed.subscribe([f"SYM_{i}" for i in range(50)], depth=True)
    assert len(feed.symbols) == 50
    # Exceeding 50 symbols in full_d30 mode emits StatusKind.LIMIT
    feed.subscribe(["SYM_51"], depth=True)
    assert len(feed.symbols) == 50
    assert feed.ingest_frame(payload) == 2


def test_eod_bhavcopy_and_relative_delivery_scanner() -> None:
    csv_lines = [
        "SYMBOL,SERIES,DATE1,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,CLOSE_PRICE,PREV_CLOSE,TTL_TRD_QNTY,TURNOVER_LACS,DELIV_QTY,DELIV_PER"
    ]
    for d in range(1, 11):
        csv_lines.append(
            f"RELIANCE,EQ,2026-11-{d:02d},1300,1310,1295,1305,1300,100000,1305.0,45000,45.0"
        )
        csv_lines.append(
            f"CHURNCO,EQ,2026-11-{d:02d},200,205,198,201,200,500000,1005.0,200000,40.0"
        )
    # Day 11: RELIANCE has a 2.6x delivery spike with 62% delivery and +2.3% close -> LONG_ACCUMULATION
    csv_lines.append(
        "RELIANCE,EQ,2026-11-11,1305,1340,1302,1335,1305,190000,2520.0,117800,62.0"
    )
    # Day 11: CHURNCO spikes +8% on tiny 15% delivery -> RETAIL_CHURN_FADE
    csv_lines.append(
        "CHURNCO,EQ,2026-11-11,201,222,200,217,201,2000000,4300.0,300000,15.0"
    )

    rows = parse_cash_bhavcopy_csv("\n".join(csv_lines))
    assert len(rows) == 22
    assert rows[-1].known_at.hour == 19 and rows[-1].known_at.minute == 30

    by_sym: dict[str, list] = {}
    for r in rows:
        by_sym.setdefault(r.symbol, []).append(r)

    watch = scan_relative_delivery_spikes(by_sym, lookback_days=10)
    by_watch = {w.symbol: w for w in watch}
    assert by_watch["RELIANCE"].bias is DeliveryBias.LONG_ACCUMULATION
    assert by_watch["RELIANCE"].delivery_qty_multiple >= 2.5
    assert by_watch["CHURNCO"].bias is DeliveryBias.RETAIL_CHURN_FADE

    fno_csv = (
        "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,OPEN_INT,CHG_IN_OI,TIMESTAMP\n"
        "FUTIDX,NIFTY,2026-11-24,0,XX,24000,24150,23950,24100,24100,120000,11500000,450000,2026-11-10\n"
    )
    fno_rows = parse_fno_bhavcopy_csv(fno_csv)
    assert len(fno_rows) == 1
    assert fno_rows[0].oi == 11_500_000


def test_participant_oi_and_regime() -> None:
    text = """Participant wise Open Interest
Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short,Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short
Client,220000,140000,1500000,900000,1200000,900000,1400000,1100000
DII,60000,90000,200000,1800000,5000,80000,0,0
FII,110000,75000,1900000,1400000,450000,550000,320000,410000
Pro,55000,62000,600000,550000,680000,590000,590000,720000
"""
    rows = parse_participant_oi_csv(text, trade_date=date(2026, 11, 10))
    assert len(rows) == 4
    fii = next(r for r in rows if r.category is ParticipantCategory.FII)
    assert fii.net_fut_idx == 35_000
    reg = summarize_participant_regime(rows)
    assert reg.institutional_bias == "BULLISH"
    assert reg.score_pts > 0


def test_corporate_actions_constituents_and_event_blackout() -> None:
    divs = [
        DividendAction("HDFCBANK", date(2026, 11, 20), 19.5, announced_on=date(2026, 11, 1)),
        DividendAction("HDFCBANK", date(2026, 11, 22), 10.0, announced_on=date(2026, 11, 15)),
    ]
    # On 2026-11-10, only the first dividend was announced (point-in-time safety)
    pv = pv_discrete_dividends(
        divs, symbol="HDFCBANK", as_of=date(2026, 11, 10), expiry=date(2026, 11, 24)
    )
    assert 19.0 < pv <= 19.5

    splits = [SplitBonusAction("RELIANCE", date(2026, 10, 28), numerator=2, denominator=1)]
    adj_k, adj_lot = adjust_strike_and_lot(
        2600.0, 250, splits, symbol="RELIANCE", from_date=date(2026, 10, 1), to_date=date(2026, 11, 1)
    )
    assert adj_k == 1300.0
    assert adj_lot == 500

    mwpl_ban = MWPLStatus(date(2026, 11, 10), "ZEEL", 96.4, in_ban=True)
    assert not mwpl_ban.allow_new_fno_entry
    assert mwpl_ban.early_warning

    members = default_nifty50_heavyweights()
    pit = point_in_time_constituents(
        [*members, IndexConstituent("OLDCO", "METAL", 1.2, date(2020, 1, 1), date(2025, 1, 1))],
        date(2026, 11, 10),
    )
    assert all(m.symbol != "OLDCO" for m in pit)

    breadth = compute_constituent_breadth(
        members,
        {"HDFCBANK": 1.2, "RELIANCE": 0.9, "ICICIBANK": 1.1, "INFY": -0.2, "TCS": -0.1},
    )
    assert breadth.weighted_breadth_pct > 45.0
    assert breadth.top_sector_leader == "BANKS"

    now = datetime(2026, 11, 10, 10, 5, tzinfo=IST)
    rbi_ev = ScheduledEvent(
        "RBI MPC Policy Decision",
        EventCategory.RBI_MPC,
        datetime(2026, 11, 10, 10, 0, tzinfo=IST),
        datetime(2026, 11, 10, 10, 30, tzinfo=IST),
    )
    status = evaluate_event_risk([rbi_ev], now)
    assert status.in_macro_blackout
    assert not status.order_flow_allowed
    assert status.short_vega_scale_factor == 0.0

    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_event_risk([], datetime(2026, 11, 10, 10, 0))  # noqa: DTZ001
