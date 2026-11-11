from __future__ import annotations

from datetime import datetime

import pytest

from engine.core.rules import IST
from engine.data.recorder.universe import (
    UnderlyingSpec,
    UniverseConfig,
    band_indices,
    expected_move,
    select_universe,
)
from engine.data.reference.instrument_master import InstrumentMaster, Kind
from tests.data.conftest import MONTHLY, WEEKLY, opt_symbol

NOW = datetime(2026, 11, 11, 9, 20, tzinfo=IST)
NIFTY = UnderlyingSpec(
    "NIFTY",
    "NSE:NIFTY50-INDEX",
    expiry_roles=("current_weekly", "current_monthly"),
    expiry_weights={"current_weekly": 1.0, "current_monthly": 2.0},
)


def cfg(budget: int, **kw: object) -> UniverseConfig:
    base: dict[str, object] = {
        "underlyings": (NIFTY,),
        "budget": budget,
        "vix_symbol": "NSE:INDIAVIX-INDEX",
        "min_strikes_each_side": 3,
        "sigma_mult": 0.0,
        "tick_strikes_each_side": 1,
    }
    base.update(kw)
    return UniverseConfig(**base)  # type: ignore[arg-type]


def test_expected_move() -> None:
    assert expected_move(25000, 0.16, 1.0) == pytest.approx(4000)
    assert expected_move(25000, 0.16, 0.0) == pytest.approx(25000 * 0.16 / 365**0.5)
    with pytest.raises(ValueError):
        expected_move(0, 0.1, 1)


def test_band_indices() -> None:
    ks = [float(k) for k in range(24000, 26050, 50)]  # 41 strikes
    lo, atm, hi = band_indices(ks, 25010, 3, 0)
    assert ks[atm] == 25000 and (ks[lo], ks[hi]) == (24850, 25150)
    lo, atm, hi = band_indices(ks, 25010, 3, 400)  # ±400 points wider than ±3 strikes
    assert (ks[lo], ks[hi]) == (24650, 25400)
    lo, atm, hi = band_indices(ks, 24010, 25, 0)  # clamped at the edge
    assert lo == 0 and ks[hi] == 25250
    with pytest.raises(ValueError):
        band_indices([], 1, 1, 0)


def test_select_core_only_without_spot(master: InstrumentMaster) -> None:
    sel = select_universe(
        master, cfg(100), now=NOW, spots={}, iv=None, sticky=set(), depth_counts_separately=True
    )
    assert sel.symbols == ["NSE:NIFTY50-INDEX", "NSE:INDIAVIX-INDEX", "NSE:NIFTY26NOVFUT"]
    assert any("no spot" in n for n in sel.notes)
    assert sel.cost == 4  # future has depth, which uses a second slot


def test_select_full_band_and_tick_set(master: InstrumentMaster) -> None:
    sel = select_universe(
        master,
        cfg(1000),
        now=NOW,
        spots={"NIFTY": 25010.0},
        iv=0.13,
        sticky=set(),
        depth_counts_separately=False,
    )
    opts = [s for s in sel.symbols if master.get(s).kind.is_option]
    assert len(opts) == 2 * 2 * 7  # two expiries x CE/PE x (ATM ± 3)
    assert sel.atm == {"NIFTY 2026-11-17": 25000.0, "NIFTY 2026-11-23": 25000.0}
    assert not sel.dropped and sel.cost == len(sel.symbols)
    # tick set: core + current weekly within ±1 strike
    weekly_ticks = {s for s in sel.tick_symbols if master.get(s).kind.is_option}
    assert weekly_ticks == {
        opt_symbol(WEEKLY, k, kd) for k in (24950, 25000, 25050) for kd in (Kind.CALL, Kind.PUT)
    }


def test_budget_keeps_atm_first_and_weights_expiries(master: InstrumentMaster) -> None:
    sel = select_universe(
        master,
        cfg(4 + 2 * 8),  # core uses 4 slots with depth; 8 options with depth
        now=NOW,
        spots={"NIFTY": 25010.0},
        iv=0.13,
        sticky=set(),
        depth_counts_separately=True,
    )
    opts = [s for s in sel.symbols if master.get(s).kind.is_option]
    assert len(opts) == 8 and sel.cost == 20
    # weekly (weight 1) gets ATM and ±1; monthly (weight 2) only ATM at that distance
    assert opt_symbol(WEEKLY, 25050, Kind.CALL) in opts
    assert opt_symbol(MONTHLY, 25000, Kind.CALL) in opts
    assert opt_symbol(MONTHLY, 25050, Kind.CALL) not in opts
    assert opt_symbol(MONTHLY, 25050, Kind.CALL) in sel.dropped
    assert sel.dropped and any("budget" in n for n in sel.notes)


def test_sticky_contracts_stay_after_recentre(master: InstrumentMaster) -> None:
    far = opt_symbol(WEEKLY, 24000, Kind.PUT)
    sel = select_universe(
        master,
        cfg(1000),
        now=NOW,
        spots={"NIFTY": 25010.0},
        iv=0.13,
        sticky={far},
        depth_counts_separately=False,
    )
    assert far in sel.symbols  # outside today's band but entered it before
    assert far not in sel.tick_symbols


def test_sigma_band_widens_beyond_min_strikes(master: InstrumentMaster) -> None:
    sel = select_universe(
        master,
        cfg(10_000, sigma_mult=3.0),
        now=NOW,
        spots={"NIFTY": 25010.0},
        iv=0.13,
        sticky=set(),
        depth_counts_separately=False,
    )
    assert sel.half_width["NIFTY 2026-11-17"] > 3 * 50
    weekly = {master.get(s).strike for s in sel.symbols if master.get(s).expiry == WEEKLY}
    assert min(k for k in weekly if k) < 25000 - 3 * 50
