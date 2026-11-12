"""Evening flow and reference files from NSE (SPEC L5 positioning, L3 reference data).

- ``participant_vol``: participant-wise F&O volumes (same columns as participant OI; parse with
  :func:`engine.data.eod.participant_oi.parse_participant_oi_csv`).
- ``fo_ban``: securities in the F&O ban period for the *next* trade date (no new positions).
- ``bulk`` / ``block``: the day's bulk and block deals (public disclosures by NSE).

NSE keeps only the current ``fo_ban``, ``bulk`` and ``block`` files at fixed URLs, so they are
downloaded every evening and filed under the date written inside the file. All are known after
publication in the evening (``known_at`` 19:30 IST on the trade date; the ban list applies to
the next session).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from engine.core.net import http_get_bytes
from engine.data.eod.download import EodFetch

CURRENT_EOD_URLS: dict[str, str] = {
    "fo_ban": "https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
    "bulk": "https://nsearchives.nseindia.com/content/equities/bulk.csv",
    "block": "https://nsearchives.nseindia.com/content/equities/block.csv",
}
_MARKERS = {"fo_ban": "Securities in Ban", "bulk": "Symbol", "block": "Symbol"}
_BAN_DATE = re.compile(r"Trade Date\s+(\d{2}-[A-Za-z]{3}-\d{4})")
_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1
)}  # fmt: skip


def _nse_date(text: str) -> date:
    d, mon, y = text.strip().split("-")
    return date(int(y), _MONTHS[mon.upper()[:3]], int(d))


@dataclass(frozen=True, slots=True)
class Deal:
    trade_date: date
    kind: str  # bulk | block
    symbol: str
    name: str
    client: str
    side: str  # BUY | SELL
    qty: int
    price: float

    @property
    def value_cr(self) -> float:
        return self.qty * self.price / 1e7


def parse_ban_list(text: str) -> tuple[date | None, list[str]]:
    """(trade date the ban applies to, symbols)."""
    m = _BAN_DATE.search(text)
    day = _nse_date(m.group(1)) if m else None
    syms = []
    for line in text.splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[1]:
            syms.append(parts[1])
    return day, syms


def parse_deals(text: str, kind: str) -> list[Deal]:
    out: list[Deal] = []
    for r in csv.DictReader(io.StringIO(text.strip())):
        norm = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        try:
            out.append(
                Deal(
                    trade_date=_nse_date(norm["date"]),
                    kind=kind,
                    symbol=norm["symbol"],
                    name=norm.get("security name", ""),
                    client=norm.get("client name", ""),
                    side=norm.get("buy/sell", "").upper(),
                    qty=int(float(norm.get("quantity traded", "0").replace(",", "") or 0)),
                    price=float(
                        norm.get("trade price / wght. avg. price", "0").replace(",", "") or 0
                    ),
                )
            )
        except (KeyError, ValueError):
            continue
    return out


def file_date(dataset: str, text: str) -> date | None:
    if dataset == "fo_ban":
        return parse_ban_list(text)[0]
    deals = parse_deals(text, dataset)
    return deals[0].trade_date if deals else None


def current_path(raw_dir: Path, dataset: str, day: date) -> Path:
    return raw_dir / "eod" / dataset / f"{day.year}" / f"{day.isoformat()}.csv"


def fetch_current_eod(
    dataset: str,
    *,
    raw_dir: Path,
    now: datetime,
    fetcher: Callable[[str], bytes] = http_get_bytes,
) -> EodFetch:
    """Download a current-only NSE file and file it under the date written inside it."""
    url = CURRENT_EOD_URLS[dataset]
    try:
        body = fetcher(url)
    except urllib.error.HTTPError as exc:
        return EodFetch(dataset, now.date(), "error", detail=f"HTTP {exc.code}")
    except OSError as exc:
        return EodFetch(dataset, now.date(), "error", detail=type(exc).__name__)
    text = body.decode("utf-8", errors="replace")
    if _MARKERS[dataset] not in text[:400]:
        return EodFetch(dataset, now.date(), "missing", detail="not a valid file")
    day = file_date(dataset, text)
    if day is None:
        return EodFetch(dataset, now.date(), "missing", detail="no rows (nothing listed today)")
    path = current_path(raw_dir, dataset, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    meta = {
        "url": url,
        "fetched_at": now.isoformat(),
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "file_date": day.isoformat(),
    }
    path.with_name(path.name + ".meta.json").write_text(json.dumps(meta, indent=2), "utf-8")
    return EodFetch(dataset, day, "ok", path)


def latest_file(raw_dir: Path, dataset: str, on_or_before: date | None = None) -> Path | None:
    base = raw_dir / "eod" / dataset
    files = sorted(
        p
        for p in base.glob("*/*.csv")
        if on_or_before is None or p.stem <= on_or_before.isoformat()
    )
    return files[-1] if files else None
