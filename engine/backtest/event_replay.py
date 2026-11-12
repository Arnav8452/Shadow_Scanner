"""Event replay of recorded days (M6): honest fills on the recorded order book.

* Events come from the recorder's ``quotes`` dataset in known-at order (``ts``, ``seq``): tick rows
  replay every message received; snapshot rows can be included for slow contracts.
* A strategy sees one event at a time and may return orders. Each order reaches the "exchange"
  ``latency_ms`` later and is filled by :class:`PaperBroker` against the book **as recorded at
  that later moment** — never at LTP, never with the book the strategy saw (no lookahead).
* ``slippage_mult`` stretches every level's distance from the mid before the depth walk, so
  ``2.0`` doubles each fill's slippage versus mid (CLAUDE.md: rerun at 2x slippage).
* Orders still pending at the end of the window are reported as unfilled, not forced.

It uses the same PaperBroker as paper trading, so a replay of a recorded day reproduces what the
paper engine would have done on that day by construction (SPEC §13 M6).
"""

from __future__ import annotations

import bisect
import heapq
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal, Protocol

import pyarrow as pa

from engine.core.clock import NS_PER_S, from_ns
from engine.core.rules import RuleBook
from engine.data.adapters.base import Depth, Level, Quote
from engine.data.schema import DEPTH_LEVELS
from engine.data.store import Store
from engine.execution.order_fsm import OrderRecord, OrderState
from engine.execution.paper_broker import PaperBroker, PaperFillResult

US = 1000  # ns per microsecond
ROW_BATCH = 4096  # rows turned into Python dicts at a time


def _ns(ts: datetime | None) -> int | None:
    if ts is None:
        return None
    return int(ts.timestamp()) * NS_PER_S + ts.microsecond * US


def _in_d30_window(recv_ns: int, d30_times: Sequence[int], hold_ns: int) -> bool:
    """True when ``recv_ns`` falls inside an active 30-level depth window for this symbol."""
    if not d30_times:
        return False
    i = bisect.bisect_right(d30_times, recv_ns + NS_PER_S) - 1
    if i < 0:
        return False
    return recv_ns - d30_times[i] <= hold_ns


