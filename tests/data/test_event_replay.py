"""Event replay: fills on the recorded book after latency, never at LTP; 2x slippage; round trip
from the recorder's Parquet files back to events."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time
from pathlib import Path

import pytest

from engine.backtest.event_replay import (
    ReplayConfig,
    ReplayContext,
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


