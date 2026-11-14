"""Parsers for the equity panel's inputs that the bhavcopy module doesn't cover.

- MTO files (security-wise delivery position), NSE's delivery data for every year.
- The dated F&O ban-list archive.
- NSE's symbol-change list, so a renamed stock keeps one identity across years.
- The set of stocks with futures on a day, read straight from an F&O bhavcopy (either layout).
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from engine.core.net import http_get_bytes

SYMBOL_CHANGES_URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"
STOCK_FUTURES = frozenset({"STF", "FUTSTK"})


@dataclass(frozen=True, slots=True)
class MtoRow:
    trade_date: date
    symbol: str
    series: str
    traded_qty: int
    deliverable_qty: int
    deliverable_pct: float


@dataclass(frozen=True, slots=True)
class SymbolChange:
    old: str
    new: str
    effective: date  # first trading day under the new symbol


def _dmy(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%d-%b-%Y").date()


_TRADE_DATE = re.compile(r"Trade Date <(\d{2}-[A-Za-z]{3}-\d{4})>")


def parse_mto(text: str) -> list[MtoRow]:
    """Record-type 20 lines: ``20,sr,symbol,series,traded,deliverable,pct``."""
    m = _TRADE_DATE.search(text)
    if not m:
        raise ValueError("MTO file without a 'Trade Date <..>' header")
    day = _dmy(m.group(1))
    rows: list[MtoRow] = []
    for cells in csv.reader(io.StringIO(text)):
        if len(cells) < 7 or cells[0].strip() != "20":
            continue
        try:
            rows.append(
                MtoRow(
                    trade_date=day,
                    symbol=cells[2].strip(),
                    series=cells[3].strip(),
                    traded_qty=int(float(cells[4])),
                    deliverable_qty=int(float(cells[5])),
                    deliverable_pct=float(cells[6]),
                )
            )
        except ValueError:
            continue
    return rows


_BAN_DATE = re.compile(r"Trade Date (\d{2}-[A-Za-z]{3}-\d{4})")


def parse_secban(text: str) -> tuple[date, list[str]]:
    """``Securities in Ban For Trade Date 03-JAN-2018:`` then ``n,SYMBOL`` lines (or NIL)."""
    m = _BAN_DATE.search(text)
    if not m:
        raise ValueError("ban-list file without a trade date")
    symbols = []
    for line in text.splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and parts[1]:
            symbols.append(parts[1])
    return _dmy(m.group(1)), symbols


def parse_symbol_changes(text: str) -> list[SymbolChange]:
    out: list[SymbolChange] = []
    for cells in csv.reader(io.StringIO(text)):
        if len(cells) < 4:
            continue
        old, new, eff = cells[-3].strip(), cells[-2].strip(), cells[-1].strip()
        if not (old and new and eff) or old == "OLD SYMBOL":
            continue
        try:
            out.append(SymbolChange(old, new, _dmy(eff)))
        except ValueError:
            continue
    return out


class SymbolMap:
    """Maps a symbol as traded on a day to the stock's latest symbol. A change applies only to
    rows dated before its effective date, so a symbol reused later by another company keeps
    its own identity."""

    def __init__(self, changes: Sequence[SymbolChange]) -> None:
        self._by_old: dict[str, list[SymbolChange]] = {}
        for c in sorted(changes, key=lambda c: c.effective):
            self._by_old.setdefault(c.old, []).append(c)

    def canonical(self, symbol: str, day: date) -> str:
        seen = {symbol}
        cur = symbol
        while True:
            nxt = next((c.new for c in self._by_old.get(cur, ()) if day < c.effective), None)
            if nxt is None or nxt in seen:
                return cur
            seen.add(nxt)
            cur = nxt


def canonical_symbol(symbol: str, day: date, changes: Sequence[SymbolChange]) -> str:
    return SymbolMap(changes).canonical(symbol, day)


def symbol_changes_path(raw_dir: Path) -> Path:
    return raw_dir / "reference" / "symbolchange.csv"


def fetch_symbol_changes(
    raw_dir: Path, *, fetcher: Callable[[str], bytes] = http_get_bytes
) -> Path:
    """NSE's full symbol-change list (one file, all years; checked 14 Nov 2026)."""
    body = fetcher(SYMBOL_CHANGES_URL)
    if b"OLD SYMBOL" not in body[:2000]:
        raise ValueError("symbol-change list: unexpected content")
    path = symbol_changes_path(raw_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    meta = {"url": SYMBOL_CHANGES_URL, "fetched_at": datetime.now().astimezone().isoformat()}
    path.with_name(path.name + ".meta.json").write_text(json.dumps(meta, indent=2), "utf-8")
    return path


def fo_stock_symbols(text: str) -> set[str]:
    """Underlyings with stock futures in an F&O bhavcopy (old or UDiFF layout); fast, no full
    parse."""
    reader = csv.reader(io.StringIO(text))
    header = [h.strip().upper() for h in next(reader, [])]
    try:
        i_inst = (
            header.index("INSTRUMENT") if "INSTRUMENT" in header else header.index("FININSTRMTP")
        )
        i_sym = header.index("SYMBOL") if "SYMBOL" in header else header.index("TCKRSYMB")
    except ValueError:
        return set()
    out: set[str] = set()
    for cells in reader:
        if len(cells) > max(i_inst, i_sym) and cells[i_inst].strip() in STOCK_FUTURES:
            out.add(cells[i_sym].strip())
    return out
