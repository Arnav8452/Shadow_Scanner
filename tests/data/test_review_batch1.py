"""13 Nov 2026 review, batch 1 (owner approved): stop override, paper fill latency and
leg-by-leg multi-leg fills, broker-margin gate, charges check for credit spreads, and trade
side against the quote before the trade."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from engine.cockpit.condor import Leg, SpreadAccount
from engine.cockpit.tape import IndexTape, trade_side
from engine.core.config import load_settings
from engine.core.rules import RuleBook
from engine.data.reference.instrument_master import Kind
from tests.data.conftest import WEEKLY, opt_symbol
from tests.data.test_cockpit import _account
from tests.data.test_plays import D, bk, fut_state

T = D.replace(hour=11)


def _spread(repo_root: Path, tmp_path: Path, name: str = "sp") -> SpreadAccount:
    settings = load_settings(root=repo_root, environ={})
    return SpreadAccount(name, book=RuleBook.load(repo_root / "config" / "rules"),
                         limits=settings.risk, broker_name="upstox_plus", session_open=D,
                         journal=tmp_path / f"paper_{name}.jsonl")


# ---------------------------------------------------------------- 1. stop override


def test_a_model_price_hold_gives_way_after_a_few_seconds(repo_root: Path, tmp_path: Path) -> None:
    acct = _account(repo_root, tmp_path)
    acct.stop_confirm = lambda sym, bid, stop: False  # the model always disagrees
    sym = opt_symbol(WEEKLY, 25000, Kind.CALL)
    ok, _ = acct.open_long_option(sym, bk(99.5, 100.0), now=T, lots=1, stop_pct=10, reason="t")
    assert ok
    crash = bk(89.0, 89.5)  # under the 90.00 stop, but not past 1.5x the distance (85.00)
    assert acct.check_exits({sym: crash}, T + timedelta(seconds=1)) == []  # held: air pocket?
    assert acct.check_exits({sym: crash}, T + timedelta(seconds=3)) == []
    out = acct.check_exits({sym: crash}, T + timedelta(seconds=7))
    assert sym not in acct.positions and "override" in acct.closed[-1].reason, out


def test_a_deep_drop_overrides_the_model_at_once(repo_root: Path, tmp_path: Path) -> None:
    acct = _account(repo_root, tmp_path)
    acct.stop_confirm = lambda sym, bid, stop: False
    sym = opt_symbol(WEEKLY, 25000, Kind.CALL)
    acct.open_long_option(sym, bk(99.5, 100.0), now=T, lots=1, stop_pct=10, reason="t")
    acct.check_exits({sym: bk(84.0, 84.5)}, T + timedelta(seconds=1))  # past 1.5x the distance
    assert sym not in acct.positions and "1.5x" in acct.closed[-1].reason


# ---------------------------------------------------------------- 2. fill latency


def test_a_paper_order_fills_against_the_next_book(repo_root: Path, tmp_path: Path) -> None:
    acct = _account(repo_root, tmp_path)
    acct.fill_latency_s = 0.2
    sym = opt_symbol(WEEKLY, 25000, Kind.CALL)
    ok, text = acct.open_long_option(sym, bk(99.5, 100.0), now=T, lots=1, stop_pct=20, reason="t")
    assert ok and "order sent" in text and sym not in acct.positions
    assert acct.check_exits({sym: bk(101.5, 102.0)}, T + timedelta(milliseconds=100)) == []
    out = acct.check_exits({sym: bk(101.5, 102.0)}, T + timedelta(milliseconds=250))
    pos = acct.positions[sym]
    assert pos.avg_price == pytest.approx(102.0) and "asked 100.00" in out[0]
    ok, text = acct.open_long_option(sym, bk(99.5, 100.0), now=T, lots=1, stop_pct=20, reason="t")
    assert not ok  # one position per contract


def test_legs_fill_one_at_a_time_wings_first(repo_root: Path, tmp_path: Path) -> None:
    acct = _spread(repo_root, tmp_path)
    acct.fill_latency_s, acct.leg_gap_s = 0.2, 0.3
    wing, short = opt_symbol(WEEKLY, 24800, Kind.PUT), opt_symbol(WEEKLY, 25000, Kind.PUT)
    legs = [Leg(short, "PE", 25000.0, "SELL", 65), Leg(wing, "PE", 24800.0, "BUY", 65)]
    books = {wing: bk(40.0, 40.5), short: bk(100.0, 100.5)}
    done: list[tuple[bool, str]] = []
    ok, text = acct.open_spread(structure="BULL_PUT", underlying="NIFTY", expiry=WEEKLY, legs=legs,
                                width_pts=200.0, books=books, now=T, spot=25000.0, reason="t",
                                on_done=lambda o, t, p: done.append((o, t)))
    assert ok and "order sent" in text and not acct.positions and acct.executions
    acct.process_executions(books, T + timedelta(milliseconds=210))  # the wing
    assert [lg.side for lg in acct.executions[0]["filled"]] == ["BUY"]
    later = {wing: bk(40.0, 40.5), short: bk(95.0, 95.5)}  # the short moved while we waited
    acct.process_executions(later, T + timedelta(milliseconds=400))  # too soon for leg 2
    assert len(acct.executions[0]["filled"]) == 1
    acct.process_executions(later, T + timedelta(milliseconds=520))
    assert not acct.executions and acct.positions and done and done[0][0]
    assert acct.positions[0].credit_pts == pytest.approx(95.0 - 40.5)  # paid the delay


def test_a_short_that_cannot_fill_after_the_delay_unwinds_the_wing(repo_root: Path, tmp_path: Path) -> None:
    acct = _spread(repo_root, tmp_path)
    acct.fill_latency_s, acct.leg_gap_s = 0.2, 0.3
    wing, short = opt_symbol(WEEKLY, 24800, Kind.PUT), opt_symbol(WEEKLY, 25000, Kind.PUT)
    legs = [Leg(wing, "PE", 24800.0, "BUY", 65), Leg(short, "PE", 25000.0, "SELL", 65)]
    books = {wing: bk(40.0, 40.5), short: bk(100.0, 100.5)}
    acct.open_spread(structure="BULL_PUT", underlying="NIFTY", expiry=WEEKLY, legs=legs,
                     width_pts=200.0, books=books, now=T, spot=25000.0, reason="t")
    acct.process_executions(books, T + timedelta(milliseconds=210))
    out = acct.process_executions({wing: bk(40.0, 40.5)}, T + timedelta(milliseconds=520))
    assert not acct.positions and not acct.executions and "wings unwound" in out[0]


# ---------------------------------------------------------------- 3. margin and charges


def test_the_gate_uses_broker_margin_not_the_worst_case(repo_root: Path, tmp_path: Path) -> None:
    """A bull put's worst case is ~Rs 6.5k, but the broker blocks ~Rs 43k (table Rs 49.5k).
    Two of them stay under the 50% headroom (Rs 1L); a third does not."""
    acct = _spread(repo_root, tmp_path)
    assert acct.table_margin("BULL_PUT", 1, T.date()) == 49500
    assert acct.table_margin("IRON_CONDOR", 1, T.date()) == 85000
    ok, why = acct.gate(6500, T, margin_inr=acct.table_margin("BULL_PUT", 1, T.date()))
    assert ok, why
    acct.executions = [{"max_loss": 6500, "margin": 49500, "legs": []}] * 2  # two in flight
    ok, why = acct.gate(6500, T, margin_inr=49500)
    assert not ok and "margin_headroom" in why


def test_a_credit_spread_whose_target_does_not_cover_charges_is_refused(repo_root: Path, tmp_path: Path) -> None:
    acct = _spread(repo_root, tmp_path)
    acct.charges_mult = 3.0
    wing, short = opt_symbol(WEEKLY, 24800, Kind.PUT), opt_symbol(WEEKLY, 24850, Kind.PUT)
    legs = [Leg(wing, "PE", 24800.0, "BUY", 65), Leg(short, "PE", 24850.0, "SELL", 65)]
    thin = {wing: bk(20.0, 20.5), short: bk(23.0, 23.5)}  # ~2.5-pt credit: 50% = Rs 81
    ok, text = acct.open_spread(structure="BULL_PUT", underlying="NIFTY", expiry=WEEKLY, legs=legs,
                                width_pts=50.0, books=thin, now=T, spot=25000.0, reason="t",
                                take_profit_pct=50)
    assert not ok and "round-trip charges" in text
    rich = {wing: bk(20.0, 20.5), short: bk(45.0, 45.5)}  # ~24.5-pt credit: 50% = Rs 796
    ok, text = acct.open_spread(structure="BULL_PUT", underlying="NIFTY", expiry=WEEKLY, legs=legs,
                                width_pts=50.0, books=rich, now=T, spot=25000.0, reason="t",
                                take_profit_pct=50)
    assert ok, text


# ---------------------------------------------------------------- 5. trade side


def test_trade_side_uses_the_quote_before_the_trade() -> None:
    assert trade_side(100, 101.0, 100.0, 101.0) == (100, 0.0)  # at the prior ask: buy
    assert trade_side(100, 100.0, 100.0, 101.0) == (0.0, 100)  # at the prior bid: sell
    assert trade_side(100, 100.5, 100.0, 101.0, prev_px=100.0) == (100, 0.0)  # mid, uptick
    assert trade_side(100, 100.5, 100.0, 101.0, prev_px=100.5) == (50, 50)  # mid, no tick


def test_a_sweep_that_lifts_the_touch_counts_as_buying() -> None:
    """Price swept from 25,000 through the offers; by the next sample the new touch is
    25,005/25,006 and the last print 25,005 sits on the NEW bid. Judged against the new quote
    that is selling; against the quote that was hit (ask 25,001) it is buying."""
    t = IndexTape("NIFTY")
    t.on_sample(T, spot=25000, fut=fut_state(25000.5, 1000, bid=25000, ask=25001))
    t.on_sample(T + timedelta(seconds=0.2), spot=25004,
                fut=fut_state(25005, 51000, bid=25005, ask=25006))
    bar = t.bars[-1]
    assert bar.buy == 50000 and bar.sell == 0


# ---------------------------------------------------------------- owner decisions, 13 Nov


def test_each_strategy_may_use_its_own_capital_in_margin(repo_root: Path, tmp_path: Path) -> None:
    """Paper: no strategy is blocked by another's margin (separate accounts), and one
    strategy's open positions may use up to 100% of capital (Rs 2L) in broker margin."""
    from dataclasses import replace

    acct = _spread(repo_root, tmp_path)
    acct.limits = replace(acct.limits, margin_headroom_max_used_pct=100.0)
    acct.executions = [{"max_loss": 0.0, "margin": 49500, "legs": []}] * 2  # 99k in use
    assert acct.gate(0.0, T, margin_inr=49500)[0]  # 148.5k <= 2L
    acct.executions = [{"max_loss": 0.0, "margin": 49500, "legs": []}] * 3  # 148.5k in use
    ok, why = acct.gate(0.0, T, margin_inr=85000)  # a condor more would need 233.5k
    assert not ok and "margin_headroom" in why


