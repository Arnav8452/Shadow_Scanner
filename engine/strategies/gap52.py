"""Gap to a 52-week high at the open: a forward paper test of a rule from a Reddit comment
(owner, 23 Nov 2026: "put idea one on paper exact as it is"). HYPOTHESIS; paper only.

The comment, as written: look for 52-week highs in the pre-open market; take a stock that is
about 2% up but was not on the previous day's 52-week-high list; F&O stocks only; aim for a
2-3% rise with a trailing stop-loss sell order; hold delivery, not intraday ("if it won't reach
target in same day it can in a week").

How each phrase is read (the parts the comment leaves open are config, ``gap52_paper``):

* **Pre-open, about 2% up:** the session's open is the pre-open call-auction price, so the gap
  is ``open / previous close - 1`` and must lie in [``gap_min_pct``, ``gap_max_pct``].
* **52-week top:** the open is at or above NSE's adjusted 52-week high through the previous
  session (the ``w52`` file for the session, published before it opens).
* **Not on the previous day's list:** that high was not set on the previous session.
* **F&O stocks:** stock futures in the previous session's F&O bhavcopy.
* **Trailing stop:** set at entry, ``trail_pct`` below the highest high since entry; the
  2-3% "aim" is not a fixed exit (the trailing stop is there "to get maximum return").
* **A week:** a sale at the next open after ``max_hold_sessions`` sessions held.

Everything used is known at the pre-open of the session traded. Fills are end-of-day estimates
(open plus a half-spread from ``swing_paper``); see ``engine.execution.swing_paper``.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from engine.core.calendar import TradingCalendar
from engine.core.config import RiskLimits
from engine.core.rules import RuleBook
from engine.data.eod.bhavcopy import (
    CashBhavcopyRow,
    parse_cash_bhavcopy_csv,
    parse_fno_bhavcopy_csv,
)
from engine.data.eod.delivery_watch import STOCK_FUTURES
from engine.data.eod.download import eod_path, read_eod_text
from engine.data.eod.week52 import Week52, effective_date, parse_week52
from engine.execution.swing_paper import DayBar, Order, SwingFillConfig, SwingPaperAccount

STRATEGY = "gap52"


@dataclass(frozen=True)
class Gap52Config:
    """``gap52_paper`` in settings.yaml. HYPOTHESIS values; paper only."""

    enabled: bool = True
    start_date: date = date(2026, 11, 23)  # forward test: no entries before this session
    gap_min_pct: float = 1.5  # "around 2% up"
    gap_max_pct: float = 2.5
    trail_pct: float = 2.0  # trailing stop below the highest high since entry
    max_hold_sessions: int = 5  # "it can in a week"
    ticket_inr: float = 25_000.0
    max_new_per_day: int = 2
    max_positions: int = 4


@dataclass(frozen=True)
class Gap52Signal:
    symbol: str
    open: float
    prev_close: float
    gap_pct: float
    high52: float
    high52_date: str
    prev_turnover_inr: float


def gap52_config(raw: Mapping[str, Any] | None) -> Gap52Config:
    if not raw:
        return Gap52Config()
    d = Gap52Config()
    start = raw.get("start_date", d.start_date)
    return Gap52Config(
        enabled=bool(raw.get("enabled", d.enabled)),
        start_date=start if isinstance(start, date) else date.fromisoformat(str(start)),
        gap_min_pct=float(raw.get("gap_min_pct", d.gap_min_pct)),
        gap_max_pct=float(raw.get("gap_max_pct", d.gap_max_pct)),
        trail_pct=float(raw.get("trail_pct", d.trail_pct)),
        max_hold_sessions=int(raw.get("max_hold_sessions", d.max_hold_sessions)),
        ticket_inr=float(raw.get("ticket_inr", d.ticket_inr)),
        max_new_per_day=int(raw.get("max_new_per_day", d.max_new_per_day)),
        max_positions=int(raw.get("max_positions", d.max_positions)),
    )


def find_signals(
    today: Mapping[str, CashBhavcopyRow],
    week52: Mapping[str, Week52],
    fno: set[str],
    *,
    prev_day: date,
    prev_turnover: Mapping[str, float],
    cfg: Gap52Config,
) -> list[Gap52Signal]:
    """The session's signals, most liquid (previous session's turnover) first."""
    out: list[Gap52Signal] = []
    for sym in sorted(fno):
        r, w = today.get(sym), week52.get(sym)
        if r is None or w is None or r.open <= 0 or r.prev_close <= 0:
            continue
        gap = (r.open / r.prev_close - 1.0) * 100.0
        if not cfg.gap_min_pct <= gap <= cfg.gap_max_pct:
            continue
        if r.open < w.high:  # not at a 52-week high in the pre-open
            continue
        if w.high_date == prev_day:  # on the previous day's 52-week-high list
            continue
        out.append(
            Gap52Signal(
                symbol=sym,
                open=r.open,
                prev_close=r.prev_close,
                gap_pct=round(gap, 3),
                high52=w.high,
                high52_date=w.high_date.isoformat() if w.high_date else "",
                prev_turnover_inr=prev_turnover.get(sym, 0.0),
            )
        )
    return sorted(out, key=lambda s: (-s.prev_turnover_inr, s.symbol))


def _eq_rows(raw_dir: Path, day: date) -> dict[str, CashBhavcopyRow]:
    p = eod_path(raw_dir, "delivery", day)
    if not p.exists():
        return {}
    return {
        r.symbol: r
        for r in parse_cash_bhavcopy_csv(read_eod_text(p))
        if r.trade_date == day and r.series == "EQ"
    }


def run_gap52_night(
    *,
    day: date,
    raw_dir: Path,
    state_dir: Path,
    book: RuleBook,
    cal: TradingCalendar,
    limits: RiskLimits,
    broker: str,
    fills: SwingFillConfig,
    cfg: Gap52Config,
) -> list[str]:
    """Evening run for session ``day``: place the entries the pre-open signalled, then apply
    the session's bars (fills at the open, stops, marks). Idempotent per session."""
    if not cfg.enabled or day < cfg.start_date:
        return []
    acct = SwingPaperAccount(
        state_dir / "gap52_paper_state.json", limits=limits, book=book, calendar=cal,
        broker=broker, fills=fills, max_hold_sessions=cfg.max_hold_sessions,
        journal_name=f"paper_{STRATEGY}.jsonl",
    )  # fmt: skip
    if acct.state.last_day and day.isoformat() <= acct.state.last_day:
        return [f"{day} already processed"]
    today = _eq_rows(raw_dir, day)
    if not today:
        raise FileNotFoundError(f"delivery file for {day} not downloaded")
    prev = cal.previous_trading_day(day)
    lines: list[str] = []
    signals: list[Gap52Signal] = []
    decisions: list[dict[str, Any]] = []
    fo_path, w52_path = eod_path(raw_dir, "fo", prev), eod_path(raw_dir, "w52", day)
    if not fo_path.exists() or not w52_path.exists():
        lines.append(f"no entries: F&O list for {prev} or 52-week file for {day} missing")
    else:
        w52_text = read_eod_text(w52_path)
        eff = effective_date(w52_text)
        if eff != day:
            lines.append(f"no entries: the 52-week file is for {eff}, not {day}")
        else:
            fno = {
                r.symbol
                for r in parse_fno_bhavcopy_csv(read_eod_text(fo_path))
                if r.trade_date == prev and r.instrument in STOCK_FUTURES
            }
            turnover = {s: r.turnover_inr for s, r in _eq_rows(raw_dir, prev).items()}
            signals = find_signals(
                today, parse_week52(w52_text), fno, prev_day=prev, prev_turnover=turnover,
                cfg=cfg,
            )  # fmt: skip
            budget = float(limits.per_trade_max_loss_pct.get("directional", 2.5))
            new = 0
            for s in signals:
                row: dict[str, Any] = asdict(s)
                held = len(acct.state.positions) + len(acct.state.pending)
                qty = math.floor(cfg.ticket_inr / s.open)
                if new >= cfg.max_new_per_day:
                    row["note"] = f"{cfg.max_new_per_day} entries already taken today"
                elif held >= cfg.max_positions:
                    row["note"] = f"{cfg.max_positions} positions already open"
                elif qty < 1:
                    row["note"] = "price above the ticket size"
                else:
                    o = Order(
                        id="", strategy=STRATEGY, symbol=s.symbol, instrument="CASH",
                        key=s.symbol, qty=qty, est_price=s.open, stop_pct=cfg.trail_pct,
                        trail_pct=cfg.trail_pct, max_hold=cfg.max_hold_sessions,
                        reason=f"open {s.open:g} = +{s.gap_pct:.2f}% at/above the 52-week "
                        f"high {s.high52:g} (set {s.high52_date})",
                    )  # fmt: skip
                    ok, msg = acct.place_entry(o, day=day, risk_budget_pct=budget)
                    row["note"] = msg
                    new += ok
                decisions.append(row)
            lines.append(f"{len(signals)} signal(s), {new} taken at the open")
    bars = {s: DayBar(r.open, r.high, r.low, r.close) for s, r in today.items()}
    closes = {s: r.close for s, r in today.items()}
    lines += acct.process_day(day, bars, closes)
    acct.save()
    out = state_dir / STRATEGY / f"{day.isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "day": day.isoformat(),
        "signals": decisions,
        "account": acct.snapshot(),
        "note": "HYPOTHESIS: a Reddit rule on paper exactly as written (owner, 23 Nov 2026). "
        "End-of-day fills at the open plus an estimated half-spread; not backtested.",
    }
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, out)
    return lines
