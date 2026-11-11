"""Recorder core: turns feed events into Parquet rows. Deterministic and broker-neutral.

* ``on_event`` is called for every adapter event (single consumer thread).
* ``on_timer`` is called at least once a second; it emits 1-min snapshots, flushes parts and
  runs the silence watchdog.

Capture policy (SPEC §3 L1): every message is recorded inside the tick windows (default
09:00–09:35 for the opening study) and, all day, for the "tick set" (spots, VIX, futures and
near-ATM strikes); every subscribed contract also gets a snapshot row each minute from the
last state received. Snapshot rows are known-at their snapshot time; tick rows at receive time.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

from engine.core.clock import NS_PER_S, from_ns, to_ns
from engine.data.adapters.base import Depth, FeedEvent, FeedStatus, Quote, StatusKind
from engine.data.quality.checks import FlagThresholds, row_flags
from engine.data.reference.instrument_master import Instrument
from engine.data.schema import DEPTH_LEVELS
from engine.data.store import DatasetWriter, ns_to_us, s_to_us

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecorderConfig:
    snapshot_interval_s: int = 60
    tick_windows: tuple[tuple[time, time], ...] = ((time(9, 0), time(9, 35)),)
    record_all_ticks: bool = False
    gap_threshold_s: float = 5.0
    watchdog_silence_s: float = 20.0
    flush_interval_s: float = 30.0
    flush_max_rows: int = 50_000
    flags: FlagThresholds = field(default_factory=FlagThresholds)


@dataclass(slots=True)
class BookState:
    ins: Instrument
    depth_expected: bool = False
    quote: Quote | None = None
    depth: Depth | None = None


@dataclass
class RecorderStats:
    messages: int = 0
    unknown_symbol_messages: int = 0
    tick_rows: int = 0
    snap_rows: int = 0
    gaps: int = 0
    max_gap_s: float = 0.0
    reconnects: int = 0
    watchdog_restarts: int = 0


class Recorder:
    def __init__(
        self,
        cfg: RecorderConfig,
        *,
        quotes: DatasetWriter,
        events: DatasetWriter,
        session_open: datetime,
        session_close: datetime,
        source: str,
        start_ns: int,
    ) -> None:
        self.cfg = cfg
        self.quotes = quotes
        self.events = events
        self.source = source
        self.open_ns = to_ns(session_open)
        self.close_ns = to_ns(session_close)
        self.day = session_open.date()
        self._tick_windows = [
            (
                to_ns(datetime.combine(self.day, a, session_open.tzinfo)),
                to_ns(datetime.combine(self.day, b, session_open.tzinfo)),
            )
            for a, b in cfg.tick_windows
        ]
        self.books: dict[str, BookState] = {}
        self.tick_set: set[str] = set()
        self.conn_id = 0
        self.seq = 0
        self.stats = RecorderStats()
        self.last_msg_ns: int | None = None
        self._last_flush_ns = start_ns
        self._snap_interval_ns = cfg.snapshot_interval_s * NS_PER_S
        self._next_snap_ns = (start_ns // self._snap_interval_ns + 1) * self._snap_interval_ns
        self._silence_alarmed = False

    # ------------------------------------------------------------------ subscriptions

    def set_universe(
        self,
        instruments: Iterable[Instrument],
        *,
        depth_symbols: set[str],
        tick_symbols: set[str],
        now_ns: int,
        reason: str,
    ) -> tuple[list[str], list[str]]:
        """Replace the recorded set; returns (added, removed) symbols for the adapter."""
        wanted = {i.symbol: i for i in instruments}
        added = [s for s in wanted if s not in self.books]
        removed = [s for s in self.books if s not in wanted]
        for s in removed:
            del self.books[s]
            self._event(now_ns, "unsubscribe", symbol=s, detail=reason)
        for s in added:
            self.books[s] = BookState(wanted[s])
            self._event(now_ns, "subscribe", symbol=s, detail=reason)
        for s, b in self.books.items():
            b.depth_expected = s in depth_symbols
        self.tick_set = set(tick_symbols)
        return added, removed

    # ------------------------------------------------------------------ events

    def in_tick_window(self, ns: int) -> bool:
        return any(a <= ns < b for a, b in self._tick_windows)

    def on_event(self, ev: FeedEvent) -> None:
        if isinstance(ev, FeedStatus):
            self._on_status(ev)
            return
        self.stats.messages += 1
        self._track_gap(ev.recv_ns)
        book = self.books.get(ev.symbol)
        if book is None:
            self.stats.unknown_symbol_messages += 1
            return
        if isinstance(ev, Quote):
            book.quote = ev
            msg = "quote"
        else:
            book.depth = ev
            msg = "depth"
        if (
            self.cfg.record_all_ticks
            or ev.symbol in self.tick_set
            or self.in_tick_window(ev.recv_ns)
        ):
            self._row(book, ev.recv_ns, "tick", msg)
            self.stats.tick_rows += 1

    def _on_status(self, ev: FeedStatus) -> None:
        if ev.kind is StatusKind.CONNECTED:
            self.conn_id += 1
            if self.conn_id > 1:
                self.stats.reconnects += 1
        self._event(ev.recv_ns, "feed_status", detail=f"{ev.kind.value}: {ev.detail}"[:500])
        if ev.kind in (StatusKind.LIMIT, StatusKind.AUTH_FAILED, StatusKind.ERROR):
            log.warning("feed %s: %s", ev.kind.value, ev.detail)

    def _track_gap(self, ns: int) -> None:
        last = self.last_msg_ns
        self.last_msg_ns = ns if last is None else max(last, ns)
        self._silence_alarmed = False
        if last is None or ns <= last:
            return
        secs = (ns - last) / NS_PER_S
        inside = self.open_ns <= last and ns <= self.close_ns
        if inside and secs > self.cfg.gap_threshold_s:
            self.stats.gaps += 1
            self.stats.max_gap_s = max(self.stats.max_gap_s, secs)
            self._event(ns, "gap", start_ns=last, end_ns=ns, value=secs)
            log.warning("feed gap %.1fs ending %s", secs, from_ns(ns).strftime("%H:%M:%S"))

    # ------------------------------------------------------------------ timer

    def on_timer(self, now_ns: int) -> bool:
        """Snapshots, flushes, watchdog. Returns True if the feed should be restarted."""
        while now_ns >= self._next_snap_ns:
            self._snapshot(self._next_snap_ns)
            self._next_snap_ns += self._snap_interval_ns
        if (
            len(self.quotes) >= self.cfg.flush_max_rows
            or now_ns - self._last_flush_ns >= self.cfg.flush_interval_s * NS_PER_S
        ):
            self.flush(now_ns)
        return self._watchdog(now_ns)

    def _watchdog(self, now_ns: int) -> bool:
        if not (self.open_ns <= now_ns < self.close_ns) or not self.books:
            return False
        ref = self.last_msg_ns if self.last_msg_ns is not None else self.open_ns
        silent = (now_ns - ref) / NS_PER_S
        if silent > self.cfg.watchdog_silence_s and not self._silence_alarmed:
            self._silence_alarmed = True
            self.stats.watchdog_restarts += 1
            self._event(now_ns, "note", detail=f"watchdog: no messages for {silent:.0f}s")
            log.error("no market data for %.0fs during session; restarting feed", silent)
            return True
        return False

    def _snapshot(self, snap_ns: int) -> None:
        for book in self.books.values():
            self._row(book, snap_ns, "snap", "snap")
            self.stats.snap_rows += 1

    def flush(self, now_ns: int) -> None:
        self.quotes.flush()
        self.events.flush()
        self._last_flush_ns = now_ns

    def close(self, now_ns: int) -> None:
        last = self.last_msg_ns
        if last is not None and last < self.close_ns <= now_ns:
            secs = (self.close_ns - last) / NS_PER_S
            if secs > self.cfg.gap_threshold_s and last >= self.open_ns:
                self._event(now_ns, "gap", start_ns=last, end_ns=self.close_ns, value=secs)
        self._event(now_ns, "session", detail=f"closed; {self.stats}")
        self.flush(now_ns)

    # ------------------------------------------------------------------ rows

    def _event(
        self,
        ns: int,
        event: str,
        *,
        symbol: str | None = None,
        detail: str | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
        value: float | None = None,
    ) -> None:
        self.events.append(
            {
                "ts": ns_to_us(ns),
                "event": event,
                "symbol": symbol,
                "detail": detail,
                "start_ts": ns_to_us(start_ns),
                "end_ts": ns_to_us(end_ns),
                "value": value,
                "conn_id": self.conn_id,
                "source": self.source,
            }
        )

    def note(self, now_ns: int, event: str, detail: str, value: float | None = None) -> None:
        self._event(now_ns, event, detail=detail, value=value)

    def _row(self, book: BookState, ts_ns: int, capture: str, msg: str) -> None:
        ins, q, d = book.ins, book.quote, book.depth
        self.seq += 1
        row: dict[str, Any] = {
            "ts": ns_to_us(ts_ns),
            "capture": capture,
            "msg": msg,
            "symbol": ins.symbol,
            "underlying": ins.underlying,
            "kind": ins.kind.value,
            "expiry": ins.expiry,
            "strike": ins.strike,
            "quote_recv_ts": ns_to_us(q.recv_ns) if q else None,
            "depth_recv_ts": ns_to_us(d.recv_ns) if d else None,
            "qc_flags": row_flags(
                ins.kind.value,
                q,
                d,
                ts_ns,
                depth_expected=book.depth_expected,
                th=self.cfg.flags,
            ),
            "conn_id": self.conn_id,
            "seq": self.seq,
            "source": self.source,
        }
        if q is not None:
            row.update(
                exch_ts=s_to_us(q.exch_ts),
                ltt=s_to_us(q.ltt),
                ltp=q.ltp,
                ltq=q.ltq,
                atp=q.atp,
                volume=q.volume,
                oi=q.oi,
                tbq=q.tbq,
                tsq=q.tsq,
                bid=q.bid,
                bid_qty=q.bid_qty,
                ask=q.ask,
                ask_qty=q.ask_qty,
                open=q.open,
                high=q.high,
                low=q.low,
                prev_close=q.prev_close,
                upper_ckt=q.upper_ckt,
                lower_ckt=q.lower_ckt,
            )
        if d is not None:
            for side, levels in (("bid", d.bids), ("ask", d.asks)):
                for i, (px, qty, orders) in enumerate(levels[:DEPTH_LEVELS], start=1):
                    row[f"{side}_px_{i}"] = px
                    row[f"{side}_qty_{i}"] = qty
                    row[f"{side}_ord_{i}"] = orders
        self.quotes.append(row)
