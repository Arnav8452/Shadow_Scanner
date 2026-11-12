"""Cross-platform task runner (Windows has no `make`). The Makefile delegates here.

uv run python tasks.py test | lint | fmt | rules | instruments | fyers-login
                      | upstox-login | upstox-probe [D30=1] [SECONDS=n]
                      | record [SIM=1] [MINUTES=n] | qc [DATE=...] [SIM=1] [LAST=5]
                      | shadow-live [SIM=1] [MINUTES=n] [SEND=1]
                      | cockpit [OPEN=0] | replay DATE=YYYY-MM-DD [SPEED=5] [SIM=1]
                      | eod [DATE=] | eod-backfill FROM= TO= | iv-history [FROM= TO=]
                      | screener-history [DAYS=400]
                      | evening [DATE=] [STEPS=...] | fundamentals [SYMBOLS=A,B]
                      | swing [DATE=] | delivery-study [FROM= TO=] | index-weights-verify
                      | event-arb | shadow-scan [VERSION=v1_shadow|v2_bot]
                      | mine-log [FILE=...] | self-review [FILE=...]
                      | paper [VERSION=v2_bot] | backtest [TRIALS=n] | report
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
    setup_logging(settings.logging_config)
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
    setup_logging(settings.logging_config)
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


def task_eod(params: dict[str, str]) -> int:
    """Download one day's NSE end-of-day files (default: today, after ~19:30 IST).
    DATE=YYYY-MM-DD, DATASETS=fo,cm,delivery,participant_oi,participant_vol,indices (default
    all). Without DATE it also saves the F&O ban list and bulk/block deals (current-only files)."""
    raw_dir, _, _ = _eod_common()
    from engine.data.eod.download import DATASETS, fetch_eod

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
    setup_logging(settings.logging_config)
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
    return 1 if rep.errors else 0


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
    """Backfill NSE end-of-day files for FROM..TO (weekdays; holidays show as missing), then
    rebuild the IV history. DATASETS default: fo,indices (what the IV history needs)."""
    raw_dir, _, _ = _eod_common()
    from engine.data.eod.download import backfill

    if not (params.get("FROM") and params.get("TO")):
        print("usage: tasks.py eod-backfill FROM=YYYY-MM-DD TO=YYYY-MM-DD", file=sys.stderr)
        return 2
    start, end = date.fromisoformat(params["FROM"]), date.fromisoformat(params["TO"])
    sets = params.get("DATASETS", "fo,indices").split(",")
    results = backfill(sets, start, end, raw_dir=raw_dir, now=lambda: datetime.now(IST))
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"backfill {start}..{end} {sets}: {counts}")
    return task_iv_history({"FROM": params["FROM"], "TO": params["TO"]})


def task_iv_history(params: dict[str, str]) -> int:
    """Rebuild data/parquet/eod/iv_history.parquet (tier C: EOD closes) from downloaded
    bhavcopies. FROM/TO default to the last 400 calendar days."""
    raw_dir, book, cal = _eod_common()
    from datetime import timedelta

    from engine.data.eod.download import weekdays
    from engine.data.eod.iv_history import build_iv_history, write_iv_history

    end = date.fromisoformat(params["TO"]) if params.get("TO") else _today_ist()
    start = date.fromisoformat(params["FROM"]) if params.get("FROM") else end - timedelta(days=400)
    unds = params.get("UNDERLYINGS", "NIFTY,BANKNIFTY,FINNIFTY").split(",")
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
    setup_logging(settings.logging_config)
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
    setup_logging(settings.logging_config)
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
    setup_logging(settings.logging_config)
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
