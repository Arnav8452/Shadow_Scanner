"""Capital-time scoreboard for paper accounts: capital used, utilisation, returns vs Nifty."""

from __future__ import annotations

from datetime import date

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
