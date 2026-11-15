"""Cross-platform task runner (Windows has no `make`). The Makefile delegates here.

uv run python tasks.py test | lint | fmt | rules | instruments | fyers-login
                      | upstox-login | upstox-probe [D30=1] [SECONDS=n]
                      | record [SIM=1] [MINUTES=n] | qc [DATE=...] [SIM=1] [LAST=5]
                      | shadow-live [SIM=1] [MINUTES=n] [SEND=1]
                      | cockpit [OPEN=0] | replay DATE=YYYY-MM-DD [SPEED=5] [SIM=1]
                      | eod [DATE=] | eod-backfill FROM= TO= [DATASETS=fo,cm,indices] [IV=0]
                      | iv-history [FROM= TO=] | corporate-actions [FROM= TO=]
                      | equity-panel [FROM= TO=] [REFRESH_SYMBOLS=1]
                      | open-items
                      | screener-history [DAYS=400]
                      | evening [DATE=] [STEPS=...] | fundamentals [SYMBOLS=A,B]
                      | swing [DATE=] | delivery-study [FROM= TO=] | index-weights-verify
                      | event-arb | shadow-scan [VERSION=v1_shadow|v2_bot]
                      | mine-log [FILE=...] | self-review [FILE=...]
                      | paper [VERSION=v2_bot] | backtest [TRIALS=n] | report
                      | candle-backtest STRAT=condor|log3 FROM= [TO=] [SET=k=v] [VARIANTS=0]
                        [SPREADS=p90] [QUOTES=] [CACHE=] [DOTENV=] [TRIALS=]
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")

PLANNED: dict[str, str] = {}


_TASK = ""  # the running task's name, set in main()


def _apply_analytics_settings() -> None:
    """Process-wide analytics settings every task shares (the IV variance clock)."""
    sys.path.insert(0, str(ROOT))
    try:
        from engine.analytics.expiry_clock import set_variance_weights
        from engine.core.config import load_settings

        vc = (load_settings().raw.get("analytics") or {}).get("variance_clock") or {}
        set_variance_weights(float(vc.get("overnight", 0)), float(vc.get("non_trading_day", 0)))
    except Exception as exc:  # a broken setting must not stop recording
        print(f"variance clock not set ({type(exc).__name__}); using trading minutes")


def run(*cmd: str) -> int:
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(list(cmd), cwd=ROOT)


def task_test(_: dict[str, str]) -> int:
    return run(sys.executable, "-m", "pytest")


def task_lint(_: dict[str, str]) -> int:
    rc = run(sys.executable, "-m", "ruff", "check", ".")
    rc |= run(sys.executable, "-m", "mypy")
    return rc


def task_fmt(_: dict[str, str]) -> int:
    return run(sys.executable, "-m", "ruff", "check", "--fix", ".")


def task_event_arb(params: dict[str, str]) -> int:
    """Event-arbitrage scan. DATE=YYYY-MM-DD (default today IST), FETCH=0 to skip the NSE
    feed, SEND=1 to send alerts to Telegram (dry-run otherwise)."""
    sys.path.insert(0, str(ROOT))
    from engine.core.config import load_settings
    from engine.core.logging_setup import setup_logging
    from engine.sidecars.event_arb.run import run_scan
    from ops.alerts.telegram import TelegramNotifier, format_digest

    settings = load_settings()
    setup_logging(settings.logging_config, process=_TASK)
    as_of = date.fromisoformat(params["DATE"]) if params.get("DATE") else _today_ist()
    res = run_scan(settings, ROOT, as_of, fetch=params.get("FETCH", "1") != "0")
    print(f"report: {res.report_path}")
    for s in res.screened:
        print(
            f"  {s.verdict.value:<5} {s.event.symbol:<12} {s.event.type.value:<20} "
            f"{len(s.flags)} flags"
        )
    print(f"new announcement candidates: {len(res.new_candidates)}")
    for a in res.alerts:
        print(f"  alert: {a}")
    if params.get("SEND") == "1" and res.alerts:
        notifier = TelegramNotifier.from_env()
        if notifier.dry_run:
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — alerts not sent.")
        elif not notifier.send(format_digest(f"Event arb {as_of}", res.alerts)):
            return 1
    return 0


def _today_ist() -> date:
    return datetime.now(IST).date()


DEMO_BANNER = "DEMO — synthetic inputs, not market data. Nothing here measures any strategy."


def _require_demo(params: dict[str, str], task: str, why: str) -> bool:
    """Tasks that only have synthetic inputs run solely with DEMO=1 and say so loudly."""
    if params.get("DEMO") == "1":
        print(DEMO_BANNER)
        return True
    print(
        f"'{task}' has no real data source wired yet ({why}). Its only inputs are hard-coded "
        f"examples, so it refuses to produce reports that could be mistaken for results. "
        f"Run with DEMO=1 to see the plumbing on synthetic numbers.",
        file=sys.stderr,
    )
    return False


def task_instruments(params: dict[str, str]) -> int:
    """Download today's Fyers contract files (public, no login) and check them against
    config/rules: expiries per recorded underlying and lot-size mismatches."""
    sys.path.insert(0, str(ROOT))
    from engine.core.config import load_settings
    from engine.core.rules import RuleBook
    from engine.data.recorder.run import load_master, load_recorder_settings
    from engine.data.reference.instrument_master import classify_expiries, lot_size_mismatches

    rs = load_recorder_settings(load_settings())
    day = date.fromisoformat(params["DATE"]) if params.get("DATE") else _today_ist()
    master = load_master(rs, day, datetime.now(IST), fetch=params.get("FETCH", "1") != "0")
    print(f"instrument master ({master.source}) for {day}: {len(master):,} contracts")
    roles = ("current_weekly", "next_weekly", "current_monthly", "next_monthly")
    for u in rs.universe.underlyings:
        exp = classify_expiries(master.option_expiries(u.name, day), roles)
        lots = sorted({i.lot_size for e in set(exp.values()) for i in master.options(u.name, e)})
        shown = "  ".join(f"{r}={e}" for r, e in exp.items())
        print(f"  {u.name:<8} {shown}  lot={lots}")
    book = RuleBook.load(ROOT / "config" / "rules")
    for m in lot_size_mismatches(master, book, [u.name for u in rs.universe.underlyings]):
        print(
            f"  LOT MISMATCH {m.symbol}: master {m.master_lot} vs rules {m.rule_lot} "
            f"({m.rule_status})"
        )
    return 0


def task_fyers_login(params: dict[str, str]) -> int:
    """Daily Fyers login: opens the login page; you sign in in your browser and paste the
    redirect URL back. Today's access token is written to .env and never printed."""
    sys.path.insert(0, str(ROOT))
    import secrets
    import webbrowser

    from engine.core.env import get_secret, load_dotenv, update_dotenv
    from engine.data.adapters.fyers import exchange_auth_code, extract_auth_code, login_url

    env: dict[str, str] = {}
    load_dotenv(ROOT / ".env", env)
    app_id, redirect = env.get("FYERS_APP_ID", ""), env.get("FYERS_REDIRECT_URI", "")
    secret = get_secret("FYERS_SECRET_KEY", env)
    if not (app_id and redirect and secret):
        print(
            "Set FYERS_APP_ID, FYERS_SECRET_KEY and FYERS_REDIRECT_URI in .env first "
            "(see docs/RUNBOOK.md).",
            file=sys.stderr,
        )
        return 2
    state = secrets.token_urlsafe(12)
    url = login_url(app_id, redirect, state)
    print(f"Sign in to Fyers in your browser:\n  {url}")
    if params.get("OPEN", "1") == "1":
        webbrowser.open(url)
    pasted = input("Paste the full redirect URL here: ")
    token = exchange_auth_code(app_id, secret, extract_auth_code(pasted, state))
    update_dotenv(
        ROOT / ".env",
        {"FYERS_ACCESS_TOKEN": token.reveal(), "FYERS_TOKEN_DATE": _today_ist().isoformat()},
    )
    print(f"OK: today's Fyers token saved to .env ({token!r}).")
    return 0


def task_upstox_login(params: dict[str, str]) -> int:
    """Daily Upstox login: opens the login page; you sign in and paste the redirect URL back.
    Today's access token goes into .env (never printed). Not needed if the 1-year
    UPSTOX_ANALYTICS_TOKEN turns out to work for the live feed."""
    sys.path.insert(0, str(ROOT))
    import secrets
    import webbrowser

    from engine.core.env import get_secret, load_dotenv, update_dotenv
    from engine.data.adapters.upstox import (
        exchange_upstox_code,
        extract_upstox_code,
        upstox_login_url,
    )

    env: dict[str, str] = {}
    load_dotenv(ROOT / ".env", env)
    client_id, redirect = env.get("UPSTOX_API_KEY", ""), env.get("UPSTOX_REDIRECT_URI", "")
    secret = get_secret("UPSTOX_API_SECRET", env)
    if not (client_id and redirect and secret):
        print(
            "Set UPSTOX_API_KEY, UPSTOX_API_SECRET and UPSTOX_REDIRECT_URI in .env first "
            "(see docs/RUNBOOK.md).",
            file=sys.stderr,
        )
        return 2
    state = secrets.token_urlsafe(12)
    url = upstox_login_url(client_id, redirect, state)
    print(f"Sign in to Upstox in your browser:\n  {url}")
    if params.get("OPEN", "1") == "1":
        webbrowser.open(url)
    pasted = input("Paste the full redirect URL here: ")
    token = exchange_upstox_code(client_id, secret, redirect, extract_upstox_code(pasted, state))
    update_dotenv(
        ROOT / ".env",
        {"UPSTOX_ACCESS_TOKEN": token.reveal(), "UPSTOX_TOKEN_DATE": _today_ist().isoformat()},
    )
    print(f"OK: today's Upstox token saved to .env ({token!r}).")
    return 0


def task_record(params: dict[str, str]) -> int:
    """Run the market-data recorder for today; blocks until the session window closes.
    SIM=1 uses the synthetic feed (writes under data/sim/); MINUTES=n records n minutes from
    now (dry runs); FORCE=1 records on a non-trading day. Ctrl+C stops cleanly."""
    sys.path.insert(0, str(ROOT))
    import signal
    import threading

    from engine.core.config import load_settings
    from engine.core.logging_setup import setup_logging
    from engine.data.recorder.run import default_adapter, record_day
    from ops.alerts.telegram import TelegramNotifier

    settings = load_settings()
    setup_logging(settings.logging_config, process=_TASK)
    sim = params.get("SIM") == "1"
    send = params.get("SEND") == "1"
    day = _today_ist()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def alert(text: str) -> None:
        print(text)
        if send:
            TelegramNotifier.from_env().send(text)

    try:
        res = record_day(
            settings,
            ROOT,
            day=day,
            adapter_factory=default_adapter(settings, ROOT, sim=sim, day=day),
            sim=sim,
            stop=stop,
            force=params.get("FORCE") == "1",
            minutes=int(params["MINUTES"]) if params.get("MINUTES") else None,
        )
    except Exception as exc:
        alert(f"Recorder {day} did not run: {type(exc).__name__}: {exc}"[:500])
        return 1
    print(f"{res.day}: {res.status} {res.detail}".rstrip())
    if res.stats:
        print("  " + ", ".join(f"{k}={v}" for k, v in res.stats.items()))
    if res.qc_report:
        verdict = "PASS" if res.qc_passed else "FAIL"
        alert(f"Recorder {day}{' (sim)' if sim else ''}: QC {verdict} — {res.qc_report.name}")
    return 0 if res.status != "failed" else 1


def _eod_common() -> tuple[Path, Any, Any]:
    sys.path.insert(0, str(ROOT))
    from engine.core.calendar import TradingCalendar
    from engine.core.config import load_settings
    from engine.core.rules import RuleBook

    settings = load_settings()
    book = RuleBook.load(ROOT / "config" / "rules")
    return settings.paths["raw"], book, TradingCalendar.from_rulebook(book)


