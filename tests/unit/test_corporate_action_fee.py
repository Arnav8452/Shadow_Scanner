"""A buyback / open-offer / delisting tender is placed as a corporate-action order: the broker
charges a flat fee per order (Zerodha and Upstox: ₹20 + GST, non-refundable; checked on their
support pages 18 Nov 2026) instead of delivery brokerage. STT and the DP debit still apply."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from engine.backtest.costs import equity_delivery_charges
from engine.core.rules import RuleBook

ON = date(2026, 11, 18)


@pytest.fixture(scope="module")
def book(repo_root: Path) -> RuleBook:
    return RuleBook.load(repo_root / "config" / "rules")


@pytest.mark.parametrize("broker", ["zerodha", "upstox", "upstox_plus"])
def test_tender_order_pays_the_flat_corporate_action_fee(book: RuleBook, broker: str) -> None:
    c = equity_delivery_charges(
        book, ON, side="sell", value_inr=50_000, broker=broker, corporate_action=True,
        allow_unverified=True,
    )  # fmt: skip
    plain = equity_delivery_charges(
        book, ON, side="sell", value_inr=50_000, broker=broker, allow_unverified=True
    )
    assert c.brokerage == pytest.approx(20.0)
    assert c.stt == pytest.approx(plain.stt) and c.dp == pytest.approx(plain.dp)  # still a sale
    gst_rate = plain.gst / (plain.brokerage + plain.exchange + plain.sebi)
    assert c.gst == pytest.approx(gst_rate * (20.0 + c.exchange + c.sebi))


def test_zerodha_delivery_sale_has_no_brokerage_but_a_tender_does(book: RuleBook) -> None:
    sale = equity_delivery_charges(book, ON, side="sell", value_inr=50_000, broker="zerodha")
    tender = equity_delivery_charges(
        book, ON, side="sell", value_inr=50_000, broker="zerodha", corporate_action=True
    )
    assert sale.brokerage == 0.0
    assert tender.total - sale.total == pytest.approx(20.0 * 1.18)
