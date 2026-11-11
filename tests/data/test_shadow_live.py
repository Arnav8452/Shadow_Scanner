"""Live shadow scanner runner: watchlist, 30-level depth recording, opening range, OI walls,
previous-day levels, dashboard/ledger and alert labelling — on the synthetic feed."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from engine.core.clock import ManualClock, MarketSession
from engine.core.config import load_settings
from engine.core.rules import IST
from engine.data.adapters.sim import SimFeed
from engine.data.recorder.run import RecorderSettings, load_recorder_settings
from engine.data.reference.instrument_master import InstrumentMaster, Kind
from engine.data.schema import DEPTH30_SCHEMA, QUOTES_SCHEMA
from engine.data.store import DatasetWriter, Store
from engine.shadow.live import (
    ShadowLiveConfig,
    ShadowLiveRunner,
    WatchSpec,
    load_shadow_live_config,
    oi_walls,
    previous_day_levels,
    select_watchlist,
)
from engine.shadow.scanner import ShadowOrderBookScanner
from tests.data.conftest import DAY, WEEKLY, opt_symbol

SESSION = MarketSession("NSE", "fo", DAY, time(9, 15), time(15, 40), time(9, 0), time(9, 8), True)
CFG = ShadowLiveConfig(
    watch=(WatchSpec("NIFTY", futures_months=1, option_strikes_each_side=3),),
    max_symbols=12,
    dashboard_interval_s=60,
    recenter_interval_s=300,
)


@pytest.fixture
def rs(repo_root: Path) -> RecorderSettings:
    base = load_recorder_settings(load_settings(root=repo_root, environ={}))
    nifty = replace(base.universe.underlyings[0], spot_symbol="NSE:NIFTY50-INDEX")
    universe = replace(base.universe, underlyings=(nifty,), vix_symbol="NSE:INDIAVIX-INDEX")
    return replace(base, universe=universe)


def test_repo_config_fits_full_d30_limit(repo_root: Path) -> None:
    cfg = load_shadow_live_config(load_settings(root=repo_root, environ={}))
    assert cfg.max_symbols == 50
    worst = (
        2 + 1 + sum(w.futures_months + 2 * (2 * w.option_strikes_each_side + 1) for w in cfg.watch)
    )
    assert worst <= 50


def test_watchlist_core_first_then_nearest_strikes(
    master: InstrumentMaster, rs: RecorderSettings
) -> None:
    now = datetime(2026, 11, 11, 9, 20, tzinfo=IST)
    core = select_watchlist(master, CFG, rs, now=now, spots={})
    assert core == ["NSE:NIFTY50-INDEX", "NSE:INDIAVIX-INDEX", "NSE:NIFTY26NOVFUT"]
    full = select_watchlist(master, CFG, rs, now=now, spots={"NIFTY": 25010.0})
    assert full[:3] == core and len(full) == 12  # capped
    assert full[3:5] == [opt_symbol(WEEKLY, 25000, Kind.CALL), opt_symbol(WEEKLY, 25000, Kind.PUT)]


def _quote_row(ts: datetime, sym: str, kind: str, **kw: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ts": int(ts.timestamp() * 1_000_000),
        "capture": "snap",
        "msg": "snap",
        "symbol": sym,
        "underlying": "NIFTY",
        "kind": kind,
        "qc_flags": 0,
        "conn_id": 1,
        "seq": 1,
        "source": "test",
    }
    row.update(kw)
    return row


def test_previous_day_levels_and_oi_walls_from_recordings(tmp_path: Path) -> None:
    w = DatasetWriter(tmp_path, "quotes", QUOTES_SCHEMA)
    prev = date(2026, 11, 10)
    w.append(
        _quote_row(
            datetime(2026, 11, 10, 15, 39, tzinfo=IST),
            "NSE:NIFTY50-INDEX",
            "IDX",
            high=25100.0,
            low=24900.0,
            ltp=25050.0,
        )
    )
    snap = datetime(2026, 11, 11, 10, 0, tzinfo=IST)
    for k, kind, oi in [
        (25000, "CE", 900),
        (25200, "CE", 5000),
        (24800, "PE", 7000),
        (25000, "PE", 100),
    ]:
        w.append(
            _quote_row(
                snap, opt_symbol(WEEKLY, k, Kind(kind)), kind, expiry=WEEKLY, strike=float(k), oi=oi
            )
        )
    w.flush()
    st = Store(tmp_path)
    assert previous_day_levels(st, prev) == {"NSE:NIFTY50-INDEX": (25100.0, 24900.0, 25050.0)}
    now = datetime(2026, 11, 11, 10, 0, 30, tzinfo=IST)
    assert oi_walls(st, DAY, underlying="NIFTY", expiry=WEEKLY, now=now) == (25200.0, 24800.0)
    # not yet known before the snapshot time (point in time)
    early = datetime(2026, 11, 11, 9, 59, tzinfo=IST)
    assert oi_walls(st, DAY, underlying="NIFTY", expiry=WEEKLY, now=early) is None


def test_runner_end_to_end_on_sim(
    master: InstrumentMaster, rs: RecorderSettings, repo_root: Path, tmp_path: Path
) -> None:
    clock = ManualClock(datetime(2026, 11, 11, 9, 14, 0, tzinfo=IST))
    feed = SimFeed(master, clock, spots={"NIFTY": 25010.0}, max_symbols=50, depth_levels=30)
    settings = load_settings(root=repo_root, environ={})
    scanner = ShadowOrderBookScanner(settings.shadow_scanner, max_lots=1)
    alerts: list[str] = []
    runner = ShadowLiveRunner(
        CFG,
        rs,
        scanner=scanner,
        master=master,
        adapter=feed,
        clock=clock,
        session=SESSION,
        depth_writer=DatasetWriter(tmp_path, "depth30", DEPTH30_SCHEMA),
        store=Store(tmp_path),
        report_path=tmp_path / "dash" / "2026-11-11.html",
        ledger_file=tmp_path / "ledger.jsonl",
        feed_label="SYNTHETIC SIM (not market data)",
        prev_levels={"NSE:NIFTY26NOVFUT": (25200.0, 24800.0, 25000.0)},
        alert=alerts.append,
    )
    runner.start()
    assert len(feed.symbols) == 3  # core only until the spot price arrives
    feed.pump()
    runner.step(0)
    assert len(feed.symbols) == 12 and runner.stats.watchlist == 12
    fut = scanner.states["NSE:NIFTY26NOVFUT"]
    assert (fut.pdh, fut.pdl, fut.lot_size) == (25200.0, 24800.0, 65)
    for _ in range(18 * 60):  # 09:14 -> 09:32 at one update per second
        clock.advance(1)
        feed.pump()
        runner.step(0)
    assert runner.or_frozen
    assert fut.orh_15m >= fut.orl_15m > 0
    runner.finish()
    rows = pq.read_table(next((tmp_path / "depth30").rglob("*.parquet"))).to_pylist()
    assert rows and max(r["levels"] for r in rows) == 30 and len(rows[0]["bid_px"]) == 30
    html = (tmp_path / "dash" / "2026-11-11.html").read_text(encoding="utf-8")
    assert "HYPOTHESIS" in html and "SYNTHETIC SIM" in html
    # ledger is append-only and never duplicated across periodic writes
    n = runner.ledger_written
    assert runner.append_ledger() == 0 and runner.ledger_written == n
    assert all(a.startswith("HYPOTHESIS") for a in alerts)


def test_alerts_once_per_signal_and_labelled(
    master: InstrumentMaster, rs: RecorderSettings, repo_root: Path, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    clock = ManualClock(datetime(2026, 11, 11, 10, 0, tzinfo=IST))
    alerts: list[str] = []
    runner = ShadowLiveRunner(
        CFG,
        rs,
        scanner=ShadowOrderBookScanner(load_settings(root=repo_root, environ={}).shadow_scanner),
        master=master,
        adapter=SimFeed(master, clock, spots={}),
        clock=clock,
        session=SESSION,
        depth_writer=DatasetWriter(tmp_path, "depth30", DEPTH30_SCHEMA),
        store=None,
        report_path=tmp_path / "d.html",
        ledger_file=tmp_path / "l.jsonl",
        feed_label="x",
        alert=alerts.append,
    )
    t0 = datetime(2026, 11, 11, 10, 0, tzinfo=IST)

    def sig(sid: str, grade: str, minute: int, symbol: str = "X") -> SimpleNamespace:
        return SimpleNamespace(
            signal_id=sid,
            created_at=t0 + timedelta(minutes=minute),
            grade=SimpleNamespace(value=grade),
            wave=SimpleNamespace(value="STOP_HUNT_RECLAIM"),
            direction="LONG",
            symbol=symbol,
            confidence_score=82.0,
            entry_price=100.0,
            stop_loss=95.0,
            target_1=110.0,
        )

    # the scanner re-issues a new id on every evaluation: one episode, one alert
    for i in range(5):
        runner._on_signal(sig(f"S{i}", "A_PLUS", i))  # type: ignore[arg-type]
    runner._on_signal(sig("W", "WATCHLIST", 6))  # type: ignore[arg-type]
    runner._on_signal(sig("Y", "ACTIONABLE", 6, symbol="Y"))  # counted, not alerted (not A+)
    runner._on_signal(sig("S9", "A_PLUS", 30))  # type: ignore[arg-type]  # after the cool-down
    assert len(alerts) == 2 and all(a.startswith("HYPOTHESIS") for a in alerts)
    assert runner.stats.evaluations == 8
    assert runner.stats.signals == 3