def _quote_row_events(
    rows: Iterable[dict[str, Any]], d30_times_by_sym: dict[str, list[int]], hold_ns: int
) -> Iterator[Quote | Depth]:
    """Quote and 5-level Depth events from ``quotes`` rows (already in (ts, seq) order)."""
    for r in rows:
        recv = _ns(r["ts"])
        assert recv is not None
        sym = r["symbol"]
        if r["msg"] in ("quote", "snap") and r.get("quote_recv_ts") is not None:
            exch = r.get("exch_ts")
            ltt = r.get("ltt")
            yield Quote(
                sym,
                recv,
                exch_ts=int(exch.timestamp()) if exch else None,
                ltt=int(ltt.timestamp()) if ltt else None,
                ltp=r["ltp"],
                ltq=r["ltq"],
                atp=r["atp"],
                volume=r["volume"],
                oi=r["oi"],
                tbq=r["tbq"],
                tsq=r["tsq"],
                bid=r["bid"],
                bid_qty=r["bid_qty"],
                ask=r["ask"],
                ask_qty=r["ask_qty"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                prev_close=r["prev_close"],
            )
        if (
            not _in_d30_window(recv, d30_times_by_sym.get(str(sym), ()), hold_ns)
            and r["msg"] in ("depth", "snap")
            and r.get("depth_recv_ts") is not None
        ):
            bids: list[Level] = []
            asks: list[Level] = []
            for i in range(1, DEPTH_LEVELS + 1):
                bp, bq = r.get(f"bid_px_{i}"), r.get(f"bid_qty_{i}")
                ap, aq = r.get(f"ask_px_{i}"), r.get(f"ask_qty_{i}")
                if bp and bq:
                    bids.append((bp, bq, r.get(f"bid_ord_{i}")))
                if ap and aq:
                    asks.append((ap, aq, r.get(f"ask_ord_{i}")))
            yield Depth(sym, recv, tuple(bids), tuple(asks))


def _depth30_row_events(rows: Iterable[dict[str, Any]]) -> Iterator[Depth]:
    """30-level Depth events from ``depth30`` rows (already in (ts, seq) order)."""
    for r in rows:
        recv = _ns(r["ts"])
        assert recv is not None
        exch = r.get("exch_ts")
        bids30 = tuple(
            (float(px), int(qty), None)
            for px, qty in zip(r.get("bid_px") or (), r.get("bid_qty") or (), strict=False)
            if px and qty
        )
        asks30 = tuple(
            (float(px), int(qty), None)
            for px, qty in zip(r.get("ask_px") or (), r.get("ask_qty") or (), strict=False)
            if px and qty
        )
        yield Depth(
            r["symbol"], recv, bids30, asks30, exch_ts=int(exch.timestamp()) if exch else None
        )


def _rows(table: pa.Table | None) -> Iterator[dict[str, Any]]:
    """Rows as dicts, a small batch at a time (a whole window as dicts would be ~10x Arrow)."""
    if table is None or table.num_rows == 0:
        return
    for batch in table.to_batches(max_chunksize=ROW_BATCH):
        yield from batch.to_pylist()


def recorded_events(
    store: Store,
    day: date,
    *,
    as_of: datetime,
    start: datetime | None = None,
    symbols: Sequence[str] | None = None,
    include_snapshots: bool = False,
    include_depth30: bool = True,
    depth30_hold_s: float = 60.0,
    window_s: float = 300.0,
) -> Iterator[Quote | Depth]:
    """Rebuild feed events from recorded rows, in the order they became known.

    When ``include_depth30`` is True and ``depth30`` Parquet files exist for ``day``, 30-level
    depth snapshots from ``depth30`` are merged in ``(recv_ns, seq)`` order and supersede the
    5-level depth columns in ``quotes`` while the symbol is actively receiving ``depth30``
    updates (within ``depth30_hold_s`` seconds). Outside those windows (before the symbol enters
    the 30-level watchlist or after it rotates out), replay falls back to the 5-level ``quotes``
    book so the contract never goes bookless.

    The day is read ``window_s`` seconds of receive time at a time, so memory stays bounded by
    one window and the first events arrive after one query. Ties at the same receive time keep
    the quotes-before-depth30 order; the depth30 times used to suppress 5-level books are read
    from ``depth30_hold_s`` before each window to 1 s after it, so the result is identical to
    reading the whole day at once.
    """
    if window_s <= 0:
        raise ValueError("window_s must be positive")
    capture = None if include_snapshots else "tick"
    syms = list(symbols) if symbols is not None else None
    use_d30 = include_depth30 and store.has("depth30", day)
    spans = [store.ts_span("quotes", day, as_of=as_of, start=start, symbols=syms, capture=capture)]
    if use_d30:
        spans.append(store.ts_span("depth30", day, as_of=as_of, start=start, symbols=syms))
    los = [lo for lo, _ in spans if lo is not None]
    his = [hi for _, hi in spans if hi is not None]
    if not los:
        return
    hold_ns = int(depth30_hold_s * NS_PER_S)
    width = timedelta(seconds=window_s)
    # Wider than _in_d30_window looks (hold behind, 1 s ahead): extra times never change it.
    behind = timedelta(seconds=depth30_hold_s + 1)
    ahead = timedelta(seconds=2)
    w0, last = min(los), max(his)
    while w0 <= last:
        w1 = w0 + width
        q_table = store.quotes(day, as_of=as_of, start=w0, end=w1, symbols=syms, capture=capture)
        d30_table = (
            store.depth30(day, as_of=as_of, start=w0, end=w1, symbols=syms) if use_d30 else None
        )
        n_q = q_table.num_rows
        n_d = d30_table.num_rows if d30_table is not None else 0
        if n_q == 0 and n_d == 0:
            # Skip silence (overnight, feed outages) in one step instead of window by window.
            nxt = [
                store.ts_span("quotes", day, as_of=as_of, start=w1, symbols=syms, capture=capture)[
                    0
                ]
            ]
            if use_d30:
                nxt.append(store.ts_span("depth30", day, as_of=as_of, start=w1, symbols=syms)[0])
            found = [t for t in nxt if t is not None]
            if not found:
                return
            w0 = max(w1, min(found))
            continue
        d30_times_by_sym: dict[str, list[int]] = {}
        if use_d30 and n_q:
            lo_t = w0 - behind if start is None else max(start, w0 - behind)
            times = store.depth30(
                day, as_of=as_of, start=lo_t, end=w1 + ahead, symbols=syms, columns="symbol, ts"
            )
            for sym, ts in zip(
                times.column("symbol").to_pylist(), times.column("ts").to_pylist(), strict=True
            ):
                t_ns = _ns(ts)
                if t_ns is not None:
                    d30_times_by_sym.setdefault(str(sym), []).append(t_ns)
            for ts_list in d30_times_by_sym.values():
                ts_list.sort()
        # Both streams are already in (ts, seq) order; heapq.merge is stable, so at equal
        # receive times quote-derived events come first, as when the whole day was sorted.
        yield from heapq.merge(
            _quote_row_events(_rows(q_table), d30_times_by_sym, hold_ns),
            _depth30_row_events(_rows(d30_table)),
            key=_recv_ns,
        )
        del q_table, d30_table
        w0 = w1


def _recv_ns(ev: Quote | Depth) -> int:
    return ev.recv_ns


@dataclass
class ReplayContext:
    now_ns: int = 0
    quotes: dict[str, Quote] = field(default_factory=dict)
    books: dict[str, tuple[tuple[Level, ...], tuple[Level, ...]]] = field(default_factory=dict)

    @property
    def now(self) -> datetime:
        return from_ns(self.now_ns)

    def mid(self, symbol: str) -> float | None:
        b = self.books.get(symbol)
        if not b or not b[0] or not b[1]:
            return None
        return 0.5 * (b[0][0][0] + b[1][0][0])


class Strategy(Protocol):
    def on_event(
        self, ev: Quote | Depth, ctx: ReplayContext, positions: dict[str, int]
    ) -> Sequence[OrderRecord]: ...


@dataclass(frozen=True)
class ReplayConfig:
    latency_ms: int = 250
    slippage_mult: float = 1.0
    segment: Literal["options", "futures"] = "options"
    exchange: str = "NSE"
    broker: str = "zerodha"  # brokerage rows verified (secondary) for zerodha and upstox


@dataclass
class ReplayResult:
    fills: list[PaperFillResult]
    unfilled: list[OrderRecord]
    rejected: list[OrderRecord]
    net_realized_pnl_inr: float
    charges_inr: float
    open_positions: dict[str, int]
    events: int
    config: ReplayConfig


def _stretch(levels: Sequence[Level], mid: float, mult: float) -> tuple[Level, ...]:
    if mult == 1.0:
        return tuple(levels)
    return tuple((round(mid + mult * (px - mid), 4), q, o) for px, q, o in levels)


def replay_day(
    events: Iterator[Quote | Depth],
    strategy: Strategy,
    *,
    book: RuleBook,
    trade_date: date,
    cfg: ReplayConfig | None = None,
) -> ReplayResult:
    cfg = cfg or ReplayConfig()
    broker = PaperBroker(book, broker_name=cfg.broker)
    ctx = ReplayContext()
    pending: list[tuple[int, OrderRecord]] = []
    fills: list[PaperFillResult] = []
    rejected: list[OrderRecord] = []
    n = 0
    lat_ns = cfg.latency_ms * 1_000_000

    def try_fill(order: OrderRecord) -> None:
        b = ctx.books.get(order.symbol)
        mid = ctx.mid(order.symbol)
        if b is None or mid is None:
            rejected.append(order)
            return
        bids = _stretch(b[0], mid, cfg.slippage_mult)
        asks = _stretch(b[1], mid, cfg.slippage_mult)
        held = broker.positions.get(order.symbol, 0)
        is_exit = (order.side.upper() == "SELL" and held > 0) or (
            order.side.upper() == "BUY" and held < 0
        )
        res = broker.execute_against_depth(
            order,
            bids=bids,
            asks=asks,
            now=ctx.now,
            trade_date=trade_date,
            segment=cfg.segment,
            exchange=cfg.exchange,
            is_exit=is_exit,
        )
        if res.order.state in (OrderState.FILLED, OrderState.PARTIAL):
            fills.append(res)
        else:
            rejected.append(res.order)

    for ev in events:
        n += 1
        ctx.now_ns = ev.recv_ns
        if isinstance(ev, Quote):
            ctx.quotes[ev.symbol] = ev
        else:
            ctx.books[ev.symbol] = (ev.bids, ev.asks)
        due = [p for p in pending if p[0] <= ev.recv_ns]
        if due:
            pending = [p for p in pending if p[0] > ev.recv_ns]
            for _, order in due:
                try_fill(order)
        for order in strategy.on_event(ev, ctx, dict(broker.positions)):
            pending.append((ev.recv_ns + lat_ns, order))
    return ReplayResult(
        fills=fills,
        unfilled=[o for _, o in pending],
        rejected=rejected,
        net_realized_pnl_inr=broker.net_realized_pnl_inr,
        charges_inr=round(broker.total_charges_inr, 2),
        open_positions={k: v for k, v in broker.positions.items() if v},
        events=n,
        config=cfg,
    )


def replay_with_stress(
    make_events: Any,
    make_strategy: Any,
    *,
    book: RuleBook,
    trade_date: date,
    cfg: ReplayConfig | None = None,
) -> tuple[ReplayResult, ReplayResult]:
    """Run the same day twice: as recorded, and with every fill's slippage doubled."""
    base_cfg = cfg or ReplayConfig()
    base = replay_day(
        make_events(), make_strategy(), book=book, trade_date=trade_date, cfg=base_cfg
    )
    stress_cfg = ReplayConfig(
        latency_ms=base_cfg.latency_ms,
        slippage_mult=2.0 * base_cfg.slippage_mult,
        segment=base_cfg.segment,
        exchange=base_cfg.exchange,
        broker=base_cfg.broker,
    )
    stressed = replay_day(
        make_events(), make_strategy(), book=book, trade_date=trade_date, cfg=stress_cfg
    )
    return base, stressed
