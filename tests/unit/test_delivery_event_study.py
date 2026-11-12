"""Delivery event study: same classification as the live scanner, next-open entry (no
look-ahead), F&O membership as of each day, and every threshold set counted as a trial."""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

import pytest

from engine.core.rules import IST
from engine.data.eod.bhavcopy import CashBhavcopyRow, scan_relative_delivery_spikes
from engine.research.delivery_event_study import (
    DEFAULT_GRID,
    LONG_BIAS,
    DayFeatures,
    Trial,
    features,
    run_study,
    trial_count,
)

D0 = date(2026, 1, 1)


def _row(
    sym: str, i: int, *, o: float, c: float, prev: float, qty: int, dq: int
) -> CashBhavcopyRow:
    d = D0 + timedelta(days=i)
    return CashBhavcopyRow(
        trade_date=d, symbol=sym, series="EQ", open=o, high=max(o, c) * 1.01, low=min(o, c) * 0.99,
        close=c, prev_close=prev, traded_qty=qty, turnover_inr=qty * c, delivery_qty=dq,
        delivery_pct=dq / qty * 100.0, known_at=datetime.combine(d, time(19, 30), IST),
    )  # fmt: skip


def _series(sym: str, spike_at: int, n: int = 30) -> list[CashBhavcopyRow]:
    out, px = [], 100.0
    for i in range(n):
        if i == spike_at:
            out.append(_row(sym, i, o=px, c=px * 1.02, prev=px, qty=1_500_000, dq=900_000))
            px *= 1.02
        elif i == spike_at + 1:
            out.append(_row(sym, i, o=px * 1.01, c=px * 1.03, prev=px, qty=1_000_000, dq=300_000))
            px *= 1.03
        else:
            out.append(_row(sym, i, o=px, c=px, prev=px, qty=1_000_000, dq=300_000))
    return out


def test_classification_matches_the_live_scanner() -> None:
    rng = random.Random(7)
    feats_checked = 0
    for k in range(40):
        sym = f"S{k}"
        rows = []
        px = 100.0
        for i in range(26):
            qty = rng.randint(500_000, 2_000_000)
            dq = int(qty * rng.uniform(0.05, 0.8))
            c = px * (1 + rng.uniform(-0.08, 0.08))
            rows.append(_row(sym, i, o=px, c=c, prev=px, qty=qty, dq=dq))
            px = c
        fno = {r.trade_date: {sym} for r in rows}
        feats = features({sym: rows}, fno, horizons=(1, 3, 5))
        for f in feats:
            i = (f.day - D0).days
            want = scan_relative_delivery_spikes({sym: rows[i - 20 : i + 1]})
            got = Trial("accumulation").classify(f) or Trial("churn").classify(f)
            assert (want[0].bias.value if want else None) == got
            feats_checked += 1
    assert feats_checked > 0


def test_entry_is_next_open_and_universe_is_point_in_time() -> None:
    rows = _series("AAA", spike_at=22)
    spike_day = rows[22].trade_date
    feats = features({"AAA": rows}, {r.trade_date: {"AAA"} for r in rows})
    f = next(x for x in feats if x.day == spike_day)
    assert Trial("accumulation").classify(f) == LONG_BIAS
    entry, exit_ = rows[23].open, rows[23].close
    assert f.fwd[1] == pytest.approx((exit_ / entry - 1) * 100)  # not T's close -> T+1's close
    # Not in F&O that day -> not in the sample
    fno = {r.trade_date: {"AAA"} for r in rows}
    fno[spike_day] = set()
    assert all(x.day != spike_day for x in features({"AAA": rows}, fno))


def test_every_grid_cell_is_a_trial_and_costs_reduce_returns() -> None:
    feats = [
        DayFeatures("A", D0 + timedelta(days=i), 3.0, 1.5, 2.5, 3.0, {1: 1.0, 3: 2.0, 5: 3.0})
        for i in range(30)
    ] + [
        DayFeatures("B", D0 + timedelta(days=i), 1.0, 1.0, 0.0, 0.0, {1: 0.0, 3: 0.0, 5: 0.0})
        for i in range(30)
    ]
    res = run_study(feats, cost_pct=0.25, slippage_pct_per_side=0.05)
    cell = next(r for r in res if r.trial == "accum qty>=2x ret>=1%" and r.horizon == 1)
    assert cell.mean_gross_pct == 1.0 and cell.mean_net_pct == pytest.approx(0.65)
    assert cell.mean_net_2x_pct == pytest.approx(0.55) and cell.baseline_pct == 0.0
    # every cell examined (settings x biases x horizons) counts, traded or not
    assert trial_count(DEFAULT_GRID, (1, 3, 5)) == 72
    assert all(r.n_trades == 30 for r in res if r.bias == LONG_BIAS)