def _wait_for_network(
    host: str = "nsearchives.nseindia.com", timeout_s: float = 180.0, step_s: float = 10.0
) -> bool:
    """Wait until ``host`` answers on 443. The nightly job runs when the PC wakes if it slept
    through 20:00 (13 Nov: 23:33), often before Wi-Fi is back: every download then failed with
    URLError. Returns False after ``timeout_s``; the job then runs and reports the failures."""
    import socket
    import time as _time

    end = _time.monotonic() + timeout_s
    while True:
        try:
            with socket.create_connection((host, 443), timeout=5):
                return True
        except OSError:
            if _time.monotonic() >= end:
                print(f"network: {host} unreachable after {timeout_s:.0f} s; continuing anyway")
                return False
            _time.sleep(step_s)


def task_eod(params: dict[str, str]) -> int:
    """Download one day's NSE (and BSE F&O) end-of-day files (default: today, after ~19:30
    IST). DATE=YYYY-MM-DD, DATASETS=fo,cm,delivery,participant_oi,participant_vol,indices,
    bse_fo (default all). Without DATE it also saves the F&O ban list and bulk/block deals
    (current-only files)."""
    raw_dir, _, _ = _eod_common()
    from engine.data.eod.download import DATASETS, fetch_eod

    _wait_for_network()
    day = date.fromisoformat(params["DATE"]) if params.get("DATE") else _today_ist()
    sets = params.get("DATASETS", ",".join(DATASETS)).split(",")
    bad = 0
    for ds in sets:
        res = fetch_eod(ds, day, raw_dir=raw_dir, now=datetime.now(IST))
        print(f"{day} {ds:<15} {res.status:<8} {res.detail}")
        bad += res.status == "error"
    if not params.get("DATE"):
        # NSE keeps only today's ban list and bulk/block deals: save them every evening.
        from engine.data.eod.flows import CURRENT_EOD_URLS, fetch_current_eod

        for ds in CURRENT_EOD_URLS:
            res = fetch_current_eod(ds, raw_dir=raw_dir, now=datetime.now(IST))
            print(f"{res.day} {ds:<15} {res.status:<8} {res.detail}")
            bad += res.status == "error"
        # Catch up: a night missed (no network, laptop asleep) is fetched on the next run.
        # Files already on disk are skipped; holidays come back as "missing" (404).
        from engine.data.eod.download import backfill

        start = day - timedelta(days=int(params.get("CATCHUP_DAYS", "7")))
        for res in backfill(sets, start, day - timedelta(days=1), raw_dir=raw_dir,
                            now=lambda: datetime.now(IST)):  # fmt: skip
            if res.status not in ("cached", "missing"):
                print(f"{res.day} {res.dataset:<15} {res.status:<8} catch-up {res.detail}")
                bad += res.status == "error"
        try:  # the screener's per-session summary (cached; one parse per file)
            from engine.cockpit.screener import ScreenerCache

            _, book, cal = _eod_common()
            sc = ScreenerCache(
                raw_dir, ROOT / "data" / "state" / "screener", book=book, calendar=cal
            )
            sc.build(sc.available_days(day, back_days=10))
        except Exception as exc:
            print(f"screener summary skipped: {type(exc).__name__}: {exc}")
    return 1 if bad else 0


def task_screener_history(params: dict[str, str]) -> int:
    """Build the screener's per-session summaries (ATM IV, OI, volume per F&O underlying) for
    every F&O bhavcopy on disk, DAYS= back (default 400). One-time ~10 min; cached after."""
    raw_dir, book, cal = _eod_common()
    from engine.cockpit.screener import ScreenerCache

    cache = ScreenerCache(raw_dir, ROOT / "data" / "state" / "screener", book=book, calendar=cal)
    days = cache.available_days(_today_ist(), back_days=int(params.get("DAYS", "400")))
    done = cache.build(days, progress=lambda d: print(f"{d} summarised", flush=True))
    print(f"screener history: {done} sessions cached")
    return 0


def _evening_setup() -> tuple[Any, Any, Any]:
    sys.path.insert(0, str(ROOT))
    from engine.core.config import load_settings
    from engine.core.logging_setup import setup_logging
    from engine.data.eod.evening import evening_config
    from engine.shadow.live import load_shadow_live_config

    settings = load_settings()
    setup_logging(settings.logging_config, process=_TASK)
    _, _, cal = _eod_common()
    cfg = evening_config(settings.raw, load_shadow_live_config(settings).delivery_watch, ROOT)
    return settings, cfg, cal


def task_evening(params: dict[str, str]) -> int:
    """Evening job (after ~19:30 IST; schedule it daily): NSE EOD + delivery files, the
    delivery-spike watchlist for tomorrow's shadow scanner, Nifty members' results dates into
    config/events/macro_calendar.csv, and results/shareholding XBRL for the watchlist and
    heavyweights. DATE=YYYY-MM-DD trade date (default: today after 19:30, else the last trading
    day). STEPS=eod,watchlist,weights,calendar,swing,fundamentals (default all; `weights`
    derives all Nifty weights from free-float market cap; `swing` runs the swing paper account
    and scanner, engine/strategies/swing_night.py)."""
    settings, cfg, cal = _evening_setup()
    from engine.data.eod.evening import run_evening

    _wait_for_network()

    steps = params.get("STEPS", "eod,watchlist,weights,calendar,swing,fundamentals").split(",")
    rep = run_evening(
        cfg,
        raw_dir=settings.paths["raw"],
        parquet_dir=settings.paths["parquet"] / "fundamentals",
        now=datetime.now(IST),
        calendar=cal,
        trade_date=date.fromisoformat(params["DATE"]) if params.get("DATE") else None,
        steps=steps,
        swing=_swing_night(settings, cal),
    )
    print(f"evening job for trade date {rep.trade_date}:")
    for line in rep.lines:
        print("  " + line)
    print(f"{rep.errors} problem(s)")
    backup_rc = task_backup({}) if "backup" not in params.get("SKIP", "") else 0
    task_audit({})  # nightly rule audit of the day's paper trades (report only)
    task_scorecard({})  # promotion scorecard over every journal (report only)
    task_open_items({})  # checklist of unverified rules and open questions (report only)
    return 1 if rep.errors or backup_rc else 0


def task_open_items(params: dict[str, str]) -> int:
    """Checklist of everything unfinished: UNVERIFIED rule rows, PROPOSED settings and the
    "Open" items in docs/DECISIONS.md, grouped by who must act (owner / Claude). Writes
    reports/open_items.md; close an item by adding its id to config/open_items_resolved.csv."""
    sys.path.insert(0, str(ROOT))
    from engine.core.open_items import collect, render

    items, resolved = collect(ROOT)
    text = render(items, resolved, generated=datetime.now(IST).strftime("%d %b %Y %H:%M"))
    out = ROOT / "reports" / "open_items.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    owner = sum(x.owner == "owner" for x in items)
    print(f"open items: {len(items)} ({owner} need the owner, {len(items) - owner} need Claude);"
          f" {resolved} resolved -> {out}")  # fmt: skip
    return 0


def task_backfill_history(params: dict[str, str]) -> int:
    """Seed the calendar's term-spread history (1-minute option candles of both expiries) and
    the compression strategy's 15-min band widths (Upstox 1-minute index candles) for the last
    SESSIONS=20 sessions before today, instead of waiting for them to accumulate. Safe to
    rerun."""
    raw_dir, book, cal = _eod_common()
    from engine.cockpit.backfill import backfill
    from engine.core.config import load_settings
    from engine.core.env import load_dotenv

    load_dotenv(ROOT / ".env")
    n = int(params.get("SESSIONS", "20"))
    sessions: list[date] = []
    d = _today_ist()
    while len(sessions) < n:
        d = cal.previous_trading_day(d)
        sessions.append(d)
    sessions.reverse()
    from engine.data.recorder.run import load_master, load_recorder_settings

    settings = load_settings()
    journal_dir = ROOT / str((settings.raw.get("cockpit") or {}).get("journal_dir", "data/state"))
    master = load_master(
        load_recorder_settings(settings), _today_ist(), datetime.now(IST), fetch=True
    )
    for line in backfill(
        ROOT, raw_dir=raw_dir, book=book, calendar=cal, sessions=sessions,
        index_keys={"NIFTY": "NSE_INDEX|Nifty 50", "SENSEX": "BSE_INDEX|SENSEX"},
        journal_dir=journal_dir, master=master,
    ):  # fmt: skip
        print(line)
    return 0


# RBI MPC decision days (RBI press releases: 2025-26 schedule of 26 Mar 2025, 2026-27 of 23 Mar
# 2026) and the Union Budget, inside the IV-history window. Research input only.
INDEX_EVENTS = {
    "RBI_MPC": ["2025-12-05", "2026-02-06", "2026-04-08", "2026-06-05", "2026-08-05", "2026-10-07"],
    "UNION_BUDGET": ["2026-02-01"],
}


