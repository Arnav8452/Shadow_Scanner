"""13 Nov 2026 (evening): kill rules, the promotion scorecard, the daily loss room, structural
backstops, the condor's weekend reward-vs-gap rule and its entry economics."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.cockpit.scorecard import render, scorecard
from engine.risk.kill_rules import KillRules, evaluate_kill_rules
from tests.data.conftest import WEEKLY
from tests.data.test_cockpit import CE, _account, _book
from tests.data.test_cockpit_condor import (
    SC,
    SP,
    WC,
    WP,
    T,
    _bk,
    _entry_books,
    _legs,
    _strategy,
)
from tests.data.test_plays import FakeMarket, mkt  # noqa: F401  (fixture)


def test_kill_rules_drawdown_and_streak() -> None:
    assert not evaluate_kill_rules([500, -300, 400, -200]).killed
    v = evaluate_kill_rules([3000, -4000, -3500, -3000])  # 10,500 below the peak
    assert v.killed and "drawdown" in v.reasons[0] and v.drawdown_inr == 10_500
    v = evaluate_kill_rules([-100] * 5, KillRules(max_drawdown_inr=0))
    assert v.killed and v.losing_streak == 5 and "in a row" in v.reasons[0]
    # only the last 15 trades count for the drawdown
    assert not evaluate_kill_rules([-11_000] + [100] * 15).killed


def _journal(path: Path, nets: list[float], start: datetime) -> None:
    lines = []
    for i, x in enumerate(nets):
        t = (start + timedelta(days=i)).isoformat()
        lines.append({"type": "close", "t": t, "closed_at": t, "net_inr": x, "charges_inr": 100.0})
    path.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")


def test_scorecard_verdicts(tmp_path: Path) -> None:
    start = datetime(2026, 9, 1, 11, 0, tzinfo=T.tzinfo)
    _journal(tmp_path / "paper_good.jsonl", [900, 1100, 1000, 950, 1050] * 5, start)
    _journal(tmp_path / "paper_bad.jsonl", [300, -500] * 12 + [-400], start)  # 25 sessions
    _journal(tmp_path / "paper_new.jsonl", [800, 900], start)
    _journal(tmp_path / "paper_streak.jsonl", [-50] * 6, start)
    got = {s.account: s for s in scorecard(tmp_path, capital=200_000)}
    assert got["good"].verdict == "candidate" and got["good"].t_stat and got["good"].t_stat > 3
    assert got["bad"].verdict == "prune"
    assert got["new"].verdict == "collecting"
    assert got["streak"].verdict == "kill rule"
    assert got["good"].net_2x_charges == pytest.approx(got["good"].net - 2500)
    text = render(list(got.values()), now=start, trials=4)
    assert "candidate" in text and "Nothing here shows that a strategy is profitable" in text


def test_loss_room_is_the_smaller_of_the_trade_cap_and_what_is_left_of_the_day(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acct = _account(repo_root, tmp_path)
    cap = acct.limits.inr(acct.limits.per_trade_max_loss_pct["directional"])
    day = acct.limits.inr(acct.limits.daily_loss_limit_pct)
    assert acct.loss_room_inr() == pytest.approx(min(cap, day))
    monkeypatch.setattr(type(acct), "realized_net", property(lambda self: -1500.0))
    assert acct.loss_room_inr() == pytest.approx(min(cap, day - 1500))
    # a fill above the decision price must not widen the planned loss past the room
    ok, _ = acct.open_long_option(CE, _book(899, 900), now=T, lots=1, stop_pct=35, reason="t")
    pos = acct.positions.get(CE)
    if ok and pos is not None:
        assert (pos.avg_price - pos.stop_price) * pos.qty <= acct.loss_room_inr() + 1


def test_structural_stop_sets_the_premium_backstop_and_the_trail(
    repo_root: Path,
    tmp_path: Path,
    mkt: FakeMarket,  # noqa: F811
) -> None:
    from engine.cockpit.plays_long import ZoneRiderPlay

    p = ZoneRiderPlay("zone_rider", "Zone", "z", _account(repo_root, tmp_path, "zr"))
    mkt._books[CE] = _book(99.5, 100.0)
    mkt.greeks = {CE: {"delta": 0.55, "gamma": 0.003, "iv": 15.0}}
    # index 25,000, structural stop 24,980: 20 pts x 0.55 delta x 1.5 = 16.5 pts = 16.5%
    out = p.enter(mkt, "NIFTY", CE, lots=1, reason="t", bullish=True, stop_pct=35,
                  spot_stop=24980.0, trail=p.trail_steps)  # fmt: skip
    assert out.startswith("bought"), (out, p.notes)
    pos = p.account.positions[CE]
    assert pos.stop_price == pytest.approx(pos.avg_price * (1 - 0.165), rel=0.01)
    assert pos.trail == ((10.0, 1.0),)


def test_condor_books_before_a_weekend_when_little_is_left_vs_the_gap(
    repo_root: Path, tmp_path: Path
) -> None:
    st, _ = _strategy(repo_root, tmp_path)
    ok, text = st.account.open_spread(
        structure="IRON_CONDOR", underlying="NIFTY", expiry=WEEKLY, legs=_legs(), width_pts=200,
        books=_entry_books(), now=T, spot=25000.0, reason="test", take_profit_pct=50,
        stop_mult=2.0,
    )  # fmt: skip
    assert ok, text
    pos = st.account.positions[0]
    eco = pos.context["economics"]
    assert eco["breakeven_win_pct"] > 50 and eco["net_stop"] > eco["net_tp"] > 0
    # 15 of a 25-pt take-profit captured (60%: under the 80% rule)
    books = {WP: _bk(4.0, 4.05), SP: _bk(21.0, 21.05), SC: _bk(21.0, 21.05), WC: _bk(3.0, 3.05)}
    fri = datetime(2026, 11, 13, 15, 12, tzinfo=T.tzinfo)
    st.account.analytics = None  # no gap stress: only the 80% rule applies
    assert st.exit_reason(pos, fri, books, None, {}) is None
    st.account.gap_stress = lambda legs, und: {"-8%": -9000.0, "+8%": -8000.0}  # type: ignore[method-assign]
    why = st.exit_reason(pos, fri, books, None, {})
    assert why is not None and "left to the take-profit" in why, why
    st.account.gap_stress = lambda legs, und: {"-8%": -2000.0}  # type: ignore[method-assign]
    assert st.exit_reason(pos, fri, books, None, {}) is None  # the gap is small vs what is left
    assert SimpleNamespace  # keep the import for readers of the fixture style


def test_zone_rider_exits_without_follow_through(
    repo_root: Path,
    tmp_path: Path,
    mkt: FakeMarket,  # noqa: F811
) -> None:
    from engine.cockpit.plays_long import ZoneRiderPlay

    def opened(name: str) -> ZoneRiderPlay:
        p = ZoneRiderPlay("zone_rider", "Zone", "z", _account(repo_root, tmp_path / name, "zr"))
        mkt._books[CE] = _book(99.5, 100.0)
        mkt.now = T
        out = p.enter(mkt, "NIFTY", CE, lots=1, reason="t", bullish=True,
                      stop_pct=p.max_stop_pct, trail=p.trail_steps, time_stop=p.time_stop,
                      loss_cut=p.loss_cut, ema_after_pct=p.ema_after_pct)  # fmt: skip
        assert out.startswith("bought"), out
        return p

    p = opened("a")  # down 7% after 16 minutes: out
    mkt._books[CE] = _book(92.5, 93.0)
    mkt.now = T + timedelta(minutes=16)
    out = p.manage(mkt.now, mkt)
    assert out and "no follow-through" in p.account.closed[-1].reason, out
    p = opened("b")  # flat after 21 minutes: not +3% -> out
    mkt._books[CE] = _book(100.5, 101.0)
    mkt.now = T + timedelta(minutes=21)
    p.manage(mkt.now, mkt)
    assert "time stop" in p.account.closed[-1].reason
    p = opened("c")  # +5% at 21 minutes: kept, and the 12% stop holds
    mkt._books[CE] = _book(105.0, 105.5)
    mkt.now = T + timedelta(minutes=21)
    assert p.manage(mkt.now, mkt) == [] and CE in p.account.positions
    pos = p.account.positions[CE]
    assert pos.stop_price == pytest.approx(pos.avg_price * 0.88, rel=0.01)
