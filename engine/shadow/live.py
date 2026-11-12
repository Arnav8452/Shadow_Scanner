"""Live V1 shadow scanner on Upstox Plus ``full_d30`` (30-level depth, at most 50 keys).

Watch-only: it scores the order book, keeps a forward-test ledger and writes a dashboard. It
never routes orders (V1). Thresholds are unvalidated hypotheses (settings ``shadow_scanner``).

Inputs per instrument, and where they come from:

* 30-level book, LTP, volume, OI, closing-auction fields — the ``full_d30`` socket. Every depth
  message is also recorded to the ``depth30`` dataset, so the air-pocket (levels 6–25) and other
  deep-book signals can be tested on real history later.
* Previous day's high / low / close (stop pools PDH/PDL) — the recorder's own data for the
  previous trading day (``quotes`` dataset, last row per symbol). Missing on the first day.
* 15-minute opening range (ORH/ORL) — built live from LTP between the session open and
  open + 15 minutes, then frozen.
* Call / put OI walls — the recorder's latest chain snapshot for the watched expiry, refreshed
  every minute (index and futures states only).
* Delivery bias — optional ``delivery_watch`` group: F&O stocks with an EOD delivery spike,
  picked by the evening job (``engine.data.eod.delivery_watch``) from NSE files of the previous
  session(s). They take the slots after the core symbols, ahead of the farthest options, and
  carry that session's high/low/close as previous-day levels.
* Breadth, dealer gamma — not available yet; left neutral so they add no points.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from engine.core.calendar import CalendarCoverageError, TradingCalendar
from engine.core.clock import NS_PER_S, Clock, MarketSession, SystemClock, from_ns, to_ns
from engine.core.config import REPO_ROOT, EngineVersion, Settings
from engine.core.env import load_dotenv
from engine.core.rules import IST, RuleBook
from engine.data.adapters.base import Depth, FeedAdapter, FeedEvent, FeedStatus, Quote, StatusKind
from engine.data.eod.bhavcopy import DeliveryBias
from engine.data.eod.delivery_watch import (
    DeliveryWatchItem,
    OptionLiquidity,
    SpikeThresholds,
    load_delivery_watchlist,
)
from engine.data.recorder.run import (
    RecorderSettings,
    RecorderSetupError,
    load_master,
    load_recorder_settings,
    sessions_for,
    sim_spots,
    upstox_token,
)
from engine.data.recorder.universe import band_indices
from engine.data.reference.instrument_master import (
    Instrument,
    InstrumentMaster,
    Kind,
    classify_expiries,
)
from engine.data.schema import DEPTH30_SCHEMA
from engine.data.store import DatasetWriter, Store, compact_day, ns_to_us, s_to_us
from engine.shadow.models import ShadowSignal
from engine.shadow.report import _serialize_outcome, render_shadow_dashboard_html
from engine.shadow.scanner import InstrumentMicrostructureState, ShadowOrderBookScanner

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class WatchSpec:
    name: str  # underlying, e.g. NIFTY
    futures_months: int = 1
    option_strikes_each_side: int = 5
    expiry_role: str = "current_weekly"


@dataclass(frozen=True)
class DeliveryWatchConfig:
    """Optional watchlist group of delivery-spike stocks (evening job output)."""

    enabled: bool = False
    dir: Path = Path("data/state/delivery_watchlist")
    max_symbols: int = 5
    max_age_days: int = 5
    biases: tuple[str, ...] = ("LONG_ACCUMULATION", "RETAIL_CHURN_FADE")
    # full_d30 (30 levels, counts toward the 50-key cap) or full (5 levels). The 11 Nov probe got
    # Reliance cash data in full_d30 (indices come back empty there); 30 levels unconfirmed.
    feed_mode: str = "full_d30"
    # HYPOTHESIS thresholds (untested; see engine/research/delivery_event_study.py).
    spike: SpikeThresholds = field(default_factory=SpikeThresholds)
    option_liquidity: OptionLiquidity = field(default_factory=OptionLiquidity)
    # Until the event study supports it, spikes only choose what to watch: the bias is not
    # given to the scanner (no delivery points, no VWAP_DELIVERY_RECLAIM). Owner decision.
    bias_in_score: bool = False


@dataclass(frozen=True)
class ShadowLiveConfig:
    watch: tuple[WatchSpec, ...]
    max_symbols: int = 50  # Upstox Plus full_d30: 50 keys per connection
    include_vix: bool = True
    recenter_interval_s: int = 300
    dashboard_interval_s: int = 60
    oi_wall_refresh_s: int = 60
    flush_interval_s: float = 30.0
    # No feed message for this long in market hours -> restart the socket (the adapter's own
    # ping catches most drops; this catches a silent, half-open connection).
    watchdog_silence_s: float = 60.0
    opening_range_min: int = 15
    alert_grades: tuple[str, ...] = ("A_PLUS",)
    max_alerts_per_day: int = 10
    alert_cooldown_min: int = 15
    delivery_watch: DeliveryWatchConfig = field(default_factory=DeliveryWatchConfig)


def delivery_watch_config(raw: Any, root: Path) -> DeliveryWatchConfig:
    if not isinstance(raw, Mapping):
        return DeliveryWatchConfig()
    biases = tuple(str(b) for b in raw.get("biases", DeliveryWatchConfig.biases))
    for b in biases:
        if b not in DeliveryBias.__members__ or b == "NEUTRAL":
            raise RecorderSetupError(f"shadow_live.delivery_watch.biases: unknown bias {b!r}")
    cfg = DeliveryWatchConfig(
        enabled=bool(raw.get("enabled", False)),
        dir=root / str(raw.get("dir", "data/state/delivery_watchlist")),
        max_symbols=int(raw.get("max_symbols", 5)),
        max_age_days=int(raw.get("max_age_days", 5)),
        biases=biases,
        feed_mode=str(raw.get("feed_mode", "full_d30")),
        spike=SpikeThresholds(**{k: float(v) for k, v in (raw.get("spike") or {}).items()}),
        option_liquidity=OptionLiquidity(
            **{k: int(v) for k, v in (raw.get("option_liquidity") or {}).items()}
        ),
        bias_in_score=bool(raw.get("bias_in_score", False)),
    )
    if cfg.feed_mode not in ("full_d30", "full"):
        raise RecorderSetupError("shadow_live.delivery_watch.feed_mode must be full_d30 or full")
    if not 0 <= cfg.max_symbols <= 10:
        raise RecorderSetupError("shadow_live.delivery_watch.max_symbols must be 0..10")
    return cfg


def load_shadow_live_config(settings: Settings) -> ShadowLiveConfig:
    raw = settings.raw.get("shadow_live")
    if not isinstance(raw, Mapping):
        raise RecorderSetupError("config/settings.yaml has no 'shadow_live' section")
    watch = tuple(
        WatchSpec(
            name=str(w["name"]),
            futures_months=int(w.get("futures_months", 1)),
            option_strikes_each_side=int(w.get("option_strikes_each_side", 5)),
            expiry_role=str(w.get("expiry", "current_weekly")),
        )
        for w in raw.get("watch", [])
    )
    if not watch:
        raise RecorderSetupError("shadow_live.watch is empty")
    cfg = ShadowLiveConfig(
        watch=watch,
        max_symbols=int(raw.get("max_symbols", 50)),
        include_vix=bool(raw.get("include_vix", True)),
        recenter_interval_s=int(raw.get("recenter_interval_s", 300)),
        dashboard_interval_s=int(raw.get("dashboard_interval_s", 60)),
        oi_wall_refresh_s=int(raw.get("oi_wall_refresh_s", 60)),
        flush_interval_s=float(raw.get("flush_interval_s", 30)),
        watchdog_silence_s=float(raw.get("watchdog_silence_s", 60)),
        opening_range_min=int(raw.get("opening_range_min", 15)),
        alert_grades=tuple(str(g) for g in raw.get("alert_grades", ["A_PLUS"])),
        max_alerts_per_day=int(raw.get("max_alerts_per_day", 10)),
        alert_cooldown_min=int(raw.get("alert_cooldown_min", 15)),
        delivery_watch=delivery_watch_config(raw.get("delivery_watch"), REPO_ROOT),
    )
    if not 1 <= cfg.max_symbols <= 50:
        raise RecorderSetupError("shadow_live.max_symbols must be 1..50 (Upstox full_d30 limit)")
    return cfg


# --------------------------------------------------------------------------- watchlist


def select_watchlist(
    master: InstrumentMaster,
    cfg: ShadowLiveConfig,
    rs: RecorderSettings,
    *,
    now: datetime,
    spots: Mapping[str, float],
    extra: Iterable[str] = (),
) -> list[str]:
    """Spots, VIX, front futures, the optional ``extra`` group (delivery-spike stocks), then
    options nearest ATM (both underlyings interleaved by distance), cut at ``cfg.max_symbols``.
    Options need a spot price, so the first call returns only the core symbols."""
    on = now.date()
    spot_of = {u.name: u.spot_symbol for u in rs.universe.underlyings}
    core: list[str] = [spot_of[w.name] for w in cfg.watch if w.name in spot_of]
    if cfg.include_vix and rs.universe.vix_symbol:
        core.append(rs.universe.vix_symbol)
    for w in cfg.watch:
        core += [f.symbol for f in master.futures(w.name, on)[: w.futures_months]]
    core = [s for s in dict.fromkeys(core) if s in master]
    core += [s for s in extra if s in master and s not in core]
    ranked: list[tuple[int, str, float, str]] = []
    for w in cfg.watch:
        spot = spots.get(w.name)
        if not spot:
            continue
        roles = classify_expiries(master.option_expiries(w.name, on), (w.expiry_role,))
        expiry = roles.get(w.expiry_role)
        if expiry is None:
            continue
        strikes = master.strikes(w.name, expiry)
        if not strikes:
            continue
        lo, atm, hi = band_indices(strikes, spot, w.option_strikes_each_side, 0.0)
        by_key = {(i.strike, i.kind): i.symbol for i in master.options(w.name, expiry)}
        for idx in range(lo, hi + 1):
            for kind in (Kind.CALL, Kind.PUT):
                sym = by_key.get((strikes[idx], kind))
                if sym:
                    ranked.append((abs(idx - atm), w.name, strikes[idx], sym))
    ranked.sort()
    out = core + [r[3] for r in ranked]
    return list(dict.fromkeys(out))[: cfg.max_symbols]


def resolve_equities(master: InstrumentMaster, symbols: Iterable[str]) -> dict[str, str]:
    """NSE trading symbol -> broker key of its cash-market instrument (``NSE_EQ|<ISIN>`` on
    Upstox, ``NSE:<SYM>-EQ`` on Fyers/sim). Symbols without one are left out."""
    want = set(symbols)
    out: dict[str, str] = {}
    for ins in master.all():
        if ins.kind is not Kind.EQUITY or ins.exchange != "NSE":
            continue
        for name in (ins.underlying, ins.description):
            if name in want and name not in out:
                out[name] = ins.symbol
    return out


def delivery_group(
    cfg: DeliveryWatchConfig, master: InstrumentMaster, day: date
) -> tuple[dict[str, DeliveryWatchItem], str]:
    """``broker key -> item`` for today's delivery group, and a line for the log."""
    if not cfg.enabled or cfg.max_symbols == 0:
        return {}, "delivery watch off"
    file_day, items = load_delivery_watchlist(cfg.dir, day, max_age_days=cfg.max_age_days)
    if file_day is None:
        return {}, "delivery watch: no watchlist file yet (tasks.py evening)"
    items = [i for i in items if i.bias in cfg.biases][: cfg.max_symbols]
    keys = resolve_equities(master, (i.symbol for i in items))
    group = {keys[i.symbol]: i for i in items if i.symbol in keys}
    missing = [i.symbol for i in items if i.symbol not in keys]
    note = f"delivery watch {file_day}: {len(group)} stock(s)"
    if missing:
        note += f"; not in the instrument master: {', '.join(missing)}"
    return group, note