def task_event_iv_study(params: dict[str, str]) -> int:
    """Event IV crush study, pre-registered in docs/research/mechanisms.md (H1 the crush, H2 the
    event move is overpriced, H3 index events). Results meetings from NSE's board-meeting list
    (fetched once per 2-month block, cached), stock ATM IV from stock_iv.parquet, index IV from
    iv_history.parquet. Report: reports/research/event_iv_crush_<date>.md."""
    raw_dir, book, cal = _eod_common()
    from datetime import timedelta

    import pyarrow.parquet as pq

    from engine.analytics.expiry_clock import expiry_clock
    from engine.core.clock import market_session
    from engine.data.fundamentals.nse import board_meetings_url, fetch_list, parse_board_meetings
    from engine.research.event_iv import run_study, summarize, verdicts

    eod = raw_dir.parent / "parquet" / "eod"
    stock = pq.read_table(eod / "stock_iv.parquet").to_pylist()
    index = [r for r in pq.read_table(eod / "iv_history.parquet").to_pylist() if r.get("atm_iv")]
    first = min(r["trade_date"] for r in stock)
    last = max(r["trade_date"] for r in stock)

    hist = raw_dir / "fundamentals" / "lists" / "board_meetings_history"
    meetings = []
    start = date(first.year, first.month, 1)
    while start <= last:
        end = min(date(start.year + (start.month + 1) // 12, (start.month + 1) % 12 + 1, 1)
                  - timedelta(days=1), last)  # fmt: skip
        path = hist / f"{start.isoformat()}_{end.isoformat()}.json"
        if not path.exists() or end >= _today_ist() - timedelta(days=2):
            res = fetch_list(board_meetings_url(start, end), path, now=datetime.now(IST))
            if res.status not in ("ok", "cached"):
                print(f"board meetings {start}..{end}: {res.status} {res.detail}")
        if path.exists():
            meetings += parse_board_meetings(path.read_text("utf-8"))
        start = end + timedelta(days=1)

    clocks: dict[tuple[date, date, str], float] = {}

    def t_var_for(exch: str):  # type: ignore[no-untyped-def]
        def t_var(day: date, expiry: date) -> float:
            key = (day, expiry, exch)
            if key not in clocks:
                close = market_session(book, exch, "fo", day, allow_unverified=exch != "NSE")
                clocks[key] = expiry_clock(
                    close.close_dt, expiry, book=book, calendar=cal, exchange=exch
                ).t_var
            return clocks[key]

        return t_var

    sessions = sorted({r["trade_date"] for r in stock})
    rows = {(r["underlying"], r["trade_date"]): r for r in stock}
    names = {r["underlying"] for r in stock}
    events = sorted(
        {
            (m.symbol, m.meeting_date)
            for m in meetings
            if m.is_results and m.symbol in names and first < m.meeting_date < last
        }
    )
    res = run_study(rows, events, sessions, t_var_for("NSE"))
    s = summarize(res)
    v = verdicts(s)

    lines = [
        f"# Event IV crush study, {_today_ist():%d %b %Y}",
        "",
        "Pre-registered in `docs/research/mechanisms.md` (14 Nov 2026) before this ran; the "
        "thresholds below are the registered ones. Our own EOD ATM IV (tier C: closing prices).",
        "",
        f"Sample: {first} to {last}, {len(sessions)} sessions, {len(names)} F&O stocks; "
        f"{len(events)} results meetings, {s['n_events']} with comparable IV on both sides "
        f"(same expiry, after D+1); control: {s.get('n_control', 0)} two-session windows away "
        "from results.",
        "",
        "## H1: does IV fall over a results meeting?",
        "",
        f"- Events where IV fell: **{s.get('fell_pct')}%** (needs ≥ 70%); median change "
        f"**{s.get('median_change')} vol pts** (needs ≤ −3); mean {s.get('mean_change')}.",
        f"- Control windows: median {s.get('control_median')} vol pts (needs within ±0.5); "
        f"fell in {s.get('control_fell_pct')}%.",
        f"- **H1: {v['H1']}**",
        "",
        "## H2: was the event move overpriced?",
        "",
        f"- Events with an implied event move: {s.get('n_h2')}; realized below implied in "
        f"**{s.get('realized_below_pct')}%** (needs > 55%); realized/implied mean "
        f"**{s.get('mean_ratio')}** (needs < 0.9), median {s.get('median_ratio')}; t of "
        f"mean log(realized/implied) **{s.get('t_log_ratio')}** (needs ≤ −2).",
        f"- **H2: {v['H2']}** (before costs; stock option lots are large, see the note).",
        "",
        "## H3: index events (descriptive; too few for a verdict)",
        "",
        "| Index | Event | Day | IV before | IV after | Change | Implied move | Realized |",
        "|---|---|---|---|---|---|---|---|",
    ]
    irows = {(r["underlying"], r["trade_date"]): r for r in index}
    isessions = sorted({r["trade_date"] for r in index})
    for kind, days in INDEX_EVENTS.items():
        for ds in days:
            d = date.fromisoformat(ds)
            for und, exch in (("NIFTY", "NSE"), ("BANKNIFTY", "NSE"), ("SENSEX", "BSE")):
                r = run_study(irows, [(und, d)], isessions, t_var_for(exch), exclude_days=0)
                for m in r["events"]:
                    imp = f"{m.implied_move * 100:.2f}%" if m.implied_move else "—"
                    lines.append(
                        f"| {und} | {kind} | {d} | {m.iv_before:.2f} | {m.iv_after:.2f} | "
                        f"{m.iv_change:+.2f} | {imp} | {m.realized_move * 100:.2f}% |"
                    )
    lines += [
        "",
        "## Notes",
        "",
        "- D-1 and D+1 are the sessions around the meeting date; results released after the "
        "close on D are inside the window.",
        "- EOD closes, not mids: a stale close on a thin strike moves its IV. The median and the "
        "control guard against that, not fully.",
        "- Tradability is a separate question: one stock-option lot is usually ₹5-15 lakh of "
        "notional. Only defined-risk spreads could fit ₹1-2 lakh, and their costs must be "
        "counted before any conclusion.",
        "- Any later split (sector, liquidity, IV level) is a new trial.",
    ]
    out = ROOT / "reports" / "research" / f"event_iv_crush_{_today_ist().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"-> {out}")
    return 0


def task_results_fly_test(params: dict[str, str]) -> int:
    """H4, pre-registered in docs/research/mechanisms.md: a results iron fly on NSE bhavcopy
    closes (sell ATM call+put at D-1's close, wings W away, exit at D+1's close), 1 lot, max loss
    <= Rs 5,000, <= Rs 1 lakh of max loss open at once, full charges, haircut max(Rs 0.20, 1.5%)
    per leg per side and 2x. Report: reports/research/results_fly_<date>.md."""
    raw_dir, book, _ = _eod_common()
    from collections import defaultdict

    import pyarrow.parquet as pq

    from engine.backtest.costs import fno_charges
    from engine.backtest.deflated_sharpe import compute_deflated_sharpe
    from engine.data.eod.bhavcopy import parse_fno_bhavcopy_csv
    from engine.data.eod.download import eod_path, read_eod_text
    from engine.data.fundamentals.nse import parse_board_meetings
    from engine.research.results_fly import base_haircut, build_fly, select_concurrent, stats

    cap, budget, capital, trials = 5000.0, 100_000.0, 100_000.0, 3
    stock = pq.read_table(raw_dir.parent / "parquet" / "eod" / "stock_iv.parquet").to_pylist()
    names = {r["underlying"] for r in stock}
    sessions = sorted({r["trade_date"] for r in stock})
    first, last = sessions[0], sessions[-1]
    hist = raw_dir / "fundamentals" / "lists" / "board_meetings_history"
    meetings = [m for f in sorted(hist.glob("*.json")) if not f.name.endswith(".meta.json")
                for m in parse_board_meetings(f.read_text("utf-8"))]  # fmt: skip
    events = sorted(
        {
            (m.symbol, m.meeting_date)
            for m in meetings
            if m.is_results and m.symbol in names and first < m.meeting_date < last
        }
    )
    if not events:
        print("no results events: run tasks.py event-iv-study first (it caches the lists)")
        return 1

    def around(d: date) -> tuple[date, date] | None:
        prev = [x for x in sessions if x < d]
        nxt = [x for x in sessions if x > d]
        return (prev[-1], nxt[0]) if prev and nxt else None

    wanted: dict[date, set[str]] = defaultdict(set)
    windows = {}
    for sym, d in events:
        w = around(d)
        if w:
            windows[(sym, d)] = w
            wanted[w[0]].add(sym)
            wanted[w[1]].add(sym)
    chains: dict[tuple[date, str], list] = defaultdict(list)
    for day, syms in sorted(wanted.items()):
        path = eod_path(raw_dir, "fo", day)
        if not path.exists():
            continue
        for r in parse_fno_bhavcopy_csv(read_eod_text(path)):
            if r.trade_date == day and r.symbol in syms and r.option_type in ("CE", "PE"):
                chains[(day, r.symbol)].append(r)

    charge_cache: dict[tuple[date, str, int], float] = {}
    today = _today_ist()

    def charges(day: date, side: str, turnover: float) -> float:
        # Today's fee schedule (what the trade would cost now): the Upstox Plus brokerage row
        # only starts in Nov 2026, and the question is whether this pays at current costs.
        day = today
        key = (day, side, round(turnover))
        if key not in charge_cache:
            charge_cache[key] = fno_charges(
                book, day, segment="options", side=side,  # type: ignore[arg-type]
                turnover_inr=turnover, broker="upstox_plus",
            ).total  # fmt: skip
        return charge_cache[key]

    def flies(mult: float) -> tuple[list, int]:
        def hc(px: float) -> float:
            return base_haircut(px, mult)

        out, skipped = [], 0
        for (sym, d), (b, a) in windows.items():
            rb, ra = chains.get((b, sym), []), chains.get((a, sym), [])
            exps = sorted({r.expiry for r in rb if r.expiry > a} & {r.expiry for r in ra})
            if not exps:
                skipped += 1
                continue
            exp = exps[0]
            qb = {(float(r.strike), r.option_type): {"close": r.close, "contracts": r.contracts}
                  for r in rb if r.expiry == exp}  # fmt: skip
            qa = {(float(r.strike), r.option_type): {"close": r.close, "contracts": r.contracts}
                  for r in ra if r.expiry == exp}  # fmt: skip
            lot = next((r.lot_size for r in rb if r.lot_size), 0)
            s_in = next((r.underlying_price for r in rb if r.underlying_price > 0), 0.0)
            s_out = next((r.underlying_price for r in ra if r.underlying_price > 0), 0.0)
            t = build_fly(sym, d, b, a, exp, lot, s_in, s_out, qb, qa, cap_inr=cap, hc=hc)
            if t is None:
                skipped += 1
            else:
                out.append(t)
        return select_concurrent(out, hc, budget), skipped

    res = {}
    for mult in (1.0, 2.0):
        trades, skipped = flies(mult)

        def hcm(px: float, m: float = mult) -> float:
            return base_haircut(px, m)

        nets = [t.net(charges, hcm) for t in trades]
        daily: dict[date, float] = defaultdict(float)
        for t, x in zip(trades, nets, strict=True):
            daily[t.after] += x
        series = [daily.get(d, 0.0) / capital for d in sessions]
        dsr = compute_deflated_sharpe(series, n_trials=trials)
        run, peak, dd = 0.0, 0.0, 0.0
        for d in sessions:
            run += daily.get(d, 0.0)
            peak = max(peak, run)
            dd = max(dd, peak - run)
        res[mult] = {"trades": trades, "nets": nets, "skipped": skipped, "stats": stats(nets),
                     "dsr": dsr, "max_dd": round(dd, 0)}  # fmt: skip

    base = res[1.0]
    trades = base["trades"]

    def total_at(m: float) -> float:
        return sum(t.net(charges, lambda px, m=m: base_haircut(px, m)) for t in trades)

    lo, hi = 0.0, 20.0
    if total_at(lo) <= 0:
        be = 0.0
    elif total_at(hi) > 0:
        be = hi
    else:
        for _ in range(40):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if total_at(mid) > 0 else (lo, mid)
        be = lo
    s1, s2 = base["stats"], res[2.0]["stats"]
    ok1 = s1.get("n", 0) > 0 and s1["mean"] > 0 and (s1.get("t") or 0) >= 2
    ok2 = s2.get("n", 0) > 0 and s2["mean"] > 0
    ok3 = base["dsr"].deflated_sharpe_pct >= 95
    verdict = "PASS" if ok1 and ok2 and ok3 else "FAIL"
    by_month: dict[str, list[float]] = defaultdict(list)
    for t, x in zip(trades, base["nets"], strict=True):
        by_month[t.after.strftime("%Y-%m")].append(x)

    def row(name: str, r: dict) -> str:
        st = r["stats"]
        return (f"| {name} | {st.get('n')} | {st.get('win_pct')}% | ₹{st.get('total', 0):,.0f} | "
                f"₹{st.get('mean', 0):,.1f} | {st.get('t')} | ₹{st.get('avg_win') or 0:,.0f} / "
                f"₹{st.get('avg_loss') or 0:,.0f} | ₹{st.get('worst', 0):,.0f} | "
                f"{st.get('profit_factor')} | ₹{r['max_dd']:,.0f} | "
                f"{r['dsr'].deflated_sharpe_pct:.1f}% |")  # fmt: skip

    lines = [
        f"# Results iron fly (H4), {_today_ist():%d %b %Y}",
        "",
        "Pre-registered in `docs/research/mechanisms.md` (commit dd3c26a) before this ran. NSE "
        "bhavcopy closes, 1 lot, max loss ≤ ₹5,000 a trade, ≤ ₹1 lakh of max loss open at once, "
        "Upstox Plus charges at today's rates, haircut max(₹0.20, 1.5%) per leg per side. "
        "Deflated Sharpe counts "
        f"{trials} trials.",
        "",
        f"Sample {first} to {last}: {len(events)} results meetings; built and taken "
        f"{s1.get('n')} (skipped {base['skipped']}: no expiry after D+1, an untraded leg, or no "
        "wing within the cap).",
        "",
        "| Haircut | Trades | Win | Net total | Net / trade | t | Avg win / loss | Worst | "
        "Profit factor | Max DD | DSR |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        row("1x max(₹0.20, 1.5%)", base),
        row("2x", res[2.0]),
        "",
        f"Break-even haircut: **{be:.2f}x** the base (the haircut at which the base trades "
        "net zero).",
        "",
        f"**H4: {verdict}** (mean > 0 with t ≥ 2 at 1x: {'yes' if ok1 else 'no'}; > 0 at 2x: "
        f"{'yes' if ok2 else 'no'}; DSR ≥ 95%: {'yes' if ok3 else 'no'}).",
        "",
        "## By month (1x)",
        "",
        "| Month | Trades | Net |",
        "|---|---|---|",
        *[f"| {k} | {len(v)} | ₹{sum(v):,.0f} |" for k, v in sorted(by_month.items())],
        "",
        "## Ten worst and best trades (1x)",
        "",
        "| Symbol | D-1 → D+1 | Lot | ATM / wing | Spot move | Net |",
        "|---|---|---|---|---|---|",
    ]
    ranked = sorted(zip(trades, base["nets"], strict=True), key=lambda x: x[1])
    for t, x in [*ranked[:10], *ranked[-10:]]:
        mv = (t.spot_out / t.spot_in - 1) * 100 if t.spot_in else 0.0
        lines.append(f"| {t.symbol} | {t.before} → {t.after} | {t.lot} | {t.atm:g} / ±{t.wing:g} "
                     f"| {mv:+.1f}% | ₹{x:,.0f} |")  # fmt: skip
    lines += [
        "",
        "## Caveats",
        "",
        "- Closing prices are last trades; a leg's close can sit anywhere in its spread. The "
        "haircut and its 2x run are the guard, and stock-option spreads have not been measured.",
        "- Margin is not modelled: a defined-risk fly blocks roughly its max loss plus exposure "
        "margin; quote it with the broker before any paper trade.",
        "- One year of events; RBI days are excluded (no crush). Any further split is a new trial.",
    ]
    import csv as _csv

    def zero_hc(px: float) -> float:
        return 0.0

    tcsv = ROOT / "reports" / "research" / f"results_fly_{_today_ist().isoformat()}_trades.csv"
    tcsv.parent.mkdir(parents=True, exist_ok=True)
    with tcsv.open("w", newline="", encoding="utf-8") as fh:
        wr = _csv.writer(fh)
        wr.writerow(["symbol", "before", "after", "lot", "atm", "wing", "credit_close",
                     "exit_cost_close", "gross_no_haircut", "haircut_cost", "charges", "net_1x",
                     "spot_move_pct", "exit_above_wing"])  # fmt: skip
        for t, x in zip(trades, base["nets"], strict=True):
            exit_cost = sum(lg.px_out if lg.side == "SELL" else -lg.px_out for lg in t.legs)
            g0 = t.gross(zero_hc)
            g1 = t.gross(lambda px: base_haircut(px))
            mv = (t.spot_out / t.spot_in - 1) * 100 if t.spot_in else 0.0
            wr.writerow([t.symbol, t.before, t.after, t.lot, t.atm, t.wing,
                         round(t.credit(zero_hc), 2), round(exit_cost, 2), round(g0, 0),
                         round(g0 - g1, 0),
                         round(t.charges(charges, lambda px: base_haircut(px)), 0),
                         round(x, 0), round(mv, 2), exit_cost > t.wing + 1e-9])  # fmt: skip
    out = ROOT / "reports" / "research" / f"results_fly_{_today_ist().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:22]))
    print(f"-> {out}")
    return 0


def _capital_time_section(journal_dir: Path, capital: float) -> str:
    """The scorecard's capital-and-time table: each paper account, plus all of them sharing the
    capital, against Nifty over the same dates (engine/cockpit/capital_time.py)."""
    from datetime import datetime as _dt

    from engine.cockpit.capital_time import capital_time, render_capital_time
    from engine.cockpit.scorecard import account_closes
    from engine.cockpit.vol_sizing import load_daily_closes

    _, book, _ = _eod_common()
    today = _today_ist()
    rf = book.lookup("risk_free_rate", today, allow_unverified=True, tenor="91d_tbill")
    rf_pa = rf.as_float() / 100.0
    nifty = load_daily_closes(ROOT / "data" / "state" / "index_daily.json", today, None).get(
        "NIFTY", []
    )
    accounts = account_closes(journal_dir)

    def first_day(rows: list[dict[str, Any]]) -> date:
        stamps = (r.get("opened_at") or r.get("closed_at") or r.get("t") or "" for r in rows)
        return min(_dt.fromisoformat(str(s)[:10]).date() for s in stamps if s)

    rows = [
        capital_time(
            a,
            c,
            capital=capital,
            start=first_day(c),
            end=today,
            risk_free_pa=rf_pa,
            index_closes=nifty,
        )
        for a, c in sorted(accounts.items())
    ]
    every = [r for c in accounts.values() for r in c]
    if len(accounts) > 1:
        rows.append(
            capital_time(
                "all accounts (shared capital)",
                every,
                capital=capital,
                start=first_day(every),
                end=today,
                risk_free_pa=rf_pa,
                index_closes=nifty,
            )
        )
    return render_capital_time(rows, risk_free_pa=rf_pa, min_sessions_annualise=20)


def task_scorecard(params: dict[str, str]) -> int:
    """Promotion scorecard over every paper journal: sessions, trades, net, net with charges
    doubled, t-statistic, deflated Sharpe over all strategies tried, kill rules, and a verdict
    (collecting / prune / kill rule / unproven / candidate). Report in reports/scorecard/.
    TRIALS= extra variants tested elsewhere (raises the deflated-Sharpe bar)."""
    sys.path.insert(0, str(ROOT))
    from engine.cockpit.scorecard import render, scorecard
    from engine.core.config import load_settings
    from engine.risk.kill_rules import KillRules

    settings = load_settings()
    raw = settings.raw
    jd = ROOT / str((raw.get("cockpit") or {}).get("journal_dir", "data/state"))
    kr = (raw.get("risk") or {}).get("kill_rules") or {}
    rules = KillRules(
        max_drawdown_inr=float(kr.get("max_drawdown_inr", 10_000)),
        window_trades=int(kr.get("window_trades", 15)),
        max_consecutive_losses=int(kr.get("max_consecutive_losses", 5)),
    )
    capital = float(settings.risk.inr(100.0))
    extra = int(params.get("TRIALS", "0"))
    scores = scorecard(jd, capital=capital, rules=rules, extra_trials=extra)
    text = render(scores, now=datetime.now(IST), trials=max(1, len(scores) + extra))
    text += "\n" + _capital_time_section(jd, capital)
    out = ROOT / "reports" / "scorecard" / f"{_today_ist().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"-> {out}")
    return 0


def task_audit(params: dict[str, str]) -> int:
    """Check every paper trade in the journals against the hard rules (defined risk, per-trade
    risk, lots, entry window, trades per day, daily loss, square-off, scalp discipline,
    accounting). Report in reports/audit/<date>.md. DIR= another journal folder (e.g. a replay).
    Exit 1 when anything breaks a rule."""
    sys.path.insert(0, str(ROOT))
    from engine.cockpit.audit import audit_journals, render
    from engine.core.config import load_settings

    settings = load_settings()
    raw = settings.raw
    ck = raw.get("cockpit") or {}
    rk = raw.get("risk") or {}
    jdir = ROOT / (params.get("DIR") or str(ck.get("journal_dir", "data/state")))
    finds, stats = audit_journals(
        jdir,
        capital_inr=float((raw.get("capital") or {}).get("inr", 200000)),
        directional_pct=float(rk["per_trade_max_loss_pct"]["directional"]),
        spread_pct=float(rk["per_trade_max_loss_pct"]["defined_risk_spread"]),
        daily_loss_pct=float(rk["daily_loss_limit_pct"]),
        max_trades=int(rk["max_trades_per_day"]),
        max_lots=int(ck.get("paper_max_lots", 2)),
        lot_of=lambda sym: 20 if sym.startswith("BSE_") else 65,
    )
    now = datetime.now(IST)
    out = ROOT / "reports" / "audit" / f"{now:%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = render(finds, stats, now)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"-> {out}")
    return 1 if finds else 0


def task_log3_features(params: dict[str, str]) -> int:
    """SPEC 5.2 research: the index's state at each Log-3 entry (1-minute candles from Upstox)
    vs the same sessions' other minutes, and the simplest separating rule. Reads
    data/private/mined/log3_fills.csv (FILE= another mined fills CSV); writes the report next
    to it (private, gitignored)."""
    sys.path.insert(0, str(ROOT))
    import csv as _csv

    from engine.cockpit.backfill import index_minute_candles
    from engine.core.env import get_secret, load_dotenv
    from engine.research.log3_entry_features import analyze, group_ideas, render

    load_dotenv(ROOT / ".env")
    src = Path(params.get("FILE") or ROOT / "data" / "private" / "mined" / "log3_fills.csv")
    rows = list(_csv.DictReader(src.open(encoding="utf-8")))
    if not rows:
        print("no fills")
        return 1
    sensex = str(rows[0]["instrument"]).startswith("SENSEX")
    key = "BSE_INDEX|SENSEX" if sensex else "NSE_INDEX|Nifty 50"
    tok = (
        get_secret("UPSTOX_ACCESS_TOKEN").reveal() or get_secret("UPSTOX_ANALYTICS_TOKEN").reveal()
    )
    res = analyze(group_ideas(rows), lambda d: index_minute_candles(key, d, tok))
    out = src.with_name(src.stem.replace("_fills", "") + "_entry_features.md")
    out.write_text(render(res), encoding="utf-8")
    print(render(res))
    print(f"-> {out} (private)")
    return 0


def task_backup(params: dict[str, str]) -> int:
    """Copy new/changed recordings and journals (data/parquet, data/state) to backup.target
    (default C:/QuantBackup; TARGET= overrides). Never deletes from the target. Runs at the end
    of `evening`. Point it at an external drive to survive a disk failure."""
    sys.path.insert(0, str(ROOT))
    from engine.core.config import load_settings
    from engine.data.backup import run_backup

    cfg = load_settings().raw.get("backup") or {}
    if not cfg.get("enabled", True) and not params.get("TARGET"):
        print("backup: disabled (backup.enabled: false)")
        return 0
    target = Path(params.get("TARGET") or cfg.get("target") or "C:/QuantBackup")
    res = run_backup(
        ROOT,
        list(cfg.get("sources") or ["data/parquet", "data/state"]),
        target,
        min_free_gb=float(cfg.get("min_free_gb", 5)),
    )
    print(res.line() + f" -> {target}")
    for e in res.errors[:10]:
        print("  error: " + e)
    return 1 if res.errors else 0


def _swing_night(settings: Any, cal: Any) -> Any:
    """``day -> report lines`` for the nightly swing run (paper only)."""
    from engine.core.rules import RuleBook
    from engine.data.eod.download import fetch_eod
    from engine.strategies.swing_night import fill_config, run_swing_night, trend_config
    from engine.strategies.swing_scanner import swing_config

    raw = settings.raw
    book = RuleBook.load(ROOT / "config" / "rules")

    def fetch_index(d: date, raw_dir: Path, now: datetime) -> None:
        fetch_eod("indices", d, raw_dir=raw_dir, now=now)

    def run(day: date) -> list[str]:
        now = datetime.now(IST)
        raw_dir = settings.paths["raw"]
        rep = run_swing_night(
            day=day,
            now=now,
            raw_dir=raw_dir,
            parquet_dir=settings.paths["parquet"],
            state_dir=settings.paths["data"] / "state",
            book=book,
            cal=cal,
            limits=settings.risk,
            broker=settings.shadow_scanner.cost_broker,
            cfg=swing_config(raw.get("swing")),
            trend=trend_config(raw.get("trend")),
            fills=fill_config(raw.get("swing_paper")),
            fetch_index=lambda d: fetch_index(d, raw_dir, now),
        )
        return rep.lines

    return run


def task_swing(params: dict[str, str]) -> int:
    """Nightly swing run on its own (normally a step of `evening`): applies the day's bars to the
    swing paper account, scans for SPEC §5.8 setups and queues paper entries for the next open;
    the trend sleeve on month-ends. DATE=YYYY-MM-DD (default: the evening trade date). Needs
    that day's F&O bhavcopy and delivery file (tasks.py eod or evening)."""
    settings, _, cal = _evening_setup()
    from engine.data.eod.evening import evening_trade_date

    day = (
        date.fromisoformat(params["DATE"])
        if params.get("DATE")
        else evening_trade_date(datetime.now(IST), cal)
    )
    try:
        lines = _swing_night(settings, cal)(day)
    except FileNotFoundError as exc:
        print(f"swing: {exc}", file=sys.stderr)
        return 1
    print(f"swing night {day}:")
    for line in lines:
        print("  " + line)
    return 0


def task_fundamentals(params: dict[str, str]) -> int:
    """Quarterly results and shareholding from NSE XBRL filings into
    data/parquet/fundamentals/. SYMBOLS=RELIANCE,INFY (default: the Nifty heavyweights in
    settings `fundamentals`). Prints the latest quarter as known now."""
    settings, cfg, _ = _evening_setup()
    from engine.data.constituents.table import heavyweights, load_constituent_table
    from engine.data.fundamentals.store import equity_summary, refresh_symbol

    now = datetime.now(IST)
    if params.get("SYMBOLS"):
        symbols = [s.strip().upper() for s in params["SYMBOLS"].split(",") if s.strip()]
    else:
        rows = load_constituent_table(cfg.constituents_file)
        top = heavyweights(rows, cfg.index, now.date(), cfg.heavyweights_top_n)
        symbols = [r.symbol for r in top]
    out_dir = settings.paths["parquet"] / "fundamentals"
    bad = 0
    for sym in symbols:
        rep = refresh_symbol(
            sym,
            raw_dir=settings.paths["raw"],
            parquet_dir=out_dir,
            now=now,
            n_periods=cfg.periods,
            pace_s=cfg.pace_s,
        )
        bad += bool(rep.errors)
        s = equity_summary(out_dir, sym, now)
        r, h = s["results"], s["holding"]
        line = f"{sym:<12} +{rep.results_new} results +{rep.shareholding_new} holdings"
        if r:
            line += (
                f" | Q to {r['period_end']}: revenue ₹{r['revenue_cr']} cr "
                f"(YoY {r['revenue_yoy_pct']}%), PAT ₹{r['pat_cr']} cr (YoY {r['pat_yoy_pct']}%)"
            )
        if h:
            line += f" | promoter {h['promoter_pct']}% FII {h['fii_pct']}% DII {h['dii_pct']}%"
        print(line + (f" | {'; '.join(rep.errors)}" if rep.errors else ""))
    return 1 if bad else 0


def task_index_weights_verify(params: dict[str, str]) -> int:
    """Monthly: compare the derived Nifty weights with the NSE Indices factsheet's top 10 and
    mark matches (within TOL=0.3 points) verified. FILE=<pdf> to use a saved factsheet instead
    of downloading it. Needs pdfplumber (`uv sync --extra mining`)."""
    settings, cfg, _ = _evening_setup()
    import pdfplumber

    from engine.core.net import http_get_bytes
    from engine.data.constituents.table import load_constituent_table, members_on
    from engine.data.constituents.weights import (
        FACTSHEET_URL,
        parse_factsheet,
        verify_against_factsheet,
    )

    today = _today_ist()
    if params.get("FILE"):
        pdf = Path(params["FILE"])
    else:
        pdf = settings.paths["raw"] / "reference" / "factsheet" / f"ind_nifty50_{today}.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(http_get_bytes(FACTSHEET_URL))
    with pdfplumber.open(pdf) as doc:
        text = "\n".join(page.extract_text() or "" for page in doc.pages)
    import csv

    with cfg.constituents_file.open(encoding="utf-8", newline="") as fh:
        names = {
            r["company"].strip(): r["symbol"].strip()
            for r in csv.DictReader(fh)
            if r["index"].strip().upper() == cfg.index
        }
    as_of, top = parse_factsheet(text, names)
    if len(top) < 5:
        print(f"could not read the top constituents from {pdf} ({len(top)} found)")
        return 1
    res = verify_against_factsheet(
        cfg.constituents_file,
        top,
        index=cfg.index,
        source_url=f"{FACTSHEET_URL} (factsheet dated {as_of})",
        checked_on=today,
        tolerance_pts=float(params.get("TOL", "0.3")),
    )
    print(f"factsheet dated {as_of}: {len(top)} weights read")
    for sym, (ours, theirs, ok) in sorted(res.items(), key=lambda x: -x[1][1]):
        verdict = "verified" if ok else "differs"
        print(f"  {sym:<12} ours {ours:6.2f}  factsheet {theirs:6.2f}  {verdict}")
    rows = members_on(load_constituent_table(cfg.constituents_file), cfg.index, today)
    print(f"{sum(r.weight_verified for r in rows)} of {len(rows)} member weights verified")
    return 0


def task_delivery_study(params: dict[str, str]) -> int:
    """Research: do delivery spikes predict 1/3/5-session returns, net of costs? FROM=/TO=
    (default the last 400 days). Needs `eod-backfill DATASETS=delivery,fo` first. Writes
    reports/research/delivery_event_study.{md,json}. Every threshold set tried is a trial."""
    settings, _, _ = _evening_setup()
    from datetime import timedelta

    from engine.core.rules import RuleBook
    from engine.research.delivery_event_study import (
        DEFAULT_GRID,
        features,
        load_history,
        round_trip_cost_pct,
        run_study,
        trial_count,
        write_outputs,
    )

    end = date.fromisoformat(params["TO"]) if params.get("TO") else _today_ist()
    start = date.fromisoformat(params["FROM"]) if params.get("FROM") else end - timedelta(days=400)
    notional = float(params.get("NOTIONAL", "50000"))
    slip = float(params.get("SLIP_PCT", "0.05"))  # HYPOTHESIS: per side, % of price
    broker = settings.shadow_scanner.cost_broker
    rows, fno = load_history(settings.paths["raw"], start, end)
    feats = features(rows, fno)
    if not feats:
        print("no delivery/F&O history in range; run eod-backfill DATASETS=delivery,fo first")
        return 1
    book = RuleBook.load(ROOT / "config" / "rules")
    cost = round_trip_cost_pct(book, end, notional=notional, broker=broker)
    results = run_study(feats, cost_pct=cost, slippage_pct_per_side=slip)
    days = sorted({f.day for f in feats})
    meta = {
        "summary": "Long rows are cash-delivery trades; short rows (churn, distribution) can "
        "only be expressed with options (no overnight cash shorts).",
        "sample": f"{days[0]} to {days[-1]} ({len(days)} signal days, {len(feats)} stock-days)",
        "universe": "stocks with stock futures in that day's F&O bhavcopy",
        "entry / exit": "open of T+1 / close of T+h",
        "costs": f"delivery round trip {cost:.3f}% on ₹{notional:,.0f} ({broker}) + "
        f"{slip}% slippage per side (HYPOTHESIS); 2x slippage column",
        "trials": f"{trial_count(DEFAULT_GRID, (1, 3, 5))} (threshold sets x biases x horizons)",
    }
    md = write_outputs(ROOT / "reports" / "research", results, meta)
    passed = [r for r in results if r.passed]
    print(f"{len(results)} cells; {len(passed)} pass (DSR >= 90% and |t| >= 3) -> {md}")
    for r in sorted(results, key=lambda r: -r.deflated_sharpe_pct)[:8]:
        print(
            f"  {r.trial:<34} {r.bias:<18} h={r.horizon} n={r.n_trades:<5} "
            f"net {r.mean_net_pct:+.2f}% excess {r.excess_gross_pct:+.2f}% "
            f"t={r.t_stat_excess:+.1f} DSR {r.deflated_sharpe_pct:.0f}%"
        )
    return 0


def task_eod_backfill(params: dict[str, str]) -> int:
    """Backfill NSE end-of-day files for FROM..TO (weekdays; holidays show as missing).
    DATASETS default: fo,indices. Days before 2024-01-01 come in NSE's old bhavcopy layout
    (fo and cm), stored at the same paths. Then rebuilds the IV history over its usual window
    (the last 400 days, like the nightly job), so an old range never replaces it; IV=0 skips
    that. Prints progress every 50 days; safe to stop and rerun (downloaded days are skipped)."""
    raw_dir, _, _ = _eod_common()
    from engine.data.eod.download import EodFetch, backfill

    if not (params.get("FROM") and params.get("TO")):
        print("usage: tasks.py eod-backfill FROM=YYYY-MM-DD TO=YYYY-MM-DD", file=sys.stderr)
        return 2
    start, end = date.fromisoformat(params["FROM"]), date.fromisoformat(params["TO"])
    sets = params.get("DATASETS", "fo,indices").split(",")
    counts: dict[str, int] = {}
    days: set[date] = set()

    def progress(r: EodFetch) -> None:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status == "error":
            print(f"  {r.day} {r.dataset}: error {r.detail}", flush=True)
        if r.day not in days:
            days.add(r.day)
            if len(days) % 50 == 0:
                print(f"  … {r.day}: {counts}", flush=True)

    backfill(sets, start, end, raw_dir=raw_dir, now=lambda: datetime.now(IST), progress=progress)
    print(f"backfill {start}..{end} {sets}: {counts}")
    if params.get("IV") == "0":
        return 0
    return task_iv_history({})


def task_corporate_actions(params: dict[str, str]) -> int:
    """Download NSE's corporate-actions list (bonus, split, rights, dividend, demerger) by
    month into data/raw/reference/corporate_actions/. FROM default 2015-01-01, TO default
    90 days ahead (ex-dates are announced early). Past months are fetched once."""
    raw_dir, _, _ = _eod_common()
    from engine.data.equity.corporate_actions import fetch_corporate_actions

    today = _today_ist()
    start = date.fromisoformat(params.get("FROM", "2015-01-01"))
    end = date.fromisoformat(params["TO"]) if params.get("TO") else today + timedelta(days=90)
    status = fetch_corporate_actions(raw_dir, start, end, today=today)
    counts: dict[str, int] = {}
    for month, s in status.items():
        counts[s.split()[0]] = counts.get(s.split()[0], 0) + 1
        if s.startswith("error"):
            print(f"  {month}: {s}")
    print(f"corporate actions {start}..{end}: {counts}")
    return 1 if counts.get("error") else 0


def task_equity_panel(params: dict[str, str]) -> int:
    """Build the daily panel of every NSE stock into data/parquet/equity/daily/<YYYY-MM>.parquet
    (prices, corporate-action-clean returns, dividends, delivery, F&O membership, ban list;
    see engine/data/equity/panel.py). FROM default 2015-01-01 (rebuilding from a later month
    seeds each stock's previous close from the months already written), TO default today.
    Needs the cm (and ideally mto, fo, secban) files and `tasks.py corporate-actions`.
    REFRESH_SYMBOLS=1 re-downloads NSE's symbol-change list."""
    raw_dir, _, _ = _eod_common()
    from engine.data.eod.download import weekdays
    from engine.data.equity.corporate_actions import load_corporate_actions
    from engine.data.equity.panel import last_closes_before, write_panel
    from engine.data.equity.sources import (
        fetch_symbol_changes,
        parse_symbol_changes,
        symbol_changes_path,
    )

    start = date.fromisoformat(params.get("FROM", "2015-01-01"))
    end = date.fromisoformat(params["TO"]) if params.get("TO") else _today_ist()
    sc = symbol_changes_path(raw_dir)
    if params.get("REFRESH_SYMBOLS") == "1" or not sc.exists():
        fetch_symbol_changes(raw_dir)
    changes = parse_symbol_changes(sc.read_text("utf-8", errors="replace"))
    actions = load_corporate_actions(raw_dir)
    if not actions:
        print("no corporate actions downloaded: run `tasks.py corporate-actions` first")
        return 2
    out_dir = raw_dir.parent / "parquet" / "equity" / "daily"
    seed = last_closes_before(out_dir, start.strftime("%Y-%m"))
    counts = write_panel(raw_dir, out_dir, weekdays(start, end), changes, actions, last_close=seed)
    rows = sum(counts.values())
    print(
        f"equity panel {start}..{end}: {rows} rows in {len(counts)} months -> {out_dir}"
        f" ({len(changes)} symbol changes, {len(actions)} corporate actions,"
        f" seeded {len(seed)} previous closes)"
    )
    return 0 if rows else 1


def task_stock_iv_history(params: dict[str, str]) -> int:
    """Backfill data/parquet/eod/stock_iv.parquet (each F&O stock's ATM IV from its EOD
    bhavcopy, our Black-76) for FROM..TO (default: the last 400 calendar days), so the swing
    scanner's IV rank (needs 60 sessions) works now instead of in three months. Days already
    present are skipped; days without a downloaded bhavcopy add nothing."""
    raw_dir, book, cal = _eod_common()
    from datetime import timedelta

    from engine.data.eod.download import weekdays
    from engine.data.eod.stock_iv import update_stock_iv

    end = date.fromisoformat(params["TO"]) if params.get("TO") else _today_ist()
    start = date.fromisoformat(params["FROM"]) if params.get("FROM") else end - timedelta(days=400)
    path = raw_dir.parent / "parquet" / "eod" / "stock_iv.parquet"
    added = update_stock_iv(path, raw_dir, weekdays(start, end), book=book, calendar=cal)
    print(f"stock IV: {added} row(s) added for {start}..{end} -> {path}")
    return 0


def task_iv_history(params: dict[str, str]) -> int:
    """Rebuild data/parquet/eod/iv_history.parquet (tier C: EOD closes) from downloaded
    bhavcopies. FROM/TO default to the last 400 calendar days."""
    raw_dir, book, cal = _eod_common()
    from datetime import timedelta

    from engine.data.eod.download import weekdays
    from engine.data.eod.iv_history import build_iv_history, write_iv_history

    end = date.fromisoformat(params["TO"]) if params.get("TO") else _today_ist()
    start = date.fromisoformat(params["FROM"]) if params.get("FROM") else end - timedelta(days=400)
    unds = params.get("UNDERLYINGS", "NIFTY,BANKNIFTY,FINNIFTY,SENSEX").split(",")
    ivs, vix = build_iv_history(
        raw_dir, weekdays(start, end), underlyings=unds, book=book, calendar=cal
    )
    out = write_iv_history(raw_dir.parent / "parquet" / "eod" / "iv_history.parquet", ivs, vix)
    approx = sum(x.clock_approx for x in ivs)
    print(f"IV history: {len(ivs)} rows, {len(vix)} VIX days -> {out}")
    if approx:
        print(f"  {approx} rows used a weekdays-only clock (holidays for that year not loaded)")
    last = max(
        (x for x in ivs if x.underlying == "NIFTY"), key=lambda x: x.trade_date, default=None
    )
    if last:
        print(
            f"  latest NIFTY ATM IV {last.atm_iv:.2%} on {last.trade_date} (expiry {last.expiry})"
        )
    return 0


def task_upstox_probe(params: dict[str, str]) -> int:
    """Check what your Upstox account receives (works outside market hours too: Upstox sends a
    snapshot on subscribe). D30=1 tests Upstox Plus 30-level depth; SECONDS=n (default 20)."""
    sys.path.insert(0, str(ROOT))
    import os

    from engine.core.config import load_settings
    from engine.core.env import load_dotenv
    from engine.data.adapters.upstox import MODE_FULL, MODE_FULL_D30, UpstoxFeed
    from engine.data.recorder.probe import run_probe
    from engine.data.recorder.run import load_master, load_recorder_settings, upstox_token

    settings = load_settings()
    rs = load_recorder_settings(settings)
    day = _today_ist()
    env: dict[str, str] = dict(os.environ)
    load_dotenv(ROOT / ".env", env)
    master = load_master(rs, day, datetime.now(IST), fetch=True)
    d30 = params.get("D30") == "1"
    try:
        token = upstox_token(env, day)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    feed = UpstoxFeed(token=token, mode=MODE_FULL_D30 if d30 else MODE_FULL, reconnect=False)
    report = run_probe(
        feed,
        master,
        day=day,
        spots={u.name: u.spot_symbol for u in rs.universe.underlyings},
        vix_symbol=rs.universe.vix_symbol,
        seconds=float(params.get("SECONDS", "20")),
    )
    print(f"Upstox probe ({'full_d30, 30 levels' if d30 else 'full, 5 levels'}):")
    print(report.render())
    seg = report.segment_summary()
    fo = [k for k in seg if k.endswith("_FO")]
    if not any(seg.values()):
        print("\nNothing arrived: the token was refused or the account has no active segment.")
        return 1
    if fo and not all(seg[k] for k in fo):
        print(
            "\nIndices/stocks arrive but futures & options do not: F&O is not active on this "
            "account. Activate the derivatives segment, or use another account for now."
        )
        return 1
    print("\nAll segments delivered data.")
    return 0


def task_shadow_live(params: dict[str, str]) -> int:
    """Live V1 shadow scanner (watch-only) on Upstox Plus 30-level depth, for today's session.
    SIM=1 uses the synthetic feed (demo folders only); MINUTES=n runs n minutes from now;
    SEND=1 sends A+ signals to Telegram, labelled as unvalidated hypotheses. Ctrl+C stops."""
    sys.path.insert(0, str(ROOT))
    import signal
    import threading

    from engine.core.config import load_settings
    from engine.core.logging_setup import setup_logging
    from engine.shadow.live import default_shadow_adapter, run_shadow_day
    from ops.alerts.telegram import TelegramNotifier

    settings = load_settings()
    setup_logging(settings.logging_config, process=_TASK)
    sim = params.get("SIM") == "1"
    day = _today_ist()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    notifier = TelegramNotifier.from_env() if params.get("SEND") == "1" else None

    def alert(text: str) -> None:
        print(text)
        if notifier is not None:
            notifier.send(text)

    try:
        res = run_shadow_day(
            settings,
            ROOT,
            day=day,
            adapter_factory=default_shadow_adapter(settings, ROOT, sim=sim, day=day),
            sim=sim,
            stop=stop,
            force=params.get("FORCE") == "1",
            minutes=int(params["MINUTES"]) if params.get("MINUTES") else None,
            alert=alert,
        )
    except Exception as exc:
        alert(f"Shadow scanner {day} did not run: {type(exc).__name__}: {exc}"[:500])
        return 1
    print(f"{res.day}: {res.status} {res.detail}".rstrip())
    if res.stats:
        print("  " + ", ".join(f"{k}={v}" for k, v in res.stats.items()))
    if res.dashboard:
        print(f"  dashboard: {res.dashboard}")
    return 0


def task_qc(params: dict[str, str]) -> int:
    """QC report for a recorded day (DATE=YYYY-MM-DD, default today; SIM=1 for dry-run data).
    LAST=n summarises the last n saved reports (M2 acceptance: 5 sessions passing)."""
    sys.path.insert(0, str(ROOT))
    import json

    from engine.core.clock import market_session
    from engine.core.config import load_settings
    from engine.core.rules import RuleBook
    from engine.data.quality.checks import qc_day, write_qc
    from engine.data.recorder.run import load_recorder_settings
    from engine.data.store import Store

    rs = load_recorder_settings(load_settings())
    sim = params.get("SIM") == "1"
    qc_dir = rs.qc_dir / ("sim" if sim else "")
    if params.get("LAST"):
        n = int(params["LAST"])
        reports = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(qc_dir.glob("????-??-??.json"))[-n:]
        ]
        for d in reports:
            verdict = "PASS" if d["passed"] else "FAIL"
            problems = "; ".join(d["problems"])[:120] or "-"
            print(f"{d['day']}  {verdict}  max gap {d['max_gap_s']:.1f}s  {problems}")
        ok = len(reports) == n and all(d["passed"] for d in reports)
        print(f"M2 acceptance over the last {n} sessions: {'MET' if ok else 'NOT MET'}")
        return 0 if ok else 1
    day = date.fromisoformat(params["DATE"]) if params.get("DATE") else _today_ist()
    book = RuleBook.load(ROOT / "config" / "rules")
    ex, seg = rs.primary_session
    session = market_session(book, ex, seg, day, allow_unverified=True)
    q = qc_day(
        Store(rs.sim_dir if sim else rs.parquet_dir),
        day,
        session,
        gap_threshold_s=rs.core.gap_threshold_s,
        required_strikes_each_side=rs.universe.min_strikes_each_side,
        snapshot_interval_s=rs.core.snapshot_interval_s,
    )
    _, md = write_qc(q, qc_dir)
    print(f"{day}: {'PASS' if q.passed else 'FAIL'} — {md}")
    for p in q.problems:
        print(f"  - {p}")
    return 0 if q.passed else 1


