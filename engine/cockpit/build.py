"""Assemble a :class:`CockpitCore` for a live (P2) or replay (P4) engine."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

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
    from dataclasses import replace as _replace

    # Paper strategies may size up to cockpit.paper_max_lots within the approved loss limits.
    # Paper only: up to paper_max_lots, and each strategy's broker margin up to
    # paper_margin_cap_pct of capital on its own account (live keeps settings.risk).
    paper_limits = _replace(
        settings.risk,
        max_lots_per_strategy=max(settings.risk.max_lots_per_strategy, ccfg.paper_max_lots),
        margin_headroom_max_used_pct=ccfg.paper_margin_cap_pct,
    )
    square_off = session.at(sc.paper_square_off_at)

    def lot_size_of(sym: str) -> int:
        return master.get(sym).lot_size if sym in master else 1

    def is_option(sym: str) -> bool:
        return master.get(sym).kind.is_option if sym in master else False

    def expiry_of(sym: str) -> date | None:
        return master.get(sym).expiry if sym in master else None

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
            limits=paper_limits,
            broker_name=sc.cost_broker,
            session_open=session.open_dt,
            square_off_at=square_off,
            lot_size_of=lot_size_of,
            is_option=is_option,
            journal=journal_dir / f"paper_{name}.jsonl",
            entry_block=event_block,
            expiry_of=expiry_of,
            expiry_day_last_entry=ccfg.expiry_day_last_entry,
            expiry_day_exit=ccfg.expiry_day_exit,
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
        limits=paper_limits,
        broker_name=sc.cost_broker,
        session_open=session.open_dt,
        journal=journal_dir / "paper_condor.jsonl",
        entry_block=event_block,
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
            limits=paper_limits,
            broker_name=sc.cost_broker,
            session_open=session.open_dt,
            square_off_at=square_off,
            lot_size_of=lot_size_of,
            is_option=is_option,
            journal=journal_dir / f"paper_{name}.jsonl",
            entry_block=event_block,
            expiry_of=expiry_of,
            expiry_day_last_entry=ccfg.expiry_day_last_entry,
            expiry_day_exit=ccfg.expiry_day_exit,
        )
        acct.load_journal(day)
        setattr(core, f"{name}_acct", acct)
    debit = SpreadAccount(
        "debit",
        book=book,
        limits=paper_limits,
        broker_name=sc.cost_broker,
        session_open=session.open_dt,
        journal=journal_dir / "paper_debit.jsonl",
        entry_block=event_block,
    )
    debit.load_journal(day)
    core.debit_acct = debit
    quoter = None
    if source == "live":
        from engine.cockpit.margin import MarginQuoter

        quoter = MarginQuoter()
    core.plays = make_plays(
        ccfg,
        paper_limits,
        book=book,
        broker=sc.cost_broker,
        session=session,
        square_off=square_off,
        journal_dir=journal_dir,
        day=day,
        lot_size_of=lot_size_of,
        is_option=is_option,
        expiry_of=expiry_of,
        event_block=event_block,
    )
    spread_accts = [spread, debit, *(p.account for p in core.plays if hasattr(p, "plan"))]
    if quoter is not None:
        for acct in spread_accts:
            acct.attach_margin(quoter)
    for p in core.plays:
        if hasattr(p, "expiry_exit"):
            p.expiry_exit = ccfg.spread_expiry_exit
    for acct in spread_accts:
        acct.fill_latency_s = ccfg.paper_fill_latency_ms / 1000
        acct.leg_gap_s = ccfg.paper_leg_gap_ms / 1000
        acct.charges_mult = ccfg.credit_charges_mult
    for acct in [*core.long_accounts()]:
        acct.fill_latency_s = ccfg.paper_fill_latency_ms / 1000
        acct.stop_hold_max_s = ccfg.stop_hold_max_s
        acct.stop_override_mult = ccfg.stop_override_mult
    if ccfg.model_price_stops:
        for acct in [
            *core.long_accounts(),
            *(p.account for p in core.plays if hasattr(p, "managed")),
        ]:
            acct.stop_confirm = core.stop_confirm

    core.study = SignalStudy(
        book=book,
        broker_name=sc.cost_broker,
        close_at=square_off,
        journal=journal_dir / "signal_study.jsonl",
    )
    return core


def make_plays(
    ccfg: Any,
    limits: Any,
    *,
    book: RuleBook,
    broker: str,
    session: MarketSession,
    square_off: datetime,
    journal_dir: Path,
    day: date,
    lot_size_of: Any,
    is_option: Any,
    expiry_of: Any,
    event_block: Any,
) -> list[Any]:
    """The playbook strategies, each on its own paper account and journal."""
    from engine.cockpit import plays_long as L
    from engine.cockpit import plays_spread as S
    from engine.cockpit.condor import SpreadAccount
    from engine.cockpit.plays_base import HighIvPlay, HighIvRouter

    on = dict(ccfg.plays)

    def long_acct(name: str) -> PaperAccount:
        a = PaperAccount(
            name,
            book=book,
            limits=limits,
            broker_name=broker,
            session_open=session.open_dt,
            square_off_at=square_off,
            lot_size_of=lot_size_of,
            is_option=is_option,
            journal=journal_dir / f"paper_{name}.jsonl",
            entry_block=event_block,
            expiry_of=expiry_of,
            expiry_day_last_entry=ccfg.expiry_day_last_entry,
            expiry_day_exit=ccfg.expiry_day_exit,
        )
        a.load_journal(day)
        return a

    def spread_acct(name: str) -> SpreadAccount:
        a = SpreadAccount(
            name,
            book=book,
            limits=limits,
            broker_name=broker,
            session_open=session.open_dt,
            journal=journal_dir / f"paper_{name}.jsonl",
            entry_block=event_block,
        )
        a.load_journal(day)
        return a

    hiv = HighIvPlay(
        "hiv",
        "High-IV routed debit spreads",
        "rule 4",
        spread_acct("hiv"),
        rules="Naked-buy signals arriving with ATM IV at or above the 80th "
        "percentile are placed as ~0.55/0.25 delta debit spreads instead.",
    )
    router = HighIvRouter(hiv, ivp_min=hiv.ivp_min)
    plays: list[Any] = [
        L.PutPanicPlay(
            "put_panic",
            "Put-panic breakdown",
            "spec S1",
            long_acct("put_panic"),
            hiv_route=router,
            rules=L.PutPanicPlay.__doc__ or "",
        ),
        L.OiIgnitionPlay(
            "oi_ignition",
            "OI-unwinding ignition",
            "spec S2",
            long_acct("oi_ignition"),
            hiv_route=router,
        ),
        L.ZeroDtePlay("zero_dte", "0DTE afternoon gamma", "spec S3", long_acct("zero_dte")),
        L.BreakthroughPlay(
            "breakthrough",
            "Breakthrough scalper (5 pillars)",
            "rules.md",
            long_acct("breakthrough"),
            hiv_route=router,
        ),
        L.SpoofFadePlay(
            "spoof_fade", "Spoof rug-pull / eaten wall", "spoofing notes", long_acct("spoof_fade")
        ),
        L.ZoneRiderPlay(
            "zone_rider", "Absorption-zone wave rider", "ride_wave.md", long_acct("zone_rider")
        ),
        S.IronFlyPlay("iron_fly", "Straddle-VWAP iron fly", "spec A", spread_acct("iron_fly")),
        S.OiCreditPlay(
            "oi_credit", "OI-velocity credit spreads", "spec B", spread_acct("oi_credit")
        ),
        S.CalendarPlay(
            "calendar",
            "Term-structure calendar",
            "spec C",
            spread_acct("calendar"),
            overnight=True,
            history_file=journal_dir / "term_spread_history.jsonl",
        ),
        S.CompressionDebitPlay(
            "compression",
            "Compression + skew breakout debit spread",
            "spec D",
            spread_acct("compression"),
            overnight=True,
            history_file=journal_dir / "bb_width_history.jsonl",
        ),
        S.ExpiryPinPlay("expiry_pin", "Expiry pin seller", "fix2.md", spread_acct("expiry_pin")),
        S.SyntheticFuturePlay(
            "synthetic",
            "Hedged synthetic future",
            "futures 1",
            spread_acct("synthetic"),
            overnight=True,
        ),
        S.MarriedFuturePlay(
            "married",
            "Put-hedged (married) future",
            "futures 2",
            spread_acct("married"),
            overnight=True,
        ),
        S.McxMiniPlay("mcx", "MCX mini futures", "futures 3", spread_acct("mcx"), enabled=False),
        hiv,
    ]
    rules = {
        "put_panic": "After 09:45: the index breaks the 30-min opening-range low (or rejects "
        "VWAP), futures CVD z <= -2.5 and ATM put IV +0.4 in 3 min -> 0.55-0.60 delta put with "
        "2-5 days left. Gate: expected 3-min move >= 3.5x charges breakeven. Exits: 20% stop "
        "(Rs 1,500 cap), not +10% in 12 min, breakeven ratchet, 15 candles below breakeven.",
        "oi_ignition": "The index crosses the highest-OI strike while its OI falls > 3% in 6 "
        "min on > 2x futures volume and rising CVD; waits 1-3 min for a retest that holds "
        "(opposite OI building, option above its VWAP); absorption and heavyweight filters; "
        "charges gate; EMA-9 trail after +25%.",
        "zero_dte": "Expiry day only: index pinned inside ATM +- straddle 11:00-13:30, then a "
        "1-min close outside on > 3x volume (13:30-14:30) -> 2 lots ATM, ~Rs 11/unit risk, exit "
        "at delta 0.90 or 14:45. Skipped if 0DTE IV rises while next-expiry IV falls.",
        "breakthrough": "5 pillars (book 20, volume delta 25, writer pain + OI unwind 25, gamma "
        "impulse vs breakeven 15, IV velocity 15); x0.4 unless the impulse clears 2.5x "
        "breakeven. 65-79 -> 1 lot, 80+ -> 2 lots; ratchet; EMA trail on 2 lots.",
        "spoof_fade": "A displayed wall pulled without trades near price (spoof rug-pull) or "
        "traded away (eaten wall) on an index future/option -> trade the side it reveals; "
        "charges gate; breakeven ratchet.",
        "zone_rider": "A futures absorption floor (ceiling) cleared by 10 pts with 5-min CVD "
        "agreeing, or a 30-min CVD divergence -> 0.60 delta option with >= 5 days; stop when the "
        "index crosses back through the zone; held to the square-off.",
        "iron_fly": "Nifty Mon/Tue, Sensex Thu after 09:45: 30-min RV/IV < 0.85 and the ATM "
        "straddle below its VWAP for 3 bars, after the 9:20 stop flush -> sell ATM straddle, "
        "buy wings 300 pts out. Exits: straddle > VWAP + 1.5 sigma, net delta beyond 0.30, 15:15.",
        "oi_credit": "Index within 20 pts of the call (put) OI wall, that OI down > 4% in 10 min "
        "with CVD z >= +2 (<= -2) -> bull put (bear call) spread at the money, 250-pt wing. Exits: "
        "50% credit, 2x credit, back through the wall, 15:20.",
        "calendar": "Near-expiry ATM IV minus far-expiry (14+ days) ATM IV > 2.5 vol pts with a "
        "20-session z > 2 -> sell near ATM call, buy far ATM call. Exits: term spread back to 0, "
        "short delta outside 0.25-0.75, the day before the near expiry. Held overnight.",
        "compression": "15-min Bollinger width in the bottom 10% of 10 sessions, a 60-min box "
        "break with 25-delta skew moving the break's way and > 2.5x 15-min volume -> buy 0.55 / "
        "sell 0.25 delta. Exits: 70% of max value, half the debit, back in the box, 2 days.",
        "expiry_pin": "Expiry day 13:45-14:45: put OI at a strike +30% since 13:35 while call OI "
        "there unwinds 20%, and that put spikes 50% above its 13:45 price -> sell it with a wing "
        "under Rs 3. Exits: 90% of credit, 2x credit, 15:20.",
        "synthetic": "On a confirmed setup: buy the wing 350 pts away first, then long call / "
        "short put (or the mirror) at the money; planned loss = a 0.5% index stop; up to 3 days.",
        "married": "Index above the previous day's high, futures OI +2% in 15 min and the "
        "annualized basis rising -> buy the ATM put, then the future. Mirror: below the "
        "previous day's low with OI +2% and the basis shrinking -> buy the ATM call, then sell "
        "the future. Target 200 pts, stop 0.5% beyond the broken level, up to 5 days.",
        "mcx": "VWAP breakout with CVD z >= 2 and 2x volume on CRUDEOILM / NATGASMINI with the "
        "charges ratchet. Dormant: no MCX feed, instrument master or MCX cost rows yet.",
    }
    for p in plays:
        if p.key in on:
            p.enabled = on[p.key]
        p.rules = rules.get(p.key, p.rules)
    return plays


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
