"""Fundamentals in Parquet (``data/parquet/fundamentals/{results,shareholding}.parquet``).

One row per filing, stamped ``known_at`` (exchange dissemination time, UTC in the file). Readers
always pass ``as_of`` and only see filings known by then; a revised filing supersedes the
original only from its own ``known_at``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from engine.core.net import http_get_bytes
from engine.core.rules import IST
from engine.data.fundamentals.nse import (
    FilingRef,
    fetch_list,
    fetch_xbrl,
    parse_results_list,
    parse_shareholding_list,
    pick_filings,
    polite_sleep,
    results_list_url,
    shareholding_list_url,
)
from engine.data.fundamentals.xbrl import (
    QuarterlyResult,
    ShareholdingPattern,
    XbrlError,
    parse_results_xbrl,
    parse_shareholding_xbrl,
)

log = logging.getLogger(__name__)

RESULTS_FILE = "results.parquet"
# Schemas whose parser is tested on a real filing (tests/fixtures/nse). Results in any other
# schema are shown as "not parsed" instead of estimated.
PARSED_SCHEMAS = frozenset({"IndAs", "Banking", "NBFC", "LI", "GI"})
SHAREHOLDING_FILE = "shareholding.parquet"


def _arrow_type(name: str, annotation: str) -> pa.DataType:
    if name == "known_at":
        return pa.timestamp("us", tz="UTC")
    if annotation.startswith("date"):
        return pa.date32()
    if annotation.startswith("float"):
        return pa.float64()
    if annotation.startswith("int"):
        return pa.int32()
    if annotation.startswith("bool"):
        return pa.bool_()
    return pa.string()


def _schema(cls: type) -> pa.Schema:
    return pa.schema([(f.name, _arrow_type(f.name, str(f.type))) for f in fields(cls)])


RESULTS_SCHEMA = _schema(QuarterlyResult)
SHAREHOLDING_SCHEMA = _schema(ShareholdingPattern)


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows


def upsert(path: Path, records: Iterable[QuarterlyResult | ShareholdingPattern]) -> int:
    """Add rows keyed by ``filing_id`` (a re-parse replaces the row). Returns new row count."""
    new = [asdict(r) for r in records]
    if not new:
        return len(_read(path))
    schema = RESULTS_SCHEMA if "revenue_inr" in new[0] else SHAREHOLDING_SCHEMA
    by_id = {str(r["filing_id"]) or str(r["source_url"]): r for r in _read(path)}
    for r in new:
        by_id[str(r["filing_id"]) or str(r["source_url"])] = r
    rows = sorted(by_id.values(), key=lambda r: (r["symbol"], r["known_at"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), tmp)
    os.replace(tmp, path)
    return len(rows)


def known_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {str(x) for x in pq.read_table(path, columns=["filing_id"]).column(0).to_pylist()}


def _as_of_rows(
    path: Path, symbol: str, as_of: datetime, period_key: Sequence[str]
) -> list[dict[str, Any]]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in _read(path):
        if r["symbol"] != symbol or r["known_at"] > as_of:
            continue
        key = tuple(r[k] for k in period_key)
        if key not in best or r["known_at"] > best[key]["known_at"]:
            best[key] = r
    return sorted(best.values(), key=lambda r: r[period_key[0]], reverse=True)


def results_as_of(
    parquet_dir: Path, symbol: str, as_of: datetime, *, prefer_consolidated: bool = True
) -> list[dict[str, Any]]:
    """Results known at ``as_of``, newest period first, one row per period (consolidated when
    filed, else standalone)."""
    rows = _as_of_rows(
        parquet_dir / RESULTS_FILE, symbol, as_of, ("period_end", "consolidated", "period_months")
    )
    per: dict[tuple[date, int], dict[str, Any]] = {}
    for r in rows:
        key = (r["period_end"], r["period_months"])
        cur = per.get(key)
        if cur is None or (r["consolidated"] == prefer_consolidated and not cur["consolidated"]):
            per[key] = r
    return sorted(per.values(), key=lambda r: r["period_end"], reverse=True)


def shareholding_as_of(parquet_dir: Path, symbol: str, as_of: datetime) -> list[dict[str, Any]]:
    return _as_of_rows(parquet_dir / SHAREHOLDING_FILE, symbol, as_of, ("as_on",))


# --------------------------------------------------------------------------- refresh


@dataclass
class RefreshReport:
    symbol: str
    results_new: int = 0
    shareholding_new: int = 0
    errors: list[str] = field(default_factory=list)


def _list_path(raw_dir: Path, kind: str, symbol: str, now: datetime) -> Path:
    return raw_dir / "fundamentals" / "lists" / kind / symbol / f"{now.date().isoformat()}.json"


def refresh_symbol(
    symbol: str,
    *,
    raw_dir: Path,
    parquet_dir: Path,
    now: datetime,
    n_periods: int = 5,
    fetcher: Callable[[str], bytes] = http_get_bytes,
    pace_s: float = 1.0,
    sleep: Callable[[float], None] | None = None,
    kinds: Sequence[str] = ("results", "shareholding"),
) -> RefreshReport:
    """Fetch NSE's results and shareholding lists for ``symbol``, download the XBRL files of the
    latest ``n_periods`` periods not parsed yet, and add them to Parquet."""
    rep = RefreshReport(symbol)
    pause: Callable[[float], None] = sleep if sleep is not None else (lambda s: polite_sleep(s))
    jobs: tuple[tuple[str, str, Callable[[str], list[FilingRef]], str], ...] = (
        ("results", results_list_url(symbol), parse_results_list, RESULTS_FILE),
        (
            "shareholding",
            shareholding_list_url(symbol),
            lambda t: parse_shareholding_list(t, symbol),
            SHAREHOLDING_FILE,
        ),
    )
    for kind, url, parse_list, fname in jobs:
        if kind not in kinds:
            continue
        lst = fetch_list(url, _list_path(raw_dir, kind, symbol, now), now=now, fetcher=fetcher)
        pause(pace_s)
        if lst.status != "ok" or lst.path is None:
            rep.errors.append(f"{kind} list: {lst.detail}")
            continue
        try:
            refs = [r for r in parse_list(lst.path.read_text("utf-8")) if r.symbol == symbol]
        except ValueError as exc:
            rep.errors.append(f"{kind} list: {exc}")
            continue
        out_path = parquet_dir / fname
        have = known_ids(out_path)
        parsed: list[QuarterlyResult | ShareholdingPattern] = []
        for ref in pick_filings(refs, n_periods):
            if ref.filing_id in have:
                continue
            dl = fetch_xbrl(ref, raw_dir=raw_dir, now=now, fetcher=fetcher)
            if dl.status == "ok":
                pause(pace_s)
            if dl.path is None:
                rep.errors.append(f"{kind} {ref.period_end}: {dl.detail}")
                continue
            parser = parse_results_xbrl if kind == "results" else parse_shareholding_xbrl
            try:
                parsed.append(
                    parser(
                        dl.path.read_bytes(),
                        known_at=ref.known_at,
                        source_url=ref.xbrl_url,
                        filing_id=ref.filing_id,
                        revision=ref.revision,
                    )
                )
            except XbrlError as exc:
                rep.errors.append(f"{kind} {ref.period_end}: {exc}")
        upsert(out_path, parsed)
        if kind == "results":
            rep.results_new = len(parsed)
        else:
            rep.shareholding_new = len(parsed)
    return rep


# --------------------------------------------------------------------------- summaries


def _growth(cur: float | None, base: float | None) -> float | None:
    if cur is None or base is None or base == 0:
        return None
    return round((cur - base) / abs(base) * 100.0, 1)


def _cr(v: float | None) -> float | None:
    return None if v is None else round(v / 1e7, 1)


def equity_summary(parquet_dir: Path, symbol: str, as_of: datetime) -> dict[str, Any]:
    """Latest quarter with QoQ / YoY growth and the latest holding changes, as known at ``as_of``.

    Growth compares like with like (same period length, consolidated preferred). Values in ₹ cr.
    """
    res = [r for r in results_as_of(parquet_dir, symbol, as_of) if r["period_months"] == 3]
    out: dict[str, Any] = {"symbol": symbol, "results": None, "holding": None}
    if res:
        cur = res[0]
        by_end = {r["period_end"]: r for r in res}
        prev = res[1] if len(res) > 1 else None
        yoy_end = date(cur["period_end"].year - 1, cur["period_end"].month, cur["period_end"].day)
        yoy = by_end.get(yoy_end)
        parsed = cur["schema"] in PARSED_SCHEMAS

        def g(col: str, base: dict[str, Any] | None) -> float | None:
            return _growth(cur[col], base[col]) if base and parsed else None

        def v(x: float | None) -> float | None:
            return x if parsed else None

        out["results"] = {
            "not_parsed": not parsed,
            "period_end": cur["period_end"].isoformat(),
            "known_at": cur["known_at"].astimezone(IST).strftime("%d %b %Y %H:%M"),
            "consolidated": cur["consolidated"],
            "schema": cur["schema"],
            "revenue_cr": v(_cr(cur["revenue_inr"])),
            "ebitda_cr": v(_cr(cur["ebitda_inr"])),
            "operating_profit_cr": v(_cr(cur["operating_profit_inr"])),
            "pat_cr": v(_cr(cur["pat_owners_inr"] or cur["pat_inr"])),
            "eps": v(cur["eps_basic"]),
            "revenue_qoq_pct": g("revenue_inr", prev),
            "revenue_yoy_pct": g("revenue_inr", yoy),
            "pat_qoq_pct": g("pat_inr", prev),
            "pat_yoy_pct": g("pat_inr", yoy),
            "ebitda_margin_pct": (
                round(cur["ebitda_inr"] / cur["revenue_inr"] * 100.0, 1)
                if parsed and cur["ebitda_inr"] is not None and cur["revenue_inr"]
                else None
            ),
            "source_url": cur["source_url"],
        }
    shp = shareholding_as_of(parquet_dir, symbol, as_of)
    if shp:
        cur_h, prev_h = shp[0], (shp[1] if len(shp) > 1 else None)
        cols = ("promoter_pct", "fii_pct", "dii_pct", "mf_pct", "retail_pct")
        out["holding"] = {
            "as_on": cur_h["as_on"].isoformat(),
            "known_at": cur_h["known_at"].astimezone(IST).strftime("%d %b %Y %H:%M"),
            "prev_as_on": prev_h["as_on"].isoformat() if prev_h else None,
            **{c: cur_h[c] for c in cols},
            **{
                f"{c}_chg": (
                    round(cur_h[c] - prev_h[c], 2)
                    if prev_h and cur_h[c] is not None and prev_h[c] is not None
                    else None
                )
                for c in cols
            },
            "source_url": cur_h["source_url"],
        }
    return out