def task_rules(_: dict[str, str]) -> int:
    """Validate every rule table and list UNVERIFIED rows for the owner."""
    sys.path.insert(0, str(ROOT))
    from engine.core.rules import RuleBook

    book = RuleBook.load(ROOT / "config" / "rules")
    total = sum(len(book[n]) for n in book.names)
    print(f"OK: {len(book.names)} rule tables, {total} rows valid.")
    report = book.unverified_report()
    if report:
        print("UNVERIFIED rows (lookups refuse these unless allow_unverified=True):")
        for name, rows in report.items():
            for r in rows:
                keys = ",".join(r.keys.values())
                print(f"  {name:<18} {keys:<28} from {r.effective_from}  {r.notes[:70]}")
    return 0


def task_shadow_scan(params: dict[str, str]) -> int:
    """Run Version 1 (Shadow Order Book Scanner) or Version 2 (Bot) interconnected scan."""
    sys.path.insert(0, str(ROOT))
    from engine.analytics.exposures import ChainGEXNode, compute_dealer_gex_profile
    from engine.analytics.positioning import (
        StrikeOINode,
        classify_futures_oi_buildup,
        summarize_chain_positioning,
    )
    from engine.analytics.vrp import compute_vrp_snapshot
    from engine.confluence.engine import evaluate_interconnected_confluence
    from engine.core.config import load_settings
    from engine.data.adapters.base import Depth, Quote
    from engine.data.constituents.nifty_members import (
        compute_constituent_breadth,
        default_nifty50_heavyweights,
    )
    from engine.data.eod.bhavcopy import DeliveryBias
    from engine.data.events.event_calendar import evaluate_event_risk
    from engine.shadow.report import write_shadow_dashboard_and_ledger
    from engine.shadow.scanner import InstrumentMicrostructureState, ShadowOrderBookScanner

    if not _require_demo(params, "shadow-scan", "no live feed or recorded-data replay feeds it"):
        return 2
    ver_arg = params.get("VERSION")
    settings = load_settings(version=ver_arg)
    day = date.fromisoformat(params["DATE"]) if params.get("DATE") else _today_ist()
    now = datetime.now(IST)
    now_ns = int(now.timestamp() * 1_000_000_000)

    scanner = ShadowOrderBookScanner(
        settings.shadow_scanner,
        capital_inr=settings.risk.capital_inr,
        max_lots=settings.risk.max_lots_per_strategy,
    )
    st = InstrumentMicrostructureState(
        symbol="NSE:NIFTY50-FUT",
        tick_size=0.05,
        lot_size=65,
        pdh=24250.0,
        pdl=24000.0,
        prev_close=24080.0,
        orh_15m=24150.0,
        orl_15m=24010.0,
        delivery_bias=DeliveryBias.LONG_ACCUMULATION,
    )
    scanner.register_instrument(st)
    scanner.on_quote(
        Quote(
            symbol="NSE:NIFTY50-FUT",
            recv_ns=now_ns - 180_000_000_000,
            ltp=23985.0,
            ltq=650,
            volume=150_000,
            oi=12_000_000,
            open=24050.0,
            high=24060.0,
            low=23965.0,
        )
    )
    scanner.on_quote(
        Quote(
            symbol="NSE:NIFTY50-FUT",
            recv_ns=now_ns,
            ltp=24008.0,
            ltq=3250,
            volume=165_000,
            oi=11_850_000,
            open=24050.0,
            high=24060.0,
            low=23965.0,
        )
    )
    bids = tuple((24008.0 - i * 0.5, 4500 if i < 5 else 600, 12) for i in range(30))
    asks = tuple((24008.5 + i * 0.5, 900 if i < 5 else 250, 5) for i in range(30))
    scanner.on_depth(Depth("NSE:NIFTY50-FUT", now_ns, bids, asks), now=now)

    vrp = compute_vrp_snapshot(0.145, 0.118, [0.11, 0.12, 0.13, 0.14, 0.16])
    gex = compute_dealer_gex_profile(
        [ChainGEXNode(24000.0, 80_000, 150_000, 0.14, 0.15)],
        forward=24008.0,
        t_var=5.0 / 252.0,
    )
    fut_oi = classify_futures_oi_buildup(
        price_now=24008.0,
        price_3m_ago=23985.0,
        oi_now=11_850_000,
        oi_3m_ago=12_000_000,
        is_stop_hunt_reclaim=True,
    )
    pos = summarize_chain_positioning(
        [
            StrikeOINode(24000.0, 90_000, 220_000, 50_000, 95_000),
            StrikeOINode(24200.0, 240_000, 70_000, 85_000, 40_000),
        ],
        spot=24008.0,
    )
    breadth = compute_constituent_breadth(
        default_nifty50_heavyweights(),
        {"HDFCBANK": 0.85, "RELIANCE": 0.65, "ICICIBANK": 0.90, "INFY": 0.40, "TCS": 0.30},
    )
    ev_status = evaluate_event_risk([], now)

    packet = evaluate_interconnected_confluence(
        timestamp=now,
        symbol="NSE:NIFTY50-FUT",
        engine_version=settings.engine_version,
        scanner=scanner,
        state=st,
        vrp=vrp,
        gex=gex,
        futures_buildup=fut_oi,
        chain_positioning=pos,
        breadth=breadth,
        event_status=ev_status,
    )
    # Demo output never touches the real dashboard directory or the forward-test ledger.
    html_path = write_shadow_dashboard_and_ledger(
        scanner,
        session_date=day,
        report_dir=ROOT / "reports" / "demo" / "shadow_scanner",
        ledger_file=ROOT / "data" / "demo" / "shadow_ledger.jsonl",
        engine_version=settings.engine_version,
        data_feed="SYNTHETIC DEMO (not market data)",
    )
    print(packet.interconnected_summary)
    print(f"dashboard: {html_path}")
    return 0


