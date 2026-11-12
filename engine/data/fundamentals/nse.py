"""NSE corporate-filing lists and XBRL downloads (results, shareholding, board meetings).

Endpoints checked on 12 Nov 2026 with a plain HTTPS GET, generic User-Agent, no cookies (all
answered 200 with JSON; XBRL files are on the public ``nsearchives`` host):

- results: ``/api/integrated-filing-results?index=equities&symbol=X&type=Integrated Filing-
  Financials`` — SEBI Integrated Filing (Financials), quarters ended March 2025 onward. The older
  ``/api/corporates-financial-results`` list stops at the December 2024 quarter (filed Jan 2025).
- shareholding: ``/api/corporate-share-holdings-master?index=equities&symbol=X``.
- board meetings: ``/api/corporate-board-meetings?index=equities&from_date=DD-MM-YYYY&to_date=``
  (or ``&symbol=X``).

These ``www.nseindia.com/api`` endpoints are the ones NSE's own filing pages call; NSE publishes
no documentation or terms for them, so they may change or start requiring a browser session.
Every function here fails soft (a status per symbol) so the evening job keeps running.

Point in time: a filing is known from NSE's dissemination time (``broadcast_Date``,
``broadcastDate``, ``bm_timestamp``, IST); a revised filing from its revision time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time as _time
import urllib.error
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from engine.core.net import http_get_bytes
from engine.core.rules import IST

log = logging.getLogger(__name__)

NSE_API = "https://www.nseindia.com/api"
ARCHIVE_PREFIX = "https://nsearchives.nseindia.com/corporate/"
RESULTS_TYPE = "Integrated Filing- Financials"


def results_list_url(symbol: str) -> str:
    q = urllib.parse.urlencode({"index": "equities", "symbol": symbol, "type": RESULTS_TYPE})
    return f"{NSE_API}/integrated-filing-results?{q}"


def shareholding_list_url(symbol: str) -> str:
    q = urllib.parse.urlencode({"index": "equities", "symbol": symbol})
    return f"{NSE_API}/corporate-share-holdings-master?{q}"


def board_meetings_url(start: date, end: date) -> str:
    q = urllib.parse.urlencode(
        {
            "index": "equities",
            "from_date": start.strftime("%d-%m-%Y"),
            "to_date": end.strftime("%d-%m-%Y"),
        }
    )
    return f"{NSE_API}/corporate-board-meetings?{q}"


def nse_timestamp(raw: str | None) -> datetime | None:
    """NSE list timestamps ('30-Oct-2026 21:32:55', '21-OCT-2026 16:25:15') as IST."""
    if not raw:
        return None
    s = raw.strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(s.title(), fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _nse_day(raw: str | None) -> date | None:
    ts = nse_timestamp(raw)
    return ts.date() if ts else None


@dataclass(frozen=True, slots=True)
class FilingRef:
    kind: str  # results | shareholding
    symbol: str
    company: str
    period_end: date
    consolidated: bool | None  # None for shareholding
    known_at: datetime
    xbrl_url: str
    filing_id: str
    revision: str  # Original | Revised


def _archive_url(url: Any) -> str | None:
    """Only follow links to NSE's own archive host."""
    if isinstance(url, str) and url.startswith(ARCHIVE_PREFIX) and url.endswith(".xml"):
        return url
    return None


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("data", [])
    return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []


def parse_results_list(text: str) -> list[FilingRef]:
    out: list[FilingRef] = []
    for r in _rows(json.loads(text)):
        url = _archive_url(r.get("xbrl"))
        known = nse_timestamp(r.get("broadcast_Date")) or nse_timestamp(r.get("creation_Date"))
        revised = nse_timestamp(r.get("revised_Date"))
        period_end = _nse_day(r.get("qe_Date"))
        if not (url and known and period_end and r.get("symbol")):
            continue
        out.append(
            FilingRef(
                kind="results",
                symbol=str(r["symbol"]),
                company=str(r.get("cmName") or r.get("smName") or ""),
                period_end=period_end,
                consolidated=str(r.get("consolidated", "")).lower().startswith("consolidated"),
                known_at=max(known, revised) if revised else known,
                xbrl_url=url,
                filing_id=str(r.get("seq_Id") or url.rsplit("/", 1)[-1]),
                revision=str(r.get("type_Sub") or ("Revised" if revised else "Original")),
            )
        )
    return out


