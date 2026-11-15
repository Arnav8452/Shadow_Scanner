"""Upstox Plus brokerage against Upstox's own brokerage API (queried 15 Nov 2026)."""

from __future__ import annotations

from datetime import date

import pytest

from engine.backtest.costs import equity_delivery_charges, equity_intraday_charges, fno_charges
from engine.core.config import REPO_ROOT
from engine.core.rules import RuleBook

ON = date(2026, 11, 13)


@pytest.fixture(scope="module")
def book() -> RuleBook:
    return RuleBook.load(REPO_ROOT / "config" / "rules")


@pytest.mark.parametrize(("value", "brokerage"), [(800.0, 20.0), (1500.0, 30.0), (50_000.0, 30.0)])
def test_delivery_is_lower_of_2_5_pct_and_30(
    book: RuleBook, value: float, brokerage: float
) -> None:
    b = equity_delivery_charges(book, ON, side="buy", value_inr=value, broker="upstox_plus",
                                include_dp=False, allow_unverified=True)  # fmt: skip
    assert b.brokerage == pytest.approx(brokerage)


@pytest.mark.parametrize(("value", "brokerage"), [(18_500.0, 11.10), (40_000.0, 24.0)])
def test_intraday_is_0_06_pct_capped(book: RuleBook, value: float, brokerage: float) -> None:
    b = equity_intraday_charges(book, ON, side="buy", value_inr=value, broker="upstox_plus",
                                allow_unverified=True)  # fmt: skip
    assert b.brokerage == pytest.approx(brokerage)


def test_fno_statutory_lines_match_the_api(book: RuleBook) -> None:
    # Options: 2 lots of 65 at 165 sold; futures: 1 lot at 25,300 sold (API figures).
    o = fno_charges(book, ON, segment="options", side="sell", turnover_inr=21_450.0,
                    broker="upstox_plus")  # fmt: skip
    # The API prints paise half up (32.175 -> 32.18); compare within half a paisa.
    assert (o.brokerage, o.stt, o.exchange) == pytest.approx((30.0, 32.18, 7.62), abs=0.011)
    f = fno_charges(book, ON, segment="futures", side="sell", turnover_inr=65 * 25_300.0,
                    broker="upstox_plus")  # fmt: skip
    assert (f.brokerage, f.stt, f.exchange) == pytest.approx((30.0, 822.25, 30.09), abs=0.011)
    fb = fno_charges(book, ON, segment="futures", side="buy", turnover_inr=65 * 25_300.0,
                     broker="upstox_plus")  # fmt: skip
    assert fb.stamp == pytest.approx(32.89, abs=0.011)