# --------------------------------------------------------------------------- daily inputs


def previous_day_levels(store: Store, prev_day: date) -> dict[str, tuple[float, float, float]]:
    """``symbol -> (high, low, close)`` from the recorder's last row per symbol on ``prev_day``.
    Known-at: the end of that day, so this is point-in-time safe for today."""
    as_of = datetime.combine(prev_day, time(23, 59, 59), IST)
    table = store.latest(prev_day, as_of=as_of)
    out: dict[str, tuple[float, float, float]] = {}
    if table.num_rows == 0:
        return out
    for r in table.select(["symbol", "high", "low", "ltp"]).to_pylist():
        hi, lo, last = r["high"] or 0.0, r["low"] or 0.0, r["ltp"] or 0.0
        if hi > 0 and lo > 0:
            out[str(r["symbol"])] = (float(hi), float(lo), float(last))
    return out


# NSE index names in ind_close_all for the index underlyings the scanner watches.
_EOD_INDEX_NAMES = {
    "NIFTY": "nifty 50",
    "BANKNIFTY": "nifty bank",
    "FINNIFTY": "nifty financial services",
    "MIDCPNIFTY": "nifty midcap select",
    "NIFTYNXT50": "nifty next 50",
}


def eod_previous_day_levels(
    raw_dir: Path,
    prev_day: date,
    instruments: Iterable[Instrument],
    *,
    now: datetime | None = None,
    fetcher: Callable[[str], bytes] | None = None,
) -> dict[str, tuple[float, float, float]]:
    """``symbol -> (high, low, close)`` for ``prev_day`` from NSE's end-of-day files.

    Covers NSE index spots, India VIX, and NSE futures and options (matched on underlying,
    expiry, strike and type). BSE contracts (Sensex) are not in these files. Files are known
    from 19:30 IST on ``prev_day``, so they are point-in-time safe for the next session. With
    ``now`` given, a missing file is downloaded first (not before 19:30 of ``prev_day``).
    """
    from engine.core.net import http_get_bytes
    from engine.data.eod.bhavcopy import parse_fno_bhavcopy_csv
    from engine.data.eod.download import eod_path, fetch_eod, read_eod_text

    if now is not None and now >= datetime.combine(prev_day, time(19, 30), IST):
        for ds in ("fo", "indices"):
            res = fetch_eod(
                ds, prev_day, raw_dir=raw_dir, now=now, fetcher=fetcher or http_get_bytes
            )
            if res.status not in ("ok", "cached"):
                log.warning("EOD %s for %s: %s %s", ds, prev_day, res.status, res.detail)

    index_levels: dict[str, tuple[float, float, float]] = {}
    idx_path = eod_path(raw_dir, "indices", prev_day)
    if idx_path.exists():
        for line in read_eod_text(idx_path).splitlines()[1:]:
            cells = [c.strip() for c in line.split(",")]
            if len(cells) < 6:
                continue
            try:
                hi, lo, close = float(cells[3]), float(cells[4]), float(cells[5])
            except ValueError:
                continue
            if hi > 0 and lo > 0:
                index_levels[cells[0].lower()] = (hi, lo, close)

    contract_levels: dict[tuple[str, date, float, str], tuple[float, float, float]] = {}
    fo_path = eod_path(raw_dir, "fo", prev_day)
    if fo_path.exists():
        for row in parse_fno_bhavcopy_csv(read_eod_text(fo_path)):
            if row.trade_date != prev_day or row.high <= 0 or row.low <= 0:
                continue  # untraded contracts carry zero high/low
            kind = row.option_type if row.option_type in ("CE", "PE") else "FUT"
            strike = round(row.strike, 2) if kind != "FUT" else 0.0
            contract_levels[(row.symbol, row.expiry, strike, kind)] = (
                row.high,
                row.low,
                row.close,
            )

    out: dict[str, tuple[float, float, float]] = {}
    for ins in instruments:
        if ins.exchange != "NSE":
            continue
        if ins.kind is Kind.INDEX:
            name = (
                "india vix" if "VIX" in ins.symbol.upper() else _EOD_INDEX_NAMES.get(ins.underlying)
            )
            if name and name in index_levels:
                out[ins.symbol] = index_levels[name]
        elif ins.kind in (Kind.FUTURE, Kind.CALL, Kind.PUT) and ins.expiry is not None:
            strike = round(ins.strike or 0.0, 2) if ins.kind.is_option else 0.0
            key = (ins.underlying, ins.expiry, strike, str(ins.kind.value))
            if key in contract_levels:
                out[ins.symbol] = contract_levels[key]
    return out