def parse_shareholding_list(text: str, symbol: str = "") -> list[FilingRef]:
    out: list[FilingRef] = []
    for r in _rows(json.loads(text)):
        url = _archive_url(r.get("xbrl"))
        known = nse_timestamp(r.get("broadcastDate")) or nse_timestamp(r.get("submissionDate"))
        revised = nse_timestamp(r.get("revisionDate")) or nse_timestamp(r.get("revisedDate"))
        period_end = _nse_day(r.get("date"))
        sym = str(r.get("symbol") or symbol)
        if not (url and known and period_end and sym):
            continue
        out.append(
            FilingRef(
                kind="shareholding",
                symbol=sym,
                company=str(r.get("name") or ""),
                period_end=period_end,
                consolidated=None,
                known_at=max(known, revised) if revised else known,
                xbrl_url=url,
                filing_id=str(r.get("recordId") or url.rsplit("/", 1)[-1]),
                revision="Revised" if str(r.get("revisedData", "N")).upper() == "Y" else "Original",
            )
        )
    return out


def pick_filings(refs: Iterable[FilingRef], n_periods: int) -> list[FilingRef]:
    """Latest ``n_periods`` periods; per period the consolidated filing (standalone when no
    consolidated one exists), and of those the latest known (a revision supersedes)."""
    best: dict[date, FilingRef] = {}
    for f in refs:
        cur = best.get(f.period_end)
        rank = (f.consolidated is not False, f.known_at)
        if cur is None or rank > (cur.consolidated is not False, cur.known_at):
            best[f.period_end] = f
    return [best[d] for d in sorted(best, reverse=True)[:n_periods]]


# --------------------------------------------------------------------------- board meetings


@dataclass(frozen=True, slots=True)
class BoardMeeting:
    symbol: str
    company: str
    isin: str
    meeting_date: date
    purpose: str
    description: str
    known_at: datetime  # when NSE disseminated the intimation

    @property
    def is_results(self) -> bool:
        text = f"{self.purpose} {self.description}".lower()
        return "financial result" in text


def parse_board_meetings(text: str) -> list[BoardMeeting]:
    out: list[BoardMeeting] = []
    for r in _rows(json.loads(text)):
        day = _nse_day(r.get("bm_date"))
        known = nse_timestamp(r.get("bm_timestamp")) or nse_timestamp(r.get("sysTime"))
        if not (day and known and r.get("bm_symbol")):
            continue
        out.append(
            BoardMeeting(
                symbol=str(r["bm_symbol"]),
                company=str(r.get("sm_name") or ""),
                isin=str(r.get("sm_isin") or ""),
                meeting_date=day,
                purpose=str(r.get("bm_purpose") or ""),
                description=" ".join(str(r.get("bm_desc") or "").split()),
                known_at=known,
            )
        )
    return out


# --------------------------------------------------------------------------- downloads


@dataclass(frozen=True)
class Download:
    url: str
    status: str  # ok | cached | error
    path: Path | None = None
    detail: str = ""


def _write(path: Path, body: bytes, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    meta = {**meta, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
    path.with_name(path.name + ".meta.json").write_text(json.dumps(meta, indent=2), "utf-8")


def fetch_list(
    url: str,
    path: Path,
    *,
    now: datetime,
    fetcher: Callable[[str], bytes] = http_get_bytes,
) -> Download:
    """Download a JSON filing list and keep it (bitemporal: one file per fetch day)."""
    try:
        body = fetcher(url)
    except urllib.error.HTTPError as exc:
        return Download(url, "error", detail=f"HTTP {exc.code}")
    except OSError as exc:
        return Download(url, "error", detail=type(exc).__name__)
    head = body[:64].lstrip()
    if not head.startswith((b"[", b"{")):
        return Download(url, "error", detail="not JSON (blocked or HTML page)")
    _write(path, body, {"url": url, "fetched_at": now.isoformat()})
    return Download(url, "ok", path)


def xbrl_path(raw_dir: Path, ref: FilingRef) -> Path:
    name = ref.xbrl_url.rsplit("/", 1)[-1]
    return raw_dir / "fundamentals" / "xbrl" / ref.kind / ref.symbol / name


def fetch_xbrl(
    ref: FilingRef,
    *,
    raw_dir: Path,
    now: datetime,
    fetcher: Callable[[str], bytes] = http_get_bytes,
) -> Download:
    path = xbrl_path(raw_dir, ref)
    if path.exists():
        return Download(ref.xbrl_url, "cached", path)
    try:
        body = fetcher(ref.xbrl_url)
    except urllib.error.HTTPError as exc:
        return Download(ref.xbrl_url, "error", detail=f"HTTP {exc.code}")
    except OSError as exc:
        return Download(ref.xbrl_url, "error", detail=type(exc).__name__)
    if b"xbrl" not in body[:2048]:
        return Download(ref.xbrl_url, "error", detail="not an XBRL file")
    _write(
        path,
        body,
        {
            "url": ref.xbrl_url,
            "fetched_at": now.isoformat(),
            "known_at": ref.known_at.isoformat(),
            "filing_id": ref.filing_id,
            "revision": ref.revision,
        },
    )
    return Download(ref.xbrl_url, "ok", path)


def polite_sleep(seconds: float, sleep: Callable[[float], None] = _time.sleep) -> None:
    if seconds > 0:
        sleep(seconds)
