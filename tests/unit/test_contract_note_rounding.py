"""Contract-note rounding of the cost model's raw charges (synthetic numbers)."""

from __future__ import annotations

import pytest

from engine.backtest.costs import ChargeBreakdown, as_contract_note


def test_rounding_matches_contract_note_conventions() -> None:
    raw = ChargeBreakdown(
        brokerage=0.0, stt=12.345, exchange=0.4321, sebi=0.0123, stamp=0.0,
        gst=0.18 * (0.4321 + 0.0123), dp=15.34,
    )  # fmt: skip
    cn = as_contract_note(raw)
    assert (cn.stt, cn.exchange, cn.sebi, cn.stamp) == (12.0, 0.43, 0.01, 0.0)
    assert cn.gst == pytest.approx(0.08)  # 18% of the rounded 0.44
    assert cn.dp == 15.34  # ledger charge, not on the note
    assert as_contract_note(ChargeBreakdown(0, 41.5, 0, 0, 6.2625, 0, 0)).stt == 42.0  # half up