def oi_walls(
    store: Store, day: date, *, underlying: str, expiry: date, now: datetime, lookback_s: int = 180
) -> tuple[float, float] | None:
    """(call wall, put wall): strikes with the largest call / put OI in the recorder's latest
    chain snapshot (rows known by ``now``)."""
    if not store.has("quotes", day):
        return None
    src = store.source_sql("quotes", day)
    rows = store.rows(
        f"""SELECT kind, strike, oi FROM {src}
            WHERE capture = 'snap' AND underlying = ? AND expiry = ? AND kind IN ('CE', 'PE')
              AND oi IS NOT NULL AND ts <= ? AND ts >= ?
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ts DESC, seq DESC) = 1""",
        [underlying, expiry, now, now - timedelta(seconds=lookback_s)],
    )
    best: dict[str, tuple[int, float]] = {}
    for kind, strike, oi in rows:
        if oi and (kind not in best or oi > best[kind][0]):
            best[kind] = (int(oi), float(strike))
    if "CE" not in best or "PE" not in best:
        return None
    return best["CE"][1], best["PE"][1]


# --------------------------------------------------------------------------- runner


@dataclass
class ShadowLiveStats:
    quotes: int = 0
    depths: int = 0
    evaluations: int = 0
    signals: int = 0  # distinct tradeable-grade episodes
    alerts: int = 0
    feed_events: dict[str, int] = field(default_factory=dict)
    watchlist: int = 0
    dropped_events: int = 0


