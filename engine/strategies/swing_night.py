"""The nightly swing run (evening step ``swing``): paper account first, then new candidates.

Order on trade date ``T`` (after NSE's files, ~19:30 IST):

1. stock IV for ``T`` -> ``data/parquet/eod/stock_iv.parquet`` (for IV rank);
2. the swing paper account applies ``T``'s bars (fills orders placed the night before at
   ``T``'s open, stops, line/time/expiry exits, marks, loss limits);
3. scan the F&O stocks on bars up to ``T``; choose the instrument (IV rank, results dates from
   NSE's board-meeting list, the ban list for ``T+1``, the premium budget) and size;
4. queue entries for ``T+1``'s open within ``settings.risk`` (approved limits, unchanged);
5. on a month's last trading day, the trend sleeve's signal (NIFTYBEES, SPEC §5.6).

Writes ``data/state/swing/<T>.json`` (candidates, plans, what was queued and why not).
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from engine.core.calendar import TradingCalendar
from engine.core.config import RiskLimits
from engine.core.rules import RuleBook
from engine.data.eod.bhavcopy import parse_fno_bhavcopy_csv
from engine.data.eod.delivery_watch import STOCK_FUTURES, ban_for_next_session, delivery_history
from engine.data.eod.download import eod_path, read_eod_text
from engine.data.eod.stock_iv import iv_ranks, update_stock_iv
from engine.data.fundamentals.nse import parse_board_meetings
from engine.execution.swing_paper import (
    DayBar,
    Order,
    SwingFillConfig,
    SwingPaperAccount,
    option_key,
)
from engine.strategies.swing_scanner import (
    Bar,
    SwingConfig,
    atm_option,
    plan_trade,
    rank_candidates,
    scan_symbol,
)
from engine.strategies.trend_sleeve import ETF, is_month_end, tsmom_signal


@dataclass(frozen=True)
class TrendConfig:
    """``trend`` in settings.yaml. HYPOTHESIS; paper only."""

    enabled: bool = True
    allocation_pct: float = 20.0  # of capital, in NIFTYBEES when the signal is long
    disaster_stop_pct: float = 10.0  # also its risk for the per-trade gate (SPEC §6 stress)


@dataclass
class SwingNightReport:
    day: date
    lines: list[str] = field(default_factory=list)
    candidates: int = 0
    queued: int = 0


def results_dates(raw_dir: Path, as_of: date) -> dict[str, date]:
    """Earliest results board meeting after ``as_of`` per symbol, from the newest saved NSE
    board-meeting list fetched on or before ``as_of``."""
    base = raw_dir / "fundamentals" / "lists" / "board_meetings"
    if not base.exists():
        return {}
    files = sorted(p for p in base.glob("*.json") if p.stem <= as_of.isoformat())
    if not files:
        return {}
    out: dict[str, date] = {}
    for m in parse_board_meetings(files[-1].read_text("utf-8")):
        if m.is_results and m.meeting_date > as_of:
            cur = out.get(m.symbol)
            if cur is None or m.meeting_date < cur:
                out[m.symbol] = m.meeting_date
    return out


def run_swing_night(
    *,
    day: date,
    now: datetime,
    raw_dir: Path,
    parquet_dir: Path,
    state_dir: Path,
    book: RuleBook,
    cal: TradingCalendar,
    limits: RiskLimits,
    broker: str,
    cfg: SwingConfig,
    trend: TrendConfig,
    fills: SwingFillConfig,
    fetch_index: Callable[[date], None] | None = None,
) -> SwingNightReport:
    rep = SwingNightReport(day)
    fo_path = eod_path(raw_dir, "fo", day)
    if not fo_path.exists() or not eod_path(raw_dir, "delivery", day).exists():
        raise FileNotFoundError(f"F&O bhavcopy or delivery file for {day} not downloaded")
    fo_rows = [r for r in parse_fno_bhavcopy_csv(read_eod_text(fo_path)) if r.trade_date == day]
    fno = {r.symbol for r in fo_rows if r.instrument in STOCK_FUTURES}
    need = max(cfg.lookback, cfg.sma_days + 10, cfg.vol_avg_days) + 5
    hist = delivery_history(raw_dir, day, sessions=need, max_calendar_days=need * 2 + 30)
    bars = {s: [Bar.from_row(r) for r in sorted(rows, key=lambda r: r.trade_date)]
            for s, rows in hist.items()}  # fmt: skip

    added = update_stock_iv(parquet_dir / "eod" / "stock_iv.parquet", raw_dir, [day], book=book,
                            calendar=cal)  # fmt: skip
    rep.lines.append(f"stock IV: {added} row(s) for {day}")

    # ---- the paper account applies today's bars
    acct = SwingPaperAccount(
        state_dir / "swing_paper_state.json", limits=limits, book=book, calendar=cal,
        broker=broker, fills=fills, max_hold_sessions=cfg.max_hold_sessions,
        time_stop_sessions=cfg.time_stop_sessions,
    )  # fmt: skip
    day_bars: dict[str, DayBar] = {}
    closes: dict[str, float] = {}
    for s, bs in bars.items():
        if bs and bs[-1].day == day:
            b = bs[-1]
            day_bars[s] = DayBar(b.open, b.high, b.low, b.close)
            closes[s] = b.close
    for r in fo_rows:
        if r.instrument in ("STO", "OPTSTK") and r.close > 0:
            day_bars[option_key(r.symbol, r.expiry, r.strike, r.option_type)] = DayBar(
                r.open, r.high, r.low, r.close
            )
    rep.lines += [f"paper: {x}" for x in acct.process_day(day, day_bars, closes)]

    # ---- scan
    ban_day, banned = ban_for_next_session(raw_dir, day)
    results = results_dates(raw_dir, day)
    cands = [c for s in sorted(fno) if (c := scan_symbol(s, bars.get(s, []), cfg)) is not None]
    cands = rank_candidates(cands, cfg)
    ranks = iv_ranks(parquet_dir / "eod" / "stock_iv.parquet", [c.symbol for c in cands], day)
    plans = []
    for c in cands:
        rdate = results.get(c.symbol)
        if rdate is not None and rdate > day + timedelta(days=cfg.results_buffer_days):
            rdate = None
        opt = atm_option(
            fo_rows, c.symbol, "CE" if c.direction == "LONG" else "PE", day=day,
            min_trading_days=cfg.min_option_days, trading_days_to=cal.trading_days_between,
        )  # fmt: skip
        plan = plan_trade(
            c, cfg=cfg, capital=limits.capital_inr, iv_rank=ranks.get(c.symbol, (None,))[0],
            has_fno=c.symbol in fno, banned=c.symbol in banned, results_date=rdate, option=opt,
        )  # fmt: skip
        plans.append(plan)
    rep.candidates = len(plans)
    budget = float(limits.per_trade_max_loss_pct.get("directional", 2.5))
    placed: list[dict[str, Any]] = []
    for p in plans:
        row = p.to_dict()
        if p.vehicle in ("CASH", "OPTION"):
            c = p.candidate
            o = Order(
                id="", strategy="swing", symbol=c.symbol, instrument=p.vehicle,
                key=p.option.contract if p.option else c.symbol, qty=p.qty,
                est_price=p.est_entry,
                stop_pct=cfg.option_premium_stop_pct if p.option else cfg.cash_stop_pct,
                line=c.level if c.direction == "LONG" else 0.0, setup_close=c.close,
                expiry=p.option.expiry.isoformat() if p.option else "",
                reason=f"{c.setup.value}: {c.rationale}",
            )  # fmt: skip
            ok, msg = acct.place_entry(o, day=day, risk_budget_pct=budget)
            row["queued"], row["queue_note"] = ok, msg
            rep.queued += ok
        placed.append(row)

    # ---- trend sleeve
    trend_row: dict[str, Any] | None = None
    if trend.enabled and is_month_end(cal, day):
        rf = book.lookup("risk_free_rate", day, allow_unverified=True, tenor="91d_tbill").as_float()
        sig = tsmom_signal(raw_dir, day, cal=cal, rf_pct=rf, fetch=fetch_index)
        if sig is None:
            rep.lines.append("trend: index closes missing; no signal")
        else:
            trend_row = {"signal": sig.text, "long": sig.long, "ret_12m_pct": sig.ret_12m_pct}
            held = acct.holds(ETF)
            if sig.long and not held and closes.get(ETF):
                px = closes[ETF]
                qty = math.floor(limits.inr(trend.allocation_pct) / px)
                o = Order(id="", strategy="trend", symbol=ETF, instrument="CASH", key=ETF, qty=qty,
                          est_price=px, stop_pct=trend.disaster_stop_pct,
                          reason=sig.text)  # fmt: skip
                ok, msg = acct.place_entry(o, day=day, risk_budget_pct=budget)
                trend_row["queued"], trend_row["queue_note"] = ok, msg
            elif not sig.long and held:
                acct.queue_exit(ETF, "trend signal flat")
                trend_row["queue_note"] = "exit queued"
            rep.lines.append("trend: " + sig.text)

    acct.save()
    out = state_dir / "swing" / f"{day.isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "day": day.isoformat(),
        "known_at": f"{day.isoformat()}T19:30:00+05:30",
        "ban_trade_date": ban_day.isoformat() if ban_day else None,
        "plans": placed,
        "trend": trend_row,
        "account": acct.snapshot(),  # after tonight's queueing, for the cockpit
        "note": "HYPOTHESIS rules (SPEC §5.8); paper only. Stock options are paper-only while "
        "stock F&O is deferred at ₹2L capital.",
    }
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, out)
    rep.lines.append(f"swing: {rep.candidates} candidate(s), {rep.queued} queued for the next open")
    return rep


def trend_config(raw: Mapping[str, Any] | None) -> TrendConfig:
    if not raw:
        return TrendConfig()
    return TrendConfig(
        enabled=bool(raw.get("enabled", True)),
        allocation_pct=float(raw.get("allocation_pct", 20.0)),
        disaster_stop_pct=float(raw.get("disaster_stop_pct", 10.0)),
    )


def fill_config(raw: Mapping[str, Any] | None) -> SwingFillConfig:
    if not raw:
        return SwingFillConfig()
    return SwingFillConfig(
        cash_half_spread_pct=float(raw.get("cash_half_spread_pct", 0.05)),
        option_half_spread_pct=float(raw.get("option_half_spread_pct", 1.5)),
    )
