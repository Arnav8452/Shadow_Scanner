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

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

from engine.core.clock import NS_PER_S, from_ns
from engine.core.rules import RuleBook
from engine.data.adapters.base import Depth, Level, Quote
from engine.data.schema import DEPTH_LEVELS
from engine.data.store import Store
from engine.execution.order_fsm import OrderRecord, OrderState
from engine.execution.paper_broker import PaperBroker, PaperFillResult

US = 1000  # ns per microsecond


def _ns(ts: datetime | None) -> int | None:
    if ts is None:
        return None
    return int(ts.timestamp()) * NS_PER_S + ts.microsecond * US


def recorded_events(
    store: Store,
    day: date,
    *,
    as_of: datetime,
    start: datetime | None = None,
    symbols: Sequence[str] | None = None,
    include_snapshots: bool = False,
) -> Iterator[Quote | Depth]:
    """Rebuild feed events from recorded rows, in the order they became known."""
    capture = None if include_snapshots else "tick"
    table = store.quotes(day, as_of=as_of, start=start, symbols=symbols, capture=capture)
    if table.num_rows == 0:
        return
    for r in table.to_pylist():
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
        if r["msg"] in ("depth", "snap") and r.get("depth_recv_ts") is not None:
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
