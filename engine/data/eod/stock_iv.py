"""Daily ATM IV of F&O stocks from NSE F&O bhavcopies, and its IV rank (SPEC §5.8).

Same method and fidelity tier as the index IV history (``iv_history.atm_iv_for_day``: synthetic
forward from put-call parity, Black-76 on EOD closes, tier C), applied to stock options. Kept in
``data/parquet/eod/stock_iv.parquet``, one row per (trade date, stock), appended day by day;
rows are known at 19:30 IST on the trade date.

IV rank here is the percentile of today's IV among the stock's previous ``lookback`` daily IVs
(strictly earlier days only); ``None`` with fewer than ``min_obs`` observations.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from engine.core.calendar import TradingCalendar
from engine.core.rules import RuleBook
from engine.data.eod.bhavcopy import parse_fno_bhavcopy_csv
from engine.data.eod.download import eod_path, read_eod_text
from engine.data.eod.iv_history import DailyIV, atm_iv_for_day, percentile_rank

STOCK_OPTION_TYPES = ("STO", "OPTSTK")


def stock_ivs_for_day(
    raw_dir: Path, day: date, *, book: RuleBook, calendar: TradingCalendar | None
) -> list[DailyIV]:
    fo = eod_path(raw_dir, "fo", day)
    if not fo.exists():
        return []
    rows = [r for r in parse_fno_bhavcopy_csv(read_eod_text(fo)) if r.trade_date == day]
    r = book.lookup("risk_free_rate", day, allow_unverified=True, tenor="91d_tbill").as_float()
    out: list[DailyIV] = []
    by_sym: dict[str, list[Any]] = {}
    for x in rows:
        if x.instrument in STOCK_OPTION_TYPES:
            by_sym.setdefault(x.symbol, []).append(x)
    for sym, opts in sorted(by_sym.items()):
        iv = atm_iv_for_day(
            opts, underlying=sym, book=book, calendar=calendar, r=r / 100,
            instruments=STOCK_OPTION_TYPES,
        )  # fmt: skip
        if iv is not None:
            out.append(iv)
    return out


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows


def update_stock_iv(
    path: Path,
    raw_dir: Path,
    days: Iterable[date],
    *,
    book: RuleBook,
    calendar: TradingCalendar | None,
) -> int:
    """Add the given days (already present days are skipped). Returns rows added."""
    existing = _read(path)
    have = {r["trade_date"] for r in existing}
    new: list[dict[str, Any]] = []
    for d in days:
        if d in have:
            continue
        new.extend(asdict(x) for x in stock_ivs_for_day(raw_dir, d, book=book, calendar=calendar))
    if not new:
        return 0
    rows = sorted(existing + new, key=lambda r: (r["trade_date"], r["underlying"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(pa.Table.from_pylist(rows), tmp)
    os.replace(tmp, path)
    return len(new)


def iv_rank(
    path: Path, symbol: str, day: date, *, lookback: int = 252, min_obs: int = 60
) -> tuple[float | None, float | None, int]:
    """(IV rank 0-100, IV on ``day``, observations) for ``symbol``, using days before ``day``."""
    return iv_ranks(path, [symbol], day, lookback=lookback, min_obs=min_obs)[symbol]


def iv_ranks(
    path: Path, symbols: Iterable[str], day: date, *, lookback: int = 252, min_obs: int = 60
) -> dict[str, tuple[float | None, float | None, int]]:
    """:func:`iv_rank` for several symbols with one read."""
    want = set(symbols)
    by: dict[str, list[dict[str, Any]]] = {}
    for r in _read(path):
        if r["underlying"] in want and r["trade_date"] <= day:
            by.setdefault(r["underlying"], []).append(r)
    out: dict[str, tuple[float | None, float | None, int]] = {}
    for s in want:
        rows = sorted(by.get(s, []), key=lambda r: r["trade_date"])
        if not rows or rows[-1]["trade_date"] != day:
            out[s] = (None, None, len(rows))
            continue
        today = float(rows[-1]["atm_iv"])
        hist = [float(r["atm_iv"]) for r in rows[:-1]][-lookback:]
        rank = round(percentile_rank(hist, today), 1) if len(hist) >= min_obs else None
        out[s] = (rank, today, len(hist))
    return out
