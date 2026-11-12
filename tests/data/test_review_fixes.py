"""Fixes from the 12 Nov 2026 review of the Sensex expiry close (new/ folder notes)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from engine.cockpit.setups import SetupConfig, SetupTracker
from engine.cockpit.strategies import _Bar, absorption_trap
from engine.cockpit.zones import ZoneConfig, ZoneTracker
from engine.core.config import ShadowScannerConfig
from engine.core.rules import IST
from engine.data.reference.instrument_master import Kind
from engine.shadow.models import SignalWave
from engine.shadow.scanner import (
    InstrumentMicrostructureState,
    ShadowOrderBookScanner,
    book_disagrees,
)
from tests.data.conftest import DAY, WEEKLY, opt_symbol
from tests.data.test_cockpit import _account, _book, _core, _signal

T = datetime(2026, 11, 11, 11, 0, tzinfo=IST)


def _opt(bid: float, ask: float, qty: int = 650) -> InstrumentMicrostructureState:
    st = InstrumentMicrostructureState("OPT", lot_size=65, is_option=True)
    st.bids = tuple((round(bid - i * 0.05, 2), qty, 5) for i in range(5) if bid - i * 0.05 > 0)
    st.asks = tuple((round(ask + i * 0.05, 2), qty, 5) for i in range(5))
    st.ltp = (bid + ask) / 2
    return st


# ---------------------------------------------------------------- scanner hygiene


def test_dead_and_illiquid_options_are_not_scored() -> None:
    sc = ShadowOrderBookScanner(ShadowScannerConfig())
    assert sc.ineligible_reason(_opt(0.05, 0.10)) == "tick floor"
    assert sc.ineligible_reason(_opt(2.0, 2.1)) == "premium too small"
    assert sc.ineligible_reason(_opt(20.0, 24.0)) == "spread too wide"
    no_bid = _opt(20.0, 20.1)
    no_bid.bids = ()
    assert sc.ineligible_reason(no_bid) == "no bid"
    assert sc.ineligible_reason(_opt(88.75, 89.0)) is None
    fut = _opt(0.05, 0.10)
    fut.is_option = False
    assert sc.ineligible_reason(fut) is None  # only options are filtered
    dead = _opt(0.05, 0.10)
    sc.states["OPT"] = dead
    assert sc.evaluate_instrument("OPT", now=T) is None


def test_book_disagreement_and_location_gate() -> None:
    # Touch leans down (40 bid vs 420 ask) while a passive 32-lakh bid one level lower
    # makes the top-5 imbalance strongly positive: the 12 Nov 82000 PE book.
    bids = ((88.75, 40, 1), (88.70, 3_297_000, 1))
    asks = ((89.00, 420, 1), (89.05, 843_440, 1))
    assert book_disagrees(bids, asks, 0.47)
    assert not book_disagrees(bids, asks, -0.47)
    assert not book_disagrees(((100.0, 500, 1),), ((100.5, 500, 1),), 0.6)  # no lean
    sc = ShadowOrderBookScanner(ShadowScannerConfig())
    st = _opt(100.0, 100.5)  # no previous-day or opening-range levels: location 0
    sc.states["OPT"] = st
    late = T.replace(hour=15, minute=10)  # inside 15:00-15:40: no CAS bonus for options
    sig = sc.evaluate_instrument("OPT", now=late)
    assert sig is not None and sig.wave is not SignalWave.CAS_3PM_VWAP_REVERSAL
    assert sig.breakdown.structural_location_pts < 5
    assert sig.confidence_score <= 60.0


# ---------------------------------------------------------------- readings and setups


def test_readings_are_excluded_late_on_expiry_and_for_dead_options(repo_root: Path) -> None:
    core = _core(repo_root)
    sig = _signal(opt_symbol(WEEKLY, 25000, Kind.CALL), "LONG", T)
    core.analytics = SimpleNamespace(detail={})  # type: ignore[assignment]
    assert core.reading_excluded(sig, "NIFTY") is None
    late = _signal(opt_symbol(WEEKLY, 25000, Kind.CALL), "LONG", T.replace(hour=15, minute=16))
    assert "after 15:15" in str(core.reading_excluded(late, "NIFTY"))
    core.runner.subscribed = [opt_symbol(WEEKLY, 25000, Kind.CALL)]  # type: ignore[union-attr]
    on_expiry = _signal(opt_symbol(WEEKLY, 25000, Kind.CALL), "LONG",
                        datetime(2026, 11, 17, 14, 50, tzinfo=IST))  # fmt: skip
    assert "expires today" in str(core.reading_excluded(on_expiry, "NIFTY"))
    core.analytics.detail[sig.symbol] = {"delta": 0.995, "time_value": 0.06, "intrinsic": 88.8}
    assert "|delta|" in str(core.reading_excluded(sig, "NIFTY"))
    core.analytics.detail[sig.symbol] = {"delta": 0.6, "time_value": 0.5, "intrinsic": 40.0}
    assert "time value" in str(core.reading_excluded(sig, "NIFTY"))
    core.analytics.detail[sig.symbol] = {"settling": True}
    assert "settlement" in str(core.reading_excluded(sig, "NIFTY"))


def test_setups_close_at_the_bell() -> None:
    tr = SetupTracker(SetupConfig(), journal=None)
    s = tr.add_reading(underlying="SENSEX", bullish=False, now=T, spot=81967.0,
                       label="x", score=81, wave="W")  # fmt: skip
    out = tr.close_all(T.replace(hour=15, minute=30), "market closed")
    assert s is not None and s.state == "FAILED" and out and not tr.setups
    assert tr.history[-1]["why"] == "market closed"


# ---------------------------------------------------------------- expiry-day rules


def test_long_options_expiring_today_stop_at_1430_and_exit_at_1450(
    repo_root: Path, tmp_path: Path
) -> None:
    acct = _account(repo_root, tmp_path)
    acct.expiry_of = lambda s: WEEKLY
    sym = opt_symbol(WEEKLY, 25000, Kind.CALL)
    exp_day = datetime(2026, 11, 17, 9, 15, tzinfo=IST)
    acct.session_open = exp_day
    acct.square_off_at = exp_day.replace(hour=15, minute=20)
    ok, text = acct.open_long_option(sym, _book(99.9, 100.0), now=exp_day.replace(hour=14, minute=31),
                                     lots=1, stop_pct=25)  # fmt: skip
    assert not ok and "expires today" in text
    assert acct.open_long_option(sym, _book(99.9, 100.0), now=exp_day.replace(hour=13),
                                 lots=1, stop_pct=25)[0]  # fmt: skip
    assert not acct.check_exits({sym: _book(101.0, 101.1)}, exp_day.replace(hour=14, minute=49))
    out = acct.check_exits({sym: _book(101.0, 101.1)}, exp_day.replace(hour=14, minute=50))
    assert out and acct.closed[-1].reason == "expiry-day exit 14:50"
    # Not on other days: a position in the same contract on 11 Nov is not closed at 14:50.
    acct2 = _account(repo_root, tmp_path / "b")
    acct2.expiry_of = lambda s: WEEKLY
    t = datetime.combine(DAY, datetime.min.time(), IST).replace(hour=13)
    assert acct2.open_long_option(sym, _book(99.9, 100.0), now=t, lots=1, stop_pct=25)[0]
    assert not acct2.check_exits({sym: _book(101.0, 101.1)}, t.replace(hour=14, minute=50))


def test_absorption_trap_on_the_breakout_bar() -> None:
    t = T
    stalled = _Bar(t, 100.0, 104.0, 99.0, 100.5, vol=3000.0)  # 3x volume, small body
    assert absorption_trap(stalled, 1000.0, up=True)
    wick = _Bar(t, 100.0, 110.0, 99.5, 103.0, vol=2100.0)  # long upper wick vs an up-break
    assert "wick" in str(absorption_trap(wick, 1000.0, up=True))
    clean = _Bar(t, 100.0, 106.0, 99.8, 105.6, vol=3000.0)
    assert absorption_trap(clean, 1000.0, up=True) is None


# ---------------------------------------------------------------- futures zones and OI read


def test_zone_tracker_finds_a_floor_that_traps_sellers() -> None:
    zt = ZoneTracker(ZoneConfig(window_min=30, min_battle_min=8, iceberg_ratio=12))
    cum = 0.0
    t = T
    for i in range(12 * 60):  # 12 minutes of selling into 25,200-25,210 at the bid
        cum += 100
        zt.on_sample("NIFTY", t, ltp=25204.0, cum_volume=cum, bid=25204.0, ask=25204.5,
                     bid_qty=650, ask_qty=650)  # fmt: skip
        t += timedelta(seconds=1)
    assert zt.zones("NIFTY", 25205.0)["floors"] == []  # not yet left the zone
    z = zt.zones("NIFTY", 25222.0)
    assert z["floors"] and z["floors"][0]["low"] == 25200.0 and z["floors"][0]["ratio"] >= 12
    assert z["ceilings"] == []
    # Volume shown at the bid as large as what traded is not an iceberg.
    zt2 = ZoneTracker(ZoneConfig())
    cum = 0.0
    t = T
    for _ in range(12 * 60):
        cum += 100
        zt2.on_sample("NIFTY", t, ltp=25204.0, cum_volume=cum, bid=25204.0, ask=25204.5,
                      bid_qty=500_000, ask_qty=650)  # fmt: skip
        t += timedelta(seconds=1)
    assert zt2.zones("NIFTY", 25222.0)["floors"] == []


def test_option_oi_read(repo_root: Path) -> None:
    core = _core(repo_root)
    core.analytics = SimpleNamespace(prev_oi={})  # type: ignore[assignment]
    ins = core.master.get(opt_symbol(WEEKLY, 24900, Kind.PUT))
    st = SimpleNamespace(oi_now=0)
    put_writing = core._oi_read(ins, st, {"chg_pct": -40.0, "doi": 1_762_000})
    assert put_writing["label"].startswith("writing") and put_writing["index_view"] == "UP"
    cover = core._oi_read(core.master.get(opt_symbol(WEEKLY, 25100, Kind.CALL)), st,
                          {"chg_pct": 30.0, "doi": -500})  # fmt: skip
    assert cover["label"] == "short covering" and cover["index_view"] == "UP"
    core.analytics.prev_oi = {"NIFTY": {(WEEKLY.isoformat(), 24900.0, "PE"): 1000}}
    st2 = SimpleNamespace(oi_now=1500)
    r = core._oi_read(ins, st2, {"chg_pct": 5.0, "doi": -9})
    assert r["oi_chg"] == 500 and r["baseline"] == "previous session"
    assert r["label"].startswith("buying")