class ShadowLiveRunner:
    def __init__(
        self,
        cfg: ShadowLiveConfig,
        rs: RecorderSettings,
        *,
        scanner: ShadowOrderBookScanner,
        master: InstrumentMaster,
        adapter: FeedAdapter,
        clock: Clock,
        session: MarketSession,
        depth_writer: DatasetWriter,
        store: Store | None,
        report_path: Path,
        ledger_file: Path,
        feed_label: str,
        prev_levels: Mapping[str, tuple[float, float, float]] | None = None,
        alert: Callable[[str], None] | None = None,
        cockpit: Any | None = None,
        record_depth: bool = True,
        delivery: Mapping[str, DeliveryWatchItem] | None = None,
    ) -> None:
        self.cfg = cfg
        self.delivery = dict(delivery or {})
        self.rs = rs
        self.scanner = scanner
        self.master = master
        self.adapter = adapter
        self.clock = clock
        self.session = session
        self.depth_writer = depth_writer
        self.store = store
        self.report_path = report_path
        self.ledger_file = ledger_file
        self.feed_label = feed_label
        self.prev_levels = dict(prev_levels or {})
        self.alert = alert
        # Cockpit hook (engine/cockpit/core.py); every call is guarded, see _cockpit().
        self.cockpit = cockpit
        self.cockpit_errors = 0
        self.record_depth = record_depth  # False in replay: never re-record recorded data
        self.last_eval: dict[str, ShadowSignal] = {}
        self.lag_ms: deque[float] = deque(maxlen=500)
        self.last_feed_status = ""
        self.queue: queue.Queue[FeedEvent] = queue.Queue(maxsize=200_000)
        self.subscribed: list[str] = []
        self.stats = ShadowLiveStats()
        self.seq = 0
        self._episodes: dict[tuple[str, str, str], int] = {}
        self.ledger_written = 0
        self.open_ns = to_ns(session.open_dt)
        self.or_end_ns = to_ns(session.open_dt + timedelta(minutes=cfg.opening_range_min))
        self.or_hi: dict[str, float] = {}
        self.or_lo: dict[str, float] = {}
        self.or_frozen = False
        # Paper trades still open at this time are closed at the book (config).
        self.square_off_ns = to_ns(session.at(scanner.config.paper_square_off_at))
        self.squared_off = False
        now = clock.now_ns()
        self._next_recenter = 0
        self._next_dashboard = now + cfg.dashboard_interval_s * NS_PER_S
        self._next_oi = now
        self._next_flush = now + int(cfg.flush_interval_s * NS_PER_S)
        self.spot_symbol = {u.name: u.spot_symbol for u in rs.universe.underlyings}
        # Contracts added on demand (owner -> symbols): the page's "open any instrument" and a
        # strategy's legs. They displace the farthest auto-selected options, never the cap.
        self.pins: dict[str, list[str]] = {}
        self.last_event_ns = now  # last feed message of any kind (for the watchdog)
        self.watchdog_restarts = 0
        self._close_ns = to_ns(session.close_dt)

    def pin(self, owner: str, symbols: list[str], *, limit: int = 10) -> list[str]:
        """Keep ``symbols`` in the feed for ``owner`` (replaces that owner's earlier pins).
        Returns the symbols accepted (known to the master, within ``limit`` pins in total)."""
        others = {s for o, syms in self.pins.items() if o != owner for s in syms}
        room = max(0, limit - len(others))
        ok = [s for s in dict.fromkeys(symbols) if s in self.master][:room]
        if ok:
            self.pins[owner] = ok
        else:
            self.pins.pop(owner, None)
        self.recenter(self.clock.now_ns(), f"pin {owner}")
        return ok

    def pinned(self) -> list[str]:
        return list(dict.fromkeys(s for syms in self.pins.values() for s in syms))

    # ------------------------------------------------------------------ events

    def _enqueue(self, ev: FeedEvent) -> None:
        try:
            self.queue.put_nowait(ev)
        except queue.Full:  # the recorder is the system of record; count, don't hide
            self.stats.dropped_events += 1

    def _cockpit(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run a cockpit hook; its failures are logged and counted, never raised."""
        try:
            fn(*args)
        except Exception:
            self.cockpit_errors += 1
            if self.cockpit_errors <= 5 or self.cockpit_errors % 1000 == 0:
                log.exception("cockpit hook failed (%d so far)", self.cockpit_errors)

    def start(self) -> None:
        if self.cockpit is not None:
            self._cockpit(self.cockpit.attach, self)
        self.adapter.start(self._enqueue)
        self.recenter(self.clock.now_ns(), "start")

    def spots(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, sym in self.spot_symbol.items():
            st = self.scanner.states.get(sym)
            if st is not None and st.ltp > 0:
                out[name] = st.ltp
        return out

    def _state_for(self, ins: Instrument) -> InstrumentMicrostructureState:
        item = self.delivery.get(ins.symbol)
        default = (item.prev_high, item.prev_low, item.prev_close) if item else (0.0, 0.0, 0.0)
        hi, lo, close = self.prev_levels.get(ins.symbol, default)
        return InstrumentMicrostructureState(
            symbol=ins.symbol,
            tick_size=ins.tick_size or 0.05,
            lot_size=max(1, ins.lot_size),
            pdh=hi,
            pdl=lo,
            prev_close=close,
            is_option=ins.kind.is_option,
            is_equity=ins.kind is Kind.EQUITY,
            underlying=ins.underlying,
            delivery_bias=(
                DeliveryBias(item.bias)
                if item and self.cfg.delivery_watch.bias_in_score
                else DeliveryBias.NEUTRAL
            ),
        )

    def recenter(self, now_ns: int, reason: str) -> None:
        want = select_watchlist(
            self.master,
            self.cfg,
            self.rs,
            now=from_ns(now_ns),
            spots=self.spots(),
            extra=[*self.delivery],
        )
        pinned = self.pinned()
        if pinned:
            auto = [s for s in want if s not in pinned]
            want = list(dict.fromkeys(auto[: max(0, self.cfg.max_symbols - len(pinned))] + pinned))
        gone = [s for s in self.subscribed if s not in want]
        new = [s for s in want if s not in self.subscribed]
        if gone:
            self.adapter.unsubscribe(gone, depth=True)
            for s in gone:
                if s not in self.scanner.active_signals:
                    self.scanner.states.pop(s, None)
        for s in new:
            if s not in self.scanner.states:
                try:
                    self.scanner.register_instrument(self._state_for(self.master.get(s)))
                except ValueError as exc:
                    log.warning("shadow watchlist: %s", exc)
        if new:
            self.adapter.subscribe(new, depth=True)
        self.subscribed = want
        self.stats.watchlist = len(want)
        if new or gone:
            log.info(
                "shadow watchlist %s: %d keys (+%d -%d)", reason, len(want), len(new), len(gone)
            )
        self._next_recenter = now_ns + self.cfg.recenter_interval_s * NS_PER_S

    def on_event(self, ev: FeedEvent) -> None:
        if not isinstance(ev, FeedStatus):
            self.last_event_ns = self.clock.now_ns()
        if isinstance(ev, FeedStatus):
            key = ev.kind.value
            self.last_feed_status = key
            self.stats.feed_events[key] = self.stats.feed_events.get(key, 0) + 1
            if ev.kind in (StatusKind.AUTH_FAILED, StatusKind.LIMIT, StatusKind.ERROR):
                log.warning("shadow feed %s: %s", key, ev.detail)
            return
        if isinstance(ev, Quote):
            self.stats.quotes += 1
            self.scanner.on_quote(ev)
            if ev.ltp and self.open_ns <= ev.recv_ns < self.or_end_ns:
                self.or_hi[ev.symbol] = max(self.or_hi.get(ev.symbol, ev.ltp), ev.ltp)
                self.or_lo[ev.symbol] = min(self.or_lo.get(ev.symbol, ev.ltp), ev.ltp)
            return
        self.stats.depths += 1
        if ev.exch_ts:
            self.lag_ms.append(ev.recv_ns / 1e6 - ev.exch_ts * 1000.0)
        if self.record_depth:
            self._record_depth(ev)
        sig = self.scanner.on_depth(ev, now=from_ns(ev.recv_ns))
        if sig is not None:
            self.last_eval[ev.symbol] = sig
            self._on_signal(sig)
        else:  # not scored (e.g. a dead option): do not keep showing its old reading
            self.last_eval.pop(ev.symbol, None)

    def _record_depth(self, d: Depth) -> None:
        ins = self.master.get(d.symbol) if d.symbol in self.master else None
        self.seq += 1
        self.depth_writer.append(
            {
                "ts": ns_to_us(d.recv_ns),
                "symbol": d.symbol,
                "underlying": ins.underlying if ins else None,
                "kind": ins.kind.value if ins else None,
                "expiry": ins.expiry if ins else None,
                "strike": ins.strike if ins else None,
                "exch_ts": s_to_us(d.exch_ts),
                "levels": max(len(d.bids), len(d.asks)),
                "bid_px": [lv[0] for lv in d.bids],
                "bid_qty": [lv[1] for lv in d.bids],
                "ask_px": [lv[0] for lv in d.asks],
                "ask_qty": [lv[1] for lv in d.asks],
                "seq": self.seq,
                "source": self.adapter.name,
            }
        )

    def _on_signal(self, sig: ShadowSignal) -> None:
        """The scanner re-evaluates on every depth message and returns a new signal each time.
        A signal *episode* is (symbol, direction, setup) at a tradeable grade; it is counted and
        alerted once, and continues while it keeps reappearing within the cool-down."""
        self.stats.evaluations += 1
        if sig.grade.value not in ("A_PLUS", "ACTIONABLE") or not getattr(sig, "confirmed", True):
            return
        key = (sig.symbol, sig.direction, sig.wave.value)
        now_ns = to_ns(sig.created_at)
        last = self._episodes.get(key)
        self._episodes[key] = now_ns
        if last is not None and now_ns - last < self.cfg.alert_cooldown_min * 60 * NS_PER_S:
            return
        self.stats.signals += 1
        if self.cockpit is not None:
            self._cockpit(self.cockpit.on_episode, sig)
        if (
            self.alert is not None
            and sig.grade.value in self.cfg.alert_grades
            and self.stats.alerts < self.cfg.max_alerts_per_day
        ):
            self.stats.alerts += 1
            self.alert(
                f"HYPOTHESIS shadow signal (unvalidated, not a recommendation): "
                f"{sig.direction} {sig.symbol} score {sig.confidence_score:.0f} "
                f"entry {sig.entry_price:.2f} SL {sig.stop_loss:.2f} T1 {sig.target_1:.2f}"
            )

    # ------------------------------------------------------------------ timers

    def _freeze_opening_range(self) -> None:
        for sym, st in self.scanner.states.items():
            if sym in self.or_hi:
                st.orh_15m = self.or_hi[sym]
                st.orl_15m = self.or_lo[sym]
        self.or_frozen = True
        log.info("opening range frozen for %d instruments", len(self.or_hi))

    def _refresh_oi_walls(self, now_ns: int) -> None:
        if self.store is None:
            return
        now = from_ns(now_ns)
        for w in self.cfg.watch:
            roles = classify_expiries(
                self.master.option_expiries(w.name, now.date()), (w.expiry_role,)
            )
            expiry = roles.get(w.expiry_role)
            if expiry is None:
                continue
            try:
                walls = oi_walls(self.store, now.date(), underlying=w.name, expiry=expiry, now=now)
            except Exception as exc:  # the recorder may be mid-write; try again next minute
                log.debug("oi walls unavailable: %s", exc)
                continue
            if walls is None:
                continue
            for sym, st in self.scanner.states.items():
                ins = self.master.get(sym) if sym in self.master else None
                if ins and ins.underlying == w.name and ins.kind in (Kind.INDEX, Kind.FUTURE):
                    st.call_oi_wall, st.put_oi_wall = walls

    def write_dashboard(self) -> Path:
        html = render_shadow_dashboard_html(
            self.scanner,
            session_date=self.session.day,
            engine_version=EngineVersion.V1_SHADOW,
            data_feed=self.feed_label,
        )
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.report_path.with_suffix(".tmp")
        tmp.write_text(html, encoding="utf-8")
        os.replace(tmp, self.report_path)
        return self.report_path

    def append_ledger(self) -> int:
        new = self.scanner.closed_outcomes[self.ledger_written :]
        if new:
            self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_file.open("a", encoding="utf-8") as fh:
                for outcome in new:
                    fh.write(_serialize_outcome(outcome) + "\n")
            self.ledger_written += len(new)
        return len(new)

    def step(self, max_wait_s: float = 0.5) -> None:
        try:
            ev: FeedEvent | None = (
                self.queue.get(timeout=max_wait_s) if max_wait_s > 0 else self.queue.get_nowait()
            )
        except queue.Empty:
            ev = None
        # With the cockpit on, publish between batches so a burst (e.g. a max-speed replay)
        # never freezes the page. Only take the next event when it will be processed.
        cap = 2_000 if self.cockpit is not None else 50_000
        n = 0
        while ev is not None:
            self.on_event(ev)
            n += 1
            if n >= cap:
                break
            try:
                ev = self.queue.get_nowait()
            except queue.Empty:
                ev = None
        now = self.clock.now_ns()
        pinned = set(self.pinned())
        # Pinned contracts (strategy legs, the page's picks) do not count: the auto-selected
        # strikes around spot still have to be added once the spot price is known.
        has_options = any(
            self.master.get(s).kind.is_option
            for s in self.subscribed
            if s in self.master and s not in pinned
        )
        if now >= self._next_recenter or (not has_options and self.spots()):
            self.recenter(now, "recenter")
        if not self.or_frozen and now >= self.or_end_ns:
            self._freeze_opening_range()
        if not self.squared_off and now >= self.square_off_ns:
            self.scanner.close_open_signals(from_ns(now), "Square-off time")
            self.squared_off = True
        if now >= self._next_oi:
            self._refresh_oi_walls(now)
            self._next_oi = now + self.cfg.oi_wall_refresh_s * NS_PER_S
        if now >= self._next_flush:
            self.depth_writer.flush()
            self._next_flush = now + int(self.cfg.flush_interval_s * NS_PER_S)
        if now >= self._next_dashboard:
            self.write_dashboard()
            self.append_ledger()
            self._next_dashboard = now + self.cfg.dashboard_interval_s * NS_PER_S
        self._watchdog(now)
        if self.cockpit is not None:
            self._cockpit(self.cockpit.on_step, now)

    def _watchdog(self, now: int) -> None:
        """Restart a silent feed during market hours, at most once per silence period."""
        silent_s = (now - self.last_event_ns) / NS_PER_S
        if not (self.open_ns <= now < self._close_ns) or silent_s < self.cfg.watchdog_silence_s:
            return
        self.watchdog_restarts += 1
        log.warning("shadow feed silent for %.0f s in market hours: restarting it", silent_s)
        self.last_event_ns = now  # wait another full period before the next restart
        try:
            self.adapter.restart()
        except Exception as exc:  # the adapter's own reconnect loop keeps trying
            log.warning("shadow feed restart failed: %s", type(exc).__name__)

    def run(self, end_ns: int, stop: threading.Event) -> None:
        # With the cockpit on, wake at least 10x a second so snapshots go out on time.
        wait = 0.1 if self.cockpit is not None else 0.5
        while not stop.is_set() and self.clock.now_ns() < end_ns:
            self.step(wait)

    def finish(self) -> None:
        self.adapter.stop()
        self.step(0)
        # Never leave paper trades open: close at the last book seen (e.g. a short test run or a
        # feed that stopped early). Untriggered signals are dropped and counted.
        self.scanner.close_open_signals(from_ns(self.clock.now_ns()), "Session end")
        if self.cockpit is not None:
            self._cockpit(self.cockpit.finish, from_ns(self.clock.now_ns()))
        self.depth_writer.flush()
        self.write_dashboard()
        self.append_ledger()


# --------------------------------------------------------------------------- one day


@dataclass
class ShadowRunResult:
    day: date
    status: str  # ran | skipped
    detail: str = ""
    dashboard: Path | None = None
    stats: dict[str, Any] = field(default_factory=dict)


AdapterFactory = Callable[
    [ShadowLiveConfig, RecorderSettings, InstrumentMaster, Clock], FeedAdapter
]


def default_shadow_adapter(
    settings: Settings, root: Path, *, sim: bool, day: date
) -> AdapterFactory:
    def make(
        cfg: ShadowLiveConfig, rs: RecorderSettings, master: InstrumentMaster, clock: Clock
    ) -> FeedAdapter:
        if sim:
            from engine.data.adapters.sim import SimFeed

            return SimFeed(
                master,
                clock,
                spots=sim_spots(master, rs, day),
                max_symbols=cfg.max_symbols,
                threaded=True,
                depth_levels=30,
            )
        if settings.data_primary != "upstox":
            raise RecorderSetupError(
                "the live shadow scanner needs data.primary: upstox (full_d30)"
            )
        from engine.data.adapters.upstox import MODE_FULL_D30, UpstoxFeed

        env: dict[str, str] = dict(os.environ)
        load_dotenv(root / ".env", env)
        stocks_in_full = cfg.delivery_watch.feed_mode == "full"
        return UpstoxFeed(
            token=upstox_token(env, day),
            mode=MODE_FULL_D30,
            max_symbols=cfg.max_symbols,
            full_mode_prefixes=("NSE_EQ|",) if stocks_in_full else (),
        )

    return make


def run_shadow_day(
    settings: Settings,
    root: Path,
    *,
    day: date,
    adapter_factory: AdapterFactory,
    sim: bool,
    clock: Clock | None = None,
    stop: threading.Event | None = None,
    force: bool = False,
    minutes: int | None = None,
    fetch_master: bool = True,
    alert: Callable[[str], None] | None = None,
) -> ShadowRunResult:
    clock = clock or SystemClock()
    stop = stop or threading.Event()
    rs = load_recorder_settings(settings)
    cfg = load_shadow_live_config(settings)
    book = RuleBook.load(root / "config" / "rules")
    cal = TradingCalendar.from_rulebook(book)
    if minutes is None and not force and not cal.is_trading_day(day):
        return ShadowRunResult(day, "skipped", "not a trading day")
    sessions = sessions_for(rs, book, day)
    primary = next(
        (s for s in sessions if (s.exchange, s.segment) == rs.primary_session), sessions[0]
    )
    if minutes is not None:
        start = from_ns(clock.now_ns())
        end = start + timedelta(minutes=minutes)
    else:
        start = primary.first_dt - timedelta(minutes=rs.pre_buffer_min)
        end = primary.close_dt + timedelta(minutes=rs.post_buffer_min)
        if clock.now_ns() >= to_ns(end):
            return ShadowRunResult(day, "skipped", f"window already closed at {end:%H:%M}")
    master = load_master(
        rs, day, from_ns(clock.now_ns()), fetch=fetch_master, close_time=primary.normal_close
    )
    data_dir = rs.sim_dir if sim else rs.parquet_dir
    store = Store(data_dir)
    prev_levels: dict[str, tuple[float, float, float]] = {}
    eod_filled = 0
    try:
        prev_day = cal.previous_trading_day(day)
        prev_levels = previous_day_levels(store, prev_day)
        if not sim:
            # Our own recordings first (they cover BSE/Sensex too); NSE's EOD files fill the rest,
            # e.g. on the first recording day or for contracts the recorder did not watch.
            eod = eod_previous_day_levels(
                rs.raw_dir, prev_day, master.all(), now=from_ns(clock.now_ns())
            )
            for sym, lv in eod.items():
                if sym not in prev_levels:
                    prev_levels[sym] = lv
                    eod_filled += 1
    except (CalendarCoverageError, OSError) as exc:
        log.warning("previous-day levels unavailable: %s", exc)
    if sim:  # synthetic data never lands next to real dashboards or the real ledger
        report_path = root / "reports" / "demo" / "shadow_scanner" / f"{day.isoformat()}.html"
        ledger_file = root / "data" / "demo" / "shadow_ledger.jsonl"
        feed_label = "SYNTHETIC SIM (not market data)"
    else:
        report_path = settings.shadow_scanner.report_dir / f"{day.isoformat()}.html"
        ledger_file = settings.shadow_scanner.ledger_file
        feed_label = "upstox full_d30 (30-level depth)"
    scanner = ShadowOrderBookScanner(
        settings.shadow_scanner,
        capital_inr=settings.risk.capital_inr,
        max_lots=settings.risk.max_lots_per_strategy,
    )
    delivery, delivery_note = delivery_group(cfg.delivery_watch, master, day)
    log.info(delivery_note)
    adapter = adapter_factory(cfg, rs, master, clock)
    cockpit = None
    try:
        from engine.cockpit.build import build_cockpit
        from engine.cockpit.config import load_cockpit_config

        ccfg = load_cockpit_config(settings, root)
        cockpit = build_cockpit(
            settings,
            root,
            source="sim" if sim else "live",
            day=day,
            session=primary,
            master=master,
            rs=rs,
            book=book,
            calendar=cal,
            port=ccfg.engine_port,
            journal_dir=(root / "data" / "demo") if sim else ccfg.journal_dir,
            chain_dir=data_dir,
        )
    except Exception as exc:  # the cockpit is optional; recording must start regardless
        log.warning("cockpit disabled for this run: %s", exc)
        cockpit = None
    runner = ShadowLiveRunner(
        cfg,
        rs,
        scanner=scanner,
        master=master,
        adapter=adapter,
        clock=clock,
        session=primary,
        depth_writer=DatasetWriter(data_dir, "depth30", DEPTH30_SCHEMA),
        store=store,
        report_path=report_path,
        ledger_file=ledger_file,
        feed_label=feed_label,
        prev_levels=prev_levels,
        alert=alert,
        cockpit=cockpit,
        delivery=delivery,
    )
    while not stop.is_set() and clock.now_ns() < to_ns(start):
        stop.wait(1.0)
    log.info("shadow scanner %s %s–%s via %s", day, f"{start:%H:%M}", f"{end:%H:%M}", adapter.name)
    try:
        runner.start()
        runner.run(to_ns(end), stop)
    finally:
        runner.finish()
        if cockpit is not None:
            if cockpit.analytics is not None:
                cockpit.analytics.stop()
            if cockpit.publisher is not None:
                cockpit.publisher.close()
    compact_day(data_dir, "depth30", day, DEPTH30_SCHEMA)
    return ShadowRunResult(
        day,
        "ran",
        dashboard=report_path,
        stats=runner.stats.__dict__
        | {
            "prev_day_levels": len(prev_levels),
            "prev_day_levels_from_eod": eod_filled,
            "delivery_watch": sorted(i.symbol for i in delivery.values()),
        },
    )
