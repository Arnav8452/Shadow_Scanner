"""Derived index weights (free-float market cap, the way NSE weights the Nifty 50).

NSE's free monthly factsheet lists only the top 10 weights. For all 50 members the weight is
derived as ``close × shares outstanding × free-float factor``, normalised to 100%, with
``1 − promoter holding`` (from the latest shareholding pattern) as the free-float proxy.

This is an approximation of NSE's Investible Weight Factor: NSE also excludes other locked-in
holdings (government strategic stakes, FDI, ESOP trusts, etc.) and rounds the factor to bands,
and the share count from the last pattern misses later bonuses, splits or buybacks. Rows written
here carry ``weight_status = derived``; a weight becomes verified (empty status) only when it
matches the NSE Indices factsheet within ``tolerance_pts`` (:func:`verify_against_factsheet`).
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DERIVED = "derived"
FACTSHEET_URL = "https://www.niftyindices.com/Factsheet/ind_nifty50.pdf"


@dataclass(frozen=True, slots=True)
class WeightInput:
    close: float
    shares: float
    promoter_pct: float | None  # None = no promoter (e.g. HDFC Bank): free float 1.0
    shp_as_on: date


@dataclass(frozen=True, slots=True)
class DerivedWeight:
    symbol: str
    close: float
    shares: float
    free_float: float
    ff_mcap_cr: float
    weight_pct: float
    shp_as_on: date


def derive_weights(inputs: Mapping[str, WeightInput]) -> dict[str, DerivedWeight]:
    caps: dict[str, tuple[WeightInput, float, float]] = {}
    for sym, x in inputs.items():
        if x.close <= 0 or x.shares <= 0:
            continue
        ff = 1.0 - (x.promoter_pct or 0.0) / 100.0
        ff = min(1.0, max(0.0, ff))
        caps[sym] = (x, ff, x.close * x.shares * ff)
    total = sum(c for _, _, c in caps.values())
    if total <= 0:
        return {}
    return {
        sym: DerivedWeight(
            symbol=sym,
            close=x.close,
            shares=x.shares,
            free_float=round(ff, 4),
            ff_mcap_cr=round(cap / 1e7, 1),
            weight_pct=round(cap / total * 100.0, 2),
            shp_as_on=x.shp_as_on,
        )
        for sym, (x, ff, cap) in caps.items()
    }


def _rewrite(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        return [dict(x) for x in r], list(r.fieldnames or [])


# Earlier weight notes (carried-over weight, a previous derivation, a factsheet match) are
# replaced on each derivation; the membership note before them is kept.
_OLD_WEIGHT_NOTE = re.compile(
    r"\s*(Weight carried over from code|Weight derived |Matches the factsheet).*$"
)


def write_derived_weights(
    path: Path, weights: Mapping[str, DerivedWeight], *, index: str, price_date: date
) -> int:
    """Set ``weight_pct``/``weight_status = derived`` on the active rows of ``index`` (rows with
    no ``effective_to``). Returns rows updated. Membership columns are never changed."""
    rows, fields = _read(path)
    n = 0
    for r in rows:
        if r["index"].strip().upper() != index.upper() or (r.get("effective_to") or "").strip():
            continue
        dw = weights.get(r["symbol"].strip())
        if dw is None:
            continue
        r["weight_pct"] = f"{dw.weight_pct:.2f}"
        r["weight_status"] = DERIVED
        base = _OLD_WEIGHT_NOTE.sub("", r.get("notes") or "").strip()
        r["notes"] = (
            f"{base} Weight derived {price_date.isoformat()}: close x shares (shareholding "
            f"pattern {dw.shp_as_on.isoformat()}) x (1 - promoter holding = {dw.free_float:.4f}), "
            f"normalised over the members; approximates NSE's IWF."
        ).strip()
        n += 1
    _rewrite(path, rows, fields)
    return n


def verify_against_factsheet(
    path: Path,
    factsheet: Mapping[str, float],
    *,
    index: str,
    source_url: str,
    checked_on: date,
    tolerance_pts: float = 0.3,
) -> dict[str, tuple[float, float, bool]]:
    """Mark a derived weight verified (empty ``weight_status``) when it is within
    ``tolerance_pts`` of the factsheet's; others stay ``derived``. Returns
    ``symbol -> (ours, factsheet, matched)``."""
    rows, fields = _read(path)
    out: dict[str, tuple[float, float, bool]] = {}
    for r in rows:
        sym = r["symbol"].strip()
        if r["index"].strip().upper() != index.upper() or sym not in factsheet:
            continue
        if not (r.get("weight_pct") or "").strip():
            continue
        ours, theirs = float(r["weight_pct"]), factsheet[sym]
        ok = abs(ours - theirs) <= tolerance_pts
        out[sym] = (ours, theirs, ok)
        if ok:
            r["weight_status"] = ""
            r["notes"] = (
                f"{(r.get('notes') or '').strip()} Matches the factsheet ({theirs:.2f}%) within "
                f"{tolerance_pts} pts: {source_url} checked {checked_on.isoformat()}."
            ).strip()
    _rewrite(path, rows, fields)
    return out


_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December",
)  # fmt: skip


def _undouble(line: str) -> str:
    """Bold PDF text often comes out with every character twice ('OOccttoobbeerr')."""

    def one(w: str) -> str:
        if len(w) >= 2 and len(w) % 2 == 0 and all(w[i] == w[i + 1] for i in range(0, len(w), 2)):
            return w[::2]
        return w

    return " ".join(one(w) for w in line.split())


def parse_factsheet(
    text: str, company_to_symbol: Mapping[str, str]
) -> tuple[date | None, dict[str, float]]:
    """(factsheet date, ``symbol -> weight %``) from the text of an NSE Indices factsheet's
    "Top constituents by weightage" table. Companies are matched by the names in our
    constituents file (NSE uses the same names in both)."""
    as_of: date | None = None
    head = _undouble(text.splitlines()[0]) if text.strip() else ""
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", head)
    if m and m.group(1) in _MONTHS:
        as_of = date(int(m.group(3)), _MONTHS.index(m.group(1)) + 1, int(m.group(2)))
    i = text.find("Top constituents by weightage")
    body = text[i:] if i >= 0 else text
    out: dict[str, float] = {}
    for name, sym in company_to_symbol.items():
        hit = re.search(re.escape(name) + r"\s+(\d{1,2}\.\d{1,2})\b", body)
        if hit:
            out[sym] = float(hit.group(1))
    return as_of, out
