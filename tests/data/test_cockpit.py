"""Cockpit: engine<->gateway bus, paper accounts B/C, vehicle choice, live IV strip, replay feed,
research stats and the gateway's local-only guards. Synthetic data only."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.analytics.expiry_clock import expiry_clock
from engine.analytics.iv import black76_price
from engine.cockpit.analytics_live import LiveAnalytics
from engine.cockpit.bus import EngineLink, EnginePublisher
from engine.cockpit.config import CockpitConfig
from engine.cockpit.core import CockpitCore, instrument_label
from engine.cockpit.paper import Book, PaperAccount
from engine.cockpit.replay import ReplayFeed
from engine.cockpit.research import signal_ledger_stats
from engine.core.calendar import TradingCalendar
from engine.core.clock import ManualClock, to_ns
from engine.core.config import load_settings
from engine.core.rules import IST, RuleBook
from engine.data.adapters.base import Depth, Quote
from engine.data.reference.instrument_master import Kind
from engine.shadow.models import ShadowSignalStatus
from engine.shadow.scanner import InstrumentMicrostructureState, ShadowOrderBookScanner
from tests.data.conftest import DAY, WEEKLY, build_master, opt_symbol

OPEN = datetime(2026, 11, 11, 9, 15, tzinfo=IST)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(cond, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- bus


def test_bus_publishes_snapshots_and_carries_commands() -> None:
    port = _free_port()
    key = b"k" * 64
    pub = EnginePublisher("127.0.0.1", port, key)
    pub.start()
    link = EngineLink("t", "127.0.0.1", port, key)
    link.start()
    try:
        assert _wait(lambda: link.connected)
        pub.publish({"time": "10:00:00", "n": 1})
        assert _wait(lambda: link.latest == {"time": "10:00:00", "n": 1})
        assert link.send({"cmd": "ping", "id": "x1"})
        cid, cmd = pub.commands.get(timeout=5)
        assert cmd == {"cmd": "ping", "id": "x1"}
        pub.reply(cid, {"id": "x1", "ok": True, "text": "pong"})
        assert _wait(lambda: any(a.get("text") == "pong" for a in link.acks))
    finally:
        link.stop()
        pub.close()


def test_bus_refuses_wrong_key_and_non_loopback() -> None:
    port = _free_port()
    pub = EnginePublisher("127.0.0.1", port, b"a" * 64)
    pub.start()
    bad = EngineLink("bad", "127.0.0.1", port, b"b" * 64)
    bad.start()
    try:
        time.sleep(0.5)
        assert not bad.connected
        pub.publish({"secret": 1})
        time.sleep(0.3)
        assert bad.latest is None
    finally:
        bad.stop()
        pub.close()
    with pytest.raises(ValueError):
        EnginePublisher("0.0.0.0", port, b"a" * 64)


# ---------------------------------------------------------------- paper accounts


def _account(repo_root: Path, tmp_path: Path, name: str = "manual") -> PaperAccount:
    settings = load_settings(root=repo_root, environ={})
    master = build_master(range(24900, 25150, 50))
    return PaperAccount(
        name,
        book=RuleBook.load(repo_root / "config" / "rules"),
        limits=settings.risk,
        broker_name="upstox_plus",
        session_open=OPEN,
        square_off_at=OPEN.replace(hour=15, minute=20),
        lot_size_of=lambda s: master.get(s).lot_size if s in master else 65,
        is_option=lambda s: s in master and master.get(s).kind.is_option,
        journal=tmp_path / f"paper_{name}.jsonl",
    )


def _book(bid: float, ask: float, qty: int = 650) -> Book:
    return Book(
        tuple((round(bid - i * 0.05, 2), qty, 5) for i in range(30)),
        tuple((round(ask + i * 0.05, 2), qty, 5) for i in range(30)),
    )


CE = opt_symbol(WEEKLY, 25000, Kind.CALL)


def test_paper_open_stop_exit_and_charges(repo_root: Path, tmp_path: Path) -> None:
    acct = _account(repo_root, tmp_path)
    t = OPEN.replace(hour=10, minute=30)
    ok, text = acct.open_long_option(CE, _book(99.9, 100.0), now=t, lots=1, stop_pct=25)
    assert ok, text
    pos = acct.positions[CE]
    assert pos.qty == 65 and pos.avg_price == 100.0 and pos.stop_price == 75.0
    assert pos.entry_charges >= 30.0  # ₹30 Upstox Plus brokerage + statutory charges
    # Price falls through the stop: the account sells at the bid.
    out = acct.check_exits({CE: _book(74.5, 74.6)}, t + timedelta(minutes=5))
    assert out and CE not in acct.positions
    trade = acct.closed[-1]
    assert trade.reason == "stop on premium"
    assert trade.gross_inr == pytest.approx((74.5 - 100.0) * 65)
    assert trade.net_inr < trade.gross_inr  # costs deducted


def test_paper_gate_refuses_opening_minutes_non_options_and_cooloff(
    repo_root: Path, tmp_path: Path
) -> None:
    acct = _account(repo_root, tmp_path)
    ok, text = acct.open_long_option(
        CE, _book(99.9, 100.0), now=OPEN.replace(minute=20), lots=1, stop_pct=25
    )
    assert not ok and "blocked_first_15_minutes" in text
    ok, text = acct.open_long_option(
        "NSE:NIFTY26NOVFUT", _book(1, 2), now=OPEN.replace(hour=10), lots=1, stop_pct=25
    )
    assert not ok and "only long options" in text
    t = OPEN.replace(hour=10, minute=30)
    assert acct.open_long_option(CE, _book(99.9, 100.0), now=t, lots=1, stop_pct=25)[0]
    acct.close(CE, _book(90.0, 90.1), now=t + timedelta(minutes=1), reason="test")
    assert acct.closed[-1].net_inr < 0
    ok, text = acct.open_long_option(
        CE, _book(99.9, 100.0), now=t + timedelta(minutes=3), lots=1, stop_pct=25
    )
    assert not ok and "in_loss_cooloff" in text
    ok, text = acct.open_long_option(CE, _book(99.9, 100.0), now=t, lots=2, stop_pct=25)
    assert not ok


def test_paper_square_off_and_journal_restore(repo_root: Path, tmp_path: Path) -> None:
    acct = _account(repo_root, tmp_path, "bot")
    t = OPEN.replace(hour=13, minute=0)
    assert acct.open_long_option(CE, _book(99.9, 100.0), now=t, lots=1, stop_pct=25)[0]
    again = _account(repo_root, tmp_path, "bot")
    assert again.load_journal(DAY) >= 1
    assert CE in again.positions and again.trades_today == 1
    out = again.check_exits({CE: _book(101.0, 101.1)}, OPEN.replace(hour=15, minute=21))
    assert out and again.closed[-1].reason == "square-off"
    third = _account(repo_root, tmp_path, "bot")
    third.load_journal(DAY)
    assert not third.positions and len(third.closed) == 1


# ---------------------------------------------------------------- vehicle choice


def _core(repo_root: Path, spot: float = 25010.0) -> CockpitCore:
    master = build_master(range(24900, 25150, 50))
    settings = load_settings(root=repo_root, environ={})
    sc = ShadowOrderBookScanner(settings.shadow_scanner)
    for ins in master.all():
        st = InstrumentMicrostructureState(
            ins.symbol, lot_size=max(1, ins.lot_size), is_option=ins.kind.is_option
        )
        st.bids, st.asks = ((100.0, 650, 5),), ((100.5, 650, 5),)
        st.ltp = spot if ins.kind is not Kind.CALL and ins.kind is not Kind.PUT else 100.0
        sc.states[ins.symbol] = st
    runner = SimpleNamespace(master=master, scanner=sc, spots=lambda: {"NIFTY": spot})
    core = CockpitCore(
        CockpitConfig(), source="test", bot=None, manual=None, analytics=None, publisher=None
    )  # type: ignore[arg-type]
    core.runner = runner  # type: ignore[assignment]
    return core


def test_vehicle_choice_maps_signals_to_long_options(repo_root: Path) -> None:
    core = _core(repo_root)
    sig = SimpleNamespace(symbol="NSE:NIFTY26NOVFUT", direction="LONG")
    assert core.choose_vehicle(sig) == opt_symbol(WEEKLY, 25000, Kind.CALL)  # type: ignore[arg-type]
    sig.direction = "SHORT"
    assert core.choose_vehicle(sig) == opt_symbol(WEEKLY, 25000, Kind.PUT)  # type: ignore[arg-type]
    short_call = SimpleNamespace(symbol=opt_symbol(WEEKLY, 25100, Kind.CALL), direction="SHORT")
    assert core.choose_vehicle(short_call) == opt_symbol(WEEKLY, 25000, Kind.PUT)  # type: ignore[arg-type]
    long_put = SimpleNamespace(symbol=opt_symbol(WEEKLY, 24950, Kind.PUT), direction="LONG")
    assert core.choose_vehicle(long_put) == long_put.symbol  # type: ignore[arg-type]
    assert instrument_label(core.master.get(CE)) == "NIFTY 25000 CE 17NOV"


# ---------------------------------------------------------------- live IV strip


def test_live_strip_recovers_the_volatility_used_to_price_the_chain(repo_root: Path) -> None:
    master = build_master(range(24800, 25250, 50))
    book = RuleBook.load(repo_root / "config" / "rules")
    cal = TradingCalendar.from_rulebook(book)
    now = datetime(2026, 11, 11, 11, 0, tzinfo=IST)
    clk = expiry_clock(now, WEEKLY, book=book, calendar=cal)
    r = book.lookup("risk_free_rate", now, tenor="91d_tbill").as_float() / 100
    spot, fwd, vol = 25000.0, 25012.0, 0.15
    states = {}
    for ins in master.all():
        st = InstrumentMicrostructureState(ins.symbol, is_option=ins.kind.is_option)
        if ins.kind.is_option and ins.expiry == WEEKLY:
            px = black76_price(fwd, ins.strike, clk.t_var, vol, is_call=ins.kind is Kind.CALL,
                               t_cal=clk.t_cal, risk_free_rate=r)  # fmt: skip
            st.bids, st.asks = ((px - 0.05, 650, 5),), ((px + 0.05, 650, 5),)
        elif ins.kind is Kind.INDEX:
            st.ltp = spot if "NIFTY" in ins.symbol else 14.0
        states[ins.symbol] = st
    la = LiveAnalytics(
        book=book, calendar=cal, master=master,
        underlyings={"NIFTY": "NSE:NIFTY50-INDEX"},
        iv_history={"NIFTY": [0.10 + i * 0.001 for i in range(100)]},
        chain_dir=None,
    )  # fmt: skip
    la.update(now, states)
    row = la.view()["NIFTY"]
    assert row["forward"] == pytest.approx(fwd, abs=1.0)
    assert row["atm_iv_pct"] == pytest.approx(15.0, abs=0.3)
    assert row["iv_percentile"] == 50.0  # 0.150 sits above 50 of 100 values 0.100..0.199
    assert row["r_verified"] is True


# ---------------------------------------------------------------- replay feed


def test_replay_feed_paces_filters_and_drives_the_clock() -> None:
    start = datetime(2026, 11, 11, 9, 15, tzinfo=IST)
    evs = [
        Quote("A", to_ns(start + timedelta(seconds=1)), ltp=1.0),
        Quote("B", to_ns(start + timedelta(seconds=2)), ltp=2.0),
        Depth("A", to_ns(start + timedelta(seconds=3)), ((1.0, 1, 1),), ((1.1, 1, 1),)),
    ]
    clock = ManualClock(start)
    feed = ReplayFeed(lambda: iter(evs), clock, speed=0, end_ns=to_ns(start + timedelta(minutes=1)))
    got: list[object] = []
    feed.subscribe(["A"], depth=True)
    feed.start(got.append)
    assert _wait(lambda: feed.done)
    assert [type(e).__name__ for e in got] == ["FeedStatus", "Quote", "Depth"]  # B filtered
    assert clock.now_ns() >= to_ns(start + timedelta(minutes=1))  # ends the runner loop
    assert feed.progress()["done"] is True


def test_replay_feed_pause_and_speed() -> None:
    start = datetime(2026, 11, 11, 9, 15, tzinfo=IST)
    evs = [Quote("A", to_ns(start + timedelta(seconds=i)), ltp=float(i)) for i in range(1, 4)]
    feed = ReplayFeed(
        lambda: iter(evs), ManualClock(start), speed=1.0, end_ns=to_ns(start + timedelta(minutes=1))
    )
    got: list[object] = []
    feed.subscribe(["A"], depth=True)
    feed.pause()
    feed.start(got.append)
    time.sleep(0.4)
    assert len(got) <= 2  # status (+ first event) only, then paused
    feed.set_speed(0)
    feed.resume()
    assert _wait(lambda: feed.done)
    assert len(got) == 4
    feed.stop()


# ---------------------------------------------------------------- research


def test_signal_ledger_stats_by_score_band(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    rows = [
        {"confidence_score": 76, "net_pnl_inr": 100, "net_pnl_2x_slippage_inr": 80, "r_multiple_net": 0.5,
         "wave": "W", "status": "HIT_T1", "opened_at": "2026-11-12T10:05:00+05:30", "closed_at": "2026-11-12T10:20:00+05:30"},
        {"confidence_score": 85, "net_pnl_inr": -50, "net_pnl_2x_slippage_inr": -70, "r_multiple_net": -0.4,
         "wave": "W", "status": "HIT_SL", "opened_at": "2026-11-12T11:05:00+05:30", "closed_at": "2026-11-12T11:20:00+05:30", "tradable": False},
    ]  # fmt: skip
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    d = signal_ledger_stats(path)
    bands = {b["band"]: b for b in d["bands"]}
    assert bands["75–80"]["n"] == 1 and bands["75–80"]["net"] == 100
    assert bands["80–90"]["win_pct"] == 0.0 and bands["80–90"]["net_2x"] == -70
    assert d["total"]["n"] == 2 and d["days"] == 1
    assert {x["key"] for x in d["tradable"]} == {"tradable", "not tradable"}


# ---------------------------------------------------------------- gateway guards


def test_gateway_requires_token_host_and_origin(repo_root: Path, tmp_path: Path) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from engine.cockpit.gateway import Gateway

    port = _free_port()
    ccfg = CockpitConfig(
        gateway_port=port, engine_port=_free_port(), replay_port=_free_port(),
        key_file=tmp_path / "cockpit.key", journal_dir=tmp_path, replay_dir=tmp_path / "replay",
    )  # fmt: skip
    gw = Gateway(ccfg, repo_root, ledger_file=tmp_path / "ledger.jsonl")

    async def run() -> None:
        async with TestClient(TestServer(gw.app(), host="127.0.0.1", port=port)) as c:
            r = await c.get("/")
            assert r.status == 200 and gw.token in await r.text()
            r = await c.get("/api/research/ledger")
            assert r.status == 403
            r = await c.get("/api/research/ledger", headers={"X-Cockpit-Token": gw.token})
            assert r.status == 200 and (await r.json())["total"]["n"] == 0
            r = await c.get(
                "/api/research/ledger",
                headers={"X-Cockpit-Token": gw.token, "Origin": "http://evil.example"},
            )
            assert r.status == 403
            r = await c.get("/", headers={"Host": "attacker.example:80"})
            assert r.status == 421
            r = await c.get("/ws")
            assert r.status == 403
            ws = await c.ws_connect(f"/ws?token={gw.token}")
            msg = await ws.receive_json(timeout=5)
            assert msg["type"] == "status" and msg["live"] is False
            await ws.send_json({"type": "cmd", "cmd": {"cmd": "rm -rf"}})
            ack = await ws.receive_json(timeout=5)
            assert ack["type"] == "ack" and not ack["ok"]
            await ws.close()

    asyncio.run(run())


def test_track_a_status_values_exist() -> None:
    assert ShadowSignalStatus.SQUARED_OFF_EOD.value == "SQUARED_OFF_EOD"
    assert threading.active_count() >= 1


# ---------------------------------------------------------------- bot follows Track A


def _signal(symbol: str, direction: str, at: datetime, score: float = 85.0):  # type: ignore[no-untyped-def]
    from engine.analytics.order_flow import OrderFlowMechanic
    from engine.shadow.models import ConfidenceBreakdown, ShadowSignal, SignalGrade, SignalWave

    return ShadowSignal(
        signal_id=f"SIG-{symbol}", created_at=at, symbol=symbol, direction=direction,
        wave=SignalWave.STOP_HUNT_RECLAIM, mechanic=list(OrderFlowMechanic)[0],
        status=ShadowSignalStatus.ACTIVE, grade=SignalGrade.A_PLUS, confidence_score=score,
        breakdown=ConfidenceBreakdown(20, 25, 20, 20), ltp_at_signal=25010.0,
        entry_price=25010.0, stop_loss=24990.0, target_1=25040.0, target_2=25060.0,
        rr_t1=1.5, rr_t2=2.5, sl_wall_info="", target_wall_info="", real_execution_ratio=0.8,
        obi_l5=0.6, air_pocket_ratio=0.4, cvd_z_score=2.1, futures_oi_quadrant="LONG_BUILDUP",
        recommended_risk_pct=1.0, recommended_qty=65, rationale="test",
    )  # fmt: skip


def test_bot_follows_track_a_and_snapshot_serialises(repo_root: Path, tmp_path: Path) -> None:
    from collections import deque

    from engine.core.clock import MarketSession
    from engine.shadow.models import ShadowLedgerOutcome

    core = _core(repo_root)
    core.bot = _account(repo_root, tmp_path, "bot")
    core.manual = _account(repo_root, tmp_path, "manual")
    runner = core.runner
    sc = runner.scanner  # type: ignore[union-attr]
    t = OPEN.replace(hour=10, minute=30)
    clock = ManualClock(t)
    session = MarketSession("NSE", "fo", DAY, OPEN.timetz().replace(tzinfo=None), OPEN.replace(hour=15, minute=40).timetz().replace(tzinfo=None), None, None, True)  # fmt: skip
    runner.__dict__.update(  # type: ignore[union-attr]
        clock=clock, subscribed=list(sc.states), last_eval={}, session=session,
        spot_symbol={"NIFTY": "NSE:NIFTY50-INDEX"}, or_hi={}, or_lo={}, or_frozen=False,
        lag_ms=deque([120.0, 180.0]), adapter=SimpleNamespace(name="sim"), last_feed_status="connected",
        stats=SimpleNamespace(quotes=1, depths=1, dropped_events=0, signals=1), cockpit_errors=0,
    )  # fmt: skip
    sig = _signal("NSE:NIFTY26NOVFUT", "LONG", t)
    sc.active_signals[sig.symbol] = sig
    core.on_step(to_ns(t))
    vehicle = opt_symbol(WEEKLY, 25000, Kind.CALL)
    assert vehicle in core.bot.positions, core.bot.refusals
    # Track A closes the episode -> the bot sells its option.
    del sc.active_signals[sig.symbol]
    sc.closed_outcomes.append(
        ShadowLedgerOutcome(
            sig.signal_id, sig.symbol, "LONG", sig.wave, 85.0, ShadowSignalStatus.HIT_T1,
            25010.0, 25040.0, 24990.0, 25040.0, 25060.0, 65, 1950.0, 300.0, 1650.0, 1.2,
            t, t + timedelta(minutes=4), "Hit Target 1",
        )
    )  # fmt: skip
    core.on_step(to_ns(t + timedelta(minutes=4)))
    assert vehicle not in core.bot.positions
    assert core.bot.closed[-1].reason == "signal exit (HIT_T1)"
    # A below-threshold signal is tracked by A but not traded by the bot.
    low = _signal(opt_symbol(WEEKLY, 25050, Kind.CALL), "LONG", t, score=76.0)
    sc.active_signals[low.symbol] = low
    core.on_step(to_ns(t + timedelta(minutes=5)))
    assert low.symbol not in core.bot.positions
    from engine.cockpit.condor import CondorConfig, CondorStrategy, SpreadAccount
    from engine.cockpit.setups import SetupConfig, SetupTracker

    core.setups = SetupTracker(SetupConfig(), journal=None)
    core.spread = SpreadAccount(
        "condor", book=core.bot.book, limits=core.bot.limits, broker_name="upstox_plus",
        session_open=OPEN, journal=None,
    )  # fmt: skip
    core.condor = CondorStrategy(
        cfg=CondorConfig(), account=core.spread, lot_size=lambda s: 65,
        option_symbol=core.option_symbol, pin=lambda syms: None,
        trading_day_before=lambda d: d - timedelta(days=1),
    )  # fmt: skip
    assert core.option_symbol("NIFTY", WEEKLY, 25000.0, "PE") == opt_symbol(WEEKLY, 25000, Kind.PUT)
    hits = core.search("25000 ce")
    assert hits and all("25000 CE" in h["label"] for h in hits) and hits[0]["live"]
    assert [h["label"] for h in hits[:2]] == ["NIFTY 25000 CE 17NOV", "NIFTY 25000 CE 23NOV"]
    assert core.search("  ") == []
    from engine.cockpit.core import _levels

    assert _levels(((100.0, 650, None), (99.95, 130, 4))) == [
        [100.0, 650.0, 0.0],
        [99.95, 130.0, 4.0],
    ]
    snap = core.snapshot(t + timedelta(minutes=5))
    assert snap["setups"]["live"] == [] and snap["condor"]["account"]["positions"] == []
    row = next(r for r in snap["scanner"] if r["symbol"] == vehicle)
    assert "score_1m" in row and "oi" in row and len(snap["books"][vehicle]["b"][0]) == 3
    text = json.dumps(snap, default=str)
    assert snap["mode"] == "PAPER" and snap["feed"]["lag_ms"] == 150
    assert snap["accounts"]["bot"]["closed"][0]["reason"] == "signal exit (HIT_T1)"
    assert any(r["symbol"] == vehicle for r in snap["scanner"]) and len(text) < 2_000_000
    # Feed stale in market hours (approved rule): entries and exits wait for fresh quotes.
    t30 = t + timedelta(minutes=30)
    for st in sc.states.values():
        st.last_recv_ns = to_ns(t30 - timedelta(seconds=1))
    core._check_feed(to_ns(t30), t30)
    assert not core.feed_frozen
    for st in sc.states.values():
        st.last_recv_ns = to_ns(t30 - timedelta(seconds=25))
    core._check_feed(to_ns(t30), t30)
    assert core.feed_frozen and core.events[0]["text"].startswith("FEED STALE")
    ok, msg = core.handle({"cmd": "buy", "symbol": vehicle, "stop_pct": 20}, t30)
    assert not ok and "feed stale" in msg
    for st in sc.states.values():
        st.last_recv_ns = to_ns(t30 - timedelta(seconds=1))
    core._check_feed(to_ns(t30), t30)
    assert not core.feed_frozen and "resume" in core.events[0]["text"]
    # Outside market hours a quiet feed is normal: nothing is frozen.
    core._check_feed(to_ns(t30) + 6 * 3600 * 10**9, t30 + timedelta(hours=6))
    assert not core.feed_frozen
    # Manual command path: buy via the page, then close.
    ok, msg = core.handle({"cmd": "buy", "symbol": vehicle, "stop_pct": 20}, t30)
    assert ok, msg
    ok, msg = core.handle({"cmd": "close", "symbol": vehicle}, t + timedelta(minutes=31))
    assert ok, msg
    ok, msg = core.handle({"cmd": "pause"}, t)
    assert not ok and "replay" in msg


def test_replay_compresses_long_silences() -> None:
    start = datetime(2026, 11, 11, 9, 15, tzinfo=IST)
    evs = [
        Quote("A", to_ns(start + timedelta(seconds=1)), ltp=1.0),
        Quote("A", to_ns(start + timedelta(hours=2)), ltp=2.0),  # a 2-hour hole in the data
    ]
    clock = ManualClock(start)
    feed = ReplayFeed(lambda: iter(evs), clock, speed=1.0, end_ns=to_ns(start + timedelta(hours=3)))
    got: list[object] = []
    feed.subscribe(["A"], depth=True)
    t0 = time.monotonic()
    feed.start(got.append)
    assert _wait(lambda: feed.done, timeout=5)
    assert time.monotonic() - t0 < 3.0  # played as ~1 s, not 2 hours
    assert feed.progress()["skipped_gaps"] == 1 and len(got) == 3


def test_stock_label_is_the_trading_symbol_not_the_isin_key() -> None:
    from engine.data.reference.instrument_master import Instrument, Kind

    eq = Instrument("NSE_EQ|INE062A01020", "upstox", "NSE", "cm", "NSE_EQ|INE062A01020", "3045",
                    "SBIN", Kind.EQUITY, None, None, None, 1, 0.05, "INE062A01020", "SBIN")
    assert instrument_label(eq) == "SBIN EQ"
