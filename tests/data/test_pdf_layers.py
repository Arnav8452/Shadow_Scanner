"""Layers added from the "retail access to hedge-fund data" review: OI timing, SVI smile,
constituents and dispersion, macro event calendar, EOD flows, position Greeks."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.analytics.svi import SviFit, fit_svi
from engine.cockpit.analytics_live import LiveAnalytics
from engine.cockpit.paper import Book
from engine.core.calendar import TradingCalendar
from engine.core.rules import IST, RuleBook
from engine.data.constituents.table import (
    broker_key,
    heavyweights,
    load_constituent_table,
    weighted_average_correlation,
)
from engine.data.eod.flows import fetch_current_eod, parse_ban_list, parse_deals
from engine.data.events.calendar_file import blackout_status, load_calendar, upcoming
from engine.data.recorder.run import constituent_keys
from engine.data.reference.instrument_master import Kind
from engine.shadow.scanner import InstrumentMicrostructureState, oi_window
from tests.data.conftest import WEEKLY, build_master, opt_symbol

S = 1_000_000_000


# ---------------------------------------------------------------- OI timing


def test_oi_window_uses_oi_update_instants_over_three_minutes() -> None:
    hist = [(0, 100.0, 1000), (60 * S, 101.0, 1010), (200 * S, 103.0, 1050)]
    earlier, latest = oi_window(hist, 180 * S, 120 * S)  # type: ignore[misc]
    assert earlier == (0, 100.0, 1000) and latest[2] == 1050
    assert oi_window(hist[:2], 180 * S, 120 * S) is None  # only 60 s of OI history
    assert oi_window(hist[:1], 180 * S, 120 * S) is None


def test_oi_history_records_only_oi_changes() -> None:
    from engine.data.adapters.base import Quote
    from engine.shadow.scanner import ShadowOrderBookScanner

    sc = ShadowOrderBookScanner()
    for i, (px, oi) in enumerate([(100.0, 500), (100.5, 500), (101.0, 500), (101.5, 520)]):
        sc.on_quote(Quote("F", i * S, ltp=px, oi=oi))
    assert [h[2] for h in sc.states["F"].oi_history] == [500, 520]
    assert sc.states["F"].oi_history[-1][1] == 101.5  # price when OI actually changed


# ---------------------------------------------------------------- SVI


def test_svi_fit_recovers_a_known_smile() -> None:
    true = SviFit(0.0008, 0.02, -0.5, 0.01, 0.03, 6 / 365, 0, 0)
    ks = [-0.06 + i * 0.006 for i in range(21)]
    fit = fit_svi(ks, [true.iv(k) for k in ks], 6 / 365)
    assert fit is not None and fit.rmse_vol_pts < 0.2
    assert fit.iv(0.0) == pytest.approx(true.iv(0.0), abs=0.003)
    assert fit.b * (1 + abs(fit.rho)) <= 4 / fit.t + 1e-9  # Lee wing bound
    assert fit_svi(ks[:3], [0.2, 0.2, 0.2], 0.02) is None


# ---------------------------------------------------------------- constituents and dispersion


def test_constituent_table_is_point_in_time(repo_root: Path) -> None:
    rows = load_constituent_table(repo_root / "config" / "reference" / "nifty50_constituents.csv")
    assert len([r for r in rows if r.index == "NIFTY"]) == 50
    top = heavyweights(rows, "NIFTY", date(2026, 11, 12), 8)
    assert [r.symbol for r in top][:3] == ["HDFCBANK", "RELIANCE", "ICICIBANK"]
    assert not any(r.weight_verified for r in top)  # weights carried over without a source
    assert heavyweights(rows, "NIFTY", date(2026, 1, 1), 8) == []  # no history before load
    assert broker_key(top[0], "upstox") == "NSE_EQ|INE040A01034"
    assert broker_key(top[0], "fyers") == "NSE:HDFCBANK-EQ"
    keys = constituent_keys(
        {"index": "NIFTY", "top_n": 3, "file": "config/reference/nifty50_constituents.csv"},
        "upstox",
        on=date(2026, 11, 12),
    )
    assert len(keys) == 3 and all(k.startswith("NSE_EQ|") for k in keys)


def test_weighted_average_correlation() -> None:
    a = [math.sin(i / 3) for i in range(40)]
    b = [2 * x for x in a]
    c = [-x for x in a]
    corr, pairs = weighted_average_correlation({"A": a, "B": b}, {"A": 1.0, "B": 1.0})
    assert corr == pytest.approx(1.0) and pairs == 1
    corr, _ = weighted_average_correlation({"A": a, "C": c}, {"A": 1.0, "C": 1.0})
    assert corr == pytest.approx(-1.0)
    assert weighted_average_correlation({"A": a[:5], "B": b[:5]}, {"A": 1, "B": 1})[0] is None


def test_breadth_feeds_index_scanner_states(repo_root: Path) -> None:
    master = build_master(range(24900, 25150, 50))
    la = LiveAnalytics(
        book=RuleBook.load(repo_root / "config" / "rules"), calendar=None, master=master,
        underlyings={"NIFTY": "NSE:NIFTY50-INDEX"}, iv_history={}, chain_dir=None,
    )  # fmt: skip
    spot = InstrumentMicrostructureState("NSE:NIFTY50-INDEX", ltp=25100.0, session_open=25000.0)
    fut = InstrumentMicrostructureState("NSE:NIFTY26NOVFUT", ltp=24950.0, session_open=25000.0)
    states = {spot.symbol: spot, fut.symbol: fut}
    la.dispersion = {"weighted_breadth_pct": 40.0}
    la._apply_breadth(states)
    assert spot.breadth_aligned is True  # broad up-breadth and the index is up
    assert fut.breadth_aligned is False  # futures down against up-breadth
    la.dispersion = {"weighted_breadth_pct": 10.0}
    la._apply_breadth(states)
    assert spot.breadth_aligned is False  # breadth too narrow to count


# ---------------------------------------------------------------- event calendar


def test_macro_calendar_loads_and_blocks_around_rbi(repo_root: Path) -> None:
    events = load_calendar(repo_root / "config" / "events" / "macro_calendar.csv")
    assert len(events) >= 15
    assert all(e.source_url for e in events if e.status != "UNVERIFIED")
    rbi = datetime(2026, 12, 4, 10, 5, tzinfo=IST)
    st = blackout_status(events, rbi)
    assert st.in_macro_blackout and any("RBI" in t for t in st.active_blackout_events)
    assert not blackout_status(events, datetime(2026, 12, 4, 12, 0, tzinfo=IST)).in_macro_blackout
    nxt = upcoming(events, datetime(2026, 11, 20, 9, 0, tzinfo=IST), 2)
    assert nxt[0]["title"] == "RBI MPC policy decision" and nxt[0]["in_hours"] > 0


def test_calendar_requires_sources(tmp_path: Path) -> None:
    p = tmp_path / "cal.csv"
    p.write_text(
        "date,time_ist,category,title,source_url,status\n2026-12-04,10:00,RBI_MPC,RBI,,primary\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source_url"):
        load_calendar(p)


def test_event_window_blocks_paper_entries(repo_root: Path, tmp_path: Path) -> None:
    from tests.data.test_cockpit import CE, _account, _book

    acct = _account(repo_root, tmp_path)
    acct.entry_block = lambda now: "event window: RBI MPC policy decision"
    ok, text = acct.open_long_option(
        CE, _book(99.9, 100.0), now=datetime(2026, 11, 11, 10, 30, tzinfo=IST), lots=1, stop_pct=25
    )
    assert not ok and "event window" in text


# ---------------------------------------------------------------- EOD flows


BAN = "Securities in Ban For Trade Date 12-NOV-2026:\n1,KAYNES\n2,ABC\n"
DEALS = (
    "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,"
    "Trade Price / Wght. Avg. Price,Remarks\n"
    "11-NOV-2026,XYZ,Xyz Ltd,Some Fund,BUY,100000,250.50,-\n"
)


def test_flow_parsers() -> None:
    day, syms = parse_ban_list(BAN)
    assert day == date(2026, 11, 12) and syms == ["KAYNES", "ABC"]
    deals = parse_deals(DEALS, "bulk")
    assert deals[0].trade_date == date(2026, 11, 11) and deals[0].side == "BUY"
    assert deals[0].value_cr == pytest.approx(100000 * 250.5 / 1e7)


def test_current_files_are_filed_under_their_own_date(tmp_path: Path) -> None:
    now = datetime(2026, 11, 11, 20, 0, tzinfo=IST)
    res = fetch_current_eod("fo_ban", raw_dir=tmp_path, now=now, fetcher=lambda u: BAN.encode())
    assert res.status == "ok" and res.path is not None and res.path.stem == "2026-11-12"
    res = fetch_current_eod("bulk", raw_dir=tmp_path, now=now, fetcher=lambda u: b"<html>")
    assert res.status == "missing"
    empty = DEALS.splitlines()[0].encode()
    res = fetch_current_eod("block", raw_dir=tmp_path, now=now, fetcher=lambda u: empty)
    assert res.status == "missing" and "no rows" in res.detail


# ---------------------------------------------------------------- position greeks


def test_position_greeks_use_our_forward_and_iv(repo_root: Path) -> None:
    from engine.analytics.expiry_clock import expiry_clock
    from engine.analytics.iv import black76_price
    from engine.cockpit.config import CockpitConfig
    from engine.cockpit.core import CockpitCore

    master = build_master(range(24900, 25150, 50))
    book = RuleBook.load(repo_root / "config" / "rules")
    cal = TradingCalendar.from_rulebook(book)
    la = LiveAnalytics(book=book, calendar=cal, master=master, underlyings={"NIFTY": "NSE:NIFTY50-INDEX"},
                       iv_history={}, chain_dir=None)  # fmt: skip
    la.strip["NIFTY"] = {"forward": 25000.0}
    core = CockpitCore(
        CockpitConfig(), source="t", bot=None, manual=None, analytics=la, publisher=None
    )  # type: ignore[arg-type]
    core.runner = SimpleNamespace(master=master)  # type: ignore[assignment]
    now = datetime(2026, 11, 11, 11, 0, tzinfo=IST)
    clk = expiry_clock(now, WEEKLY, book=book, calendar=cal)
    r = book.lookup("risk_free_rate", now, tenor="91d_tbill").as_float() / 100
    px = black76_price(
        25000.0, 25000.0, clk.t_var, 0.14, is_call=True, t_cal=clk.t_cal, risk_free_rate=r
    )
    g = core.position_greeks(
        opt_symbol(WEEKLY, 25000, Kind.CALL),
        65,
        Book(((px - 0.05, 650, 1),), ((px + 0.05, 650, 1),)),
        now,
    )
    assert g is not None
    assert g["iv"] == pytest.approx(14.0, abs=0.1)
    assert 0.45 * 65 < g["delta"] < 0.55 * 65  # ATM call
    assert g["theta"] < 0 and g["vega"] > 0
    put = core.position_greeks(
        opt_symbol(WEEKLY, 25000, Kind.PUT),
        65,
        Book(((px - 0.05, 650, 1),), ((px + 0.05, 650, 1),)),
        now,
    )
    assert put is not None and put["delta"] < 0
    _ = timedelta  # keep import for readers