def task_mine_log(params: dict[str, str]) -> int:
    """Parse, reconcile and summarise a reference Trade History PDF (FILE=..., LABEL=log1).

    Outputs go to data/private/mined/<LABEL>.* and are named by LABEL only: the PDF's file name
    often contains the trader's name, so it is never printed or written anywhere.
    """
    sys.path.insert(0, str(ROOT))
    from engine.trade_mining import (
        parse_pdftotext_report,
        profile_mined_trader,
        validate_parsed_report,
    )

    path_str = params.get("FILE")
    if not path_str:
        print("Usage: tasks.py mine-log FILE=data/private/<report>.pdf LABEL=log1", file=sys.stderr)
        return 2
    path = Path(path_str)
    if path.suffix.lower() != ".pdf":
        parsed = parse_pdftotext_report(path.read_text(encoding="utf-8"))
        val = validate_parsed_report(parsed)
        prof = profile_mined_trader(parsed.rows)
        print(
            f"parsed {val.total_rows} trades, valid={val.valid_rows}, "
            f"passed={val.passed}, style={prof.inferred_style}"
        )
        return 0 if val.passed else 1

    import csv
    import dataclasses

    from engine.trade_mining.diagnostics import diagnostics_markdown
    from engine.trade_mining.pdf_report import parse_trade_history_pdf

    label = params.get("LABEL", "log")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", label):
        print("LABEL must be letters, digits, _ or - (no names)", file=sys.stderr)
        return 2
    res = parse_trade_history_pdf(path)
    rep = res.report
    val = validate_parsed_report(rep)
    prof = profile_mined_trader(rep.rows)
    out_dir = ROOT / "data" / "private" / "mined"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{label}_fills.csv").open("w", newline="", encoding="utf-8") as fh:
        fields = [f.name for f in dataclasses.fields(rep.rows[0])] if rep.rows else []
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rep.rows:
            w.writerow(dataclasses.asdict(r))
    md = diagnostics_markdown(rep.rows, label=label, stated_daily_nets=rep.stated_daily_nets)
    (out_dir / f"{label}.md").write_text(md, encoding="utf-8")
    passed = val.passed and not res.issues and bool(rep.rows)
    print(
        f"{label}: {len(rep.rows)} fills on {len({r.trade_date for r in rep.rows})} days, "
        f"net ₹{sum(r.net_pnl_inr for r in rep.rows):,.2f}; "
        f"row errors {len(val.row_mismatches)}, day errors {len(val.daily_net_mismatches)}, "
        f"parse issues {len(res.issues)} -> {'RECONCILED' if passed else 'NOT RECONCILED'}; "
        f"style {prof.inferred_style}"
    )
    for issue in res.issues[:10]:
        print(f"  issue p{issue.page + 1} {issue.trade_date}: {issue.message}")
    for m in val.daily_net_mismatches[:10]:
        print(
            f"  day {m.trade_date}: summed ₹{m.summed_net_inr:,.2f} "
            f"vs stated ₹{m.stated_net_inr:,.2f}"
        )
    print(f"report: data/private/mined/{label}.md (private, gitignored)")
    return 0 if passed else 1