def test_short_legs_expiring_today_close_at_15_00(repo_root: Path, tmp_path: Path) -> None:
    from engine.cockpit.plays_base import SpreadPlay
    from tests.data.test_plays import FakeMarket, build_master

    acct = _spread(repo_root, tmp_path, "pin")
    wing, short = opt_symbol(WEEKLY, 24800, Kind.PUT), opt_symbol(WEEKLY, 25000, Kind.PUT)
    legs = [Leg(wing, "PE", 24800.0, "BUY", 65), Leg(short, "PE", 25000.0, "SELL", 65)]
    books = {wing: bk(40.0, 40.5), short: bk(100.0, 100.5)}
    expiry_day = T.replace(year=WEEKLY.year, month=WEEKLY.month, day=WEEKLY.day, hour=13)
    ok, text = acct.open_spread(structure="BULL_PUT", underlying="NIFTY", expiry=WEEKLY, legs=legs,
                                width_pts=200.0, books=books, now=expiry_day, spot=25000.0,
                                reason="t")
    assert ok, text
    play = SpreadPlay("expiry_pin", "Expiry pin", "fix2.md", acct)
    m = FakeMarket(expiry_day.replace(hour=14, minute=59), build_master(range(24300, 25800, 50)))
    m._books = books
    assert play.manage(m.now, m) == [] and acct.positions
    m.now = expiry_day.replace(hour=15, minute=0, second=1)
    out = play.manage(m.now, m)
    assert not acct.positions and "expiry-day exit 15:00" in acct.closed[-1].reason, out


