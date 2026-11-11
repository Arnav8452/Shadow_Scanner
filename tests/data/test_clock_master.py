from __future__ import annotations

import gzip
import json
from datetime import date, datetime, time
from pathlib import Path

import pytest

from engine.core.clock import (
    ManualClock,
    from_ns,
    market_session,
    recording_window,
    to_ns,
)
from engine.core.rules import IST, RuleBook, UnverifiedRuleError
from engine.data.reference.instrument_master import (
    InstrumentMaster,
    Kind,
    MasterError,
    classify_expiries,
    fetch_fyers_master,
    latest_snapshot,
    load_fyers_master,
    lot_size_mismatches,
    parse_fyers_master,
)


@pytest.fixture(scope="module")
def book(repo_root: Path) -> RuleBook:
    return RuleBook.load(repo_root / "config" / "rules")


# ------------------------------------------------------------------ clock & sessions


def test_ns_round_trip_and_naive_rejected() -> None:
    dt = datetime(2026, 11, 11, 9, 15, 0, 123456, tzinfo=IST)
    assert from_ns(to_ns(dt)) == dt
    with pytest.raises(ValueError, match="naive"):
        to_ns(datetime(2026, 11, 11, 9, 15))  # noqa: DTZ001
    c = ManualClock(dt)
    c.advance(1.5)
    assert from_ns(c.now_ns()).time() == time(9, 15, 1, 623456)


def test_fo_close_moves_to_1540_on_3_aug_2026(book: RuleBook) -> None:
    before = market_session(book, "NSE", "fo", date(2026, 7, 31))
    after = market_session(book, "NSE", "fo", date(2026, 8, 3))
    assert before.normal_close == time(15, 30)
    assert after.normal_close == time(15, 40)  # NSE/FAOP/74467
    assert after.normal_open == time(9, 15) and after.pre_open_start == time(9, 0)
    assert after.verified
    old = market_session(book, "NSE", "fo", date(2025, 6, 2))
    assert old.pre_open_start is None  # F&O pre-open only from 8 Dec 2025


def test_unverified_sessions_need_opt_in(book: RuleBook) -> None:
    with pytest.raises(UnverifiedRuleError):
        market_session(book, "NSE", "cm", date(2026, 11, 11))
    cm = market_session(book, "NSE", "cm", date(2026, 11, 11), allow_unverified=True)
    assert not cm.verified


def test_recording_window_is_union_plus_buffers(book: RuleBook) -> None:
    day = date(2026, 11, 11)
    fo = market_session(book, "NSE", "fo", day)
    cm = market_session(book, "NSE", "cm", day, allow_unverified=True)
    start, end = recording_window([fo, cm], pre_buffer_min=10, post_buffer_min=5)
    assert start == datetime(2026, 11, 11, 8, 50, tzinfo=IST)
    assert end == datetime(2026, 11, 11, 15, 45, tzinfo=IST)


# ------------------------------------------------------------------ Fyers master parsing

# Rows in the documented Fyers sym_details layout (21 columns, no header).
FO_ROWS = (
    "101126111739278,NIFTY 17 Nov 26 25000 CE,14,65,0.05,,0915-1540|1815-1915:,2026-11-08,"
    "1794910200,NSE:NIFTY26N1725000CE,10,11,39278,NIFTY,26000,25000.0,CE,101000000026000,None,0,0.0\n"
    "101126111739279,NIFTY 17 Nov 26 25000 PE,14,65,0.05,,0915-1540|1815-1915:,2026-11-08,"
    "1794910200,NSE:NIFTY26N1725000PE,10,11,39279,NIFTY,26000,25000.0,PE,101000000026000,None,0,0.0\n"
    "101126112361471,NIFTY 23 Nov 26 FUT,11,65,0.1,,0915-1540|1815-1915:,2026-11-08,"
    "1795428600,NSE:NIFTY26NOVFUT,10,11,61471,NIFTY,26000,-1.0,XX,101000000026000,None,0,0.0\n"
    "12112701281100930,SENSEX 28 Jan 27 FUT ,11,25,0.05,,0915-1540|1815-1915:,2026-11-08,"
    "1801131000,BSE:SENSEX27JANFUT,12,11,1100930,SENSEX,1,-1.0,,12100000001,None,0,0.0\n"
)
CM_ROWS = (
    "101000000026000,NIFTY50-INDEX,10,0,0.05,,0915-1530|1815-1915:,2026-11-09,,"
    "NSE:NIFTY50-INDEX,10,10,26000,NIFTY,26000,-1.0,XX,101000000026000,None,0,0.0\n"
    "10100000002885,RELIANCE INDUSTRIES LTD,0,1,0.1,INE002A01018,0915-1530|1815-1915:,2026-11-09,,"
    "NSE:RELIANCE-EQ,10,10,2885,RELIANCE,2885,-1.0,XX,10100000002885,None,1,3.6\n"
    "10100000099999,SOME BOND,0,1,0.01,,0915-1530|1815-1915:,2026-11-09,,"
    "NSE:SOMEBOND-GB,10,10,99999,SOMEBOND,99999,-1.0,XX,10100000099999,None,1,0.0\n"
)