def task_self_review(params: dict[str, str]) -> int:
    """Run behavioral self-review on FILE=path (or synthetic demo journal if omitted)."""
    sys.path.insert(0, str(ROOT))
    from engine.trade_mining import (
        MinedTradeRow,
        analyze_trade_journal,
        parse_pdftotext_report,
    )

    if params.get("FILE"):
        rows = parse_pdftotext_report(Path(params["FILE"]).read_text(encoding="utf-8")).rows
    else:
        if not _require_demo(params, "self-review", "no FILE= journal given"):
            return 2
        day = _today_ist()
        rows = (
            MinedTradeRow(
                day,
                "09:20:00",
                "09:28:00",
                "ACCT-DEMO",
                "NIFTY24000CE",
                "BUY",
                65,
                "INTRADAY",
                120.0,
                110.0,
                5.0,
                12.0,
                -650.0,
                -705.0,
                55.0,
                False,
                0.0,
                False,
                "SL_M",
            ),
            MinedTradeRow(
                day,
                "09:32:00",
                "09:39:00",
                "ACCT-DEMO",
                "NIFTY24000CE",
                "BUY",
                130,
                "INTRADAY",
                112.0,
                102.0,
                2.0,
                11.0,
                -1300.0,
                -1395.0,
                95.0,
                False,
                0.0,
                False,
                "SL_M",
            ),
            MinedTradeRow(
                day,
                "10:15:00",
                "10:42:00",
                "ACCT-DEMO",
                "NIFTY24000CE",
                "BUY",
                65,
                "INTRADAY",
                115.0,
                145.0,
                32.0,
                4.0,
                1950.0,
                1892.0,
                58.0,
                False,
                0.0,
                False,
                "LIMIT",
            ),
        )
    rev = analyze_trade_journal(rows)
    print(
        f"trades={rev.total_trades} win_rate={rev.win_rate_pct}% "
        f"gross=₹{rev.gross_pnl_inr} charges=₹{rev.total_charges_inr} net=₹{rev.net_pnl_inr}"
    )
    print(f"revenge post-loss trades={rev.revenge.post_loss_trade_count}")
    return 0


