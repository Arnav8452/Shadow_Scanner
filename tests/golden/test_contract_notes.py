"""Golden tests: the cost model against the owner's real contract notes.

The notes are personal records, so their numbers live only in data/private/golden/*.json
(git-ignored; no names, client codes or PAN in them). Without that folder these tests skip,
so CI stays green while the owner's machine checks every note. Bar (CLAUDE.md): each printed
line within ₹0.01 after contract-note rounding (the broker's ₹0.01 token brokerage on a
zero-brokerage delivery note is the one allowed difference), the total within ₹1.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from engine.backtest.costs import as_contract_note, equity_delivery_charges
from engine.core.config import REPO_ROOT
from engine.core.rules import RuleBook

GOLDEN = REPO_ROOT / "data" / "private" / "golden"
# Only contract-note fixtures: a file must say what it is ("trade_date" and "orders"), so a raw
# dump saved here by mistake is skipped instead of failing the suite.
NOTES = (
    sorted(
        p
        for p in GOLDEN.glob("*.json")
        if {"trade_date", "orders"} <= set(json.loads(p.read_text(encoding="utf-8")))
    )
    if GOLDEN.exists()
    else []
)


@pytest.mark.skipif(not NOTES, reason="no private contract notes on this machine")
@pytest.mark.parametrize("path", NOTES, ids=[p.stem for p in NOTES])
def test_cost_model_matches_contract_note(path: Path) -> None:
    note = json.loads(path.read_text(encoding="utf-8"))
    book = RuleBook.load(REPO_ROOT / "config" / "rules")
    on = date.fromisoformat(note["trade_date"])
    total = None
    for o in note["orders"]:
        assert o["segment"] == "equity_delivery", "only delivery notes are modelled so far"
        b = equity_delivery_charges(
            book, on, side=o["side"], value_inr=o["value_inr"], broker=note["broker"],
            exchange=o["exchange"], include_dp=False,
        )  # fmt: skip
        total = b if total is None else total + b
    assert total is not None
    got = as_contract_note(total)
    want = note["charges"]
    for line in ("exchange", "sebi", "stamp", "gst", "stt"):
        assert got.__getattribute__(line) == pytest.approx(want[line], abs=0.011), line
    assert got.brokerage == pytest.approx(want["brokerage"], abs=0.011)
    printed = sum(want[k] for k in ("brokerage", "exchange", "sebi", "stamp", "gst", "stt"))
    assert abs(got.total - printed) <= 1.0

    ledger = note.get("ledger")
    if ledger:  # the cash that actually moved, from the broker's ledger
        o = note["orders"][0]
        with_dp = equity_delivery_charges(
            book, on, side=o["side"], value_inr=o["value_inr"], broker=note["broker"],
            exchange=o["exchange"], include_dp=True,
        )  # fmt: skip
        assert with_dp.dp == pytest.approx(ledger["dp"], abs=0.005)
        # Settlement uses unrounded exchange, SEBI and GST, STT to the rupee, and the broker's
        # token brokerage: our raw charges with only STT rounded land within a few paise.
        cash = sum(o2["value_inr"] * (1 if o2["side"] == "sell" else -1) for o2 in note["orders"])
        settled = cash - (total.exchange + total.sebi + total.gst + total.stamp
                          + got.stt + want["brokerage"])  # fmt: skip
        assert settled == pytest.approx(ledger["net_settlement"], abs=0.02)
