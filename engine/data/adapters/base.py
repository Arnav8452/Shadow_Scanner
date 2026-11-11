"""Broker-neutral market-data feed interface.

Adapters translate broker messages into :class:`Quote`, :class:`Depth` and
:class:`FeedStatus` events and push them to a sink. Every event carries ``recv_ns``, the local
receive time (``time.time_ns()``), stamped as early as possible. Exchange timestamps are kept
as broker-provided epoch seconds (``None`` when the broker does not send one).

Retail feeds are conflated snapshots, not exchange tick-by-tick (SPEC §3 L1).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    recv_ns: int
    exch_ts: int | None = None  # exchange feed time, epoch seconds
    ltt: int | None = None  # last trade time, epoch seconds
    ltp: float | None = None
    ltq: int | None = None
    atp: float | None = None  # average traded price
    volume: int | None = None  # cumulative for the day
    oi: int | None = None
    tbq: int | None = None  # total buy qty
    tsq: int | None = None  # total sell qty
    bid: float | None = None
    bid_qty: int | None = None
    ask: float | None = None
    ask_qty: int | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    upper_ckt: float | None = None
    lower_ckt: float | None = None
    # Upstox V3 full_d30 / option-chain native Greeks & CAS equilibrium fields:
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    iep: float | None = None  # Indicative Equilibrium Price (Pre-open & 15:00-15:40 CAS)
    iiq_total: int | None = None  # Indicative Imbalance Quantity in CAS


Level = tuple[float, int, int | None]  # price, quantity, number of orders


@dataclass(frozen=True, slots=True)
class Depth:
    symbol: str
    recv_ns: int
    bids: tuple[Level, ...]  # best first
    asks: tuple[Level, ...]  # best first
    exch_ts: int | None = None


class StatusKind(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    SUBSCRIBED = "subscribed"
    ERROR = "error"
    LIMIT = "limit"  # broker refused a subscription (symbol cap)
    AUTH_FAILED = "auth_failed"


@dataclass(frozen=True, slots=True)
class FeedStatus:
    kind: StatusKind
    recv_ns: int
    detail: str = ""


FeedEvent = Quote | Depth | FeedStatus
Sink = Callable[[FeedEvent], None]


class FeedAdapter(Protocol):
    """One live connection to a broker's market-data stream."""

    name: str
    max_symbols: int  # broker cap on concurrently subscribed symbols (per user)
    depth_counts_separately: bool  # does a depth subscription use a second slot?
    supports_depth: bool

    def start(self, sink: Sink) -> None: ...

    def subscribe(self, symbols: Sequence[str], *, depth: bool) -> None: ...

    def unsubscribe(self, symbols: Sequence[str], *, depth: bool) -> None: ...

    def restart(self) -> None:
        """Drop and re-open the connection, re-subscribing everything."""
        ...

    def stop(self) -> None: ...


def subscription_cost(n_symbols: int, n_depth: int, adapter: FeedAdapter) -> int:
    return n_symbols + (n_depth if adapter.depth_counts_separately else 0)