def task_paper(params: dict[str, str]) -> int:
    """Run PaperBroker execution check & Version route verification."""
    sys.path.insert(0, str(ROOT))
    from engine.core.config import load_settings
    from engine.core.rules import RuleBook
    from engine.execution import OrderRecord, PaperBroker, resolve_execution_route

    if not _require_demo(params, "paper", "no strategy loop or market data feeds PaperBroker"):
        return 2
    settings = load_settings(version=params.get("VERSION", "v2_bot"))
    route = resolve_execution_route(settings, today=_today_ist())
    book = RuleBook.load(ROOT / "config" / "rules")
    broker = PaperBroker(book)
    order = OrderRecord("ORD-001", "NIFTY24000CE", "BUY", "LIMIT", 65, 150.0)
    fill = broker.execute_against_depth(
        order,
        bids=((149.5, 650, 10),),
        asks=((150.0, 650, 10),),
        now=datetime.now(IST),
        trade_date=_today_ist(),
    )
    print(
        f"route={route.target} order_state={fill.order.state.value} "
        f"fill_px={fill.order.avg_fill_price} charges=₹{fill.charges.total:.2f}"
    )
    return 0


def task_backtest(params: dict[str, str]) -> int:
    """Run Event-Driven Backtester with 2x Slippage Rerun, Latency Sweep, and Deflated Sharpe."""
    sys.path.insert(0, str(ROOT))
    from engine.backtest import (
        SimulatedTrade,
        compute_deflated_sharpe,
        evaluate_backtest_with_stress,
        run_block_bootstrap_mc,
    )

    if not _require_demo(
        params, "backtest", "no event replay over recorded data exists yet (M6 not met)"
    ):
        return 2
    trials = int(params.get("TRIALS", "3"))
    sample_trades = [
        SimulatedTrade("NIFTY", "LONG", 65, 100.0, 118.0, 0.25, 58.0),
        SimulatedTrade("NIFTY", "LONG", 65, 110.0, 103.0, 0.25, 58.0),
        SimulatedTrade("NIFTY", "SHORT", 65, 125.0, 104.0, 0.25, 58.0),
        SimulatedTrade("NIFTY", "LONG", 65, 95.0, 115.0, 0.25, 58.0),
        SimulatedTrade("NIFTY", "LONG", 65, 105.0, 124.0, 0.25, 58.0),
        SimulatedTrade("NIFTY", "LONG", 65, 102.0, 96.0, 0.25, 58.0),
        SimulatedTrade("NIFTY", "LONG", 65, 108.0, 129.0, 0.25, 58.0),
    ]
    ev = evaluate_backtest_with_stress(sample_trades)
    pnls = [t.net_pnl_at() for t in sample_trades]
    dsr = compute_deflated_sharpe(pnls, n_trials=trials)
    mc = run_block_bootstrap_mc(pnls, n_paths=200)
    print(
        f"trades={ev.total_trades} net_1x=₹{ev.net_pnl_1x_inr} "
        f"net_2x_slip=₹{ev.net_pnl_2x_slippage_inr} passes_2x={ev.passes_2x_slippage_gate} "
        f"survives_50ms={ev.survives_50ms_retail_latency} DSR={dsr.deflated_sharpe_pct}% "
        f"MC_MDD99=₹{mc.p99_max_drawdown_inr}"
    )
    return 0


