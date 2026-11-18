"""Capital-time scoreboard for paper accounts: capital used, utilisation, returns vs Nifty."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from engine.cockpit.capital_time import (
    capital_time,
    capital_used,
    index_window,
    render_capital_time,
    sessions_between,
)

SPREAD = {
    "type": "close",
    "net_inr": 556.0,
    "opened_at": "2026-11-12T13:00:40+05:30",
    "closed_at": "2026-11-13T15:10:00+05:30",
    "margin": {"final": 73600.0, "required": 76765.0},
    "legs": [{"side": "BUY", "qty": 65, "entry_px": 23.75}],
}
LONG = {
    "type": "close",
    "net_inr": -2407.0,
    "qty": 20,
    "entry_price": 887.0,
    "opened_at": "2026-11-13T13:42:00+05:30",
    "closed_at": "2026-11-13T15:20:00+05:30",
}


def test_capital_used_per_kind_of_trade() -> None:
    assert capital_used(SPREAD) == 73600.0  # the margin actually blocked
    assert capital_used({**SPREAD, "margin": {"required": 76765.0}}) == 76765.0
    assert capital_used(LONG) == pytest.approx(20 * 887.0)  # premium paid
    debit = {"legs": [{"side": "BUY", "qty": 65, "entry_px": 20.0},
                      {"side": "SELL", "qty": 65, "entry_px": 12.0}]}  # fmt: skip
    assert capital_used(debit) == pytest.approx(65 * 8.0)  # net debit when no margin known
    assert capital_used({"qty": 10, "price": 100.0}) == 1000.0
    assert capital_used({"net_inr": 5.0}) is None


def test_sessions_count_weekdays_inclusive() -> None:
    assert sessions_between(date(2026, 11, 13), date(2026, 11, 13)) == 1  # intraday
    assert sessions_between(date(2026, 11, 12), date(2026, 11, 13)) == 2
    assert sessions_between(date(2026, 11, 13), date(2026, 11, 16)) == 2  # Fri -> Mon


def test_index_window_return_and_drawdown() -> None:
    closes = [(date(2026, 11, 2), 100.0), (date(2026, 11, 3), 110.0), (date(2026, 11, 4), 99.0),
              (date(2026, 11, 5), 105.0), (date(2026, 11, 20), 200.0)]  # fmt: skip
    ret, dd = index_window(closes, date(2026, 11, 3), date(2026, 11, 5))
    assert ret == pytest.approx(105 / 110 - 1)
    assert dd == pytest.approx(99 / 110 - 1)
    assert index_window(closes, date(2027, 1, 1), date(2027, 2, 1)) == (None, None)


def test_capital_time_metrics() -> None:
    ct = capital_time(
        "mixed",
        [SPREAD, LONG],
        capital=200_000.0,
        start=date(2026, 11, 12),
        end=date(2026, 11, 13),
        risk_free_pa=0.06,
        index_closes=[(date(2026, 11, 11), 100.0), (date(2026, 11, 13), 101.0)],
        sessions_per_year=248,
        min_sessions_annualise=1,
    )
    cap_days = 73600.0 * 2 + 17740.0 * 1
    assert ct.period_sessions == 2 and ct.trades_with_capital == 2
    assert ct.capital_sessions == pytest.approx(cap_days)
    assert ct.utilisation == pytest.approx(cap_days / (200_000.0 * 2))
    net = 556.0 - 2407.0
    assert ct.deployed_return_pa == pytest.approx(net / cap_days * 248)
    util = cap_days / 400_000.0
    expected_account = net / 200_000.0 * 248 / 2 + 0.06 * (1 - util)
    assert ct.account_return_pa == pytest.approx(expected_account)
    assert ct.max_drawdown_inr == pytest.approx(-2407.0)  # from the +556 peak down to -1851
    # Nifty: start date has no close, so the window starts at the last close before it.
    assert ct.nifty_return_pa == pytest.approx((101 / 100 - 1) * 248 / 2)


def test_short_history_is_not_annualised() -> None:
    ct = capital_time(
        "x", [LONG], capital=200_000.0, start=date(2026, 11, 13), end=date(2026, 11, 13),
        risk_free_pa=0.06, index_closes=[], sessions_per_year=248, min_sessions_annualise=20,
    )  # fmt: skip
    assert ct.deployed_return_pa is None and ct.account_return_pa is None
    assert ct.utilisation == pytest.approx(17740.0 / 200_000.0)
    text = render_capital_time([ct], risk_free_pa=0.06, min_sessions_annualise=20)
    assert "Utilisation" in text and "x" in text and "—" in text


def test_swing_journal_shape_is_normalised(tmp_path: Path) -> None:
    import json

    from engine.cockpit.scorecard import account_closes

    rec = {"type": "close", "id": "SW-1", "strategy": "swing", "symbol": "ACME", "qty": 10,
           "entry_day": "2026-11-13", "entry_price": 400.0, "exit_day": "2026-11-16",
           "net": -120.5, "charges": 20.0}  # fmt: skip
    (tmp_path / "paper_swing.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    rows = account_closes(tmp_path)["swing"]
    assert rows[0]["net_inr"] == -120.5 and rows[0]["charges_inr"] == 20.0
    assert rows[0]["opened_at"].startswith("2026-11-13") and rows[0]["closed_at"].startswith(
        "2026-11-16"
    )
    ct = capital_time("swing", rows, capital=200_000.0, start=date(2026, 11, 13),
                      end=date(2026, 11, 16), risk_free_pa=0.05, index_closes=[])  # fmt: skip
    assert ct.capital_sessions == 4000.0 * 2 and ct.net == -120.5


def test_index_closes_read_from_nse_eod_files() -> None:
    from engine.cockpit.capital_time import index_closes_from_eod

    head = (
        "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
        "Closing Index Value\n"
    )
    d1 = head + "Nifty 50,17-11-2026,1,1,1,25387.4\nNifty Midcap 150,17-11-2026,1,1,1,22648.7\n"
    d2 = head + "Nifty 50,18-11-2026,1,1,1,25300\nNifty 50,18-11-2026,1,1,1,25300\n"
    out = index_closes_from_eod([d1, d2], ["Nifty 50", "Nifty Midcap 150"])
    assert out["Nifty 50"] == [(date(2026, 11, 17), 25387.4), (date(2026, 11, 18), 25300.0)]
    assert out["Nifty Midcap 150"] == [(date(2026, 11, 17), 22648.7)]


def test_yearly_rebalanced_blend() -> None:
    from engine.cockpit.capital_time import blend_closes

    a = [(date(2025, 12, 30), 100.0), (date(2025, 12, 31), 110.0), (date(2026, 1, 1), 110.0),
         (date(2026, 1, 2), 121.0)]  # fmt: skip
    b = [(date(2025, 12, 30), 100.0), (date(2025, 12, 31), 90.0), (date(2026, 1, 1), 90.0),
         (date(2026, 1, 2), 90.0)]  # fmt: skip
    mix = dict(blend_closes(a, b, weight_a=0.5))
    assert mix[date(2025, 12, 30)] == pytest.approx(100.0)
    assert mix[date(2025, 12, 31)] == pytest.approx(100.0)  # +10% and -10% halves
    # rebalanced to 50/50 on the first session of 2026, so A's +10% moves half the value
    assert mix[date(2026, 1, 2)] == pytest.approx(100.0 * (0.5 * 1.1 + 0.5 * 1.0))


def test_capital_time_reports_extra_benchmarks() -> None:
    closes = [(date(2026, 11, 11), 100.0), (date(2026, 11, 13), 103.0)]
    ct = capital_time(
        "x", [LONG], capital=200_000.0, start=date(2026, 11, 12), end=date(2026, 11, 13),
        risk_free_pa=0.06, index_closes=[(date(2026, 11, 11), 100.0), (date(2026, 11, 13), 101.0)],
        sessions_per_year=248, min_sessions_annualise=1,
        benchmarks={"Midcap 150": closes},
    )  # fmt: skip
    name, ret_pa, dd = ct.benchmarks[0]
    assert name == "Midcap 150" and ret_pa == pytest.approx(0.03 * 248 / 2) and dd == 0.0
    text = render_capital_time([ct], risk_free_pa=0.06, min_sessions_annualise=1)
    assert "Midcap 150 p.a." in text