def test_parse_fyers_rows() -> None:
    items = {i.symbol: i for i in parse_fyers_master(FO_ROWS + CM_ROWS)}
    assert "NSE:SOMEBOND-GB" not in items  # unknown series skipped
    ce = items["NSE:NIFTY26N1725000CE"]
    assert ce.kind is Kind.CALL and ce.strike == 25000 and ce.lot_size == 65
    assert ce.expiry == date(2026, 11, 17)
    assert ce.expiry_ts is not None and ce.expiry_ts.time() == time(15, 40)  # IST
    assert items["NSE:NIFTY26NOVFUT"].kind is Kind.FUTURE
    assert items["BSE:SENSEX27JANFUT"].exchange == "BSE"  # blank option type on BSE futures
    assert items["NSE:NIFTY50-INDEX"].kind is Kind.INDEX
    assert items["NSE:RELIANCE-EQ"].kind is Kind.EQUITY and items["NSE:RELIANCE-EQ"].segment == "cm"


def test_parse_rejects_malformed_rows() -> None:
    with pytest.raises(MasterError, match="columns"):
        parse_fyers_master("1,2,3\n")
    bad = FO_ROWS.splitlines()[0].replace(",25000.0,CE,", ",0,CE,")
    with pytest.raises(MasterError, match="strike"):
        parse_fyers_master(bad + "\n")


def test_classify_expiries_handles_holiday_shifted_monthly() -> None:
    exp = [date(2026, 11, 17), date(2026, 11, 23), date(2026, 12, 1), date(2026, 12, 29)]
    roles = classify_expiries(
        exp, ["current_weekly", "next_weekly", "current_monthly", "next_monthly"]
    )
    assert roles == {
        "current_weekly": date(2026, 11, 17),
        "next_weekly": date(2026, 11, 23),
        "current_monthly": date(2026, 11, 23),  # Monday: 24 Nov is a holiday
        "next_monthly": date(2026, 12, 29),
    }
    with pytest.raises(ValueError, match="unknown"):
        classify_expiries(exp, ["weekly"])
    assert classify_expiries([], ["current_weekly"]) == {}


def test_lot_size_cross_check(book: RuleBook) -> None:
    master = InstrumentMaster(parse_fyers_master(FO_ROWS), as_of=date(2026, 11, 10), source="t")
    mism = lot_size_mismatches(master, book, ["NIFTY", "SENSEX"])
    # NIFTY 65 matches the primary rule; SENSEX Jan-27 lot 25 matches the new UNVERIFIED row.
    assert mism == []
    changed = FO_ROWS.replace(",14,65,0.05,", ",14,75,0.05,")
    master = InstrumentMaster(parse_fyers_master(changed), as_of=date(2026, 11, 10), source="t")
    mism = lot_size_mismatches(master, book, ["NIFTY"])
    assert {m.master_lot for m in mism} == {75} and all(m.rule_lot == 65 for m in mism)


def test_fetch_snapshot_and_fallback(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake(url: str) -> bytes:
        calls.append(url)
        return FO_ROWS.encode()

    now = datetime(2026, 11, 11, 8, 50, tzinfo=IST)
    p = fetch_fyers_master(
        "NSE_FO", raw_dir=tmp_path, day=date(2026, 11, 11), now=now, fetcher=fake
    )
    assert calls == ["https://public.fyers.in/sym_details/NSE_FO.csv"]
    assert gzip.decompress(p.read_bytes()).decode() == FO_ROWS
    meta = json.loads(p.with_name(p.name + ".meta.json").read_text())
    assert meta["instruments_parsed"] == 4 and meta["fetched_at"].startswith("2026-11-11T08:50")
    # second call the same day does not download again
    fetch_fyers_master("NSE_FO", raw_dir=tmp_path, day=date(2026, 11, 11), now=now, fetcher=fake)
    assert len(calls) == 1
    assert latest_snapshot(tmp_path, "fyers", "NSE_FO", date(2026, 11, 12)) == p
    assert latest_snapshot(tmp_path, "fyers", "NSE_FO", date(2026, 11, 10)) is None
    m = load_fyers_master([p], as_of=date(2026, 11, 11))
    assert m.option_expiries("NIFTY", date(2026, 11, 11)) == [date(2026, 11, 17)]
    with pytest.raises(ValueError, match="unsupported"):
        fetch_fyers_master("MCX_COM", raw_dir=tmp_path, day=date(2026, 11, 11), now=now)