def test_an_open_loss_counts_toward_the_daily_limit(repo_root: Path, tmp_path: Path) -> None:
    acct = _account(repo_root, tmp_path)
    sym = opt_symbol(WEEKLY, 25000, Kind.CALL)
    acct.open_long_option(sym, bk(99.5, 100.0), now=T, lots=1, stop_pct=70, reason="t")
    acct.check_exits({sym: bk(40.0, 40.5)}, T + timedelta(seconds=5))  # down ~Rs 3,900 open
    st = acct.risk_state()
    assert st.realized_daily_pnl_inr < -3800  # not 0: the open loss counts
    other = opt_symbol(WEEKLY, 25050, Kind.CALL)
    ok, why = acct.open_long_option(other, bk(19.9, 20.0), now=T + timedelta(seconds=6), lots=1,
                                    stop_pct=20, reason="t")
    assert ok, why  # Rs 4,000 limit not yet reached
    acct.check_exits({sym: bk(30.0, 30.5), other: bk(19.9, 20.0)}, T + timedelta(seconds=7))
    ok, why = acct.open_long_option(opt_symbol(WEEKLY, 25100, Kind.CALL), bk(19.9, 20.0),
                                    now=T + timedelta(seconds=8), lots=1, stop_pct=20, reason="t")
    assert not ok and "daily_loss_limit_hit" in why


