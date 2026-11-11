from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from engine.core.clock import NS_PER_S, ManualClock, MarketSession, to_ns
from engine.core.rules import IST
from engine.data.adapters.base import Depth, FeedStatus, Quote, StatusKind
from engine.data.quality.checks import (
    Flag,
    FlagThresholds,
    describe_flags,
    qc_day,
    render_markdown,
    row_flags,
    write_qc,
)
from engine.data.recorder.core import Recorder, RecorderConfig
from engine.data.reference.instrument_master import InstrumentMaster, Kind
from engine.data.schema import EVENTS_SCHEMA, QUOTES_SCHEMA
from engine.data.store import DatasetWriter, Store, compact_day
from tests.data.conftest import DAY, WEEKLY, opt_symbol

TH = FlagThresholds(stale_s=60, lag_s=5, ahead_s=1)
T0 = to_ns(datetime(2026, 11, 11, 10, 0, tzinfo=IST))
SESSION = MarketSession(
    "NSE", "fo", DAY, time(9, 15), time(15, 40), time(9, 0), time(9, 8), verified=True
)


def q(sym: str = "X", recv: int = T0, **kw: object) -> Quote:
    return Quote(sym, recv, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------ row flags


def test_row_flags_book_conditions() -> None:
    ok = q(exch_ts=T0 // NS_PER_S, ltp=10, bid=9.9, ask=10.1)
    assert row_flags("CE", ok, None, T0, depth_expected=False, th=TH) == 0
    crossed = q(bid=10.2, ask=10.1)
    assert row_flags("CE", crossed, None, T0, depth_expected=False, th=TH) & Flag.CROSSED
    locked = q(bid=10.0, ask=10.0)
    assert row_flags("CE", locked, None, T0, depth_expected=False, th=TH) & Flag.LOCKED
    zero = row_flags("PE", q(bid=0, ask=5), None, T0, depth_expected=True, th=TH)
    assert describe_flags(zero) == ["ZERO_BID", "NO_DEPTH"]
    assert row_flags("CE", None, None, T0, depth_expected=False, th=TH) & Flag.NO_QUOTE


def test_row_flags_time_conditions() -> None:
    stale = row_flags("CE", q(bid=1, ask=2), None, T0 + 61 * NS_PER_S, depth_expected=False, th=TH)
    assert stale & Flag.STALE
    lag = q(exch_ts=T0 // NS_PER_S - 6, bid=1, ask=2)
    assert row_flags("CE", lag, None, T0, depth_expected=False, th=TH) & Flag.LAG_HIGH
    ahead = q(exch_ts=T0 // NS_PER_S + 2, bid=1, ask=2)
    assert row_flags("CE", ahead, None, T0, depth_expected=False, th=TH) & Flag.EXCH_TS_AHEAD
    band = q(ltp=50, lower_ckt=1, upper_ckt=40, bid=49, ask=51)
    assert row_flags("CE", band, None, T0, depth_expected=False, th=TH) & Flag.LTP_OUTSIDE_BAND


def test_row_flags_depth_and_index() -> None:
    good = Depth("X", T0, ((10.0, 65, 1), (9.95, 65, 2)), ((10.05, 65, 1), (10.1, 65, 3)))
    assert row_flags("CE", q(bid=0, ask=0), good, T0, depth_expected=True, th=TH) == 0
    bad = Depth("X", T0, ((9.95, 65, 1), (10.0, 65, 2)), ((10.05, 65, 1),))
    assert row_flags("CE", None, bad, T0, depth_expected=True, th=TH) & Flag.DEPTH_DISORDER
    # indices have no book: no ZERO_BID etc.
    assert row_flags("IDX", q(ltp=25000), None, T0, depth_expected=False, th=TH) == 0


# ------------------------------------------------------------------ recorder


def make_recorder(tmp: Path, clock: ManualClock, **cfg: object) -> Recorder:
    return Recorder(
        RecorderConfig(**cfg),  # type: ignore[arg-type]
        quotes=DatasetWriter(tmp, "quotes", QUOTES_SCHEMA),
        events=DatasetWriter(tmp, "recorder_events", EVENTS_SCHEMA),
        session_open=SESSION.open_dt,
        session_close=SESSION.close_dt,
        source="test",
        start_ns=clock.now_ns(),
    )


def subscribe_all(rec: Recorder, master: InstrumentMaster, symbols: list[str], ticks: set[str]):
    rec.set_universe(
        [master.get(s) for s in symbols],
        depth_symbols=set(symbols[2:]),
        tick_symbols=ticks,
        now_ns=rec.open_ns,
        reason="test",
    )


ATM_CE = opt_symbol(WEEKLY, 25000, Kind.CALL)
FAR_PE = opt_symbol(WEEKLY, 24000, Kind.PUT)
SPOT = "NSE:NIFTY50-INDEX"


def test_capture_policy_snapshots_gaps_and_pit(tmp_path: Path, master: InstrumentMaster) -> None:
    clock = ManualClock(datetime(2026, 11, 11, 9, 34, 57, tzinfo=IST))
    rec = make_recorder(tmp_path, clock)
    subscribe_all(rec, master, [SPOT, "NSE:INDIAVIX-INDEX", ATM_CE, FAR_PE], {SPOT, ATM_CE})
    rec.on_event(FeedStatus(StatusKind.CONNECTED, clock.now_ns(), "ok"))

    def tick(sym: str, px: float) -> None:
        exch = clock.now_ns() // NS_PER_S
        if sym == SPOT:
            rec.on_event(Quote(sym, clock.now_ns(), exch_ts=exch, ltp=px))
        else:
            rec.on_event(
                Quote(sym, clock.now_ns(), exch_ts=exch, ltp=px, bid=px - 0.5, ask=px + 0.5)
            )
        rec.on_timer(clock.now_ns())

    # 09:34:57-59 is inside the 09:00-09:35 tick window: every message is recorded
    for _ in range(3):
        tick(FAR_PE, 2.0)
        clock.advance(1)
    assert rec.stats.tick_rows == 3
    # from 09:35 the far strike is kept only by snapshots; tick-set symbols on every message
    for _ in range(4):
        tick(FAR_PE, 2.0)
        tick(ATM_CE, 120.0)
        tick(SPOT, 25010.0)
        clock.advance(1)
    assert rec.stats.tick_rows == 3 + 8
    # 09:35:03 -> 09:35:12 silence inside the session is a gap
    clock.advance(8)
    tick(SPOT, 25011.0)
    assert rec.stats.gaps == 1 and rec.stats.max_gap_s == pytest.approx(9.0)
    clock.set(datetime(2026, 11, 11, 9, 37, 0, 500_000, tzinfo=IST))
    rec.on_timer(clock.now_ns())
    rec.close(clock.now_ns())

    st = Store(tmp_path)
    end_of_day = datetime(2026, 11, 11, 23, 0, tzinfo=IST)
    snaps = st.quotes(DAY, as_of=end_of_day, capture="snap")
    times = sorted({t.astimezone(IST).strftime("%H:%M") for t in snaps.column("ts").to_pylist()})
    assert times == ["09:35", "09:36", "09:37"] and snaps.num_rows == 3 * 4
    # point-in-time: at 09:35:02.5 we know exactly the ticks received up to then
    as_of = datetime(2026, 11, 11, 9, 35, 2, 500_000, tzinfo=IST)
    start = datetime(2026, 11, 11, 9, 35, tzinfo=IST)
    known = st.quotes(DAY, as_of=as_of, start=start, capture="tick")
    assert all(t <= as_of for t in known.column("ts").to_pylist())
    assert known.num_rows == 3 * 2  # ATM_CE + SPOT at :00, :01, :02
    chain = st.latest(DAY, as_of=datetime(2026, 11, 11, 9, 36, 30, tzinfo=IST), expiry=WEEKLY)
    assert set(chain.column("symbol").to_pylist()) == {ATM_CE, FAR_PE}
    ev = st.events(DAY).to_pylist()
    assert [e["event"] for e in ev].count("gap") == 1
    assert {e["event"] for e in ev} >= {"subscribe", "feed_status", "session"}


def test_watchdog_restart_once_per_silence(tmp_path: Path, master: InstrumentMaster) -> None:
    clock = ManualClock(datetime(2026, 11, 11, 11, 0, 0, tzinfo=IST))
    rec = make_recorder(tmp_path, clock, watchdog_silence_s=20.0)
    subscribe_all(rec, master, [SPOT, "NSE:INDIAVIX-INDEX", ATM_CE], set())
    rec.on_event(Quote(SPOT, clock.now_ns(), ltp=25000.0))
    clock.advance(10)
    assert rec.on_timer(clock.now_ns()) is False
    clock.advance(11)
    assert rec.on_timer(clock.now_ns()) is True
    clock.advance(5)
    assert rec.on_timer(clock.now_ns()) is False  # no restart storm
    rec.on_event(FeedStatus(StatusKind.CONNECTED, clock.now_ns(), ""))
    rec.on_event(FeedStatus(StatusKind.CONNECTED, clock.now_ns(), ""))
    assert rec.conn_id == 2 and rec.stats.reconnects == 1
    rec.on_event(Quote(SPOT, clock.now_ns(), ltp=25001.0))
    clock.advance(25)
    assert rec.on_timer(clock.now_ns()) is True
    # after the close there is no watchdog
    clock.set(datetime(2026, 11, 11, 15, 41, tzinfo=IST))
    assert rec.on_timer(clock.now_ns()) is False


def test_unknown_symbols_and_depth_rows(tmp_path: Path, master: InstrumentMaster) -> None:
    clock = ManualClock(datetime(2026, 11, 11, 9, 20, 0, tzinfo=IST))
    rec = make_recorder(tmp_path, clock)
    subscribe_all(rec, master, [SPOT, "NSE:INDIAVIX-INDEX", ATM_CE], set())
    rec.on_event(Quote("NSE:NOT-SUBSCRIBED", clock.now_ns(), ltp=1.0))
    assert rec.stats.unknown_symbol_messages == 1
    d = Depth(ATM_CE, clock.now_ns(), ((119.5, 650, 3),), ((120.5, 130, 1),))
    rec.on_event(d)
    rec.close(clock.now_ns())
    rows = Store(tmp_path).quotes(DAY, as_of=datetime(2026, 11, 12, tzinfo=IST)).to_pylist()
    row = next(r for r in rows if r["msg"] == "depth")
    assert (row["bid_px_1"], row["bid_qty_1"], row["bid_ord_1"]) == (119.5, 650, 3)
    assert row["ask_px_2"] is None and row["quote_recv_ts"] is None
    assert row["qc_flags"] & Flag.NO_QUOTE


def test_compaction_keeps_every_row(tmp_path: Path, master: InstrumentMaster) -> None:
    clock = ManualClock(datetime(2026, 11, 11, 9, 20, 0, tzinfo=IST))
    rec = make_recorder(tmp_path, clock, flush_interval_s=1.0)
    subscribe_all(rec, master, [SPOT, "NSE:INDIAVIX-INDEX", ATM_CE], set())
    for _ in range(5):
        rec.on_event(Quote(ATM_CE, clock.now_ns(), ltp=100.0, bid=99.0, ask=101.0))
        clock.advance(2)
        rec.on_timer(clock.now_ns())
    rec.close(clock.now_ns())
    part_dir = tmp_path / "quotes" / f"date={DAY.isoformat()}"
    before = sum(pq.read_metadata(p).num_rows for p in part_dir.glob("*.parquet"))
    assert len(list(part_dir.glob("*.parquet"))) > 1
    out = compact_day(tmp_path, "quotes", DAY, QUOTES_SCHEMA)
    assert out is not None and [p.name for p in part_dir.glob("*.parquet")] == [out.name]
    assert pq.read_metadata(out).num_rows == before
    assert compact_day(tmp_path, "quotes", date(2026, 1, 1), QUOTES_SCHEMA) is None


def test_store_requires_aware_as_of(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Store(tmp_path).quotes(DAY, as_of=datetime(2026, 11, 11, 10, 0))  # noqa: DTZ001


# ------------------------------------------------------------------ day QC


def test_qc_day_report(tmp_path: Path, master: InstrumentMaster) -> None:
    """A short synthetic day: full snapshots, one 7 s gap, narrow chain, no OI."""
    session = MarketSession("NSE", "fo", DAY, time(9, 15), time(9, 20), None, None, verified=True)
    clock = ManualClock(datetime(2026, 11, 11, 9, 15, 0, tzinfo=IST))
    rec = Recorder(
        RecorderConfig(record_all_ticks=True),
        quotes=DatasetWriter(tmp_path, "quotes", QUOTES_SCHEMA),
        events=DatasetWriter(tmp_path, "recorder_events", EVENTS_SCHEMA),
        session_open=session.open_dt,
        session_close=session.close_dt,
        source="test",
        start_ns=clock.now_ns() - 1,
    )
    syms = [SPOT, "NSE:INDIAVIX-INDEX"] + [
        opt_symbol(WEEKLY, k, kd) for k in (24950, 25000, 25050) for kd in (Kind.CALL, Kind.PUT)
    ]
    subscribe_all(rec, master, syms, set())
    rec.on_timer(clock.now_ns())  # 09:15:00 snapshot
    t_end = to_ns(session.close_dt)
    while clock.now_ns() < t_end:
        if not (
            to_ns(datetime(2026, 11, 11, 9, 17, 0, tzinfo=IST))
            < clock.now_ns()
            < to_ns(datetime(2026, 11, 11, 9, 17, 7, tzinfo=IST))
        ):
            exch = clock.now_ns() // NS_PER_S
            rec.on_event(Quote(SPOT, clock.now_ns(), exch_ts=exch, ltp=25010.0))
            for s in syms[2:]:
                rec.on_event(Quote(s, clock.now_ns(), exch_ts=exch, ltp=50.0, bid=49.0, ask=51.0))
        clock.advance(1)
        rec.on_timer(clock.now_ns())
    rec.close(clock.now_ns())

    qc = qc_day(Store(tmp_path), DAY, session, required_strikes_each_side=2)
    assert qc.snapshot_minutes_expected == 5 and not qc.snapshot_minutes_missing
    assert qc.max_gap_s == pytest.approx(7.0)
    assert len(qc.gaps) == 1  # tick scan and recorder log the same gap once
    cov = qc.strike_coverage[f"NIFTY {WEEKLY.isoformat()}"]
    assert cov["min_below"] == 2 and cov["min_above"] == 1  # 25010: 24950,25000 | 25050
    assert not qc.oi_present
    assert not qc.passed
    assert any("gap" in p for p in qc.problems)
    assert any("open interest" in p for p in qc.problems)
    assert any("strikes below/above" in p for p in qc.problems)
    md = render_markdown(qc)
    assert "FAIL" in md and "09:17:00" in md  # times shown in IST
    j, m = write_qc(qc, tmp_path / "qc")
    assert j.exists() and m.exists()


def test_qc_day_without_data(tmp_path: Path) -> None:
    qc = qc_day(Store(tmp_path), DAY, SESSION)
    assert not qc.passed and qc.problems == ["no quotes recorded"]
