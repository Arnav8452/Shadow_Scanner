"""Recorder process: one trading day, start to finish (``tasks.py record``).

Flow: calendar check → session window from config/rules → today's instrument master →
connect feed → subscribe spots/VIX/futures → once spot prices arrive, select the option band
→ record until the window closes (re-centring every minute) → QC report → compact parts.

Real recordings go to ``data/parquet``; simulated runs (``SIM=1``) go to ``data/sim/parquet``.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from engine.core.calendar import TradingCalendar
from engine.core.clock import (
    NS_PER_S,
    Clock,
    MarketSession,
    SystemClock,
    from_ns,
    market_session,
    recording_window,
    to_ns,
)
from engine.core.config import Settings
from engine.core.env import Secret, get_secret, load_dotenv
from engine.core.rules import RuleBook, RuleNotFoundError
from engine.data.adapters.base import FeedAdapter, FeedEvent
from engine.data.quality.checks import FlagThresholds, qc_day, write_qc
from engine.data.recorder.core import Recorder, RecorderConfig
from engine.data.recorder.universe import Selection, UnderlyingSpec, UniverseConfig, select_universe
from engine.data.reference.instrument_master import (
    InstrumentMaster,
    classify_expiries,
    fetch_fyers_master,
    fetch_upstox_master,
    latest_snapshot,
    load_fyers_master,
    load_upstox_master,
    lot_size_mismatches,
)
from engine.data.schema import EVENTS_SCHEMA, QUOTES_SCHEMA
from engine.data.store import DatasetWriter, Store, compact_day

log = logging.getLogger(__name__)


class RecorderSetupError(RuntimeError):
    pass


# --------------------------------------------------------------------------- settings


def _t(s: str) -> time:
    hh, mm = str(s).split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class RecorderSettings:
    universe: UniverseConfig  # budget filled in from the adapter cap at run time
    core: RecorderConfig
    symbol_caps: Mapping[str, int]
    depth_counts_separately: Mapping[str, bool]
    budget_margin: int
    recenter_interval_s: int
    master_refresh_min: int
    master_broker: str
    master_segments: tuple[str, ...]
    sessions: tuple[tuple[str, str], ...]
    primary_session: tuple[str, str]
    pre_buffer_min: int
    post_buffer_min: int
    sticky_file: Path
    qc_dir: Path
    parquet_dir: Path
    sim_dir: Path
    raw_dir: Path
    log_dir: Path


def load_recorder_settings(settings: Settings) -> RecorderSettings:
    raw = settings.raw.get("recorder")
    if not isinstance(raw, Mapping):
        raise RecorderSetupError("config/settings.yaml has no 'recorder' section")
    band = raw.get("band", {})
    master = raw.get("master", {})
    # Symbols are broker keys, so the instrument master must come from the data broker.
    broker = str(master.get("broker") or settings.data_primary)
    if broker != settings.data_primary:
        raise RecorderSetupError(
            f"recorder.master.broker={broker!r} but data.primary={settings.data_primary!r}"
        )

    def per_broker(value: Any, what: str) -> Any:
        if isinstance(value, Mapping):
            if broker not in value:
                raise RecorderSetupError(f"{what} has no entry for broker {broker!r}")
            return value[broker]
        return value

    unds = tuple(
        UnderlyingSpec(
            name=str(u["name"]),
            spot_symbol=str(per_broker(u["spot"], f"underlying {u['name']} spot")),
            expiry_roles=tuple(u.get("expiries", ("current_weekly",))),
            expiry_weights={str(k): float(v) for k, v in (u.get("weights") or {}).items()},
            futures_months=int(u.get("futures_months", 1)),
        )
        for u in raw["underlyings"]
    )
    universe = UniverseConfig(
        underlyings=unds,
        budget=0,
        extra_symbols=tuple(raw.get("extra_symbols") or ()),
        vix_symbol=per_broker(raw.get("vix_symbol"), "vix_symbol"),
        min_strikes_each_side=int(band.get("min_strikes_each_side", 25)),
        buffer_strikes=int(band.get("buffer_strikes", 0)),
        sigma_mult=float(band.get("sigma_mult", 3.0)),
        fallback_iv=float(band.get("fallback_iv", 0.15)),
        tick_strikes_each_side=int(band.get("tick_strikes_each_side", 10)),
        tick_expiry_roles=tuple(band.get("tick_expiry_roles", ("current_weekly",))),
        depth=bool(raw.get("depth", True)),
    )
    core = RecorderConfig(
        snapshot_interval_s=int(raw.get("snapshot_interval_s", 60)),
        tick_windows=tuple((_t(a), _t(b)) for a, b in raw.get("tick_windows", [])),
        record_all_ticks=bool(raw.get("record_all_ticks", False)),
        gap_threshold_s=float(raw.get("gap_threshold_s", 5)),
        watchdog_silence_s=float(raw.get("watchdog_silence_s", 20)),
        flush_interval_s=float(raw.get("flush_interval_s", 30)),
        flags=FlagThresholds(
            stale_s=float(raw.get("stale_quote_s", 120)),
            lag_s=float(raw.get("lag_warn_s", 5)),
            ahead_s=float(raw.get("clock_ahead_tolerance_s", 1)),
        ),
    )
    if core.snapshot_interval_s <= 0 or 60 % core.snapshot_interval_s:
        raise RecorderSetupError("snapshot_interval_s must divide 60")
    sessions = tuple((str(a), str(b)) for a, b in raw.get("sessions", [["NSE", "fo"]]))
    paths = settings.paths
    root = paths["data"].parent
    return RecorderSettings(
        universe=universe,
        core=core,
        symbol_caps={str(k): int(v) for k, v in raw.get("symbol_caps", {}).items()},
        depth_counts_separately={
            str(k): bool(v) for k, v in raw.get("depth_counts_separately", {}).items()
        },
        budget_margin=int(raw.get("budget_margin", 2)),
        recenter_interval_s=int(raw.get("recenter_interval_s", 60)),
        master_refresh_min=int(raw.get("master_refresh_min", 30)),
        master_broker=broker,
        master_segments=tuple(per_broker(master.get("segments", ()), "master.segments")),
        sessions=sessions,
        primary_session=sessions[0],
        pre_buffer_min=int(raw.get("pre_buffer_min", 10)),
        post_buffer_min=int(raw.get("post_buffer_min", 5)),
        sticky_file=root / str(raw.get("sticky_file", "data/state/recorder_sticky.json")),
        qc_dir=root / str(raw.get("qc_dir", "reports/qc")),
        parquet_dir=paths["parquet"],
        sim_dir=paths["data"] / "sim" / "parquet",
        raw_dir=paths["raw"],
        log_dir=paths["logs"],
    )


# --------------------------------------------------------------------------- sticky set


class StickyStore:
    """Contracts that once entered the band keep being recorded until they expire."""

    def __init__(self, path: Path, today: date) -> None:
        self.path = path
        self.items: dict[str, str] = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.items = {s: e for s, e in data.items() if e >= today.isoformat()}

    def add(self, symbol: str, expiry: date) -> None:
        self.items[symbol] = expiry.isoformat()

    def symbols(self) -> set[str]:
        return set(self.items)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=0, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- runner


class RecorderRunner:
    """Glue between adapter threads, the single-threaded Recorder and the universe."""

    def __init__(
        self,
        rs: RecorderSettings,
        *,
        adapter: FeedAdapter,
        master: InstrumentMaster,
        clock: Clock,
        recorder: Recorder,
        sticky: StickyStore,
        master_reloader: Callable[[], InstrumentMaster] | None = None,
    ) -> None:
        self.rs = rs
        self.adapter = adapter
        self.master = master
        self.clock = clock
        self.recorder = recorder
        self.sticky = sticky
        self.budget = max(1, adapter.max_symbols - rs.budget_margin)
        self.universe = replace(rs.universe, budget=self.budget)
        self.queue: queue.Queue[FeedEvent] = queue.Queue(maxsize=500_000)
        self.dropped_events = 0
        self.selection: Selection | None = None
        self._next_recenter_ns = 0
        self.restarts = 0
        self.master_reloader = master_reloader
        self._reload_every_ns = rs.master_refresh_min * 60 * NS_PER_S
        self._next_reload_ns = clock.now_ns() + self._reload_every_ns
        self._reload_thread: threading.Thread | None = None
        self._pending_master: InstrumentMaster | None = None
        self.contracts_added = 0

    def _enqueue(self, ev: FeedEvent) -> None:
        try:
            self.queue.put_nowait(ev)
        except queue.Full:
            self.dropped_events += 1
            if self.dropped_events in (1, 1000, 100_000):
                log.error("recorder queue full; %d events dropped", self.dropped_events)

    def start(self) -> None:
        self.adapter.start(self._enqueue)
        self.recenter(self.clock.now_ns(), reason="start")

    def spots_and_iv(self) -> tuple[dict[str, float], float | None]:
        spots: dict[str, float] = {}
        for u in self.universe.underlyings:
            b = self.recorder.books.get(u.spot_symbol)
            px = b.quote.ltp if b and b.quote else None
            if px:
                spots[u.name] = px
        iv = None
        vix = self.universe.vix_symbol
        vb = self.recorder.books.get(vix) if vix else None
        if vb and vb.quote and vb.quote.ltp:
            iv = vb.quote.ltp / 100.0
        return spots, iv

    def recenter(self, now_ns: int, *, reason: str) -> Selection:
        spots, iv = self.spots_and_iv()
        sel = select_universe(
            self.master,
            self.universe,
            now=from_ns(now_ns),
            spots=spots,
            iv=iv,
            sticky=self.sticky.symbols(),
            depth_counts_separately=self.adapter.depth_counts_separately,
        )
        added, removed = self.recorder.set_universe(
            [self.master.get(s) for s in sel.symbols],
            depth_symbols=sel.depth_symbols,
            tick_symbols=sel.tick_symbols,
            now_ns=now_ns,
            reason=reason,
        )
        if removed:
            self.adapter.unsubscribe(removed, depth=True)
        plain = [s for s in added if s not in sel.depth_symbols]
        deep = [s for s in added if s in sel.depth_symbols]
        if plain:
            self.adapter.subscribe(plain, depth=False)
        if deep:
            self.adapter.subscribe(deep, depth=True)
        for s in added:
            ins = self.master.get(s)
            if ins.kind.is_option and ins.expiry is not None:
                self.sticky.add(s, ins.expiry)
        if added or removed or sel.dropped:
            self.recorder.note(
                now_ns,
                "dropped",
                f"{reason}: +{len(added)} -{len(removed)}; {'; '.join(sel.notes)}"[:500],
                value=float(len(sel.dropped)),
            )
        if added or removed:
            log.info(
                "universe %s: %d symbols (+%d -%d), cost %d/%d, dropped %d",
                reason,
                len(sel.symbols),
                len(added),
                len(removed),
                sel.cost,
                self.budget,
                len(sel.dropped),
            )
        self.selection = sel
        self._next_recenter_ns = now_ns + self.rs.recenter_interval_s * NS_PER_S
        return sel

    def _options_selected(self) -> bool:
        return any(b.ins.kind.is_option for b in self.recorder.books.values())

    def step(self, max_wait_s: float = 0.5) -> None:
        """Process queued events, then run timers. Call in a loop."""
        try:
            ev = self.queue.get(timeout=max_wait_s) if max_wait_s > 0 else self.queue.get_nowait()
        except queue.Empty:
            ev = None
        n = 0
        while ev is not None:
            self.recorder.on_event(ev)
            n += 1
            if n >= 50_000:
                break
            try:
                ev = self.queue.get_nowait()
            except queue.Empty:
                ev = None
        now = self.clock.now_ns()
        self._maybe_reload_master(now)
        if now >= self._next_recenter_ns or (
            not self._options_selected() and self.spots_and_iv()[0]
        ):
            self.recenter(now, reason="recenter")
        if self.recorder.on_timer(now):
            self.restarts += 1
            self.adapter.restart()

    def _maybe_reload_master(self, now: int) -> None:
        """Fetch the contract file again every ``master_refresh_min`` in a background thread
        (download + parse take ~1 s) and merge newly listed contracts on this thread."""
        pending, self._pending_master = self._pending_master, None
        if pending is not None:
            added = self.master.add_new(pending.all())
            if added:
                self.contracts_added += len(added)
                self.recorder.note(now, "note", f"contract file refresh: +{len(added)} contracts")
                log.info("contract file refresh: %d new contracts", len(added))
                self.recenter(now, reason="new contracts")
        if (
            self.master_reloader is None
            or self._reload_every_ns <= 0
            or now < self._next_reload_ns
            or (self._reload_thread is not None and self._reload_thread.is_alive())
        ):
            return
        self._next_reload_ns = now + self._reload_every_ns
        reloader = self.master_reloader

        def work() -> None:
            try:
                self._pending_master = reloader()
            except Exception as exc:  # keep recording on the morning file
                log.warning("contract file refresh failed: %s", exc)

        self._reload_thread = threading.Thread(target=work, name="master-reload", daemon=True)
        self._reload_thread.start()

    def run(self, end_ns: int, stop: threading.Event) -> None:
        while not stop.is_set() and self.clock.now_ns() < end_ns:
            self.step()

    def finish(self) -> None:
        self.adapter.stop()
        self.step(0)
        self.recorder.close(self.clock.now_ns())
        self.sticky.save()


# --------------------------------------------------------------------------- one day


@dataclass
class RecordResult:
    day: date
    status: str  # recorded | skipped | failed
    detail: str = ""
    out_dir: Path | None = None
    qc_report: Path | None = None
    qc_passed: bool | None = None
    stats: dict[str, Any] = field(default_factory=dict)


def load_master(
    rs: RecorderSettings,
    day: date,
    now: datetime,
    *,
    fetch: bool,
    close_time: time | None = None,
    refresh_tag: str | None = None,
) -> InstrumentMaster:
    """Today's master (downloaded once a day). ``refresh_tag`` forces a fresh intraday
    download saved under that tag, keeping the morning snapshot intact."""
    broker = rs.master_broker
    fetchers = {"fyers": fetch_fyers_master, "upstox": fetch_upstox_master}
    if broker not in fetchers:
        raise RecorderSetupError(f"no instrument-master loader for {broker}")
    paths: list[Path] = []
    for seg in rs.master_segments:
        p: Path | None = None
        if fetch:
            try:
                p = fetchers[broker](
                    seg,
                    raw_dir=rs.raw_dir,
                    day=day,
                    now=now,
                    refresh=refresh_tag is not None,
                    tag=refresh_tag,
                )
            except Exception as exc:  # network trouble: fall back to the last snapshot
                log.warning("master %s fetch failed (%s); using last snapshot", seg, exc)
        if p is None:
            p = latest_snapshot(rs.raw_dir, broker, seg, day)
            if p is not None and p.parent.name != day.isoformat():
                log.warning("using %s master from %s (stale)", seg, p.parent.name)
        if p is None:
            raise RecorderSetupError(f"no {seg} instrument master available")
        paths.append(p)
    if broker == "upstox":
        return load_upstox_master(paths, as_of=day, close_time=close_time)
    return load_fyers_master(paths, as_of=day)


def sessions_for(rs: RecorderSettings, book: RuleBook, day: date) -> list[MarketSession]:
    out = []
    for ex, seg in rs.sessions:
        try:
            # Wider windows are always safe for recording, so UNVERIFIED rows are allowed here.
            out.append(market_session(book, ex, seg, day, allow_unverified=True))
        except RuleNotFoundError:
            log.warning("no session times for %s %s on %s", ex, seg, day)
    if not out:
        raise RecorderSetupError(f"no market sessions configured for {day}")
    return out


def sim_spots(master: InstrumentMaster, rs: RecorderSettings, day: date) -> dict[str, float]:
    """Plausible starting levels for the simulator: middle of the current weekly's strikes."""
    out: dict[str, float] = {}
    for u in rs.universe.underlyings:
        roles = classify_expiries(master.option_expiries(u.name, day), ("current_weekly",))
        if "current_weekly" in roles:
            ks = master.strikes(u.name, roles["current_weekly"])
            out[u.name] = ks[len(ks) // 2]
    return out


AdapterFactory = Callable[[RecorderSettings, InstrumentMaster, Clock], FeedAdapter]


def upstox_token(env: Mapping[str, str], day: date) -> Secret:
    """Today's OAuth token if present (``tasks.py upstox-login``), else the 1-year Analytics
    Token. Whether the Analytics Token may open the feed is undocumented: if the socket refuses
    it, the recorder logs AUTH_FAILED and the daily login is the fallback."""
    if env.get("UPSTOX_ACCESS_TOKEN") and env.get("UPSTOX_TOKEN_DATE") == day.isoformat():
        return get_secret("UPSTOX_ACCESS_TOKEN", env)
    if env.get("UPSTOX_ANALYTICS_TOKEN"):
        return get_secret("UPSTOX_ANALYTICS_TOKEN", env)
    raise RecorderSetupError(
        "no Upstox token: set UPSTOX_ANALYTICS_TOKEN in .env or run "
        "`uv run python tasks.py upstox-login`"
    )


def default_adapter(settings: Settings, root: Path, *, sim: bool, day: date) -> AdapterFactory:
    def make(rs: RecorderSettings, master: InstrumentMaster, clock: Clock) -> FeedAdapter:
        if sim:
            from engine.data.adapters.sim import SimFeed

            return SimFeed(
                master,
                clock,
                spots=sim_spots(master, rs, day),
                max_symbols=rs.symbol_caps.get("sim", 5000),
                threaded=True,
            )
        broker = settings.data_primary
        env: dict[str, str] = dict(os.environ)
        load_dotenv(root / ".env", env)
        if broker == "upstox":
            from engine.data.adapters.upstox import MODE_FULL, UpstoxFeed

            return UpstoxFeed(
                token=upstox_token(env, day),
                mode=MODE_FULL,
                max_symbols=rs.symbol_caps.get("upstox", 2000),
            )
        if broker != "fyers":
            raise RecorderSetupError(f"no live adapter for data broker {broker!r} yet")
        from engine.data.adapters.fyers import FyersFeed

        if env.get("FYERS_TOKEN_DATE") != day.isoformat():
            raise RecorderSetupError(
                "no Fyers access token for today; run `uv run python tasks.py fyers-login`"
            )
        return FyersFeed(
            env.get("FYERS_APP_ID", ""),
            get_secret("FYERS_ACCESS_TOKEN", env),
            max_symbols=rs.symbol_caps.get("fyers", 100),
            depth_counts_separately=rs.depth_counts_separately.get("fyers", True),
            log_dir=rs.log_dir / "fyers_sdk",
        )

    return make


def record_day(
    settings: Settings,
    root: Path,
    *,
    day: date,
    adapter_factory: AdapterFactory,
    sim: bool,
    clock: Clock | None = None,
    stop: threading.Event | None = None,
    fetch_master: bool = True,
    force: bool = False,
    run_qc: bool = True,
    minutes: int | None = None,
) -> RecordResult:
    """Record one day. ``minutes`` (dry runs only) records from now for that long, ignoring
    the calendar and session times."""
    clock = clock or SystemClock()
    stop = stop or threading.Event()
    rs = load_recorder_settings(settings)
    book = RuleBook.load(root / "config" / "rules")
    cal = TradingCalendar.from_rulebook(book)
    if minutes is None and not force and not cal.is_trading_day(day):
        return RecordResult(day, "skipped", "not a trading day")
    if minutes is not None:
        start = from_ns(clock.now_ns())
        end = start + timedelta(minutes=minutes)
        if end.date() != start.date():
            raise RecorderSetupError("a dry run must end before midnight")
        day = start.date()
        primary = MarketSession(
            *rs.primary_session, day, start.time(), end.time(), None, None, verified=False
        )
    else:
        sessions = sessions_for(rs, book, day)
        primary = next(
            (s for s in sessions if (s.exchange, s.segment) == rs.primary_session), sessions[0]
        )
        start, end = recording_window(
            sessions, pre_buffer_min=rs.pre_buffer_min, post_buffer_min=rs.post_buffer_min
        )
    if clock.now_ns() >= to_ns(end):
        return RecordResult(day, "skipped", f"window already closed at {end:%H:%M}")
    master = load_master(
        rs, day, from_ns(clock.now_ns()), fetch=fetch_master, close_time=primary.normal_close
    )
    adapter = adapter_factory(rs, master, clock)
    out_dir = rs.sim_dir if sim else rs.parquet_dir
    source = adapter.name
    quotes = DatasetWriter(out_dir, "quotes", QUOTES_SCHEMA)
    events = DatasetWriter(out_dir, "recorder_events", EVENTS_SCHEMA)
    recorder = Recorder(
        rs.core,
        quotes=quotes,
        events=events,
        session_open=primary.open_dt,
        session_close=primary.close_dt,
        source=source,
        start_ns=clock.now_ns(),
    )
    for mm in lot_size_mismatches(master, book, [u.name for u in rs.universe.underlyings]):
        recorder.note(
            clock.now_ns(),
            "lot_mismatch",
            f"{mm.symbol}: master {mm.master_lot} vs rules {mm.rule_lot} ({mm.rule_status})",
        )
    if not primary.verified:
        recorder.note(clock.now_ns(), "note", "session times include UNVERIFIED rows")
    while not stop.is_set() and clock.now_ns() < to_ns(start):
        stop.wait(1.0)
    runner = RecorderRunner(
        rs,
        adapter=adapter,
        master=master,
        clock=clock,
        recorder=recorder,
        sticky=StickyStore(rs.sticky_file if not sim else rs.sim_dir / "sticky.json", day),
        master_reloader=(
            (
                lambda: load_master(
                    rs,
                    day,
                    from_ns(clock.now_ns()),
                    fetch=True,
                    close_time=primary.normal_close,
                    refresh_tag=from_ns(clock.now_ns()).strftime("%H%M"),
                )
            )
            if fetch_master and not sim
            else None
        ),
    )
    log.info(
        "recording %s %s–%s via %s (cap %d) into %s",
        day,
        start.strftime("%H:%M"),
        end.strftime("%H:%M"),
        source,
        adapter.max_symbols,
        out_dir,
    )
    try:
        runner.start()
        runner.run(to_ns(end), stop)
    finally:
        runner.finish()
    stats = recorder.stats.__dict__ | {
        "restarts": runner.restarts,
        "contracts_added_intraday": runner.contracts_added,
        "dropped_events": runner.dropped_events,
    }
    result = RecordResult(day, "recorded", out_dir=out_dir, stats=stats)
    if run_qc:
        qc = qc_day(
            Store(out_dir),
            day,
            primary,
            gap_threshold_s=rs.core.gap_threshold_s,
            required_strikes_each_side=rs.universe.min_strikes_each_side,
            snapshot_interval_s=rs.core.snapshot_interval_s,
        )
        _, md = write_qc(qc, rs.qc_dir / ("sim" if sim else ""))
        result.qc_report, result.qc_passed = md, qc.passed
    compact_day(out_dir, "quotes", day, QUOTES_SCHEMA)
    compact_day(out_dir, "recorder_events", day, EVENTS_SCHEMA)
    return result
