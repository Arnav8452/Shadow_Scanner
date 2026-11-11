"""NSE end-of-day downloads (M3): bhavcopies, delivery, participant OI, index closes.

Files are stored as downloaded (bitemporal: ``fetched_at`` in a ``.meta.json`` beside each file)
under ``data/raw/eod/<dataset>/<YYYY>/<YYYY-MM-DD>.<ext>``. Parsing happens on read
(``engine.data.eod.bhavcopy`` / ``participant_oi``), so a parser fix never needs a re-download.

URLs checked on 11 Nov 2026 (all public, no login; a 404 means a holiday or not yet published):
NSE publishes these around 18:00–19:30 IST; parsers stamp them known at 19:30 on the trade date,
so they can only inform the next session.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time as _time
import urllib.error
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from engine.core.net import http_get_bytes

log = logging.getLogger(__name__)

NSE_EOD_URLS: dict[str, str] = {
    "fo": "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip",
    "cm": "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip",
    "delivery": "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{dmy}.csv",
    "participant_oi": "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{dmy}.csv",
    "indices": "https://nsearchives.nseindia.com/content/indices/ind_close_all_{dmy}.csv",
}
# A text marker each genuine file contains (NSE sometimes answers with an HTML error page).
_MARKERS: dict[str, bytes] = {
    "fo": b"TckrSymb",
    "cm": b"TckrSymb",
    "delivery": b"DELIV_PER",
    "participant_oi": b"Client Type",
    "indices": b"Index Name",
}
DATASETS = tuple(NSE_EOD_URLS)


@dataclass(frozen=True)
class EodFetch:
    dataset: str
    day: date
    status: str  # ok | cached | missing | error
    path: Path | None = None
    detail: str = ""


def eod_url(dataset: str, day: date) -> str:
    return NSE_EOD_URLS[dataset].format(ymd=day.strftime("%Y%m%d"), dmy=day.strftime("%d%m%Y"))


def eod_path(raw_dir: Path, dataset: str, day: date) -> Path:
    ext = ".csv.zip" if NSE_EOD_URLS[dataset].endswith(".zip") else ".csv"
    return raw_dir / "eod" / dataset / f"{day.year}" / f"{day.isoformat()}{ext}"


def read_eod_text(path: Path) -> str:
    """File contents as text (unzips the single CSV inside bhavcopy archives)."""
    data = path.read_bytes()
    if path.name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            data = zf.read(zf.namelist()[0])
    return data.decode("utf-8", errors="replace")


def _validate(dataset: str, body: bytes) -> bool:
    if NSE_EOD_URLS[dataset].endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                names = zf.namelist()
                return bool(names) and _MARKERS[dataset] in zf.read(names[0])[:4096]
        except zipfile.BadZipFile:
            return False
    return _MARKERS[dataset] in body[:4096]


def fetch_eod(
    dataset: str,
    day: date,
    *,
    raw_dir: Path,
    now: datetime,
    fetcher: Callable[[str], bytes] = http_get_bytes,
    refresh: bool = False,
) -> EodFetch:
    if dataset not in NSE_EOD_URLS:
        raise ValueError(f"unknown EOD dataset {dataset!r}; use {DATASETS}")
    path = eod_path(raw_dir, dataset, day)
    if path.exists() and not refresh:
        return EodFetch(dataset, day, "cached", path)
    url = eod_url(dataset, day)
    try:
        body = fetcher(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return EodFetch(dataset, day, "missing", detail="404 (holiday or not published)")
        return EodFetch(dataset, day, "error", detail=f"HTTP {exc.code}")
    except OSError as exc:
        return EodFetch(dataset, day, "error", detail=type(exc).__name__)
    if not _validate(dataset, body):
        return EodFetch(dataset, day, "missing", detail="not a valid file (HTML or empty)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    meta = {
        "url": url,
        "fetched_at": now.isoformat(),
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
    }
    path.with_name(path.name + ".meta.json").write_text(json.dumps(meta, indent=2), "utf-8")
    return EodFetch(dataset, day, "ok", path)


def weekdays(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def backfill(
    datasets: Iterable[str],
    start: date,
    end: date,
    *,
    raw_dir: Path,
    now: Callable[[], datetime],
    fetcher: Callable[[str], bytes] = http_get_bytes,
    pace_s: float = 0.4,
    sleep: Callable[[float], None] = _time.sleep,
) -> list[EodFetch]:
    """Fetch every weekday in [start, end] (holidays come back as ``missing``). Paced politely;
    already-downloaded files are skipped."""
    out: list[EodFetch] = []
    for day in weekdays(start, end):
        for ds in datasets:
            res = fetch_eod(ds, day, raw_dir=raw_dir, now=now(), fetcher=fetcher)
            out.append(res)
            if res.status != "cached":
                sleep(pace_s)
    return out
