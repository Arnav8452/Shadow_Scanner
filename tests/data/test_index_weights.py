"""Derived Nifty weights: close x shares x (1 - promoter %), normalised; written as `derived`,
verified only against the NSE factsheet. Synthetic numbers; no network."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from engine.core.rules import IST
from engine.data.constituents.table import heavyweights, load_constituent_table
from engine.data.constituents.weights import (
    WeightInput,
    derive_weights,
    verify_against_factsheet,
    write_derived_weights,
)
from engine.data.eod.download import eod_path
from engine.data.eod.evening import EveningConfig, update_index_weights
from engine.data.fundamentals.store import upsert
from engine.data.fundamentals.xbrl import parse_shareholding_xbrl

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "nse"
Q = date(2026, 9, 30)
HEADER = (
    "index,symbol,company,industry,isin,weight_pct,weight_status,effective_from,effective_to,"
    "source_url,verified_on,status,notes\n"
)


def _csv(path: Path) -> Path:
    src = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
    path.write_text(
        HEADER
        + f"NIFTY,AAA,A Ltd,X,INE000A01011,9.0,UNVERIFIED,2026-11-11,,{src},2026-11-12,primary,"
        "Membership. Weight carried over from code without a source: check the NSE Indices "
        "factsheet.\n"
        f"NIFTY,BBB,B Ltd,X,INE000B01011,,,2026-11-11,,{src},2026-11-12,primary,Membership.\n"
        f"NIFTY,CCC,C Ltd,X,INE000C01011,,,2026-11-11,,{src},2026-11-12,primary,Membership.\n"
        f"NIFTY,OLD,Old Ltd,X,INE000D01011,1.0,,2025-01-01,2026-11-11,{src},2026-11-12,primary,"
        "Left the index.\n",
        encoding="utf-8",
    )
    return path


def test_derive_weights_free_float_and_normalisation() -> None:
    w = derive_weights(
        {
            "AAA": WeightInput(100.0, 1_000.0, 50.0, Q),  # ff cap 50,000
            "BBB": WeightInput(50.0, 1_000.0, None, Q),  # no promoter: ff 1 -> 50,000
            "CCC": WeightInput(0.0, 1_000.0, 10.0, Q),  # no price: left out
        }
    )
    assert set(w) == {"AAA", "BBB"}
    assert w["AAA"].weight_pct == 50.0 and w["BBB"].weight_pct == 50.0
    assert w["AAA"].free_float == 0.5 and w["BBB"].free_float == 1.0


def test_write_derived_marks_not_verified_and_keeps_membership(tmp_path: Path) -> None:
    path = _csv(tmp_path / "c.csv")
    w = derive_weights(
        {
            "AAA": WeightInput(100.0, 3_000.0, 50.0, Q),
            "BBB": WeightInput(100.0, 1_000.0, None, Q),
            "CCC": WeightInput(100.0, 1_000.0, 0.0, Q),
        }
    )
    assert write_derived_weights(path, w, index="NIFTY", price_date=date(2026, 11, 11)) == 3
    rows = {r.symbol: r for r in load_constituent_table(path)}
    assert rows["AAA"].weight_pct == pytest.approx(42.86) and rows["AAA"].weight_status == "derived"
    assert not any(r.weight_verified for r in rows.values() if r.symbol != "OLD")
    assert rows["OLD"].weight_pct == 1.0  # a former member's row is untouched
    text = path.read_text(encoding="utf-8")
    assert "carried over from code" not in text and "Weight derived 2026-11-11" in text
    # every member with a weight now counts as a heavyweight candidate
    assert [
        r.symbol for r in heavyweights(list(rows.values()), "NIFTY", date(2026, 11, 12), 2)
    ] == [
        "AAA",
        "BBB",
    ]
    res = verify_against_factsheet(
        path, {"AAA": 43.0, "BBB": 20.0}, index="NIFTY",
        source_url="https://www.niftyindices.com/Factsheet/ind_nifty50.pdf",
        checked_on=date(2026, 11, 12),
    )  # fmt: skip
    assert res["AAA"][2] and not res["BBB"][2]
    rows = {r.symbol: r for r in load_constituent_table(path)}
    assert rows["AAA"].weight_verified and rows["AAA"].weight_status == ""
    assert not rows["BBB"].weight_verified and rows["BBB"].weight_status == "derived"


def test_evening_weights_step_offline(tmp_path: Path) -> None:
    cons = _csv(tmp_path / "c.csv")
    raw, pq_dir = tmp_path / "raw", tmp_path / "pq"
    day = date(2026, 11, 11)
    p = eod_path(raw, "delivery", day)
    p.parent.mkdir(parents=True)
    p.write_text(
        "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
        "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
        + "".join(
            f"{s}, EQ, 11-Nov-2026, 100, 100, 101, 99, {c}, {c}, 100, 1000, 1, 10, 500, 50.00\n"
            for s, c in (("AAA", 200.0), ("BBB", 100.0), ("CCC", 100.0))
        ),
        encoding="utf-8",
    )
    base = parse_shareholding_xbrl(
        (FIX / "shp_reliance_2026-09-30.xml").read_bytes(),
        known_at=datetime(2026, 10, 21, 16, 25, tzinfo=IST),
    )
    upsert(
        pq_dir / "shareholding.parquet",
        [
            replace(base, symbol="AAA", filing_id="a", total_shares=1_000.0, promoter_pct=50.0),
            replace(base, symbol="BBB", filing_id="b", total_shares=1_000.0, promoter_pct=None),
        ],
    )
    calls: list[str] = []

    def fetch(url: str) -> bytes:  # CCC has no pattern yet: one list request, nothing filed
        calls.append(url)
        return b"[]"

    cfg = EveningConfig(constituents_file=cons, pace_s=0)
    now = datetime(2026, 11, 11, 20, tzinfo=IST)
    with pytest.raises(ValueError, match="CCC"):
        update_index_weights(cfg, raw_dir=raw, parquet_dir=pq_dir, day=day, now=now, fetcher=fetch)
    assert len(calls) == 1 and "share-holdings" in calls[0]
    upsert(
        pq_dir / "shareholding.parquet",
        [replace(base, symbol="CCC", filing_id="c", total_shares=1_000.0, promoter_pct=0.0)],
    )
    n, note = update_index_weights(
        cfg, raw_dir=raw, parquet_dir=pq_dir, day=day, now=now, fetcher=fetch
    )
    assert n == 3 and len(calls) == 1  # fresh patterns are not fetched again
    rows = {r.symbol: r for r in load_constituent_table(cons)}
    # AAA 200*1000*0.5 = 100k, BBB 100k, CCC 100k -> a third each
    assert rows["AAA"].weight_pct == pytest.approx(33.33, abs=0.01)
    assert "top:" in note


def test_parse_factsheet_two_column_layout() -> None:
    from engine.data.constituents.weights import parse_factsheet

    text = (
        "OOccttoobbeerr 3300,, 22002266\nThe Nifty 50 index ...\n"
        "Healthcare 4.68 Top constituents by weightage\nMetals & Mining 4.45\n"
        "Company's Name Weight(%)\nConstruction 4.25\nConsumer Services 3.28 A Ltd 9.95\n"
        "B Ltd 7.30\nCapital Goods 1.24 C Ltd 4.25\n"
    )
    day, w = parse_factsheet(text, {"A Ltd": "AAA", "B Ltd": "BBB", "C Ltd": "CCC", "Z Ltd": "Z"})
    assert day == date(2026, 10, 30)
    assert w == {"AAA": 9.95, "BBB": 7.30, "CCC": 4.25}  # sector rows are not read as members
