"""One saved copy a day of the cockpit's live analytics, so they build history.

The live analytics (per-index strip: spot, ATM IV, skew, forward basis, gamma regime, PCR,
walls; the chain summary; realised correlation, forward dispersion and weighted breadth) exist
only in memory. Research needs them as a daily series. Once per trading day, at the first
analytics update at or after ``at`` (15:30 by default, the cash close), the current
``LiveAnalytics.view()`` is appended to ``analytics_daily.jsonl`` in the journal folder. A
restart the same day doesn't write a second copy. Replays use their own journal folder, so
they never touch the live file.

Point in time: a snapshot stamped ``t`` contains only what the live engine knew at ``t``.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILE_NAME = "analytics_daily.jsonl"


def last_snapshot_day(path: Path) -> date | None:
    """The day of the last saved snapshot, or None."""
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        return date.fromisoformat(json.loads(lines[-1])["day"]) if lines else None
    except (OSError, ValueError, KeyError):
        return None


def snapshot_due(now: datetime, *, at: time, last_day: date | None) -> bool:
    return now.weekday() < 5 and now.time() >= at and last_day != now.date()


def write_snapshot(path: Path, now: datetime, view: dict[str, Any]) -> dict[str, Any]:
    rec = {"day": now.date().isoformat(), "t": now.isoformat(), "analytics": view}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
    return rec


class DailySnapshot:
    """Holds the last-written day so the check each analytics update is cheap."""

    def __init__(self, journal_dir: Path, at: time) -> None:
        self.path = journal_dir / FILE_NAME
        self.at = at
        self.last_day = last_snapshot_day(self.path)

    def maybe_write(self, now: datetime, view: dict[str, Any]) -> bool:
        if not snapshot_due(now, at=self.at, last_day=self.last_day):
            return False
        if not view:
            return False  # nothing computed yet (feed not up): try again next update
        try:
            write_snapshot(self.path, now, view)
        except OSError as exc:  # a full disk must not stop the engine
            log.warning("analytics snapshot not saved: %s", exc)
            return False
        self.last_day = now.date()
        return True


__all__ = ["FILE_NAME", "DailySnapshot", "last_snapshot_day", "snapshot_due", "write_snapshot"]
