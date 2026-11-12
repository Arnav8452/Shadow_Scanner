"""P4: replay a recorded day through the same engine, at a chosen speed.

The feed thread walks the recorded events (``quotes`` ticks and ``depth30`` books, merged in the
order they became known) and sets a :class:`ManualClock` to each event's receive time before
handing it to the runner, so every timer in the engine (recenter, opening range, square-off)
behaves as it did live. Speed 0 means as fast as possible.

Replays never write market data and never touch the live paper journals: their journals and
ledger go under ``data/replay/<date>/`` and are reset at the start of each replay.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from engine.backtest.event_replay import recorded_events
from engine.core.calendar import CalendarCoverageError, TradingCalendar
from engine.core.clock import ManualClock, from_ns, to_ns
from engine.core.config import Settings
from engine.core.rules import IST, RuleBook
from engine.data.adapters.base import Depth, FeedStatus, Quote, Sink, StatusKind
from engine.data.recorder.run import load_master, load_recorder_settings, sessions_for
from engine.data.schema import DEPTH30_SCHEMA
from engine.data.store import DatasetWriter, Store
from engine.shadow.live import (
    ShadowLiveRunner,
    load_shadow_live_config,
    previous_day_levels,
)
from engine.shadow.scanner import ShadowOrderBookScanner

log = logging.getLogger(__name__)


class ReplayFeed:
    """A :class:`FeedAdapter` that plays recorded events with real-time pacing."""

    name = "replay"
    max_symbols = 100_000
    depth_counts_separately = False
    supports_depth = True

    def __init__(
        self,
        events: Callable[[], Iterator[Quote | Depth]],
        clock: ManualClock,
        *,
        speed: float = 1.0,
        end_ns: int,
        max_gap_s: float = 60.0,
    ) -> None:
        self._events = events
        self.clock = clock
        self.speed = max(0.0, speed)
        self.end_ns = end_ns
        # Silence longer than this (feed outage, separate test runs) plays as one second.
        self.max_gap_s = max_gap_s
        self.skipped_gaps = 0
        self.paused = False
        self.done = False
        self.played = 0
        self.first_ns = 0
        self.last_ns = 0
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # FeedAdapter ------------------------------------------------------------

    def start(self, sink: Sink) -> None:
        self._thread = threading.Thread(target=self._run, args=(sink,), name="replay", daemon=True)
        self._thread.start()

    def subscribe(self, symbols: Sequence[str], *, depth: bool) -> None:
        with self._lock:
            self._subscribed.update(symbols)

    def unsubscribe(self, symbols: Sequence[str], *, depth: bool) -> None:
        with self._lock:
            self._subscribed.difference_update(symbols)

    def restart(self) -> None:
        pass

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # controls ---------------------------------------------------------------

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False
        self._wake.set()

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.0, float(speed))
        self._wake.set()

    def progress(self) -> dict[str, Any]:
        span = max(1, self.end_ns - self.first_ns) if self.first_ns else 1
        done_pct = (
            100.0
            if self.done
            else (100.0 * (self.last_ns - self.first_ns) / span if self.first_ns else 0.0)
        )
        return {
            "speed": self.speed,
            "paused": self.paused,
            "done": self.done,
            "events": self.played,
            "at": from_ns(self.last_ns).strftime("%H:%M:%S") if self.last_ns else None,
            "pct": round(min(100.0, done_pct), 1),
            "skipped_gaps": self.skipped_gaps,
        }

    # loop -------------------------------------------------------------------

    def _run(self, sink: Sink) -> None:
        sink(FeedStatus(StatusKind.CONNECTED, self.clock.now_ns(), "replay"))
        prev_ns: int | None = None
        try:
            for ev in self._events():
                if self._stop.is_set():
                    return
                if not self.first_ns:
                    self.first_ns = ev.recv_ns
                if prev_ns is not None and ev.recv_ns > prev_ns:
                    gap = (ev.recv_ns - prev_ns) / 1e9
                    if gap > self.max_gap_s:
                        self.skipped_gaps += 1
                        gap = 1.0 * max(self.speed, 1.0)
                    self._pace(gap)
                    if self._stop.is_set():
                        return
                while self.paused and not self._stop.is_set():
                    self._wake.wait(0.2)
                    self._wake.clear()
                prev_ns = ev.recv_ns
                self.last_ns = ev.recv_ns
                if ev.recv_ns > self.clock.now_ns():
                    self.clock.set(from_ns(ev.recv_ns))
                with self._lock:
                    wanted = ev.symbol in self._subscribed
                if wanted:
                    sink(ev)
                    self.played += 1
        finally:
            self.done = True
            if not self._stop.is_set():
                self.clock.set(from_ns(max(self.clock.now_ns(), self.end_ns)))

    def _pace(self, gap_s: float) -> None:
        """Sleep the event gap divided by speed, reacting to speed/pause changes at once."""
        remaining = gap_s
        while remaining > 0 and not self._stop.is_set():
            if self.speed <= 0:
                return
            step = min(0.25, remaining / self.speed)
            t0 = time.monotonic()
            self._wake.wait(step)
            self._wake.clear()
            if self.paused:
                return  # the caller waits for resume
            remaining -= (time.monotonic() - t0) * self.speed


def _replay_symbols(store: Store, day: date) -> list[str] | None:
    """Symbols worth replaying: everything the 30-level watchlist recorded, plus the index,
    VIX and futures rows the recorder kept all day. None means 'all' (small or sim days)."""
    if not store.has("depth30", day):
        return None
    rows = store.rows(f"SELECT DISTINCT symbol FROM {store.source_sql('depth30', day)}")
    syms = {str(r[0]) for r in rows}
    if store.has("quotes", day):
        q = store.rows(
            f"SELECT DISTINCT symbol FROM {store.source_sql('quotes', day)} "
            "WHERE kind IN ('IDX', 'FUT')"
        )
        syms.update(str(r[0]) for r in q)
    return sorted(syms)


def _recorded_span(store: Store, day: date) -> tuple[datetime | None, datetime | None]:
    lo: datetime | None = None
    hi: datetime | None = None
    for ds in ("quotes", "depth30"):
        if not store.has(ds, day):
            continue
        (a, b), *_ = store.rows(f"SELECT min(ts), max(ts) FROM {store.source_sql(ds, day)}")
        if a is not None:
            a, b = a.astimezone(IST), b.astimezone(IST)
            lo = a if lo is None or a < lo else lo
            hi = b if hi is None or b > hi else hi
    return lo, hi


def run_replay_day(
    settings: Settings,
    root: Path,
    *,
    day: date,
    speed: float,
    sim: bool,
    port: int | None,
    stop: threading.Event | None = None,
    hold_after_s: float = 6 * 3600,
) -> dict[str, Any]:
    """Replay ``day`` and keep serving the final state until stopped (or ``hold_after_s``)."""
    from engine.cockpit.build import build_cockpit
    from engine.cockpit.config import load_cockpit_config

    stop = stop or threading.Event()
    rs = load_recorder_settings(settings)
    cfg = load_shadow_live_config(settings)
    ccfg = load_cockpit_config(settings, root)
    book = RuleBook.load(root / "config" / "rules")
    cal = TradingCalendar.from_rulebook(book)
    data_dir = rs.sim_dir if sim else rs.parquet_dir
    store = Store(data_dir)
    if not store.has("quotes", day) and not store.has("depth30", day):
        raise FileNotFoundError(f"nothing recorded for {day} under {data_dir}")
    sessions = sessions_for(rs, book, day)
    primary = next(
        (s for s in sessions if (s.exchange, s.segment) == rs.primary_session), sessions[0]
    )
    start = primary.first_dt - timedelta(minutes=rs.pre_buffer_min)
    end = primary.close_dt + timedelta(minutes=rs.post_buffer_min)
    master = load_master(
        rs, day, datetime.combine(day, primary.normal_close, IST), fetch=False,
        close_time=primary.normal_close,
    )  # fmt: skip
    prev: dict[str, tuple[float, float, float]] = {}
    with contextlib.suppress(CalendarCoverageError, OSError):
        prev = previous_day_levels(store, cal.previous_trading_day(day))
    out_dir = ccfg.replay_dir / f"{'sim-' if sim else ''}{day.isoformat()}"
    shutil.rmtree(out_dir, ignore_errors=True)  # a replay always starts from a clean slate
    out_dir.mkdir(parents=True, exist_ok=True)

    # Cover whatever was recorded (short test runs or synthetic days can fall outside hours).
    first, last = _recorded_span(store, day)
    if first is not None and first < start:
        start = first - timedelta(seconds=5)
    if last is not None and last > end:
        end = last + timedelta(seconds=30)
    clock = ManualClock(start)
    as_of = (last or end) + timedelta(seconds=1)
    symbols = _replay_symbols(store, day)

    def events() -> Iterator[Quote | Depth]:
        # The feed runs on its own thread: it gets its own DuckDB connection (one connection
        # refuses concurrent calls, and the engine thread queries the store for OI walls).
        return recorded_events(Store(data_dir), day, as_of=as_of, symbols=symbols)

    feed = ReplayFeed(events, clock, speed=speed, end_ns=to_ns(end))
    scanner = ShadowOrderBookScanner(
        settings.shadow_scanner,
        capital_inr=settings.risk.capital_inr,
        max_lots=settings.risk.max_lots_per_strategy,
    )
    cockpit = build_cockpit(
        settings,
        root,
        source="replay",
        day=day,
        session=primary,
        master=master,
        rs=rs,
        book=book,
        calendar=cal,
        port=port,
        journal_dir=out_dir,
        chain_dir=data_dir,
        replay=feed,
        on_stop=stop.set,
    )
    runner = ShadowLiveRunner(
        cfg,
        rs,
        scanner=scanner,
        master=master,
        adapter=feed,
        clock=clock,
        session=primary,
        depth_writer=DatasetWriter(out_dir / "unused", "depth30", DEPTH30_SCHEMA),
        store=store,
        report_path=out_dir / "dashboard.html",
        ledger_file=out_dir / "shadow_ledger.jsonl",
        feed_label=f"REPLAY of {day} ({'sim' if sim else 'recorded'})",
        prev_levels=prev,
        cockpit=cockpit,
        record_depth=False,
    )
    try:
        runner.start()
        runner.run(to_ns(end), stop)
    finally:
        feed.stop()
        runner.finish()
    # Keep serving the final state (and taking paper/close commands) until told to stop.
    t_end = time.monotonic() + hold_after_s
    while not stop.is_set() and time.monotonic() < t_end:
        if cockpit is not None:
            try:
                cockpit.on_step(clock.now_ns())
            except Exception:
                log.exception("replay cockpit step failed")
        stop.wait(0.5)
    if cockpit is not None:
        if cockpit.analytics is not None:
            cockpit.analytics.stop()
        if cockpit.publisher is not None:
            cockpit.publisher.close()
    return {
        "day": day.isoformat(),
        "events": feed.played,
        "signals": runner.stats.signals,
        "track_a_trades": len(scanner.closed_outcomes),
        "out_dir": str(out_dir),
    }
