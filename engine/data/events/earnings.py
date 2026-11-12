"""Index-constituent results dates from NSE board-meeting intimations -> the event calendar.

Companies must tell the exchange in advance when the board meets to approve results (SEBI LODR
Reg. 29). NSE's board-meeting list gives the date and when the intimation was disseminated, not
the release time, so each row covers the whole session (09:15 for 375 minutes); results often
come out during or after the meeting.

Rows written here are marked ``[auto:nse-board-meetings]`` in ``notes``. A refresh replaces
future auto rows (a postponed meeting moves), keeps past ones (history for replays) and never
touches hand-entered rows; a hand-entered row for the same symbol and date wins.
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path

from engine.data.constituents.table import ConstituentRow, members_on
from engine.data.fundamentals.nse import BoardMeeting

AUTO_TAG = "[auto:nse-board-meetings]"
SOURCE_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"
CATEGORY = "CONSTITUENT_EARNINGS"
FIELDS = (
    "date",
    "time_ist",
    "category",
    "title",
    "symbol",
    "index_weight_pct",
    "duration_min",
    "pre_blackout_min",
    "post_blackout_min",
    "source_url",
    "verified_on",
    "status",
    "notes",
)


def latest_results_meetings(
    meetings: Iterable[BoardMeeting], symbols: Iterable[str], *, on: date
) -> list[BoardMeeting]:
    """Per symbol, the most recently intimated results meeting dated ``on`` or later."""
    wanted = set(symbols)
    best: dict[str, BoardMeeting] = {}
    for m in meetings:
        if m.symbol not in wanted or not m.is_results or m.meeting_date < on:
            continue
        cur = best.get(m.symbol)
        if cur is None or (m.known_at, m.meeting_date) > (cur.known_at, cur.meeting_date):
            best[m.symbol] = m
    return sorted(best.values(), key=lambda m: (m.meeting_date, m.symbol))


def earnings_rows(
    meetings: Iterable[BoardMeeting],
    constituents: Sequence[ConstituentRow],
    *,
    index: str,
    on: date,
) -> list[dict[str, str]]:
    members = {r.symbol: r for r in members_on(constituents, index, on)}
    rows: list[dict[str, str]] = []
    for m in latest_results_meetings(meetings, members, on=on):
        c = members[m.symbol]
        if c.weight_pct is None:
            weight, wnote = "", f"{index} weight not loaded (counts 0 in the 24 h earnings weight)"
        else:
            weight = f"{c.weight_pct:g}"
            wnote = f"{index} weight {c.weight_pct:g}%" + (
                ""
                if c.weight_verified
                else " derived from free-float market cap, not yet checked against the NSE "
                "factsheet"
                if c.weight_status == "derived"
                else " UNVERIFIED (no source for the weight)"
            )
        rows.append(
            {
                "date": m.meeting_date.isoformat(),
                "time_ist": "09:15",
                "category": CATEGORY,
                "title": f"{m.symbol} results (board meeting)",
                "symbol": m.symbol,
                "index_weight_pct": weight,
                "duration_min": "375",
                "pre_blackout_min": "15",
                "post_blackout_min": "30",
                "source_url": SOURCE_PAGE,
                "verified_on": on.isoformat(),
                "status": "primary",
                "notes": (
                    f"{AUTO_TAG} Intimated to NSE {m.known_at:%d %b %Y %H:%M} IST: "
                    f"{m.purpose}. Release time not given; window covers the session. {wnote}."
                ),
            }
        )
    return rows


def _render(row: dict[str, str], fieldnames: Sequence[str]) -> str:
    """One CSV line in the file's hand-written style: ``notes`` always quoted, other fields
    quoted only when they need it."""
    out = []
    for k in fieldnames:
        v = row.get(k, "")
        if k == "notes" or any(c in v for c in ',"\n'):
            v = '"' + v.replace('"', '""') + '"'
        out.append(v)
    return ",".join(out)


def merge_calendar(path: Path, new_rows: Sequence[dict[str, str]], *, today: date) -> int:
    """Write ``new_rows`` into the calendar CSV; returns the number of auto rows written.
    Existing lines are kept byte for byte; only auto rows are added or removed."""
    lines: list[str] = []
    fieldnames = list(FIELDS)
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    if lines:
        fieldnames = next(csv.reader([lines[0]]))
    header = lines[0] if lines else ",".join(fieldnames)
    existing: list[tuple[dict[str, str], str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        values = next(csv.reader(io.StringIO(line)))
        existing.append((dict(zip(fieldnames, values, strict=False)), line))
    kept = [
        (r, line)
        for r, line in existing
        if not (AUTO_TAG in r.get("notes", "") and r.get("date", "") >= today.isoformat())
    ]
    manual = {
        (r.get("symbol", ""), r.get("date", ""))
        for r, _ in kept
        if r.get("category") == CATEGORY and AUTO_TAG not in r.get("notes", "")
    }
    past_auto = {
        (r.get("symbol", ""), r.get("date", "")) for r, _ in kept if AUTO_TAG in r.get("notes", "")
    }
    added = [
        (r, _render(r, fieldnames))
        for r in new_rows
        if (r["symbol"], r["date"]) not in manual and (r["symbol"], r["date"]) not in past_auto
    ]
    rows = sorted(kept + added, key=lambda x: (x[0].get("date", ""), x[0].get("time_ist", "")))
    text = "\n".join([header, *(line for _, line in rows)]) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return len(added)
