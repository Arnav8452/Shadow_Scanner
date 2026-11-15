"""Daily saved copy of the cockpit's live analytics."""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

from engine.cockpit.daily_snapshot import DailySnapshot, last_snapshot_day, snapshot_due
from engine.core.rules import IST

AT = time(15, 30)
VIEW = {"NIFTY": {"spot": 25299.35, "atm_iv": 0.153, "chain": {"pcr": 0.76}}}


def _t(day: int, hh: int, mm: int) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


def test_due_once_per_weekday_after_the_time() -> None:
    assert not snapshot_due(_t(16, 15, 29), at=AT, last_day=None)
    assert snapshot_due(_t(16, 15, 30), at=AT, last_day=None)
    assert not snapshot_due(_t(16, 15, 45), at=AT, last_day=date(2026, 11, 16))
    assert not snapshot_due(_t(15, 15, 45), at=AT, last_day=None)  # Sunday


def test_writes_once_a_day_and_survives_restart(tmp_path: Path) -> None:
    s = DailySnapshot(tmp_path, AT)
    assert not s.maybe_write(_t(16, 15, 0), VIEW)  # too early
    assert not s.maybe_write(_t(16, 15, 31), {})  # nothing computed yet
    assert s.maybe_write(_t(16, 15, 31), VIEW)
    assert not s.maybe_write(_t(16, 15, 32), VIEW)  # once per day
    again = DailySnapshot(tmp_path, AT)  # engine restarted the same day
    assert again.last_day == date(2026, 11, 16)
    assert not again.maybe_write(_t(16, 15, 40), VIEW)
    assert again.maybe_write(_t(17, 15, 30), VIEW)
    lines = (tmp_path / "analytics_daily.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    assert [json.loads(x)["day"] for x in lines] == ["2026-11-16", "2026-11-17"]
    assert rec["analytics"]["NIFTY"]["chain"]["pcr"] == 0.76 and rec["t"].startswith(
        "2026-11-16T15:31"
    )
    assert last_snapshot_day(tmp_path / "missing.jsonl") is None
