"""Assemble a :class:`CockpitCore` for a live (P2) or replay (P4) engine."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from engine.cockpit.analytics_live import LiveAnalytics, load_iv_percentile_history
from engine.cockpit.bus import EnginePublisher
from engine.cockpit.config import auth_key, load_cockpit_config
from engine.cockpit.core import CockpitCore, ReplayControl
from engine.cockpit.paper import PaperAccount
from engine.core.calendar import TradingCalendar
from engine.core.clock import MarketSession
from engine.core.config import Settings
from engine.core.rules import IST, RuleBook
from engine.data.recorder.run import RecorderSettings
from engine.data.reference.instrument_master import InstrumentMaster

log = logging.getLogger(__name__)


def build_cockpit(
    settings: Settings,
    root: Path,
    *,
    source: str,
    day: date,
    session: MarketSession,
    master: InstrumentMaster,
    rs: RecorderSettings,
    book: RuleBook,
    calendar: TradingCalendar | None,
    port: int | None,
    journal_dir: Path,
    chain_dir: Path | None,
    replay: ReplayControl | None = None,
    on_stop: Callable[[], None] | None = None,
) -> CockpitCore | None:
    ccfg = load_cockpit_config(settings, root)
    if not ccfg.enabled:
        return None
    sc = settings.shadow_scanner
    square_off = session.at(sc.paper_square_off_at)

    def lot_size_of(sym: str) -> int:
        return master.get(sym).lot_size if sym in master else 1

    def is_option(sym: str) -> bool:
        return master.get(sym).kind.is_option if sym in master else False

    from engine.data.events.calendar_file import blackout_status, load_calendar

    events = load_calendar(ccfg.events_file)

    def event_block(now: datetime) -> str | None:
        if not ccfg.block_entries_in_event_window or not events:
            return None
        st = blackout_status(events, now)
        return (
            f"event window: {', '.join(st.active_blackout_events)}"
            if st.in_macro_blackout
            else None
        )

    accounts = []
    for name in ("bot", "manual"):
        acct = PaperAccount(
            name,
            book=book,
            limits=settings.risk,
            broker_name=sc.cost_broker,
            session_open=session.open_dt,
            square_off_at=square_off,
            lot_size_of=lot_size_of,
            is_option=is_option,
            journal=journal_dir / f"paper_{name}.jsonl",
            entry_block=event_block,
        )
        restored = acct.load_journal(day)
        if restored:
            log.info("paper %s: restored %d journal lines for %s", name, restored, day)
        accounts.append(acct)
    analytics = LiveAnalytics(
        book=book,
        calendar=calendar,
        master=master,
        underlyings={u.name: u.spot_symbol for u in rs.universe.underlyings},
        iv_history=load_iv_percentile_history(
            rs.parquet_dir / "eod" / "iv_history.parquet", before=day
        ),
        chain_dir=chain_dir,
        chain_interval_s=ccfg.chain_interval_s,
        constituents=_constituents(settings, rs),
        dispersion_index=str(
            (settings.raw.get("recorder") or {}).get("constituents", {}).get("index", "NIFTY")
        ),
        prev_oi=_prev_oi(rs, day),
    )
    publisher: EnginePublisher | None = None
    if port is not None:
        try:
            publisher = EnginePublisher(ccfg.host, port, auth_key(ccfg.key_file))
            publisher.start()
        except OSError as exc:  # port taken (another engine running): paper still runs
            log.warning("cockpit publisher not started on port %s: %s", port, exc)
            publisher = None
    core = CockpitCore(
        ccfg,
        source=source,
        bot=accounts[0],
        manual=accounts[1],
        analytics=analytics,
        publisher=publisher,
        replay=replay,
        on_stop=on_stop,
    )
    core.calendar_events = events
    from engine.cockpit.condor import SpreadAccount
    from engine.cockpit.setups import SetupTracker
    from engine.cockpit.study import SignalStudy

    core.setups = SetupTracker(
        ccfg.setups, journal=journal_dir / "setups.jsonl", day=session.open_dt
    )
    spread = SpreadAccount(
        "condor",
        book=book,
        limits=settings.risk,
        broker_name=sc.cost_broker,
        session_open=session.open_dt,
        journal=journal_dir / "paper_condor.jsonl",
    )
    restored = spread.load_journal(day)
    if restored or spread.positions:
        log.info("paper condor: %d lines today, %d open", restored, len(spread.positions))
    core.spread = spread
    core.trading_calendar = calendar
    # More strategies, each on its own paper account and journal (results never mix).
    for name in ("score", "gamma"):
        acct = PaperAccount(
            name,
            book=book,
            limits=settings.risk,
            broker_name=sc.cost_broker,
            session_open=session.open_dt,
            square_off_at=square_off,
            lot_size_of=lot_size_of,
            is_option=is_option,
            journal=journal_dir / f"paper_{name}.jsonl",
            entry_block=event_block,
        )
        acct.load_journal(day)
        setattr(core, f"{name}_acct", acct)
    debit = SpreadAccount(
        "debit",
        book=book,
        limits=settings.risk,
        broker_name=sc.cost_broker,
        session_open=session.open_dt,
        journal=journal_dir / "paper_debit.jsonl",
    )
    debit.load_journal(day)
    core.debit_acct = debit

    core.study = SignalStudy(
        book=book,
        broker_name=sc.cost_broker,
        close_at=square_off,
        journal=journal_dir / "signal_study.jsonl",
    )
    return core


def _prev_oi(rs: RecorderSettings, day: date) -> dict[str, dict[tuple[str, float, str], int]]:
    """Previous session's option OI from the NSE F&O bhavcopy (missing file: empty)."""
    from engine.cockpit.chain import load_prev_oi

    try:
        return load_prev_oi(rs.raw_dir, day, [u.name for u in rs.universe.underlyings])
    except Exception as exc:  # a bad file must not stop the engine
        log.warning("previous-day OI not loaded: %s", type(exc).__name__)
        return {}


def _constituents(settings: Settings, rs: RecorderSettings) -> dict[str, tuple[str, float, bool]]:
    """broker key -> (symbol, weight %, weight verified?) of the recorded heavyweights."""
    spec = (settings.raw.get("recorder") or {}).get("constituents")
    if not spec:
        return {}
    from engine.core.config import REPO_ROOT
    from engine.data.constituents.table import broker_key, heavyweights, load_constituent_table

    rows = load_constituent_table(REPO_ROOT / str(spec["file"]))
    top = heavyweights(
        rows, str(spec.get("index", "NIFTY")), datetime.now(IST).date(), int(spec.get("top_n", 8))
    )
    out: dict[str, tuple[str, float, bool]] = {}
    for r in top:
        try:
            out[broker_key(r, settings.data_primary)] = (
                r.symbol,
                r.weight_pct or 0.0,
                r.weight_verified,
            )
        except ValueError:
            continue
    return out
