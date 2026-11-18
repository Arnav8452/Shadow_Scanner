"""Daily panel of every NSE stock (EQ and BE series) from end-of-day files.

One row per stock per trading day: prices, volume, delivery (MTO), whether the stock had
futures that day, whether it was in the F&O ban, and a corporate-action check.

**Returns are corporate-action clean.** ``ret = close / (base × ca_factor) − 1``, where ``base``
is the stock's previous close in this panel (NSE's own previous close on its first row) and
``ca_factor`` the price factor of that day's bonus, split, consolidation or rights issue from
NSE's corporate-actions list (0.5 for a 1:1 bonus; 1.0 on ordinary days). NSE's bhavcopy
previous close is not adjusted (see ``corporate_actions``), so it can't be used for this.
``ret_total`` adds that day's cash dividend. NSE's list names an action under the company's
*current* symbol and ISIN, so it is matched by the day's symbol, its ISIN, or the canonical
(latest) symbol. Two factors are estimated and marked ``ca_estimated``:

- a **demerger** (no ratio in the list) uses the ex-date open, NSE's special pre-open price
  discovery: factor = open / previous close. When the open did not price it (older demergers;
  the day's adjusted move would still exceed the gap flag) the close is used instead, so that
  day's own move is lost. A "Scheme of Arrangement" without the word demerger is treated the
  same only when the open is at least 5% below the previous close;
- an **ETF / fund-unit split** (ISIN ``INF…``; NSE's equities list does not carry them) is
  inferred when the close is within 15% of the previous close divided by 2, 4, 5, 10, 20, 25, 50
  or 100. Company shares (``INE…``) are never inferred: a 50% fall may be real.

``ca_unknown`` marks a rights issue or arrangement whose factor could not be set;
``gap_flag`` marks |ret| > 40% after adjustment: a real crash, or an action still missed.
Studies should look at flagged rows before using them. A missed weekend special
session (Budget Saturdays, Muhurat) just makes the next row's return span two sessions.

Point in time: a row for day T is known at 19:30 IST on T (the files' publication), so it can
inform session T+1 onward. ``in_fo`` / ``fo_banned`` / ``delivery_*`` are None when that day's
source file wasn't downloaded: unknown, never "no".

Stored as one parquet file per month under ``data/parquet/equity/daily/``; query with DuckDB:
``read_parquet('data/parquet/equity/daily/*.parquet')``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from engine.data.eod.bhavcopy import parse_cash_bhavcopy_csv
from engine.data.eod.download import eod_path, read_eod_text
from engine.data.equity.corporate_actions import CorpAction, parse_subject, rights_factor
from engine.data.equity.sources import (
    SymbolChange,
    SymbolMap,
    fo_stock_symbols,
    parse_mto,
    parse_secban,
)

log = logging.getLogger(__name__)

GAP_FLAG = 0.40  # |adjusted return| above this is flagged for a look

SCHEMA = pa.schema(
    [
        ("trade_date", pa.date32()),
        ("symbol", pa.string()),
        ("canonical", pa.string()),
        ("isin", pa.string()),
        ("series", pa.string()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("prev_close", pa.float64()),
        ("ret", pa.float64()),
        ("ret_total", pa.float64()),
        ("dividend", pa.float64()),
        ("traded_qty", pa.int64()),
        ("turnover_inr", pa.float64()),
        ("delivery_qty", pa.int64()),
        ("delivery_pct", pa.float64()),
        ("in_fo", pa.bool_()),
        ("fo_banned", pa.bool_()),
        ("ca_factor", pa.float64()),
        ("ca_event", pa.bool_()),
        ("ca_unknown", pa.bool_()),
        ("ca_estimated", pa.bool_()),
        ("gap_flag", pa.bool_()),
        ("ca_subject", pa.string()),
    ]
)


def _index_actions(actions: Sequence[CorpAction]) -> dict[tuple[str, date], list[CorpAction]]:
    out: dict[tuple[str, date], list[CorpAction]] = {}
    seen: set[tuple[str, date, str]] = set()
    for a in actions:
        k = (a.symbol, a.ex_date, a.subject)
        if k in seen:  # NSE's list repeats some actions
            continue
        seen.add(k)
        out.setdefault((a.symbol, a.ex_date), []).append(a)
        if a.isin:
            out.setdefault((a.isin, a.ex_date), []).append(a)
    return out


def _adjustment(acts: Sequence[CorpAction], cum_price: float) -> tuple[float, float, bool, str]:
    """(price factor, dividend ₹ per share, rights ratio unknown?, arrangement) for one stock's
    actions on a day; arrangement is "demerger", "scheme" or ""."""
    factor, div, unknown, arrangement = 1.0, 0.0, False, ""
    for a in acts:
        for kind, val in parse_subject(a.subject):
            if kind == "dividend":
                div += float(val)
            elif kind in ("bonus", "split"):
                factor *= float(val)
            elif kind == "rights":
                new, held, premium = val
                f = rights_factor(
                    new, held, premium=premium, face_value=a.face_value, cum_price=cum_price
                )
                if f is None:
                    unknown = True
                else:
                    factor *= f
            elif kind == "demerger":
                is_dm = "demerger" in a.subject.lower()
                arrangement = "demerger" if is_dm or arrangement == "demerger" else "scheme"
    return factor, div, unknown, arrangement


SCHEME_MIN_GAP = 0.95  # an arrangement without "demerger" counts only on a >= 5% lower open
ETF_SPLITS = (2, 4, 5, 10, 20, 25, 50, 100)
ETF_SPLIT_TOL = 0.15


def _etf_split(isin: str, base: float, close: float) -> int | None:
    """The inferred unit-split ratio k (1:k) for a fund unit whose close fell to ~base / k."""
    if not isin.startswith("INF") or base <= 0 or close <= 0:
        return None
    rr = close / base
    if rr >= 0.6:
        return None
    k = min(ETF_SPLITS, key=lambda k: abs(rr * k - 1))
    return k if abs(rr * k - 1) <= ETF_SPLIT_TOL else None


def build_panel(
    raw_dir: Path,
    days: Iterable[date],
    changes: Sequence[SymbolChange],
    actions: Sequence[CorpAction] = (),
    *,
    last_close: dict[str, float] | None = None,
) -> Iterator[dict[str, Any]]:
    """Rows in date order. ``last_close`` (canonical symbol → close) carries state across calls
    and is updated in place."""
    smap = SymbolMap(changes)
    by_key = _index_actions(actions)
    last = last_close if last_close is not None else {}
    for day in days:
        cm = eod_path(raw_dir, "cm", day)
        if not cm.exists():
            continue
        rows = [r for r in parse_cash_bhavcopy_csv(read_eod_text(cm)) if r.trade_date == day]
        # A side file that is missing or unreadable means "unknown" for that day (None), never a
        # crash: one malformed NSE file must not stop a ten-year build.
        deliv: dict[tuple[str, str], Any] | None = None
        mto_p = eod_path(raw_dir, "mto", day)
        if mto_p.exists():
            try:
                deliv = {(m.symbol, m.series): m for m in parse_mto(read_eod_text(mto_p))}
            except ValueError as exc:
                log.warning("equity panel: %s unreadable (%s); delivery unknown", mto_p, exc)
        fo_p = eod_path(raw_dir, "fo", day)
        fo = fo_stock_symbols(read_eod_text(fo_p)) if fo_p.exists() else None
        ban: set[str] | None = None
        ban_p = eod_path(raw_dir, "secban", day)
        if ban_p.exists():
            try:
                ban = set(parse_secban(read_eod_text(ban_p))[1])
            except ValueError as exc:
                log.warning("equity panel: %s unreadable (%s); ban list unknown", ban_p, exc)
        for r in rows:
            canon = smap.canonical(r.symbol, day)
            base = last.get(canon) or r.prev_close
            acts = (
                by_key.get((r.symbol, day))
                or by_key.get((r.isin, day))
                or by_key.get((canon, day))
                or []
            )
            factor, div, unknown, arrangement = _adjustment(acts, base)
            estimated = False
            subject = "; ".join(a.subject for a in acts) or None
            if arrangement:
                gap = r.open / base if base > 0 and r.open > 0 else None
                if gap is not None and (arrangement == "demerger" or gap <= SCHEME_MIN_GAP):
                    if abs(r.close / (base * factor * gap) - 1) > GAP_FLAG:
                        # No pre-open price discovery (before NSE's special session for
                        # demergers): the open didn't price it, so the whole day's fall is
                        # taken as the carve-out. Loses one day's real move, not ~80%.
                        gap = r.close / (base * factor)
                        subject = f"{subject} (factor from the close: open did not price it)"
                    factor *= gap
                    estimated = True
                else:
                    unknown = True
            elif not acts:
                k = _etf_split(r.isin, base, r.close)
                if k is not None:
                    factor, estimated = 1.0 / k, True
                    subject = f"inferred unit split 1:{k} (not in NSE's equities list)"
            ok = base > 0 and r.close > 0
            ret = r.close / (base * factor) - 1 if ok else None
            m = deliv.get((r.symbol, r.series)) if deliv is not None else None
            yield {
                "trade_date": day,
                "symbol": r.symbol,
                "canonical": canon,
                "isin": r.isin,
                "series": r.series,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "prev_close": r.prev_close,
                "ret": ret,
                "ret_total": (r.close + div) / (base * factor) - 1 if ok else None,
                "dividend": div,
                "traded_qty": r.traded_qty,
                "turnover_inr": r.turnover_inr,
                "delivery_qty": m.deliverable_qty if m else None,
                "delivery_pct": m.deliverable_pct if m else None,
                "in_fo": (r.symbol in fo) if fo is not None else None,
                "fo_banned": (r.symbol in ban) if ban is not None else None,
                "ca_factor": factor,
                "ca_event": abs(factor - 1) > 1e-9,
                "ca_unknown": unknown,
                "ca_estimated": estimated,
                "gap_flag": ret is not None and abs(ret) > GAP_FLAG,
                "ca_subject": subject,
            }
            if r.close > 0:
                last[canon] = r.close


def write_panel(
    raw_dir: Path,
    out_dir: Path,
    days: Iterable[date],
    changes: Sequence[SymbolChange],
    actions: Sequence[CorpAction] = (),
    *,
    last_close: dict[str, float] | None = None,
) -> dict[str, int]:
    """Build and write one parquet per month (replacing those months). Returns rows per month.
    When rebuilding from a later month, seed ``last_close`` with ``last_closes_before`` so the
    first day's returns use the panel's own previous close."""
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    month: str | None = None
    buf: list[dict[str, Any]] = []

    def flush() -> None:
        if month is None:
            return
        table = pa.Table.from_pylist(buf, schema=SCHEMA)
        tmp = out_dir / f"{month}.parquet.tmp"
        pq.write_table(table, tmp)
        tmp.replace(out_dir / f"{month}.parquet")
        counts[month] = len(buf)

    for row in build_panel(raw_dir, days, changes, actions, last_close=last_close):
        key = row["trade_date"].strftime("%Y-%m")
        if key != month:
            flush()
            month, buf = key, []
        buf.append(row)
    flush()
    return counts


def last_closes_before(out_dir: Path, month: str) -> dict[str, float]:
    """Each stock's last close in the months written before ``month`` (YYYY-MM)."""
    last: dict[str, tuple[date, float]] = {}
    for p in sorted(out_dir.glob("????-??.parquet")):
        if p.stem >= month:
            break
        t = pq.read_table(p, columns=["canonical", "trade_date", "close"]).to_pydict()
        for c, d, px in zip(t["canonical"], t["trade_date"], t["close"], strict=True):
            if px and px > 0 and (c not in last or d >= last[c][0]):
                last[c] = (d, px)
    return {c: v[1] for c, v in last.items()}


def price_index(rets: Sequence[float | None]) -> list[float]:
    """Cumulative price index from daily returns: 1.0 on the first row, then × (1 + ret).
    A missing return counts as 0 (no information that day)."""
    out: list[float] = []
    level = 1.0
    for i, r in enumerate(rets):
        if i > 0 and r is not None:
            level *= 1 + r
        out.append(level)
    return out
