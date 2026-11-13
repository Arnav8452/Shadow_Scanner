"""SPEC 6 gap stress: scenario P&L of open structures and the overnight carry rule."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.cockpit.condor import Leg
from engine.cockpit.stress import gap_pnl, worst
from tests.data.test_cockpit_condor import SC, SP, WC, WP, T, _acct, _entry_books, _legs, _open

T_VAR = 5 / 252


def _detail(iv: float = 14.0) -> dict[str, dict[str, float]]:
    return {
        sym: {"iv": iv, "forward": 25000.0, "strike": k, "t_var": T_VAR}
        for sym, k in ((WP, 24600), (SP, 24800), (SC, 25200), (WC, 25400))
    }


def test_iron_condor_gap_loss_is_bounded_by_the_wings() -> None:
    st = gap_pnl(_legs(), _detail(), 25000.0)
    assert st is not None and set(st) == {"-8%", "-5%", "+5%", "+8%"}
    w = worst(st)
    assert w is not None and w[1] < 0
    assert -w[1] <= 200 * 65  # never more than the wing width x qty
    assert st["-8%"] < st["-5%"] < 0  # a bigger gap costs more


def test_synthetic_long_loses_about_the_wing_on_a_crash_and_futures_move_one_for_one() -> None:
    legs = [
        Leg(WP, "PE", 24600, "BUY", 65),  # protective wing
        Leg(SC, "CE", 25000, "BUY", 65),
        Leg(SP, "PE", 25000, "SELL", 65),
    ]
    det = _detail()
    det[SC] = {**det[SC], "strike": 25000}
    det[SP] = {**det[SP], "strike": 25000}
    st = gap_pnl(legs, det, 25000.0)
    assert st is not None
    assert st["+8%"] > 0 > st["-8%"]
    # the 24600 wing caps it: 400 points to the wing plus the time value given up
    assert 400 * 65 < -st["-8%"] < 500 * 65
    fut = [Leg("FUT", "FUT", 0, "BUY", 75)]
    assert gap_pnl(fut, {}, 25000.0) == {"-8%": -150000, "-5%": -93750, "+5%": 93750, "+8%": 150000}
    assert gap_pnl(fut, {}, None) is None
    assert gap_pnl(legs, {}, 25000.0) is None  # no IV: no number rather than a wrong one


def test_carry_rule_closes_only_overnight_structures_over_the_limit(
    repo_root: Path, tmp_path: Path
) -> None:
    acct = _acct(repo_root, tmp_path)
    ok, text = _open(acct)
    assert ok, text
    acct.analytics = SimpleNamespace(detail=_detail(), strip={"NIFTY": {"spot": 25000.0}})
    view = acct.view(_entry_books(), T, 25000.0)["positions"][0]
    assert view["stress"]["-8%"] < 0 and view["stress_cap"] == pytest.approx(acct.gap_cap())
    late = T.replace(hour=15, minute=12)
    assert acct.carry_check(_entry_books(), late) == []  # not held overnight
    acct.holds_overnight = True
    assert acct.carry_check(_entry_books(), late) == []  # within the limit
    acct.limits = replace(
        acct.limits,
        per_trade_max_loss_pct={**acct.limits.per_trade_max_loss_pct, "defined_risk_spread": 1.0},
    )
    assert acct.carry_check(_entry_books(), T.replace(hour=14, minute=50)) == []  # too early
    out = acct.carry_check(_entry_books(), late)
    assert out and "gap stress" in out[0] and not acct.positions
