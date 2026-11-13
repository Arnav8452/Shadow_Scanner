"""Event replay: fills on the recorded book after latency, never at LTP; 2x slippage; round trip
from the recorder's Parquet files back to events."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from engine.backtest.event_replay import (
    ReplayConfig,
    ReplayContext,
    _in_d30_window,
    _ns,
    recorded_events,
    replay_day,
    replay_with_stress,
)
from engine.core.clock import NS_PER_S, ManualClock, MarketSession, to_ns
from engine.core.rules import IST, RuleBook
from engine.data.adapters.base import Depth, Quote
from engine.data.recorder.core import Recorder, RecorderConfig
from engine.data.reference.instrument_master import InstrumentMaster, Kind
from engine.data.schema import DEPTH30_SCHEMA, EVENTS_SCHEMA, QUOTES_SCHEMA
from engine.data.store import DatasetWriter, Store, ns_to_us
from engine.execution.order_fsm import OrderRecord
from tests.data.conftest import DAY, WEEKLY, opt_symbol

SYM = "NSE:NIFTY26N1725000CE"
T0 = to_ns(datetime(2026, 11, 11, 10, 0, tzinfo=IST))


def book_at(ms: int, bid: float) -> Depth:
    return Depth(SYM, T0 + ms * 1_000_000, ((bid, 650, 3), (bid - 0.5, 650, 2)), ((bid + 1, 650, 3), (bid + 1.5, 650, 2)))


EVENTS = [
    Quote(SYM, T0, ltp=100.5),
    book_at(0, 100.0),  # strategy decides to buy here (ask 101)
    book_at(100, 105.0),  # moves before the order arrives
    book_at(300, 110.0),  # order lands after 250 ms: fills at ask 111
    book_at(1000, 115.0),  # strategy decides to sell here
    book_at(1500, 120.0),  # sell lands: fills at bid 120
]


class BuyThenSell:
    def __init__(self) -> None:
        self.n = 0

    def on_event(self, ev: Quote | Depth, ctx: ReplayContext, positions: dict[str, int]) -> Sequence[OrderRecord]:
        if not isinstance(ev, Depth):
            return []
        self.n += 1
        if self.n == 1:
            return [OrderRecord("B1", SYM, "BUY", "MARKET_PROTECTED", 65, ev.asks[0][0], protection_pct=20.0)]
        if self.n == 4 and positions.get(SYM, 0) > 0:
            return [OrderRecord("S1", SYM, "SELL", "MARKET_PROTECTED", 65, ev.bids[0][0], protection_pct=20.0)]
        return []


@pytest.fixture(scope="module")
def rules(repo_root: Path) -> RuleBook:
    return RuleBook.load(repo_root / "config" / "rules")


def test_fills_use_book_after_latency_not_ltp(rules: RuleBook) -> None:
    res = replay_day(iter(EVENTS), BuyThenSell(), book=rules, trade_date=DAY, cfg=ReplayConfig(latency_ms=250))
    buy, sell = res.fills
    assert buy.order.avg_fill_price == 111.0  # not 100.5 (LTP) nor 101 (ask when decided)
    assert sell.order.avg_fill_price == 120.0
    assert res.open_positions == {} and res.events == len(EVENTS)
    gross = (120.0 - 111.0) * 65
    assert res.charges_inr > 0
    assert res.net_realized_pnl_inr == pytest.approx(gross - res.charges_inr, abs=0.01)


def test_protection_band_rejects_a_runaway_market(rules: RuleBook) -> None:
    class Tight(BuyThenSell):
        def on_event(self, ev, ctx, positions):  # type: ignore[no-untyped-def]
            orders = super().on_event(ev, ctx, positions)
            return [OrderRecord(o.order_id, o.symbol, o.side, o.order_type, o.qty, o.limit_price, protection_pct=1.0) for o in orders]

    res = replay_day(iter(EVENTS), Tight(), book=rules, trade_date=DAY)
    assert not res.fills and len(res.rejected) == 1  # ask 111 > 101 * 1.01: no fill


def test_unfilled_orders_at_end_are_reported(rules: RuleBook) -> None:
    res = replay_day(iter(EVENTS[:2]), BuyThenSell(), book=rules, trade_date=DAY)
    assert not res.fills and [o.order_id for o in res.unfilled] == ["B1"]


def test_double_slippage_costs_more(rules: RuleBook) -> None:
    base, stressed = replay_with_stress(lambda: iter(EVENTS), BuyThenSell, book=rules, trade_date=DAY)
    assert stressed.fills[0].order.avg_fill_price == pytest.approx(111.5)  # mid 110.5 + 2 x 0.5
    assert stressed.fills[1].order.avg_fill_price == pytest.approx(119.5)
    assert stressed.net_realized_pnl_inr < base.net_realized_pnl_inr
    assert stressed.config.slippage_mult == 2.0


def test_recorded_parquet_round_trips_to_events(tmp_path: Path, master: InstrumentMaster) -> None:
    session = MarketSession("NSE", "fo", DAY, time(9, 15), time(15, 40), None, None, True)
    clock = ManualClock(datetime(2026, 11, 11, 10, 0, tzinfo=IST))
    rec = Recorder(
        RecorderConfig(record_all_ticks=True),
        quotes=DatasetWriter(tmp_path, "quotes", QUOTES_SCHEMA),
        events=DatasetWriter(tmp_path, "recorder_events", EVENTS_SCHEMA),
        session_open=session.open_dt,
        session_close=session.close_dt,
        source="test",
        start_ns=clock.now_ns(),
    )
    sym = opt_symbol(WEEKLY, 25000, Kind.CALL)
    rec.set_universe([master.get(sym)], depth_symbols={sym}, tick_symbols={sym}, now_ns=clock.now_ns(), reason="t")
    rec.on_event(Quote(sym, clock.now_ns(), ltp=120.0, bid=119.5, ask=120.5, oi=1000))
    clock.advance(1)
    rec.on_event(Depth(sym, clock.now_ns(), ((119.5, 650, 3),), ((120.5, 130, 1),)))
    rec.close(clock.now_ns())
    evs = list(recorded_events(Store(tmp_path), DAY, as_of=datetime(2026, 11, 11, 23, 0, tzinfo=IST)))
    assert [type(e).__name__ for e in evs] == ["Quote", "Depth"]
    q, d = evs
    assert isinstance(q, Quote) and q.ltp == 120.0 and q.oi == 1000
    assert isinstance(d, Depth) and d.bids[0][:2] == (119.5, 650) and d.recv_ns - q.recv_ns == NS_PER_S
    # point in time: nothing known before the first message
    assert not list(recorded_events(Store(tmp_path), DAY, as_of=datetime(2026, 11, 11, 9, 59, tzinfo=IST)))


def test_recorded_depth30_supersedes_5_level_book_and_walks_deep_levels(
    tmp_path: Path, master: InstrumentMaster, rules: RuleBook
) -> None:
    sym = opt_symbol(WEEKLY, 25000, Kind.CALL)
    session = MarketSession("NSE", "fo", DAY, time(9, 15), time(15, 40), None, None, True)
    clock = ManualClock(datetime(2026, 11, 11, 10, 0, tzinfo=IST))
    rec = Recorder(
        RecorderConfig(record_all_ticks=True),
        quotes=DatasetWriter(tmp_path, "quotes", QUOTES_SCHEMA),
        events=DatasetWriter(tmp_path, "recorder_events", EVENTS_SCHEMA),
        session_open=session.open_dt,
        session_close=session.close_dt,
        source="test",
        start_ns=clock.now_ns(),
    )
    rec.set_universe(
        [master.get(sym)], depth_symbols={sym}, tick_symbols={sym}, now_ns=clock.now_ns(), reason="t"
    )
    # 5-level depth recorded in quotes at t=0 (inside d30 window -> suppressed) and at t=+300s (after watchlist rotation -> falls back to 5-level book)
    rec.on_event(Quote(sym, clock.now_ns(), ltp=100.5, bid=100.0, ask=101.0, oi=1000))
    rec.on_event(Depth(sym, clock.now_ns(), ((100.0, 650, 3),), ((101.0, 650, 3),)))
    clock.advance(300)
    rec.on_event(Depth(sym, clock.now_ns(), ((102.0, 650, 3),), ((103.0, 650, 3),)))
    rec.close(clock.now_ns())

    # 10-level depth recorded in depth30 at t=0, t=300ms (deep top-5 for entry), and t=1000ms/1300ms (thin per-level for 7-lot exit)
    d30_writer = DatasetWriter(tmp_path, "depth30", DEPTH30_SCHEMA)
    ask_px = [100.5 + 0.5 * i for i in range(1, 11)]  # 101.0 .. 105.5
    bid_px = [100.5 - 0.5 * i for i in range(1, 11)]  # 100.0 .. 95.5
    deep_qty = [650] * 10
    thin_qty = [65] * 10  # 1 lot per level -> 7 lots walk levels 1..7 (100.0 .. 97.0, avg 98.5)
    for idx, (ms, q_vec) in enumerate(
        ((0, deep_qty), (300, deep_qty), (1000, thin_qty), (1300, thin_qty))
    ):
        d30_writer.append(
            {
                "ts": ns_to_us(T0 + ms * 1_000_000),
                "symbol": sym,
                "underlying": "NIFTY",
                "kind": "CE",
                "expiry": WEEKLY,
                "strike": 25000.0,
                "exch_ts": None,
                "levels": 10,
                "bid_px": bid_px,
                "bid_qty": q_vec,
                "ask_px": ask_px,
                "ask_qty": q_vec,
                "seq": idx + 1,
                "source": "upstox_d30",
            }
        )
    d30_writer.flush()

    evs = list(
        recorded_events(Store(tmp_path), DAY, as_of=datetime(2026, 11, 11, 23, 0, tzinfo=IST))
    )
    depth_evs = [e for e in evs if isinstance(e, Depth)]
    # 5-level depth at t=0 was superseded by the four 10-level depth30 snapshots; 5-level depth at t=+300s (outside d30 window) is preserved
    assert [len(d.bids) for d in depth_evs] == [10, 10, 10, 10, 1]
    assert depth_evs[-1].bids[0][:2] == (102.0, 650)

    class BuySevenThenExitDeep:
        def __init__(self) -> None:
            self.step = 0

        def on_event(
            self, ev: Quote | Depth, ctx: ReplayContext, positions: dict[str, int]
        ) -> Sequence[OrderRecord]:
            if not isinstance(ev, Depth):
                return []
            self.step += 1
            if self.step == 1:
                return [
                    OrderRecord(
                        "B7", sym, "BUY", "MARKET_PROTECTED", 65 * 7, ev.asks[0][0], protection_pct=20.0
                    )
                ]
            if self.step == 3 and positions.get(sym, 0) > 0:
                return [
                    OrderRecord(
                        "S7", sym, "SELL", "MARKET_PROTECTED", 65 * 7, ev.bids[0][0], protection_pct=20.0
                    )
                ]
            return []

    res = replay_day(
        iter(evs), BuySevenThenExitDeep(), book=rules, trade_date=DAY, cfg=ReplayConfig(latency_ms=250)
    )
    assert len(res.fills) == 2
    buy, sell = res.fills
    assert buy.order.avg_fill_price == pytest.approx(101.0)
    # 7-lot exit walks levels 1..7 of the 10-level bid book (100.0, 99.5, 99.0, 98.5, 98.0, 97.5, 97.0 -> avg 98.5)
    assert sell.order.filled_qty == 65 * 7
    assert sell.order.avg_fill_price == pytest.approx(98.5)




# --- windowed streaming: same events as reading the whole day at once ---------------------------


def _whole_day_events(
    store: Store,
    day: date,
    *,
    as_of: datetime,
    start: datetime | None = None,
    symbols: Sequence[str] | None = None,
    include_snapshots: bool = False,
    include_depth30: bool = True,
    depth30_hold_s: float = 60.0,
) -> list[Quote | Depth]:
    """The pre-streaming implementation (whole day via to_pylist, one sort), kept as an oracle."""
    capture = None if include_snapshots else "tick"
    q_table = store.quotes(day, as_of=as_of, start=start, symbols=symbols, capture=capture)
    d30_table = store.depth30(day, as_of=as_of, start=start, symbols=symbols) if include_depth30 else None
    q_rows = q_table.to_pylist() if q_table.num_rows > 0 else []
    d30_rows = d30_table.to_pylist() if d30_table is not None and d30_table.num_rows > 0 else []
    hold_ns = int(depth30_hold_s * NS_PER_S)
    d30_times: dict[str, list[int]] = {}
    for r in d30_rows:
        t_ns = _ns(r["ts"])
        assert t_ns is not None
        d30_times.setdefault(str(r["symbol"]), []).append(t_ns)
    for times in d30_times.values():
        times.sort()
    timed: list[tuple[int, int, Quote | Depth]] = []
    idx = 0
    for r in q_rows:
        recv = _ns(r["ts"])
        assert recv is not None
        sym = r["symbol"]
        if r["msg"] in ("quote", "snap") and r.get("quote_recv_ts") is not None:
            idx += 1
            timed.append(
                (recv, idx, Quote(sym, recv, ltp=r["ltp"], oi=r["oi"], bid=r["bid"], bid_qty=r["bid_qty"],
                                  ask=r["ask"], ask_qty=r["ask_qty"], volume=r["volume"]))
            )  # fmt: skip
        if (
            not _in_d30_window(recv, d30_times.get(str(sym), ()), hold_ns)
            and r["msg"] in ("depth", "snap")
            and r.get("depth_recv_ts") is not None
        ):
            bids = tuple(
                (r[f"bid_px_{i}"], r[f"bid_qty_{i}"], r[f"bid_ord_{i}"])
                for i in range(1, 6)
                if r[f"bid_px_{i}"] and r[f"bid_qty_{i}"]
            )
            asks = tuple(
                (r[f"ask_px_{i}"], r[f"ask_qty_{i}"], r[f"ask_ord_{i}"])
                for i in range(1, 6)
                if r[f"ask_px_{i}"] and r[f"ask_qty_{i}"]
            )
            idx += 1
            timed.append((recv, idx, Depth(sym, recv, bids, asks)))
    for r in d30_rows:
        recv = _ns(r["ts"])
        assert recv is not None
        bids30 = tuple((float(p), int(q), None) for p, q in zip(r["bid_px"], r["bid_qty"], strict=False) if p and q)
        asks30 = tuple((float(p), int(q), None) for p, q in zip(r["ask_px"], r["ask_qty"], strict=False) if p and q)
        idx += 1
        timed.append((recv, idx, Depth(r["symbol"], recv, bids30, asks30)))
    timed.sort(key=lambda item: (item[0], item[1]))
    return [ev for _, _, ev in timed]


D30_SYM = "NSE:NIFTY26N1725000CE"  # enters the 30-level watchlist mid-day and rotates out
PLAIN_SYM = "NSE:NIFTY26N1725100CE"  # 5-level book only
SNAP_SYM = "NSE:NIFTY26N1724900PE"  # slow contract: snapshot rows only


def _quote_row(ms: int, sym: str, msg: str, seq: int, *, capture: str = "tick", px: float = 100.0) -> dict[str, object]:
    ts = ns_to_us(T0 + ms * 1_000_000)
    row: dict[str, object] = {
        "ts": ts, "capture": capture, "msg": msg, "symbol": sym, "underlying": "NIFTY", "kind": "CE",
        "expiry": WEEKLY, "strike": 25000.0,
        "quote_recv_ts": ts if msg in ("quote", "snap") else None,
        "depth_recv_ts": ts if msg in ("depth", "snap") else None,
        "ltp": px + 0.5, "oi": 1000 + seq, "volume": seq, "bid": px, "bid_qty": 650, "ask": px + 1, "ask_qty": 650,
        "qc_flags": 0, "conn_id": 1, "seq": seq, "source": "test",
    }  # fmt: skip
    for i in range(1, 6):
        row |= {f"bid_px_{i}": px - 0.5 * (i - 1), f"bid_qty_{i}": 65 * i, f"bid_ord_{i}": i,
                f"ask_px_{i}": px + 1 + 0.5 * (i - 1), f"ask_qty_{i}": 65 * i, f"ask_ord_{i}": i}  # fmt: skip
    return row


def _d30_row(ms: int, seq: int, *, px: float = 100.0) -> dict[str, object]:
    return {
        "ts": ns_to_us(T0 + ms * 1_000_000), "symbol": D30_SYM, "underlying": "NIFTY", "kind": "CE",
        "expiry": WEEKLY, "strike": 25000.0, "exch_ts": None, "levels": 10,
        "bid_px": [px - 0.5 * i for i in range(10)], "bid_qty": [650] * 10,
        "ask_px": [px + 1 + 0.5 * i for i in range(10)], "ask_qty": [650] * 10,
        "seq": seq, "source": "upstox_d30",
    }  # fmt: skip


@pytest.fixture
def multi_window_day(tmp_path: Path) -> Path:
    """~7 minutes of quotes for three contracts plus a 30-level episode, written in several
    Parquet parts, with rows placed on and around 60 s window edges and a 2 h silent gap."""
    quotes = DatasetWriter(tmp_path, "quotes", QUOTES_SCHEMA)
    seq = 0
    edges = [59_800, 60_000, 119_999, 120_000, 239_000, 250_000, 261_000, 299_600, 300_000]
    times_ms = [*range(0, 420_000, 3_700), *edges, 7_200_000, 7_200_000, 7_260_500]
    for k, ms in enumerate(sorted(times_ms)):
        for sym in (D30_SYM, PLAIN_SYM):
            seq += 1
            quotes.append(_quote_row(ms, sym, ("quote", "depth", "snap")[k % 3], seq, px=100 + k % 7))
        if k % 4 == 0:
            seq += 1
            quotes.append(_quote_row(ms, SNAP_SYM, "snap", seq, capture="snap", px=50 + k % 5))
        if k % 25 == 24:
            quotes.flush()  # several parts per day, as the live recorder writes them
    quotes.flush()
    d30 = DatasetWriter(tmp_path, "depth30", DEPTH30_SCHEMA)
    d30_ms = [60_300, *range(100_000, 200_001, 5_000), 299_500, 300_500, 7_200_000]
    for k, ms in enumerate(d30_ms):
        d30.append(_d30_row(ms, k + 1, px=101 + k % 3))
    d30.flush()
    return tmp_path


LATE = datetime(2026, 11, 11, 23, 0, tzinfo=IST)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"include_snapshots": True},
        {"include_depth30": False},
        {"symbols": [D30_SYM]},
        {"start": datetime(2026, 11, 11, 10, 1, 30, tzinfo=IST)},  # mid-window, mid-episode
        {"as_of": datetime(2026, 11, 11, 10, 4, 59, 600_000, tzinfo=IST)},  # 0.4 s before a d30 row
        {"depth30_hold_s": 15.0},
    ],
)
@pytest.mark.parametrize("window_s", [7.0, 60.0, 61.3, 86_400.0])
def test_windowed_replay_matches_whole_day_read(
    multi_window_day: Path, kwargs: dict[str, object], window_s: float
) -> None:
    args: dict[str, object] = {"as_of": LATE} | kwargs
    expected = _whole_day_events(Store(multi_window_day), DAY, **args)  # type: ignore[arg-type]
    got = list(recorded_events(Store(multi_window_day), DAY, window_s=window_s, **args))  # type: ignore[arg-type]
    assert len(expected) > 100
    assert [(type(e).__name__, e.symbol, e.recv_ns) for e in got] == [
        (type(e).__name__, e.symbol, e.recv_ns) for e in expected
    ]
    for g, e in zip(got, expected, strict=True):
        if isinstance(e, Depth):
            assert isinstance(g, Depth) and (g.bids, g.asks) == (e.bids, e.asks)
        else:
            assert isinstance(g, Quote)
            assert (g.ltp, g.oi, g.bid, g.ask, g.volume) == (e.ltp, e.oi, e.bid, e.ask, e.volume)


def test_depth30_supersedes_5_level_books_across_window_edges(multi_window_day: Path) -> None:
    evs = list(recorded_events(Store(multi_window_day), DAY, as_of=LATE, window_s=60.0))
    five = {e.recv_ns - T0 for e in evs if isinstance(e, Depth) and e.symbol == D30_SYM and len(e.bids) == 5}
    ms = 1_000_000
    assert 59_800 * ms not in five  # a 30-level book lands 0.5 s later, in the next window
    assert 250_000 * ms not in five  # 50 s after the last 30-level book, held in the previous window
    assert 261_000 * ms in five  # 61 s after it: the symbol rotated out, back to the 5-level book
    assert 299_600 * ms not in five  # 30-level book 0.1 s before and 0.9 s after a window edge


class _CountingStore(Store):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.quote_reads = 0

    def quotes(self, *args: Any, **kwargs: Any) -> pa.Table:
        self.quote_reads += 1
        return super().quotes(*args, **kwargs)


def test_first_events_come_from_one_window_read(multi_window_day: Path) -> None:
    store = _CountingStore(multi_window_day)
    it = recorded_events(store, DAY, as_of=LATE, window_s=60.0)
    first = next(it)
    assert first.recv_ns == T0 and store.quote_reads == 1  # not the whole day up front
    rest = list(it)
    # 7 windows of the first part of the day, then one jump over the 2 h silence (not ~120 reads)
    assert store.quote_reads < 15 and rest[-1].recv_ns == T0 + 7_260_500 * 1_000_000
