"""Playbook strategies from the owner's notes (new/, 12 Nov 2026): tape, gates, spoofing and
each strategy's trigger, driven through a scripted market."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from engine.cockpit.condor import SpreadAccount
from engine.cockpit.gates import (
    BreakevenRatchet,
    breakeven,
    expected_impulse,
    heavyweights_aligned,
    impulse_ok,
    off_cycle,
)
from engine.cockpit.paper import Book
from engine.cockpit.plays_base import HighIvRouter, Market, SpreadPlay, SpreadTrade, spread_exit
from engine.cockpit.plays_long import (
    BreakthroughPlay,
    OiIgnitionPlay,
    PutPanicPlay,
    ZeroDtePlay,
    ZoneRiderPlay,
    divergence,
)
from engine.cockpit.plays_spread import (
    ExpiryPinPlay,
    IronFlyPlay,
    MarriedFuturePlay,
    McxMiniPlay,
    OiCreditPlay,
    SyntheticFuturePlay,
)
from engine.cockpit.tape import IndexTape, OptionTape
from engine.core.config import load_settings
from engine.core.rules import IST, RuleBook
from engine.data.reference.instrument_master import Kind
from engine.shadow.spoof import SpoofTracker
from tests.data.conftest import WEEKLY, build_master, opt_symbol
from tests.data.test_cockpit import _account

D = datetime(2026, 11, 11, 9, 15, tzinfo=IST)


def bk(bid: float, ask: float, qty: int = 2000) -> Book:
    return Book(
        tuple((round(bid - i * 0.05, 2), qty, 5) for i in range(5) if bid - i * 0.05 > 0),
        tuple((round(ask + i * 0.05, 2), qty, 5) for i in range(5)),
    )


def fut_state(ltp: float, cum: float, bid: float | None = None, ask: float | None = None) -> Any:
    b = ltp - 0.05 if bid is None else bid
    a = ltp + 0.05 if ask is None else ask
    return SimpleNamespace(ltp=ltp, cum_volume=cum, bids=((b, 650, 1),), asks=((a, 650, 1),),
                           oi_now=0, symbol="NSE:NIFTY26NOVFUT")


# ---------------------------------------------------------------- tape


def test_tape_bars_cvd_vwap_and_opening_range() -> None:
    t = IndexTape("NIFTY")
    cum = 0.0
    for i in range(40):
        now = D + timedelta(minutes=i, seconds=30)
        cum += 1000
        px = 25000 + i
        t.on_sample(now, spot=px, fut=fut_state(px + 20, cum, ask=px + 20))  # trades at the ask
    assert len(t.bars) == 40 and t.cvd() > 0
    hi, lo = t.opening_range(30) or (0, 0)
    assert lo == 25000 and hi == 25029
    assert t.vwap() == pytest.approx(25039.5, abs=1)
    assert t.atr() == pytest.approx(1.0, abs=0.01)
    assert t.bars_n(15)[0].close == 25014
    assert t.realized_vol(30) is not None


def test_tape_oi_change_and_straddle_stats() -> None:
    t = IndexTape("NIFTY")
    rows = lambda oi: [{"k": 25000.0, "ce": {"oi": oi}, "pe": {"oi": 500}}]  # noqa: E731
    t.on_chain(D, rows(1000))
    t.on_chain(D + timedelta(minutes=6), rows(950))
    assert t.oi_change_pct(25000.0, "CE", 6) == pytest.approx(-5.0)
    for i in range(8):
        t.on_sample(D + timedelta(minutes=i), spot=25000, straddle=200.0 - i, straddle_cum_vol=100.0 * (i + 1))
    vwap, sd, last = t.straddle_stats() or (0, 0, 0)
    assert last == 194.0 and vwap > last and sd > 0


def test_option_tape_vwap_and_ema() -> None:
    o = OptionTape()
    for i in range(12):
        o.on_sample(D.replace(hour=9, minute=20) + timedelta(minutes=i), 100.0 + i, 100.0 * (i + 1))
    assert o.open_920 == 100.0 and o.vwap() is not None and o.ema(9) is not None


# ---------------------------------------------------------------- gates


def test_breakeven_impulse_and_ratchet(repo_root: Path) -> None:
    book = RuleBook.load(repo_root / "config" / "rules")
    be, pts = breakeven(book, "upstox_plus", date(2026, 11, 11), 150.0, 65)
    assert 151.0 < be < 154.0 and pts == pytest.approx(be - 150.0)
    assert expected_impulse(10.0, 0.5, 0.001) == pytest.approx(10 * 3**0.5 * 0.5 + 0.5 * 0.001 * 300, rel=1e-6)
    assert impulse_ok(10.0, 0.5, 0.001, pts, 3.5)[0] is (expected_impulse(10.0, 0.5, 0.001) >= 3.5 * pts)
    assert impulse_ok(None, 0.5, 0.001, pts, 3.5) == (False, 0.0)
    r = BreakevenRatchet.start(150.0, 65, be, pts, D)
    assert r.stop == pytest.approx(150 - min(18.0, 1200 / 65), abs=0.01)
    assert r.on_bid(150 + 2 * pts + 0.05, D + timedelta(seconds=10)) is None
    assert r.state == "BREAKEVEN_LOCKED" and r.stop == be
    r2 = BreakevenRatchet.start(150.0, 65, be, pts, D)
    assert "scratch" in str(r2.on_bid(150.5, D + timedelta(seconds=181)))


def test_off_cycle_and_heavyweights() -> None:
    assert not off_cycle(D.replace(hour=9, minute=20, second=10))
    assert off_cycle(D.replace(hour=9, minute=21, second=10))
    six = {"HDFCBANK": 1.0, "RELIANCE": 0.5, "ICICIBANK": 0.2, "INFY": 0.1, "LT": -0.3, "TCS": -0.1}
    assert heavyweights_aligned(six, True) is True and heavyweights_aligned(six, False) is False
    assert heavyweights_aligned({}, True) is None


# ---------------------------------------------------------------- spoof tracker


def test_spoof_rug_pull_and_eaten_wall() -> None:
    sp = SpoofTracker()
    base_bids = [(88.75, 400), (88.70, 3_297_000), (88.65, 400), (88.60, 400), (88.55, 400)]
    asks = [(89.00, 420), (89.05, 400), (89.10, 400), (89.15, 400), (89.20, 400)]
    sp.on_depth(ts=0, ltp=88.80, cum_volume=1000, bids=base_bids, asks=asks, tick=0.05)
    pulled = [(88.75, 400), (88.65, 400), (88.60, 400), (88.55, 400), (88.50, 400)]
    out = sp.on_depth(ts=1, ltp=88.75, cum_volume=1010, bids=pulled, asks=asks, tick=0.05)
    assert out["events"] and out["events"][0]["pattern"] == "spoof rug-pull"
    assert out["events"][0]["direction"] == "SHORT"
    eat = SpoofTracker()
    a2 = [(150.0, 50_000), (150.05, 500), (150.10, 500), (150.15, 500), (150.2, 500)]
    b2 = [(149.95, 500), (149.9, 500), (149.85, 500), (149.8, 500), (149.75, 500)]
    eat.on_depth(ts=0, ltp=149.95, cum_volume=0.0, bids=b2, asks=a2, tick=0.05)
    eat.on_depth(ts=1, ltp=150.0, cum_volume=1.0, bids=b2, asks=a2, tick=0.05)
    gone = [(150.05, 500), (150.10, 500), (150.15, 500), (150.2, 500), (150.25, 500)]
    out2 = eat.on_depth(ts=2, ltp=150.0, cum_volume=48_000.0, bids=b2, asks=gone, tick=0.05)
    assert out2["events"] and out2["events"][0]["pattern"] == "eaten wall squeeze"
    assert out2["events"][0]["direction"] == "LONG"
    bait = SpoofTracker().on_depth(ts=0, ltp=88.8, cum_volume=0, bids=base_bids,
                                   asks=[(89.0, 4000)] + asks[1:], tick=0.05)
    assert bait["bait"] == "FAKE_BID_BAIT" and bait["book_multiplier"] == 0.25


# ---------------------------------------------------------------- scripted market


class FakeMarket(Market):
    def __init__(self, now: datetime, master: Any) -> None:
        self.now = now
        self._books = {}
        self.tapes: dict[str, IndexTape] = {}
        self.otapes: dict[str, OptionTape] = {}
        self.spots = {"NIFTY": 25000.0}
        self.futs: dict[str, Any] = {}
        self.rows: list[dict[str, Any]] = []
        self.tbl: dict[str, Any] = {}
        self.greeks: dict[str, dict[str, Any]] = {}
        self.ivp_val: float | None = 50.0
        self.zone_view: dict[str, Any] = {}
        self.exp_today = False
        self.pinned: dict[str, list[str]] = {}
        self.core = SimpleNamespace(
            master=master, label=lambda s: s, option_symbol=self._opt, runner=SimpleNamespace(
                spot_symbol={"NIFTY": "NSE:NIFTY50-INDEX"}, subscribed=[],
                scanner=SimpleNamespace(states={}), spots=lambda: self.spots),
        )

    def _opt(self, und: str, exp: Any, k: float, kind: str) -> str:
        return opt_symbol(WEEKLY, int(k), Kind.CALL if kind == "CE" else Kind.PUT)

    def underlyings(self) -> list[str]:
        return ["NIFTY"]

    def spot(self, und: str) -> float:
        return self.spots.get(und, 0.0)

    def fut(self, und: str) -> Any:
        return self.futs.get(und)

    def books(self) -> dict[str, Book]:
        return self._books

    def tape(self, und: str) -> IndexTape:
        return self.tapes.setdefault(und, IndexTape(und))

    def otape(self, sym: str) -> OptionTape:
        return self.otapes.setdefault(sym, OptionTape())

    def strip(self, und: str) -> dict[str, Any]:
        return {"atm_iv_pct": 15.0, "forward": self.spots[und], "iv_percentile": self.ivp_val}

    def chain_rows(self, und: str) -> list[dict[str, Any]]:
        return self.rows

    def tables(self, und: str) -> dict[str, Any]:
        return self.tbl

    def detail(self, sym: str) -> dict[str, Any]:
        return self.greeks.get(sym, {"delta": 0.55, "gamma": 0.003, "iv": 15.0})

    def ivp(self, und: str) -> float | None:
        return self.ivp_val

    def expiry_today(self, und: str) -> bool:
        return self.exp_today

    def lot(self, sym: str) -> int:
        return 65

    def zones(self, und: str) -> dict[str, Any]:
        return self.zone_view

    def day_returns(self) -> dict[str, float] | None:
        return None

    def context(self, und: str) -> dict[str, Any]:
        return {"underlying": und}

    def event_block(self) -> str | None:
        return None

    def breakeven(self, entry: float, qty: int) -> tuple[float, float]:
        return entry + 2.0, 2.0

    def pin(self, owner: str, syms: list[str]) -> None:
        self.pinned[owner] = syms


def chain(spot: float = 25000.0, oi_ce: int = 1000, oi_pe: int = 1000) -> list[dict[str, Any]]:
    rows = []
    for k in range(24300, 25751, 50):
        d = abs(k - spot)
        cd = max(0.02, min(0.98, 0.5 - (k - spot) / 1200))
        rows.append({
            "k": float(k),
            "ce": {"sym": opt_symbol(WEEKLY, k, Kind.CALL), "delta": round(cd, 3), "iv": 15.0,
                   "oi": oi_ce * (5 if k == 25100 else 1), "bid": max(0.5, 100 - d / 5), "ask": max(0.6, 100.5 - d / 5),
                   "ltp": max(0.5, 100 - d / 5)},
            "pe": {"sym": opt_symbol(WEEKLY, k, Kind.PUT), "delta": round(cd - 1, 3), "iv": 15.0,
                   "oi": oi_pe * (5 if k == 24900 else 1), "bid": max(0.5, 100 - d / 5), "ask": max(0.6, 100.5 - d / 5),
                   "ltp": max(0.5, 100 - d / 5)},
        })
    return rows


@pytest.fixture
def mkt() -> FakeMarket:
    m = FakeMarket(D.replace(hour=11), build_master(range(24300, 25800, 50)))
    m.rows = chain()
    for r in m.rows:
        for key in ("ce", "pe"):
            m._books[r[key]["sym"]] = bk(r[key]["bid"], r[key]["ask"])
    return m


def _spread(repo_root: Path, tmp_path: Path, name: str) -> SpreadAccount:
    settings = load_settings(root=repo_root, environ={})
    return SpreadAccount(name, book=RuleBook.load(repo_root / "config" / "rules"),
                         limits=settings.risk, broker_name="upstox_plus", session_open=D,
                         journal=tmp_path / f"paper_{name}.jsonl")


def feed_trend(m: FakeMarket, minutes: int, start_px: float, step: float, vol: float,
               at_ask: bool = True, start: datetime | None = None) -> datetime:
    t = m.tape("NIFTY")
    now = start or D
    px = start_px
    cum = t.last_cum or 0.0
    for i in range(minutes):
        cum += vol * (1 + 0.1 * (i % 3))  # a little variation, as real volume has
        px += step
        m.spots["NIFTY"] = px
        f = fut_state(px + 20, cum, ask=px + 20 if at_ask else None, bid=None if at_ask else px + 20)
        m.futs["NIFTY"] = f
        t.on_sample(now, spot=px - step, fut=None)  # the bar opens where the last one closed
        t.on_sample(now + timedelta(seconds=40), spot=px, fut=f)
        now += timedelta(minutes=1)
    return now


# ---------------------------------------------------------------- long plays


def test_put_panic_enters_on_breakdown_with_cvd_and_iv(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    p = PutPanicPlay("put_panic", "Put panic", "S1", _account(repo_root, tmp_path, "pp"))
    t = mkt.tape("NIFTY")
    now = feed_trend(mkt, 35, 25000, 0.5, 1000)  # quiet 30-min range
    for i in range(5):  # 3-minute put IV rise
        t.iv.setdefault("pe_atm", []).append((now - timedelta(minutes=5 - i), 14.0 + i * 0.3))
    # A selling burst through the range low.
    now = feed_trend(mkt, 3, mkt.spots["NIFTY"], -15, 30_000, at_ask=False, start=now)
    mkt.now = now.replace(second=50)
    mkt.greeks = {s: {"delta": -0.57, "gamma": 0.004} for s in mkt._books}
    mkt.tbl = {WEEKLY.isoformat(): {"rows": mkt.rows}}  # 6 days to expiry: >= 2 needed
    assert p.look(mkt.now, mkt) == []  # a 15-pt drop does not clear 3.5x charges breakeven
    assert "charges gate" in p.decision
    p2 = PutPanicPlay("put_panic", "Put panic", "S1", _account(repo_root, tmp_path / "b", "pp"))
    mkt.tapes.clear()
    t = mkt.tape("NIFTY")
    now = feed_trend(mkt, 35, 25000, 0.5, 1000)
    for i in range(5):
        t.iv.setdefault("pe_atm", []).append((now - timedelta(minutes=5 - i), 14.0 + i * 0.3))
    # One big selling minute through the range low; the strategy checks as soon as it closes.
    now = feed_trend(mkt, 2, mkt.spots["NIFTY"], -100, 30_000, at_ask=False, start=now)
    mkt.now = now.replace(second=50)
    out = p2.look(mkt.now, mkt)
    p = p2
    assert out and out[0].startswith("bought"), (out, p.decision)
    mg = next(iter(p.managed.values()))
    assert mg.ratchet is not None and mg.time_stop == (12, 10) and not mg.bullish


def test_breakthrough_scores_and_two_lots_above_80(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    p = BreakthroughPlay("bt", "Breakthrough", "rules.md", _account(repo_root, tmp_path, "bt"))
    feed_trend(mkt, 25, 25000, 3, 1000)
    sym = mkt.by_delta("NIFTY", "CE", 0.55)
    assert sym is not None
    s = p.score(mkt, "NIFTY", sym[0], True)
    assert s is not None and 0 <= s["score"] <= 100 and set(s["sub"]) == {"book", "volume", "oi", "gamma", "iv"}
    assert "sl_25" in s and "passes_charges_gate" in s


def test_zero_dte_needs_expiry_day_and_a_pinned_midday(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    p = ZeroDtePlay("z", "0DTE", "S3", _account(repo_root, tmp_path, "z"))
    mkt.now = D.replace(hour=13, minute=40)
    assert p.look(mkt.now, mkt) == []  # not an expiry day: nothing
    mkt.exp_today = True
    p.bands["NIFTY"] = {"inside": False, "band": (24900, 25100), "done": False}
    p.look(mkt.now, mkt)
    assert "left the straddle band" in p.decision


def test_oi_ignition_arms_the_retest(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    p = OiIgnitionPlay("oi", "OI ignition", "S2", _account(repo_root, tmp_path, "oi"))
    t = mkt.tape("NIFTY")
    mkt.rows = chain(oi_ce=1000)
    t.on_chain(D.replace(hour=10, minute=40), mkt.rows)
    now = feed_trend(mkt, 23, 25060, 1, 1000, start=D.replace(hour=10, minute=36))
    t.on_chain(now - timedelta(minutes=1), chain(oi_ce=900))  # call OI at 25100 falls 10%
    mkt.rows = chain(oi_ce=900)
    now = feed_trend(mkt, 3, mkt.spots["NIFTY"], 12, 5000, start=now)  # through 25100 on volume
    mkt.now = now
    p.look(now, mkt)
    assert "NIFTY" in p.armed, p.decision  # waits 1-3 min for the second-mouse retest


def test_zone_rider_and_divergence(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    p = ZoneRiderPlay("zr", "Zone rider", "ride_wave", _account(repo_root, tmp_path, "zr"))
    now = feed_trend(mkt, 10, 25000, 1, 1000, start=D.replace(hour=10))
    mkt.futs["NIFTY"] = fut_state(25230.0, 99_999)
    mkt.zone_view = {"floors": [{"low": 25200.0, "high": 25210.0, "ratio": 15.0, "stop_below": 25195.0,
                                 "trapped_volume": 10_000, "minutes": 12}], "ceilings": []}
    mkt.tbl = {WEEKLY.isoformat(): {"rows": mkt.rows}}
    mkt.now = now
    out = p.look(now, mkt)
    assert out and out[0].startswith("bought"), (out, p.decision)
    assert next(iter(p.managed.values())).spot_stop is not None
    assert divergence(IndexTape("X")) is None


# ---------------------------------------------------------------- spread plays


def test_oi_credit_bull_put_when_call_writers_cover(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    p = OiCreditPlay("oc", "OI credit", "B", _spread(repo_root, tmp_path, "oc"))
    t = mkt.tape("NIFTY")
    t.on_chain(D.replace(hour=10, minute=50), chain(oi_ce=1000))
    t.on_chain(D.replace(hour=11, minute=0), chain(oi_ce=900))
    now = feed_trend(mkt, 25, 25080, 0, 1000, start=D.replace(hour=10, minute=35))
    now = feed_trend(mkt, 2, 25085, 0, 60_000, start=now)  # a buying burst: CVD z high
    mkt.now = now
    p.look(now, mkt)
    assert p.pending is not None and p.pending["structure"] == "BULL_PUT", p.decision
    assert [lg.side for lg in p.pending["legs"]] == ["BUY", "SELL"]


def test_iron_fly_waits_for_flush_and_rv_iv(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    p = IronFlyPlay("if", "Iron fly", "A", _spread(repo_root, tmp_path, "if"), post_flush=False,
                    days={"NIFTY": (0, 1, 2, 3, 4)})
    t = mkt.tape("NIFTY")
    now = D
    for i in range(40):
        t.on_sample(now, spot=25000 + (i % 2) * 0.5, fut=fut_state(25020, 1000.0 * (i + 1)),
                    straddle=200.0 - (i * 0.5 if i > 30 else 0), straddle_cum_vol=1000.0 * (i + 1))
        now += timedelta(minutes=1)
    mkt.now = now
    p.look(now, mkt)
    assert p.pending is not None and p.pending["structure"] == "IRON_FLY", p.decision
    assert sorted(lg.side for lg in p.pending["legs"]) == ["BUY", "BUY", "SELL", "SELL"]


def test_expiry_pin_needs_writing_unwinding_and_a_spike(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    p = ExpiryPinPlay("ep", "Pin", "fix2", _spread(repo_root, tmp_path, "ep"))
    mkt.exp_today = True
    t = mkt.tape("NIFTY")
    t.on_chain(D.replace(hour=13, minute=35), chain(oi_ce=1000, oi_pe=1000))
    later = chain(oi_ce=700, oi_pe=1500)
    t.on_chain(D.replace(hour=14, minute=10), later)
    mkt.rows = later
    mkt.now = D.replace(hour=13, minute=45)
    p.look(mkt.now, mkt)  # records the 13:45 prices
    sym = opt_symbol(WEEKLY, 24950, Kind.PUT)
    mkt._books[sym] = bk(200.0, 200.5)  # the put spikes
    mkt.now = D.replace(hour=14, minute=10)
    p.look(mkt.now, mkt)
    assert p.pending is not None, p.decision
    assert [lg.side for lg in p.pending["legs"]] == ["BUY", "SELL"]


def test_synthetic_future_uses_the_stop_for_planned_loss(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    p = SyntheticFuturePlay("syn", "Synthetic", "F1", _spread(repo_root, tmp_path, "syn"))
    p.on_setup("NIFTY", True, "S1")
    p.look(mkt.now, mkt)
    assert p.pending is not None
    legs = p.pending["legs"]
    assert [lg.side for lg in legs] == ["BUY", "BUY", "SELL"] and legs[0].kind == "PE"
    assert p.pending["max_loss"] == pytest.approx(25000 * 0.005 * 65)


def test_married_future_and_mcx_dormant(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    mf = MarriedFuturePlay("mf", "Married", "F2", _spread(repo_root, tmp_path, "mf"))
    assert mf.look(mkt.now, mkt) is None  # no history yet: nothing
    mc = McxMiniPlay("mcx", "MCX", "F3", _spread(repo_root, tmp_path, "mcx"))
    mc.look(mkt.now, mkt)
    assert mc.decision.startswith("dormant")


def test_spread_exit_rules_and_high_iv_router(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    pos = SimpleNamespace(credit_pts=50.0, width_pts=200.0, legs=[], opened_at=D, expiry=WEEKLY)
    tr = SpreadTrade("x", "NIFTY", D, target_pct=50, stop_mult=2.0, square_off="15:20")
    assert spread_exit(pos, tr, {}, D.replace(hour=15, minute=21), 25000) == "square-off 15:20"
    hiv = SpreadPlay("hiv", "HIV", "rule 4", _spread(repo_root, tmp_path, "hiv"))
    r = HighIvRouter(hiv)
    text = r.route(mkt, "NIFTY", True, "test")
    assert "debit spread" in text and hiv.pending is not None
    assert hiv.pending["structure"] == "BULL_CALL"


# ---------------------------------------------------------------- married future, both ways


def test_short_future_needs_a_long_call_first() -> None:
    from engine.execution.leg_sequencer import (
        NakedShortError,
        SpreadLegIntent,
        cap_short_legs_to_filled_hedges,
        sequence_entry_legs,
    )

    fut = SpreadLegIntent("NIFTYFUT", "SELL", 65, 1.0, False, is_future=True)
    call = SpreadLegIntent("C", "BUY", 65, 1.0, True, option_type="CE")
    put = SpreadLegIntent("P", "BUY", 65, 1.0, True, option_type="PE")
    buys, shorts = sequence_entry_legs([fut, call])
    assert buys == [call] and shorts == [fut]
    with pytest.raises(NakedShortError):
        sequence_entry_legs([fut])
    with pytest.raises(NakedShortError):
        sequence_entry_legs([fut, put])  # a put does not cap a short future's loss
    assert cap_short_legs_to_filled_hedges([fut], [call], lot_size=65) == [fut]


def test_married_short_future_round_trip(repo_root: Path, tmp_path: Path) -> None:
    from engine.cockpit.condor import Leg

    acct = _spread(repo_root, tmp_path, "married")
    call, fut = opt_symbol(WEEKLY, 25000, Kind.CALL), "NSE:NIFTY26NOVFUT"
    legs = [Leg(call, "CE", 25000.0, "BUY", 65), Leg(fut, "FUT", 0.0, "SELL", 65)]
    books = {call: bk(100.0, 100.5), fut: bk(25020.0, 25020.5, qty=20_000)}
    ok, text = acct.open_spread(structure="MARRIED_SHORT_FUTURE", underlying="NIFTY", expiry=WEEKLY,
                                legs=legs, width_pts=0.0, books=books, now=D.replace(hour=11),
                                spot=25000.0, reason="test", max_loss_inr=8_000.0)
    assert ok, text
    sides = [o.side for o in acct.broker.order_history if o.filled_qty]
    assert sides == ["BUY", "SELL"]  # call first, then the short future
    later = {call: bk(40.0, 40.5), fut: bk(24900.0, 24900.5, qty=20_000)}
    ok, text = acct.close(acct.positions[0], later, D.replace(hour=14), "test")
    assert ok, text
    t = acct.closed[-1]
    # Future: sold ~25020, bought back ~24900.5 = +119.5; call: 100.5 -> 40 = -60.5.
    assert t.gross_inr == pytest.approx((119.5 - 60.5) * 65, abs=65)
    assert 0 < t.net_inr < t.gross_inr


def test_spread_entries_blocked_in_an_event_window(repo_root: Path, tmp_path: Path) -> None:
    """Multi-leg strategies obey the macro-event window like the long-option accounts do."""
    from engine.cockpit.condor import Leg

    acct = _spread(repo_root, tmp_path, "oi_credit")
    acct.entry_block = lambda now: "event window: RBI_MPC"
    call, fut = opt_symbol(WEEKLY, 25000, Kind.CALL), "NSE:NIFTY26NOVFUT"
    legs = [Leg(call, "CE", 25000.0, "BUY", 65), Leg(fut, "FUT", 0.0, "SELL", 65)]
    books = {call: bk(100.0, 100.5), fut: bk(25020.0, 25020.5, qty=20_000)}
    ok, text = acct.open_spread(structure="MARRIED_SHORT_FUTURE", underlying="NIFTY", expiry=WEEKLY,
                                legs=legs, width_pts=0.0, books=books, now=D.replace(hour=11),
                                spot=25000.0, reason="test", max_loss_inr=8_000.0)
    assert not ok and "event window" in text
    assert not acct.positions and not acct.broker.order_history
    acct.entry_block = lambda now: None
    ok, text = acct.open_spread(structure="MARRIED_SHORT_FUTURE", underlying="NIFTY", expiry=WEEKLY,
                                legs=legs, width_pts=0.0, books=books, now=D.replace(hour=11),
                                spot=25000.0, reason="test", max_loss_inr=8_000.0)
    assert ok, text


def test_married_future_triggers_both_ways(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    def run(spot: float, pdh: float, pdl: float, basis_up: bool) -> MarriedFuturePlay:
        p = MarriedFuturePlay("mf", "Married", "F2", _spread(repo_root, tmp_path / str(spot), "mf"))
        st = SimpleNamespace(pdh=pdh, pdl=pdl)
        mkt.core.runner.scanner.states = {"NSE:NIFTY50-INDEX": st}
        mkt.spots["NIFTY"] = spot
        f = fut_state(spot + 20, 0)
        mkt.futs["NIFTY"] = f
        now = D.replace(hour=10, minute=30)
        p.fut_oi["NIFTY"] = [(now - timedelta(minutes=16 - i), 1_000_000 + i * 2000) for i in range(16)]
        p.basis["NIFTY"] = [5.0 + (i * 0.1 if basis_up else -i * 0.1) for i in range(30)]
        mkt.now = now
        p.look(now, mkt)
        return p

    up = run(25100.0, pdh=25050.0, pdl=24800.0, basis_up=True)
    assert up.pending is not None and up.pending["structure"] == "MARRIED_FUTURE"
    assert [(lg.kind, lg.side) for lg in up.pending["legs"]] == [("PE", "BUY"), ("FUT", "BUY")]
    down = run(24700.0, pdh=25050.0, pdl=24800.0, basis_up=False)
    assert down.pending is not None and down.pending["structure"] == "MARRIED_SHORT_FUTURE"
    assert [(lg.kind, lg.side) for lg in down.pending["legs"]] == [("CE", "BUY"), ("FUT", "SELL")]
    assert down.pending["trade"].spot_stop == pytest.approx(24800.0 * 1.005)


# ---------------------------------------------------------------- status lines


def test_status_says_why_a_strategy_is_not_trading(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    """Each strategy's line on the page names the condition it is waiting for, with numbers."""
    from engine.cockpit.plays_base import HighIvPlay

    friday = datetime(2026, 11, 13, 10, 30, tzinfo=IST)
    mkt.now = friday
    fly = IronFlyPlay("iron_fly", "Iron fly", "A", _spread(repo_root, tmp_path, "fly"))
    fly.step(friday, mkt)
    assert "not a trading day" in fly.decision and "Fri" in fly.decision
    assert "NIFTY Mon/Tue" in fly.decision

    married = MarriedFuturePlay("married", "Married", "F2", _spread(repo_root, tmp_path, "mf"))
    married.step(friday, mkt)
    assert "waiting for the front future" in married.decision

    early = friday.replace(hour=9, minute=20)
    mkt.now = early
    pp = PutPanicPlay("put_panic", "Put panic", "S1", _account(repo_root, tmp_path, "pp"))
    pp.step(early, mkt)
    assert pp.decision == "entries 09:45–14:00 (opens later)"

    pp.enabled = False
    pp.step(early, mkt)
    assert pp.decision.startswith("off")
    mcx = McxMiniPlay("mcx", "MCX", "F3", _spread(repo_root, tmp_path, "mcx"), enabled=False)
    mcx.step(early, mkt)
    assert "no MCX feed" in mcx.decision

    hiv = HighIvPlay("hiv", "High IV", "rule 4", _spread(repo_root, tmp_path, "hiv"))
    hiv.step(early, mkt)
    assert "80th percentile" in hiv.decision and "NIFTY 50" in hiv.decision


