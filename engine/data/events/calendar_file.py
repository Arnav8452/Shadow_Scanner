"""Scheduled market events from ``config/events/macro_calendar.csv``.

Each row carries its source and a status (``primary`` official page, ``secondary``, or
``UNVERIFIED``). Unverified events are still loaded: for risk, an extra caution window costs
little, while a missed event can cost a lot. They are shown as unverified on the cockpit.

Constituent results go in the same file with ``category = CONSTITUENT_EARNINGS``, a ``symbol``
and its ``index_weight_pct``, once the company announces the board-meeting date.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from engine.core.rules import IST
from engine.data.events.event_calendar import (
    EventBlackoutStatus,
    EventCategory,
    ScheduledEvent,
    evaluate_event_risk,
)

STATUSES = ("primary", "secondary", "UNVERIFIED")


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    event: ScheduledEvent
    status: str
    source_url: str
    notes: str


def load_calendar(path: Path) -> list[CalendarEvent]:
    if not path.exists():
        return []
    out: list[CalendarEvent] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for n, r in enumerate(csv.DictReader(fh), start=2):
            status = (r.get("status") or "").strip()
            if status not in STATUSES:
                raise ValueError(f"{path.name}:{n}: status must be one of {STATUSES}")
            src = (r.get("source_url") or "").strip()
            if status != "UNVERIFIED" and not src:
                raise ValueError(f"{path.name}:{n}: source_url required unless UNVERIFIED")
            d = date.fromisoformat(r["date"].strip())
            hh, mm = (int(x) for x in r["time_ist"].strip().split(":"))
            start = datetime.combine(d, time(hh, mm), IST)
            dur = int(r.get("duration_min") or 15)
            out.append(
                CalendarEvent(
                    event=ScheduledEvent(
                        title=r["title"].strip(),
                        category=EventCategory(r["category"].strip()),
                        start_ts=start,
                        end_ts=start + timedelta(minutes=dur),
                        symbol=(r.get("symbol") or "").strip() or None,
                        index_weight_pct=float(r.get("index_weight_pct") or 0.0),
                        pre_blackout_min=int(r.get("pre_blackout_min") or 15),
                        post_blackout_min=int(r.get("post_blackout_min") or 30),
                    ),
                    status=status,
                    source_url=src,
                    notes=(r.get("notes") or "").strip(),
                )
            )
    out.sort(key=lambda c: c.event.start_ts)
    return out


def blackout_status(events: Sequence[CalendarEvent], now: datetime) -> EventBlackoutStatus:
    return evaluate_event_risk([c.event for c in events], now)


def upcoming(events: Sequence[CalendarEvent], now: datetime, n: int = 4) -> list[dict[str, Any]]:
    """The next ``n`` events (and any still inside their window) for display."""
    rows: list[dict[str, Any]] = []
    for c in events:
        ev = c.event
        window_end = ev.end_ts + timedelta(minutes=ev.post_blackout_min)
        if window_end < now:
            continue
        hours = (ev.start_ts - now).total_seconds() / 3600.0
        rows.append(
            {
                "title": ev.title,
                "category": ev.category.value,
                "at": ev.start_ts.strftime("%d %b %H:%M"),
                "in_hours": round(hours, 1),
                "active": ev.start_ts - timedelta(minutes=ev.pre_blackout_min) <= now <= window_end,
                "status": c.status,
                "source": c.source_url,
            }
        )
        if len(rows) >= n:
            break
    return rows
