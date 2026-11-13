"""SPEC 5.7 vol-managed sizing overlay: factor, never sizing up, skipping, and the wiring."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from engine.cockpit.vol_sizing import VolSizing, load_daily_closes, rolling_rv
from tests.data.test_cockpit import CE, _account, _book
from tests.data.test_cockpit_condor import T, _acct, _open

DAY = date(2026, 1, 5)


def _closes(daily_moves: list[float], end: date = DAY) -> list[tuple[date, float]]:
    """Closes ending the day before ``end`` with alternating +/- moves of the given sizes."""
    px, out = 25000.0, []
    for i, m in enumerate(daily_moves):
        px *= math.exp(m if i % 2 else -m)
        out.append((end - timedelta(days=len(daily_moves) - i), px))
    return out


def test_rolling_rv_annualises_close_to_close_vol() -> None:
    rv = rolling_rv([100 * math.exp(0.01 * (i % 2)) for i in range(30)], n=20)
    assert rv[-1] == pytest.approx(0.01 * math.sqrt(252) * 100, rel=1e-6)


def test_factor_scales_down_and_never_up() -> None:
    calm, wild = [0.005] * 260, [0.005] * 240 + [0.0125] * 21
    vs = VolSizing({"NIFTY": _closes(wild)})
    k, now, med = vs.factor("NIFTY", DAY)  # type: ignore[misc]
    assert now > med and k == pytest.approx(0.5, abs=0.05)
    assert vs.size("X", 2, DAY)[0] == 0  # far above the median: skip
    mild = [0.005] * 240 + [0.0065] * 21  # factor about 0.77
    vs = VolSizing({"NIFTY": _closes(mild)})
    lots, note, ctx = vs.size("X", 2, DAY)
    assert lots == 1 and note and "2 -> 1" in note and 0.6 < ctx["vol_factor"] < 1
    assert vs.size("X", 1, DAY)[0] == 1  # one lot stays one lot
    quiet = [0.005] * 240 + [0.002] * 21  # low vol: factor capped, size unchanged
    vs = VolSizing({"NIFTY": _closes(quiet)})
    assert vs.size("X", 2, DAY)[0] == 2 and vs.factor("NIFTY", DAY)[0] == 1.5  # type: ignore[index]
    assert VolSizing({"NIFTY": _closes(calm[:30])}).size("X", 2, DAY) == (2, None, {})


def test_point_in_time_ignores_closes_on_or_after_the_day() -> None:
    hist = _closes([0.005] * 260)
    later = [*hist, (DAY, hist[-1][1] * 1.2), (DAY + timedelta(days=1), hist[-1][1])]
    assert VolSizing({"NIFTY": later}).factor("NIFTY", DAY) == VolSizing({"NIFTY": hist}).factor(
        "NIFTY", DAY
    )


def test_stock_options_fall_back_to_nifty() -> None:
    vs = VolSizing({"NIFTY": _closes([0.005] * 260)}, underlying_of=lambda s: "RELIANCE")
    assert vs.size("RELIANCE-OPT", 1, DAY)[2]["vol_factor"] == pytest.approx(1.0, abs=0.05)


def test_daily_cache_is_reused_the_same_day(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(key: str, frm: date, to: date) -> list[list[object]]:
        calls.append(key)
        return [[f"{to.isoformat()}T00:00:00+05:30", 1, 1, 1, 100.0, 0]]

    cache = tmp_path / "index_daily.json"
    a = load_daily_closes(cache, DAY, fetch, keys={"NIFTY": "k"})
    b = load_daily_closes(cache, DAY, fetch, keys={"NIFTY": "k"})
    assert calls == ["k"] and a == b == {"NIFTY": [(DAY - timedelta(days=1), 100.0)]}
    assert json.loads(cache.read_text())["as_of"] == DAY.isoformat()
    assert load_daily_closes(tmp_path / "none.json", DAY, None) == {}


def test_long_account_cuts_lots_and_spreads_skip(repo_root: Path, tmp_path: Path) -> None:
    wild = VolSizing({"NIFTY": _closes([0.005] * 240 + [0.0125] * 21, T.date())})
    mild = VolSizing({"NIFTY": _closes([0.005] * 240 + [0.0065] * 21, T.date())})
    acct = _account(repo_root, tmp_path)
    acct.limits = replace(acct.limits, max_lots_per_strategy=2)
    acct.vol_sizing = mild
    ok, why = acct.open_long_option(CE, _book(100, 100.5), now=T, lots=2, stop_pct=10, reason="t")
    assert ok, why
    pos = acct.positions[CE]
    assert pos.qty == acct.lot_size_of(CE) and "2 -> 1" in pos.context["vol_note"]
    acct2 = _account(repo_root, tmp_path, "b")
    acct2.vol_sizing = wild
    ok, why = acct2.open_long_option(CE, _book(100, 100.5), now=T, lots=1, stop_pct=10, reason="t")
    assert not ok and "entries paused" in why
    sp = _acct(repo_root, tmp_path)
    sp.vol_sizing = wild
    ok, why = _open(sp)
    assert not ok and "vol-managed sizing" in why
    sp.vol_sizing = mild
    ok, why = _open(sp)
    assert ok, why
    assert sp.positions[0].context["vol_factor"] < 1