def test_breakthrough_needs_writer_pain_and_oi_unwinding(repo_root: Path, tmp_path: Path) -> None:
    from engine.cockpit.plays_long import BreakthroughPlay
    from tests.data.test_plays import FakeMarket, build_master, chain

    m = FakeMarket(T, build_master(range(24300, 25800, 50)))
    m.rows = chain()
    for r in m.rows:
        for key in ("ce", "pe"):
            m._books[r[key]["sym"]] = bk(r[key]["bid"], r[key]["ask"])
    p = BreakthroughPlay("breakthrough", "Breakthrough", "rules.md", _account(repo_root, tmp_path, "bt"))
    sym = opt_symbol(WEEKLY, 24900, Kind.CALL)
    p.score = lambda m_, und, s, bull: {  # type: ignore[method-assign]
        "symbol": sym, "label": sym, "score": 70.0, "passes_charges_gate": True,
        "sub": {"book": 90, "volume": 90, "oi": 0, "gamma": 100, "iv": 60},
        "writer_pain_pct": 0.0, "impulse_pts": 9.0, "breakeven_pts": 2.6, "sl_25": None, "sl_30": None,
    }
    now = T.replace(minute=40, second=50)
    m.now = now
    p.step(now, m)
    assert not p.account.positions and not p.account.pending
    assert "writer pain + OI unwinding is 0 (needs 30)" in p.decision


def test_liquidity_vacuum_blocks_entries_and_delays_stops(repo_root: Path, tmp_path: Path) -> None:
    from engine.cockpit.paper import LiquidityMonitor

    mon = LiquidityMonitor(min_samples=10)
    sym = opt_symbol(WEEKLY, 25000, Kind.CALL)
    for i in range(30):  # a normal minute: 0.5% spread, deep book
        mon.observe({sym: bk(99.75, 100.25, qty=2000)}, T + timedelta(seconds=i))
    pulled = bk(97.0, 103.0, qty=300)  # makers pulled: 6% spread, thin
    assert mon.vacuum(sym, bk(99.75, 100.25, qty=2000)) is None
    assert "liquidity vacuum" in str(mon.vacuum(sym, pulled))
    acct = _account(repo_root, tmp_path)
    acct.liquidity = mon
    ok, why = acct.open_long_option(sym, pulled, now=T + timedelta(seconds=31), lots=1,
                                    stop_pct=10, reason="t")
    assert not ok and "liquidity vacuum" in why
    acct.liquidity = None
    acct.open_long_option(sym, bk(99.75, 100.25, qty=2000), now=T, lots=1, stop_pct=10, reason="t")
    acct.liquidity = mon
    gap = bk(89.0, 95.0, qty=300)  # under the 90.25 stop, in a vacuum, not past 1.5x
    assert acct.check_exits({sym: gap}, T + timedelta(seconds=40)) == []  # waits
    out = acct.check_exits({sym: gap}, T + timedelta(seconds=44))  # then exits anyway
    assert sym not in acct.positions and "after waiting" in acct.closed[-1].reason, out
