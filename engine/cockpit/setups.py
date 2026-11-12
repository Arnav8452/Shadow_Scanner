"""Setups: order-flow readings turned into ideas a person can act on (and wait for).

A *reading* is one confirmed scanner episode (score 75+, held 5 s) on any contract of an index,
expressed as a view on the index (a bid-heavy put reads DOWN). Readings come and go in seconds,
so nothing trades on a reading by itself. A *setup* collects the readings for one index and one
direction and goes through these states (all thresholds in ``cockpit.setups``, HYPOTHESIS):

FORMING    first reading seen; waiting for the index to follow through.
CONFIRMED  the index moved at least ``confirm_move_pct`` the setup's way from where the first
           reading appeared, and stayed past that line for ``hold_s`` seconds, within
           ``window_min`` minutes. This is what the directional strategy enters on and what the
           condor treats as a trending tape.
FAILED     a reading the other way arrived first, the index moved ``fail_move_pct`` against it,
           or the window ran out without follow-through.
ENDED      a confirmed setup gave back the whole move (index back through the starting level),
           an opposite setup was confirmed, or it is ``valid_min`` minutes old.

Every state change is written to ``setups.jsonl`` so the day's list survives a restart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from engine.core.rules import IST

FORMING, CONFIRMED, FAILED, ENDED = "FORMING", "CONFIRMED", "FAILED", "ENDED"


@dataclass(frozen=True)
class SetupConfig:
    confirm_move_pct: float = 0.05  # Nifty ~13 pts, Sensex ~40 pts
    hold_s: float = 60.0
    window_min: float = 10.0
    fail_move_pct: float = 0.08
    valid_min: float = 60.0


@dataclass
class Setup:
    id: str
    underlying: str
    bullish: bool
    first_at: datetime
    spot0: float
    state: str = FORMING
    readings: list[dict[str, Any]] = field(default_factory=list)
    best_score: float = 0.0
    beyond_since: datetime | None = None
    confirmed_at: datetime | None = None
    confirm_spot: float = 0.0
    ended_at: datetime | None = None
    why: str = ""
    last_spot: float = 0.0

    @property
    def direction(self) -> str:
        return "UP" if self.bullish else "DOWN"

    def move_pct(self, spot: float) -> float:
        if self.spot0 <= 0 or spot <= 0:
            return 0.0
        return (spot - self.spot0) / self.spot0 * 100.0 * (1 if self.bullish else -1)

    def row(self) -> dict[str, Any]:
        def hm(t: datetime | None) -> str | None:
            return t.astimezone(IST).strftime("%H:%M:%S") if t else None

        return {
            "id": self.id,
            "underlying": self.underlying,
            "direction": self.direction,
            "state": self.state,
            "first_at": self.first_at.isoformat(),
            "first": hm(self.first_at),
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "confirmed": hm(self.confirmed_at),
            "ended": hm(self.ended_at),
            "spot0": round(self.spot0, 2),
            "confirm_spot": round(self.confirm_spot, 2) if self.confirm_spot else None,
            "spot": round(self.last_spot, 2) if self.last_spot else None,
            "move_pct": round(self.move_pct(self.last_spot), 3) if self.last_spot else None,
            "move_pts": round((self.last_spot - self.spot0) * (1 if self.bullish else -1), 2)
            if self.last_spot
            else None,
            "best_score": round(self.best_score, 1),
            "n_readings": len(self.readings),
            "readings": self.readings[-6:],
            "why": self.why,
        }


class SetupTracker:
    def __init__(
        self, cfg: SetupConfig, *, journal: Path | None, day: datetime | None = None
    ) -> None:
        self.cfg = cfg
        self.journal = journal
        self.setups: list[Setup] = []
        self._seq = 0
        self.history: list[dict[str, Any]] = self._load(day) if day is not None else []

    def _load(self, day: datetime) -> list[dict[str, Any]]:
        """Finished setups of the day from the journal (latest row per id)."""
        if self.journal is None or not self.journal.exists():
            return []
        iso = day.astimezone(IST).date().isoformat()
        latest: dict[str, dict[str, Any]] = {}
        for line in self.journal.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if str(rec.get("first_at", ""))[:10] == iso and rec.get("state") in (FAILED, ENDED):
                latest[str(rec.get("id"))] = rec
        return list(latest.values())

    def _write(self, s: Setup) -> None:
        if self.journal is None:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(s.row(), default=str) + "\n")

    def _open(self, und: str, bullish: bool) -> Setup | None:
        for s in self.setups:
            if s.underlying == und and s.bullish == bullish and s.state in (FORMING, CONFIRMED):
                return s
        return None

    def _end(self, s: Setup, state: str, now: datetime, why: str) -> None:
        s.state, s.ended_at, s.why = state, now, why
        self._write(s)
        self.history.append(s.row())

    # ------------------------------------------------------------------ inputs

    def add_reading(
        self,
        *,
        underlying: str,
        bullish: bool,
        now: datetime,
        spot: float,
        label: str,
        score: float,
        wave: str,
    ) -> Setup | None:
        if spot <= 0:
            return None
        # A reading the other way kills a setup that is still waiting for follow-through.
        other = self._open(underlying, not bullish)
        if other is not None and other.state == FORMING:
            self._end(other, FAILED, now, "opposite reading before follow-through")
        s = self._open(underlying, bullish)
        if s is None:
            self._seq += 1
            s = Setup(
                id=f"{underlying}-{'UP' if bullish else 'DN'}-"
                f"{now.astimezone(IST):%H%M%S}-{self._seq}",
                underlying=underlying,
                bullish=bullish,
                first_at=now,
                spot0=spot,
                last_spot=spot,
            )
            self.setups.append(s)
            self._write(s)
        s.readings.append(
            {
                "at": now.astimezone(IST).strftime("%H:%M:%S"),
                "label": label,
                "score": round(score, 1),
                "wave": wave,
            }
        )
        s.best_score = max(s.best_score, score)
        return s

    def update(self, now: datetime, spots: Mapping[str, float]) -> list[Setup]:
        """Advance every open setup; returns the ones confirmed on this call."""
        confirmed: list[Setup] = []
        c = self.cfg
        for s in list(self.setups):
            if s.state not in (FORMING, CONFIRMED):
                continue
            spot = spots.get(s.underlying, 0.0)
            if spot <= 0:
                continue
            s.last_spot = spot
            mv = s.move_pct(spot)
            if s.state == FORMING:
                if mv <= -c.fail_move_pct:
                    self._end(s, FAILED, now, f"index moved {mv:+.2f}% against it")
                elif mv >= c.confirm_move_pct:
                    s.beyond_since = s.beyond_since or now
                    if (now - s.beyond_since).total_seconds() >= c.hold_s:
                        s.state, s.confirmed_at, s.confirm_spot = CONFIRMED, now, spot
                        s.why = f"index {mv:+.2f}% its way, held {c.hold_s:.0f} s"
                        self._write(s)
                        confirmed.append(s)
                        other = self._open(s.underlying, not s.bullish)
                        if other is not None and other.state == CONFIRMED:
                            self._end(other, ENDED, now, "opposite setup confirmed")
                else:
                    s.beyond_since = None
                    if now - s.first_at > timedelta(minutes=c.window_min):
                        self._end(s, FAILED, now, f"no follow-through in {c.window_min:.0f} min")
            else:  # CONFIRMED
                assert s.confirmed_at is not None
                if mv <= 0:
                    self._end(s, ENDED, now, "gave back the whole move")
                elif now - s.confirmed_at > timedelta(minutes=c.valid_min):
                    self._end(s, ENDED, now, f"{c.valid_min:.0f} min since confirmation")
        # Keep the in-memory list small: finished setups live in ``history``.
        self.setups = [s for s in self.setups if s.state in (FORMING, CONFIRMED)]
        return confirmed

    def close_all(self, now: datetime, why: str) -> list[str]:
        """End every open setup (the session is over: no confirmation after the close)."""
        out = []
        for s in list(self.setups):
            if s.state == FORMING:
                self._end(s, FAILED, now, why)
            elif s.state == CONFIRMED:
                self._end(s, ENDED, now, why)
            else:
                continue
            out.append(f"{s.underlying} {s.direction} setup {s.state.lower()}: {why}")
        self.setups = []
        return out

    # ------------------------------------------------------------------ queries

    def trend(self, underlying: str, now: datetime, lookback_min: float) -> dict[str, Any]:
        """What order flow says about an index over the last ``lookback_min`` minutes: confirmed
        setups each way (open or finished inside the window) and setups still forming."""
        since = now - timedelta(minutes=lookback_min)
        up = down = 0
        for s in self.setups:
            if s.underlying == underlying and s.state == CONFIRMED:
                up += s.bullish
                down += not s.bullish
        for r in self.history:
            if r.get("underlying") != underlying or not r.get("confirmed"):
                continue
            try:
                ended = datetime.fromisoformat(str(r.get("ended_at") or r["confirmed_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if ended >= since:
                up += r.get("direction") == "UP"
                down += r.get("direction") == "DOWN"
        forming = [
            s.direction for s in self.setups if s.underlying == underlying and s.state == FORMING
        ]
        return {"up": up, "down": down, "forming": forming}

    def view(self) -> dict[str, Any]:
        live = sorted(
            (s.row() for s in self.setups), key=lambda r: (r["state"] != CONFIRMED, r["first"])
        )
        return {
            "live": live,
            "done": list(reversed(self.history))[:30],
            "rules": {
                "confirm_move_pct": self.cfg.confirm_move_pct,
                "hold_s": self.cfg.hold_s,
                "window_min": self.cfg.window_min,
                "fail_move_pct": self.cfg.fail_move_pct,
                "valid_min": self.cfg.valid_min,
            },
        }
