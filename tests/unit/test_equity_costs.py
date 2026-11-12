"""Cash-equity costs: intraday (MIS) and delivery on the Upstox plans, from config/rules only.

Rates (checked 12 Nov 2026): NSE cash transaction charge + IPFT = ₹307 per crore each side
(NSE/FA/73061), STT 0.025% on intraday sells, stamp duty 0.003% on intraday buys, SEBI ₹10 per
crore, GST 18% on brokerage + exchange + SEBI; Upstox Plus brokerage ₹30 or 0.1% (intraday),
₹30 (delivery); Upstox standard ₹20 or 0.1% (intraday)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from engine.backtest.costs import equity_delivery_charges, equity_intraday_charges
from engine.core.rules import RuleBook

ON = date(2026, 11, 12)


@pytest.fixture(scope="module")
def book(repo_root: Path) -> RuleBook:
    return RuleBook.load(repo_root / "config" / "rules")


def test_intraday_buy_and_sell_upstox_plus(book: RuleBook) -> None:
    buy = equity_intraday_charges(book, ON, side="buy", value_inr=100_000, broker="upstox_plus")
    assert buy.brokerage == pytest.approx(30.0)  # ₹30 cap below 0.1% (₹100)
    assert buy.stt == 0 and buy.stamp == pytest.approx(3.0)
    assert buy.exchange == pytest.approx(3.07) and buy.sebi == pytest.approx(0.10)
    assert buy.gst == pytest.approx(0.18 * (30 + 3.07 + 0.10), abs=0.01) and buy.dp == 0
    sell = equity_intraday_charges(book, ON, side="sell", value_inr=100_000, broker="upstox_plus")
    assert sell.stt == pytest.approx(25.0) and sell.stamp == 0 and sell.dp == 0
    assert buy.total + sell.total == pytest.approx(
        30 + 3 + 3.07 + 0.1 + 5.97 + 30 + 25 + 3.07 + 0.1 + 5.97, abs=0.02
    )


def test_intraday_small_order_pays_the_percentage(book: RuleBook) -> None:
    small = equity_intraday_charges(book, ON, side="buy", value_inr=10_000, broker="upstox_plus")
    assert small.brokerage == pytest.approx(10.0)  # 0.1% of ₹10,000 < ₹30
    std = equity_intraday_charges(book, ON, side="buy", value_inr=100_000, broker="upstox")
    assert std.brokerage == pytest.approx(20.0)


def test_intraday_rejects_negative_value(book: RuleBook) -> None:
    with pytest.raises(ValueError):
        equity_intraday_charges(book, ON, side="buy", value_inr=-1, broker="upstox_plus")


def test_delivery_on_upstox_plans(book: RuleBook) -> None:
    buy = equity_delivery_charges(book, ON, side="buy", value_inr=100_000, broker="upstox_plus")
    assert buy.brokerage == pytest.approx(30.0) and buy.stt == pytest.approx(100.0)
    sell = equity_delivery_charges(book, ON, side="sell", value_inr=100_000, broker="upstox_plus")
    assert sell.dp == pytest.approx(23.6)  # ₹20 per scrip per sell day + 18% GST
    std = equity_delivery_charges(book, ON, side="buy", value_inr=100_000, broker="upstox")
    assert std.brokerage == pytest.approx(20.0)


def test_exchange_charge_is_primary_sourced(book: RuleBook) -> None:
    rows = book["exchange_charges"].lookup_all(ON, exchange="NSE", segment="equity_intraday")
    assert len(rows) == 1 and rows[0].verified
    assert "FA73061" in rows[0].source_url