def test_status_reports_when_legs_do_not_fit_in_the_feed(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    from engine.cockpit.condor import Leg

    class FullFeed(FakeMarket):
        def pin(self, owner: str, syms: list[str]) -> list[str]:  # type: ignore[override]
            return syms[:1]

    m = FullFeed(mkt.now, mkt.core.master)
    play = SpreadPlay("oi_credit", "OI credit", "B", _spread(repo_root, tmp_path, "oc"))
    put_a, put_b = opt_symbol(WEEKLY, 24800, Kind.PUT), opt_symbol(WEEKLY, 25000, Kind.PUT)
    legs = [Leg(put_a, "PE", 24800.0, "BUY", 65), Leg(put_b, "PE", 25000.0, "SELL", 65)]
    play.plan(m, "NIFTY", "BULL_PUT", legs, 200.0, "test plan", SpreadTrade("", "NIFTY", m.now))
    play.step(m.now, m)
    assert "only 1 of 2 legs fit in the live feed" in play.decision
    assert "placing: test plan" in play.decision


def test_a_tape_that_started_late_has_no_opening_range() -> None:
    """After a restart at 09:39 the tape's first bars are not 09:15-09:45: no range, not a
    narrow fake one (the warm start rebuilds the real one from the recording)."""
    t = IndexTape("NIFTY")
    for i in range(40):
        t.on_sample(D.replace(hour=9, minute=39) + timedelta(minutes=i, seconds=30), spot=25000 + i)
    assert t.opening_range(30) is None
    full = IndexTape("NIFTY")
    for i in range(40):
        full.on_sample(D + timedelta(minutes=i, seconds=30), spot=25000 + i)
    assert full.opening_range(30) == (25029, 25000)


def test_a_refused_entry_backs_off_instead_of_retrying_every_step(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    """A persistent trigger hitting a refusing gate (cool-off, event window) once wrote ~40
    journal lines a second. Now: one attempt, a 60-s pause per index, one journal line."""
    acct = _account(repo_root, tmp_path, "pp")
    acct.entry_block = lambda now: "event window: RBI_MPC"
    p = PutPanicPlay("put_panic", "Put panic", "S1", acct)
    sym = opt_symbol(WEEKLY, 25000, Kind.PUT)
    first = p.enter(mkt, "NIFTY", sym, lots=1, reason="test", bullish=False)
    assert "event window" in first
    assert p.enter(mkt, "NIFTY", sym, lots=1, reason="test", bullish=False) == ""
    for _ in range(5):  # the account itself also journals a repeated refusal only once a minute
        acct.open_long_option(sym, mkt.book(sym), now=mkt.now, lots=1, stop_pct=20, reason="t")
    lines = [ln for ln in (tmp_path / "paper_pp.jsonl").read_text(encoding="utf-8").splitlines()
             if '"refused"' in ln]
    assert len(lines) == 1
    mkt.now += timedelta(seconds=61)
    assert "event window" in p.enter(mkt, "NIFTY", sym, lots=1, reason="test", bullish=False)


# ---------------------------------------------------------------- charges-breakeven protocol


def test_protocol_breakeven_matches_the_owner_formula_on_a_20_rupee_plan(repo_root: Path) -> None:
    """The protocol's worked example: Rs 150 option, 1 lot of 65, Rs 20 + 20 brokerage + GST,
    1-point slippage -> true breakeven about Rs 152.10 (2.10 pts). Our config-driven costs on
    a Rs 20 plan land within a few paise; on Upstox Plus (Rs 30) the hurdle is higher."""
    book = RuleBook.load(repo_root / "config" / "rules")
    tax_be = (150 * (1 + 0.0004504) + 47.20 / 65) / (1 - 0.0019204)
    be20, pts20 = breakeven(book, "upstox", date(2026, 11, 13), 150.0, 65)
    assert be20 == pytest.approx(tax_be + 1.0, abs=0.06)
    be30, pts30 = breakeven(book, "upstox_plus", date(2026, 11, 13), 150.0, 65)
    assert pts30 > pts20


def test_ratchet_locks_at_2x_and_trails_only_beyond_3x(repo_root: Path) -> None:
    book = RuleBook.load(repo_root / "config" / "rules")
    be, pts = breakeven(book, "upstox", date(2026, 11, 11), 150.0, 65)
    r = BreakevenRatchet.start(150.0, 65, be, pts, D)
    r.on_bid(150 + 2.6 * pts, D + timedelta(seconds=20))  # past 2x: locked at breakeven
    assert r.state == "BREAKEVEN_LOCKED" and r.stop == be
    r.on_bid(150 + 2.9 * pts, D + timedelta(seconds=30))  # under 3x: no trail yet
    assert r.stop == be
    r.on_bid(150 + 4 * pts, D + timedelta(seconds=40))  # beyond 3x: trail 1.5x behind
    assert r.stop == pytest.approx(150 + 2.5 * pts, abs=0.02)


def test_scalp_entry_books_the_protocol_stop_and_needs_the_delta_band(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    acct = _account(repo_root, tmp_path, "bt")
    p = BreakthroughPlay("breakthrough", "Breakthrough", "rules.md", acct)
    sym = opt_symbol(WEEKLY, 25000, Kind.CALL)
    ask = mkt.book(sym).asks[0][0]
    p.enter(mkt, "NIFTY", sym, lots=1, reason="test", bullish=True, ratchet=True)
    pos = acct.positions[sym]
    # -12% of premium or Rs 1,200 on the lot, whichever is tighter, from the first moment.
    assert pos.stop_price == pytest.approx(ask - min(0.12 * ask, 1200 / 65), abs=0.06)
    mkt.greeks[sym] = {"delta": 0.45, "gamma": 0.003, "iv": 15.0}
    ok, why, _, _ = mkt.scalp_gate(sym, 65, 3.5, "NIFTY")
    assert not ok and "outside 0.55-0.65" in why


def test_restart_puts_an_open_scalp_back_under_the_180s_scratch(repo_root: Path, tmp_path: Path, mkt: FakeMarket) -> None:
    """A scalp found open after an engine restart (no manager) is adopted: past 180 s and still
    under its charges breakeven it is scratched, as the protocol requires."""
    acct = _account(repo_root, tmp_path, "bt2")
    sym = opt_symbol(WEEKLY, 25000, Kind.CALL)
    ok, _ = acct.open_long_option(sym, mkt.book(sym), now=mkt.now, lots=1, stop_pct=20, reason="t")
    assert ok
    fresh = BreakthroughPlay("breakthrough", "Breakthrough", "rules.md", acct)  # the restarted play
    later = mkt.now + timedelta(minutes=5)
    mkt.now = later
    out = fresh.step(later, mkt)
    assert sym not in acct.positions, out
    closed = acct.closed[-1]
    assert "scratch" in closed.reason, closed.reason


def test_spread_margin_is_quoted_in_the_background_and_journaled(repo_root: Path, tmp_path: Path) -> None:
    from engine.cockpit.condor import Leg
    from engine.cockpit.margin import MarginQuoter, fetch_margin

    seen: list[list[tuple[str, int, str]]] = []

    def fake(legs: Any) -> dict[str, Any]:
        seen.append(list(legs))
        return {"final": 42985.0, "required": 54922.0, "source": "test"}

    acct = _spread(repo_root, tmp_path, "oc")
    acct.attach_margin(MarginQuoter(fetch=fake))
    a, b = opt_symbol(WEEKLY, 24800, Kind.PUT), opt_symbol(WEEKLY, 25000, Kind.PUT)
    legs = [Leg(a, "PE", 24800.0, "BUY", 65), Leg(b, "PE", 25000.0, "SELL", 65)]
    books = {a: bk(40.0, 40.5), b: bk(100.0, 100.5)}
    ok, text = acct.open_spread(structure="BULL_PUT", underlying="NIFTY", expiry=WEEKLY, legs=legs,
                                width_pts=200.0, books=books, now=D.replace(hour=11), spot=25000.0,
                                reason="test")
    assert ok, text
    import time
    for _ in range(50):
        if acct.positions[0].margin.get("final"):
            break
        time.sleep(0.02)
    assert acct.positions[0].margin["final"] == 42985.0
    assert seen == [[(a, 65, "BUY"), (b, 65, "SELL")]]
    again = _spread(repo_root, tmp_path, "oc")  # restart: the journaled quote comes back
    again.load_journal(datetime.now(IST).date())  # the quote's line carries the real clock
    assert again.positions[0].margin["final"] == 42985.0
    # Simulated keys never reach the network.
    assert "error" in fetch_margin([("NSE:NIFTY26111725000PE", 65, "SELL")])