def task_candle_backtest(params: dict[str, str]) -> int:
    """CANDLE APPROXIMATION backtest (engine/backtest/candles) on Upstox 1-minute candles:
    STRAT=condor|log3 FROM=YYYY-MM-DD TO=YYYY-MM-DD (default yesterday). Fills at the next
    bar's open with the punitive haircut, rerun at 2x; walk-forward, Monte Carlo and deflated
    Sharpe over every parameter set tried. Order-book strategies are refused. The condor runs
    its variant grid (each a trial; VARIANTS=0 for the live config only). SET=key=value,...
    changes rule parameters (e.g. SET=side_stop_mult=0); each change is a new trial.
    SPREADS=p90|median sets the base haircut from the recorder's measured spreads (QUOTES= another
    recorded-quotes folder; the report always prints the measured table). Report
    and trades in reports/candle_backtest/. CACHE= another download folder; DOTENV= another
    .env file; TRIALS= another trial registry."""
    _raw_dir, book, cal = _eod_common()
    from engine.backtest.candles.data import UpstoxCandleSource
    from engine.backtest.candles.fills import Costs, Haircut
    from engine.backtest.candles.market import Market
    from engine.backtest.candles.runner import (
        render_report,
        render_variants,
        run_candle_backtest,
        trades_csv,
    )
    from engine.backtest.candles.strategies import Ctx, StrategyRefused, resolve
    from engine.cockpit.backfill import _http_json
    from engine.cockpit.config import load_cockpit_config
    from engine.core.config import load_settings
    from engine.core.env import get_secret, load_dotenv

    try:
        spec = resolve(params.get("STRAT", ""))
    except StrategyRefused as exc:
        print(exc, file=sys.stderr)
        return 2
    if "FROM" not in params:
        print("FROM=YYYY-MM-DD is required", file=sys.stderr)
        return 2
    today = _today_ist()
    start = date.fromisoformat(params["FROM"])
    end = date.fromisoformat(params["TO"]) if params.get("TO") else today - timedelta(days=1)
    load_dotenv(Path(params["DOTENV"]) if params.get("DOTENV") else ROOT / ".env")
    settings = load_settings()
    raw = settings.raw
    cb = raw.get("candle_backtest") or {}
    tok = ""
    for name in ("UPSTOX_ACCESS_TOKEN", "UPSTOX_ANALYTICS_TOKEN"):
        try:
            tok = get_secret(name).reveal()
        except Exception:
            tok = ""
        if tok:
            break
    if not tok:
        print("no Upstox token in .env (UPSTOX_ACCESS_TOKEN / UPSTOX_ANALYTICS_TOKEN)")
        return 2
    live: dict[date, dict[tuple[float, str], tuple[str, int]]] = {}
    try:
        from engine.data.recorder.run import load_master, load_recorder_settings
        from engine.data.reference.instrument_master import Kind

        master = load_master(load_recorder_settings(settings), today, datetime.now(IST), fetch=True)
        for e in master.option_expiries("NIFTY", today):
            live[e] = {
                (float(i.strike), "CE" if i.kind is Kind.CALL else "PE"): (i.symbol, i.lot_size)
                for i in master.options("NIFTY", e)
                if i.strike and i.kind in (Kind.CALL, Kind.PUT)
            }
    except Exception as exc:  # live contracts only matter for the last days of the range
        print(f"instrument master unavailable ({type(exc).__name__}); live expiries skipped")
    cache = Path(params.get("CACHE") or ROOT / str(cb.get("cache_dir", "data/candles")))
    src = UpstoxCandleSource(
        tok, _http_json, cache, today=today, live=live,
        window_days=int(cb.get("window_days", 14)), log=print,
    )  # fmt: skip
    sessions = src.sessions(start, end)
    print(f"{spec.key}: {len(sessions)} sessions with index candles, {start} to {end}")
    ck = load_cockpit_config(settings, ROOT)
    rk = raw.get("risk") or {}
    market = Market(src, book, cal)
    hfrom = cb.get("iv_history_from")
    from engine.backtest.candles.spreads import haircut_table, measure_spreads, render_spreads

    quotes_dir = Path(params.get("QUOTES") or settings.paths["parquet"] / "quotes")
    buckets, rec_days = measure_spreads(quotes_dir)
    appendix = render_spreads(buckets, rec_days)
    hc = Haircut(float(cb.get("haircut_min_inr", 0.10)), float(cb.get("haircut_pct", 0.5)))
    if params.get("SPREADS"):  # SPREADS=p90|median: the recorded half-spreads per price bucket
        if not buckets:
            print("SPREADS= needs recorded quotes in data/parquet/quotes", file=sys.stderr)
            return 2
        hc = Haircut(table=haircut_table(buckets, params["SPREADS"]))
        appendix += "\n\nThe base haircut is each bucket's " + params["SPREADS"] + " half-spread."
    scenarios = {
        "base": (hc, 1.0),
        "stress": (
            hc.scaled(float(cb.get("stress_mult", 2.0))),
            float(cb.get("stress_charges_mult", 2.0)),
        ),
        "plan": (
            Haircut(
                float(cb.get("plan_haircut_min_inr", 1.0)), float(cb.get("plan_haircut_pct", 0.75))
            ),
            1.0,
        ),
        "extreme": (
            Haircut(
                float(cb.get("extreme_haircut_min_inr", 1.5)),
                float(cb.get("extreme_haircut_pct", 1.5)),
            ),
            1.0,
        ),
    }

    def make_ctx(scenario: str) -> Any:
        haircut, charges_mult = scenarios[scenario]
        return Ctx(
            market=market,
            haircut=haircut,
            costs=Costs(
                book,
                today,
                broker=str(cb.get("broker", "upstox_plus")),
                fallback_broker=str(cb.get("fallback_broker", "upstox")),
                costs_on="trade_date" if cb.get("costs_on") == "trade_date" else "run_date",
                charges_mult=charges_mult,
            ),
            calendar=cal,
            capital_inr=float((raw.get("capital") or {}).get("inr", 200000)),
            risk=dict(rk),
            run_date=today,
            fill_within_min=float(cb.get("fill_within_min", 3)),
            stale_entry_min=float(cb.get("stale_entry_min", 5)),
            stale_mark_min=float(cb.get("stale_mark_min", 15)),
            eval_every_min=int(cb.get("eval_every_min", 5)),
            trigger_on_haircut=bool(cb.get("trigger_on_haircut", False)),
            iv_history_from=date.fromisoformat(str(hfrom)) if hfrom else None,
            iv_min_history=int(cb.get("iv_min_history", 20)),
            log=print,
        )

    wf = cb.get("walk_forward") or {}
    mc = cb.get("monte_carlo") or {}
    variants = [spec.params(ck)]
    overrides = params.get("SET", "")
    if spec.variants is not None and params.get("VARIANTS", "0" if overrides else "1") != "0":
        variants = spec.variants(ck)
    if overrides:  # e.g. SET=side_stop_mult=0: a new rule, counted as a trial
        from engine.backtest.candles.strategies import apply_overrides

        try:
            variants = [apply_overrides(v, overrides) for v in variants]
        except (ValueError, TypeError) as exc:
            print(f"SET={overrides}: {exc}", file=sys.stderr)
            return 2
    set_tag = "_" + re.sub(r"[^A-Za-z0-9.]+", "-", overrides).strip("-") if overrides else ""
    out = ROOT / "reports" / "candle_backtest"
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for n, var in enumerate(variants):
        label = str(getattr(var, "label", "")) if len(variants) > 1 or overrides else ""
        res = run_candle_backtest(
            spec,
            var,
            make_ctx,
            sessions,
            trials_path=Path(
                params.get("TRIALS")
                or ROOT / str(cb.get("trials_file", "data/backtest/candle_trials.jsonl"))
            ),
            also_trials=variants,
            label=label,
            extra_scenarios=("plan", "extreme"),
            appendix=appendix,
            wf_train=int(wf.get("train", 60)),
            wf_test=int(wf.get("test", 20)),
            mc_paths=int(mc.get("paths", 1000)),
            mc_block=int(mc.get("block", 5)),
            ruin_pct=float(mc.get("ruin_pct", 20)),
        )
        results.append(res)
        s1, s2 = res.s1, res.s2
        others = "; ".join(f"{d}: net {x.net:,.0f}" for d, _, x in res.extra)
        print(
            f"CANDLE APPROXIMATION {spec.key} {label}: {s1.trades} trades, net {s1.net:,.0f}; "
            f"{res.stress_desc}: net {s2.net:,.0f}; {others}; t={s1.t_stat:.2f}; "
            f"DSR {res.dsr.deflated_sharpe_pct:.1f}% over {res.n_trials} trial(s)",
            flush=True,
        )
        tag = f"_v{n + 1}" if len(variants) > 1 else ""
        csv_name = f"{spec.key}_{res.start}_{res.end}{set_tag}{tag}_trades.csv"
        (out / csv_name).write_text(trades_csv(res.base.trades), encoding="utf-8")
        res.notes.append(f"trades of this variant: `{csv_name}`")
    r0 = results[0]
    md = out / f"{spec.key}_{r0.start}_{r0.end}{set_tag}.md"
    parts = [render_variants(results)] if len(results) > 1 else []
    parts += [render_report(r, "", max_trades=40 if n == 0 else 10) for n, r in enumerate(results)]
    md.write_text("\n\n---\n\n".join(parts), encoding="utf-8")
    print(f"{src.budget.count} API calls -> {md}")
    return 0


def task_report(params: dict[str, str]) -> int:
    """Generate Shadow Scanner dashboard report."""
    return task_shadow_scan(params)


def task_cockpit(params: dict[str, str]) -> int:
    """Local cockpit page (P3) at http://127.0.0.1:8765: live scanner, order books, paper
    accounts, replays and research. Runs alongside `record` and `shadow-live`; stopping it never
    affects them. OPEN=0 skips opening the browser."""
    sys.path.insert(0, str(ROOT))
    from engine.cockpit.config import load_cockpit_config
    from engine.cockpit.gateway import run_gateway
    from engine.core.config import load_settings
    from engine.core.logging_setup import setup_logging

    settings = load_settings()
    setup_logging(settings.logging_config, process=_TASK)
    ccfg = load_cockpit_config(settings, ROOT)
    run_gateway(
        ccfg,
        ROOT,
        ledger_file=settings.shadow_scanner.ledger_file,
        open_browser=params.get("OPEN", "1") != "0",
    )
    return 0


def task_replay(params: dict[str, str]) -> int:
    """Replay a recorded day through the engine (P4), normally started from the cockpit page.
    DATE=YYYY-MM-DD, SPEED=0 (max)|1|2|5|10|20|50, SIM=1 for the synthetic recordings.
    Writes only under data/replay/; never touches live journals or recordings."""
    sys.path.insert(0, str(ROOT))
    import signal
    import threading
    from datetime import date as _date

    from engine.cockpit.config import load_cockpit_config
    from engine.cockpit.replay import run_replay_day
    from engine.core.config import load_settings
    from engine.core.logging_setup import setup_logging

    if not params.get("DATE"):
        print("Usage: tasks.py replay DATE=YYYY-MM-DD [SPEED=5] [SIM=1]", file=sys.stderr)
        return 2
    settings = load_settings()
    setup_logging(settings.logging_config, process=_TASK)
    ccfg = load_cockpit_config(settings, ROOT)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        res = run_replay_day(
            settings,
            ROOT,
            day=_date.fromisoformat(params["DATE"]),
            speed=float(params.get("SPEED", "5")),
            sim=params.get("SIM") == "1",
            port=ccfg.replay_port,
            stop=stop,
            hold_after_s=float(params.get("HOLD_S", 6 * 3600)),
        )
    except FileNotFoundError as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 1
    print("replay: " + ", ".join(f"{k}={v}" for k, v in res.items()))
    return 0


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # ₹ in notes; Windows console is cp1252
    if not argv:
        print(__doc__)
        return 2
    name, rest = argv[0], argv[1:]
    global _TASK
    _TASK = name  # each task logs to logs/engine_<task>.log (see setup_logging)
    _apply_analytics_settings()
    params = dict(a.split("=", 1) for a in rest if "=" in a)
    if name in PLANNED:
        print(f"'{name}' is not built yet — planned in {PLANNED[name]}.", file=sys.stderr)
        return 2
    fn = globals().get(f"task_{name.replace('-', '_')}")
    if fn is None:
        print(f"unknown task {name!r}", file=sys.stderr)
        return 2
    return int(fn(params))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
