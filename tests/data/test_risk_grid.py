"""Natenberg N4: price x vol risk grid, the next-session slice, and book greeks by expiry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.analytics.expiry_clock import set_variance_weights, variance_weights
from engine.cockpit.condor import Leg
from engine.cockpit.stress import (
    GRID_IV_PTS,
    GRID_SPOT_PCT,
    book_greeks,
    one_session_t_var,
    price_vol_grid,
    risk_view,
)
from tests.data.test_cockpit_condor import SC, SP, WC, WP, _legs

T_VAR = 5 / 252


def _detail(iv: float = 14.0, days: int = 5) -> dict[str, dict[str, float]]:
    return {
        sym: {"iv": iv, "forward": 25000.0, "strike": k, "t_var": T_VAR, "days": days}
        for sym, k in ((WP, 24600), (SP, 24800), (SC, 25200), (WC, 25400))
    }


def _cell(g: dict, spot_pct: float, iv_pt: float) -> float:
    return float(g["pnl"][g["iv"].index(iv_pt)][g["spot"].index(spot_pct)])


def test_grid_shape_and_zero_at_the_centre() -> None:
    g = price_vol_grid(_legs(), _detail(), 25000.0)
    assert g is not None
    assert g["spot"] == list(GRID_SPOT_PCT) and g["iv"] == list(GRID_IV_PTS)
    assert len(g["pnl"]) == len(GRID_IV_PTS) and all(len(r) == len(GRID_SPOT_PCT) for r in g["pnl"])
    assert _cell(g, 0, 0) == 0


def test_short_condor_loses_on_vol_up_and_is_bounded_by_the_wings() -> None:
    g = price_vol_grid(_legs(), _detail(), 25000.0)
    assert g is not None
    assert _cell(g, 0, 10) < 0 < _cell(g, 0, -5)  # short vega
    worst = min(min(r) for r in g["pnl"])
    assert -worst <= 200 * 65  # never more than the wing width x qty
    assert _cell(g, -8, 0) < _cell(g, -2, 0)


def test_long_call_gains_with_spot_and_with_vol() -> None:
    legs = [Leg(SC, "CE", 25200, "BUY", 65)]
    g = price_vol_grid(legs, _detail(), 25000.0)
    assert g is not None
    for row in g["pnl"]:
        assert row == sorted(row)  # monotone in spot
    for j in range(len(GRID_SPOT_PCT)):
        col = [r[j] for r in g["pnl"]]
        assert col == sorted(col)  # monotone in IV


def test_next_session_slice_shows_decay() -> None:
    g1 = price_vol_grid(_legs(), _detail(), 25000.0, t_shift=1 / 252)
    assert g1 is not None and _cell(g1, 0, 0) > 0  # short premium earns theta
    long_call = [Leg(SC, "CE", 25200, "BUY", 65)]
    g2 = price_vol_grid(long_call, _detail(), 25000.0, t_shift=1 / 252)
    assert g2 is not None and _cell(g2, 0, 0) < 0
    # past expiry: the option is worth its intrinsic value
    g3 = price_vol_grid(long_call, _detail(), 25000.0, t_shift=1.0)
    assert g3 is not None
    assert _cell(g3, 8, 0) == pytest.approx((25000 * 1.08 - 25200) * 65 - _base(long_call), abs=1)


def _base(legs: list[Leg]) -> float:
    from engine.analytics.iv import black76_price

    return sum(black76_price(25000.0, lg.strike, T_VAR, 0.14, is_call=True) * lg.qty for lg in legs)


def test_futures_leg_is_linear_and_vol_free_and_missing_iv_gives_none() -> None:
    fut = [Leg("FUT", "FUT", 0, "BUY", 75)]
    g = price_vol_grid(fut, {}, 25000.0)
    assert g is not None
    assert _cell(g, 8, 0) == _cell(g, 8, 10) == 25000 * 0.08 * 75
    assert price_vol_grid(fut, {}, None) is None
    assert price_vol_grid(_legs(), {}, 25000.0) is None
    closed = [Leg(SC, "CE", 25200, "BUY", 65, open=False)]
    g0 = price_vol_grid(closed, {}, 25000.0)
    assert g0 is not None and all(v == 0 for r in g0["pnl"] for v in r)


def test_book_greeks_signs_and_vega_by_expiry() -> None:
    det = _detail()
    det[WC] = {**det[WC], "days": 12}
    bg = book_greeks(_legs(), det, 25000.0)
    assert bg is not None
    assert bg["vega_inr_per_pt"] < 0 and bg["gamma_inr_per_1pct"] < 0
    assert set(bg["vega_by_expiry"]) == {"5d", "12d"}
    assert sum(bg["vega_by_expiry"].values()) == pytest.approx(bg["vega_inr_per_pt"], abs=2)
    long_call = book_greeks([Leg(SC, "CE", 25200, "BUY", 65)], det, 25000.0)
    assert long_call is not None
    assert long_call["delta_inr_per_1pct"] > 0 and long_call["vega_inr_per_pt"] > 0
    # delta x 1% agrees with a small repricing
    g = price_vol_grid([Leg(SC, "CE", 25200, "BUY", 65)], det, 25000.0, spot_pcts=(-0.1, 0, 0.1),
                       iv_pts=(0,))  # fmt: skip
    assert g is not None
    fd = (g["pnl"][0][2] - g["pnl"][0][0]) / 0.2
    assert long_call["delta_inr_per_1pct"] == pytest.approx(fd, rel=0.02)
    assert book_greeks(_legs(), {}, 25000.0) is None


def test_one_session_of_variance_time_follows_the_clock() -> None:
    saved = variance_weights()
    try:
        set_variance_weights(0.0, 0.0)
        assert one_session_t_var() == pytest.approx(1 / 252)
        set_variance_weights(0.49, 0.31)
        assert one_session_t_var() == pytest.approx(1.49 / (252 * 1.49 + 113 * 0.31))
    finally:
        set_variance_weights(*saved)


def test_risk_view_combines_grid_next_session_and_greeks() -> None:
    a = SimpleNamespace(detail=_detail(), strip={"NIFTY": {"spot": 25000.0}})
    rv = risk_view(_legs(), a, "NIFTY")
    assert rv is not None and set(rv) == {"grid", "next_session", "greeks"}
    assert rv["next_session"]["pnl"][GRID_IV_PTS.index(0)][GRID_SPOT_PCT.index(0)] > 0
    assert risk_view(_legs(), None, "NIFTY") is None
