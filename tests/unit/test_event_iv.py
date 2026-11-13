"""Event IV crush study: synthetic IV tables only."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from engine.research.event_iv import measure_window, run_study, summarize, verdicts

EXP = date(2026, 1, 29)


def _t(day: date, expiry: date) -> float:
    return max(1, (expiry - day).days) / 365.0


def _rows(crush_on: set[date], days: list[date]) -> dict:
    rows = {}
    spot = 1000.0
    for i, d in enumerate(days):
        iv = 0.30
        if any(0 < (e - d).days <= 1 for e in crush_on):
            iv = 0.45  # the session before the event
        rows[("ACME", d)] = {"expiry": EXP, "spot": spot * (1 + 0.001 * (i % 3)), "atm_iv": iv}
    return rows


def test_measure_needs_the_same_expiry_after_the_window() -> None:
    d0, d1 = date(2026, 1, 5), date(2026, 1, 7)
    rows = {("A", d0): {"expiry": EXP, "spot": 100.0, "atm_iv": 0.4},
            ("A", d1): {"expiry": EXP, "spot": 102.0, "atm_iv": 0.3}}  # fmt: skip
    m = measure_window(rows, "A", d0, d1, _t)
    assert m is not None and m.iv_change == pytest.approx(-10.0)
    assert m.implied_move is not None and m.implied_move > 0
    assert m.realized_move == pytest.approx(0.0198, abs=1e-3)
    rows[("A", d1)] = {**rows[("A", d1)], "expiry": date(2026, 2, 26)}  # rolled
    assert measure_window(rows, "A", d0, d1, _t) is None


def test_study_finds_a_planted_crush_and_a_flat_control() -> None:
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(25)]
    events = [("ACME", date(2026, 1, 8)), ("ACME", date(2026, 1, 16))]
    rows = _rows({d for _, d in events}, days)
    s = summarize(run_study(rows, events, days, _t))
    assert s["n_events"] == 2 and s["fell_pct"] == 100 and s["median_change"] == pytest.approx(-15)
    assert s["n_control"] > 5 and abs(s["control_median"]) < 0.01
    assert verdicts(s)["H1"] == "PASS"


def test_verdicts_use_the_registered_thresholds() -> None:
    base = {"n_events": 100, "fell_pct": 69.9, "median_change": -5, "control_median": 0.1,
            "n_h2": 100, "realized_below_pct": 60, "mean_ratio": 0.85, "t_log_ratio": -3}  # fmt: skip
    assert verdicts(base) == {"H1": "FAIL", "H2": "PASS"}
    assert verdicts({**base, "fell_pct": 70})["H1"] == "PASS"
    assert verdicts({**base, "mean_ratio": 0.9})["H2"] == "FAIL"
    assert verdicts({**base, "t_log_ratio": -1.9})["H2"] == "FAIL"
