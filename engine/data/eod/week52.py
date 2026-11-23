"""NSE's daily 52-week high/low file (dataset ``w52`` in ``engine.data.eod.download``).

The file for session ``D`` is published before ``D`` opens and holds, per security, the 52-week
high and low through the previous session, adjusted for bonus, splits and rights, with the date
each was set. So it is known at ``D``'s pre-open, and ``high_date == previous session`` means
the stock was on the previous day's 52-week-high list.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Week52:
    symbol: str
    series: str
    high: float
    high_date: date | None
    low: float
    low_date: date | None


def _num(raw: str) -> float:
    try:
        return float(raw.strip().replace(",", ""))
    except ValueError:
        return 0.0  # "-" for a security with no trades in the window


def _day(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip().title(), "%d-%b-%Y").date()
    except ValueError:
        return None


def effective_date(text: str) -> date | None:
    """The session the file applies to, from its ``"Effective for 20-Nov-2026"`` line."""
    for line in text.splitlines()[:5]:
        clean = line.strip().strip('"')
        if clean.lower().startswith("effective for"):
            return _day(clean[len("effective for") :])
    return None


def parse_week52(text: str, *, series: str = "EQ") -> dict[str, Week52]:
    """``symbol -> Week52`` for one series (default EQ); securities without a high are left out."""
    lines = text.splitlines()
    start = next((i for i, x in enumerate(lines) if x.lstrip('"').startswith("SYMBOL")), None)
    if start is None:
        return {}
    out: dict[str, Week52] = {}
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        # older files (2020-22) name the columns "Adjusted 52_Week_High" / "Adjusted 52_Week_Low"
        norm = {k.strip().replace(" ", "_"): (v or "").strip() for k, v in row.items() if k}
        if norm.get("SERIES") != series:
            continue
        high = _num(norm.get("Adjusted_52_Week_High", ""))
        if high <= 0:
            continue
        sym = norm["SYMBOL"]
        out[sym] = Week52(
            symbol=sym,
            series=series,
            high=high,
            high_date=_day(norm.get("52_Week_High_Date", "")),
            low=_num(norm.get("Adjusted_52_Week_Low", "")),
            low_date=_day(norm.get("52_Week_Low_DT", "")),
        )
    return out
