"""Signal study: does an order-flow reading predict direction over horizons we would hold?

Owner direction (12 Nov 2026): no scalping. Order flow is a confirmation and timing input for
the playbook strategies, so its signals are measured on trading horizons, not on micro
stop/target levels. For every confirmed signal episode this records:

- the underlying (spot) at the signal and at +15, +30, +60 minutes and at the square-off time,
  and whether it moved in the signal's direction;
- the ATM option that would express it (bought at the ask), its bid at each horizon, and the
  net rupee result for one lot after full round-trip charges.

Nothing is traded here. Results go to ``signal_study.jsonl`` and the Research tab shows them by
score band and horizon. Every threshold tried on this data counts as a trial (deflated Sharpe).

Every change to an entry is journaled at once (the last line per ``signal_id`` wins), so a
restart resumes it. A horizon that fell due while the engine was down is marked skipped, never
measured late at the wrong price. Readers use only ``done`` rows.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from engine.backtest.costs import fno_charges
from engine.cockpit.paper import Book
from engine.core.rules import IST, RuleBook

HORIZONS_MIN: tuple[int, ...] = (15, 30, 60)
# Signals of the same underlying and direction within this many minutes of an episode's
# first signal are one episode: the same reading firing on many strikes is one bet.
EPISODE_MIN = 15


@dataclass
class StudyEntry:
    signal_id: str
    t0: datetime
    symbol: str
    label: str
    underlying: str
    bullish: bool  # direction of the *underlying* the signal implies
    score: float
    wave: str
    spot0: float
    vehicle: str
    vehicle_label: str
    lot: int
    opt_ask0: float
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    done: bool = False
    context: dict[str, Any] = field(default_factory=dict)

    def row(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "t0": self.t0.isoformat(),
            "time": self.t0.astimezone(IST).strftime("%H:%M:%S"),
            "symbol": self.symbol,
            "label": self.label,
            "underlying": self.underlying,
            "direction": "UP" if self.bullish else "DOWN",
            "score": round(self.score, 1),
            "wave": self.wave,
            "spot0": self.spot0,
            "vehicle": self.vehicle,
            "vehicle_label": self.vehicle_label,
            "lot": self.lot,
            "opt_ask0": self.opt_ask0,
            "results": self.results,
            "done": self.done,
            "context": self.context,
        }


class SignalStudy:
    def __init__(
        self,
        *,
        book: RuleBook,
        broker_name: str,
        close_at: datetime,
        journal: Path | None,
        horizons_min: Sequence[int] = HORIZONS_MIN,
    ) -> None:
        self.book = book
        self.broker_name = broker_name
        self.close_at = close_at
        self.journal = journal
        self.horizons = tuple(horizons_min)
        self.entries: list[StudyEntry] = []
        self.done_today: list[dict[str, Any]] = self._load_today(close_at)
        # A horizon more than this late (the engine was down) is skipped, not measured late.
        self.late_s = 120.0

    def _load_today(self, close_at: datetime) -> list[dict[str, Any]]:
        """Today's finished rows; today's unfinished ones become live entries again."""
        if self.journal is None or not self.journal.exists():
            return []
        day = close_at.astimezone(IST).date().isoformat()
        last: dict[str, dict[str, Any]] = {}
        for line in self.journal.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if str(rec.get("t0", ""))[:10] == day:
                last[str(rec.get("signal_id"))] = rec
        out = []
        for rec in last.values():
            if rec.get("done"):
                out.append(rec)
                continue
            try:
                self.entries.append(StudyEntry(
                    signal_id=str(rec["signal_id"]), t0=datetime.fromisoformat(rec["t0"]),
                    symbol=rec["symbol"], label=rec["label"], underlying=rec["underlying"],
                    bullish=rec.get("direction") == "UP", score=float(rec["score"]),
                    wave=str(rec["wave"]), spot0=float(rec["spot0"]),
                    vehicle=str(rec.get("vehicle") or ""),
                    vehicle_label=str(rec.get("vehicle_label") or ""), lot=int(rec["lot"]),
                    opt_ask0=float(rec.get("opt_ask0") or 0.0),
                    results=dict(rec.get("results") or {}),
                    context=dict(rec.get("context") or {}),
                ))  # fmt: skip
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _save(self, e: StudyEntry) -> None:
        if self.journal is None:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(e.row(), default=str) + "\n")

    # ------------------------------------------------------------------ recording

    def add(
        self,
        *,
        signal_id: str,
        now: datetime,
        symbol: str,
        label: str,
        underlying: str,
        bullish: bool,
        score: float,
        wave: str,
        spot: float,
        vehicle: str,
        vehicle_label: str,
        lot: int,
        vehicle_book: Book | None,
        context: Mapping[str, Any] | None = None,
    ) -> StudyEntry | None:
        if now >= self.close_at or spot <= 0:
            return None
        ask = vehicle_book.asks[0][0] if vehicle_book is not None and vehicle_book.asks else 0.0
        entry = StudyEntry(
            signal_id=signal_id,
            t0=now,
            symbol=symbol,
            label=label,
            underlying=underlying,
            bullish=bullish,
            score=score,
            wave=wave,
            spot0=spot,
            vehicle=vehicle,
            vehicle_label=vehicle_label,
            lot=max(1, lot),
            opt_ask0=ask,
            context=dict(context or {}),
        )
        self.entries.append(entry)
        self._save(entry)
        return entry

    def _measure(
        self, e: StudyEntry, key: str, now: datetime, spot: float | None, book: Book | None
    ) -> None:
        res: dict[str, Any] = {"at": now.astimezone(IST).strftime("%H:%M:%S")}
        if spot and spot > 0:
            move = (spot - e.spot0) * (1 if e.bullish else -1)
            res.update(
                {
                    "spot": spot,
                    "move_pts": round(move, 2),
                    "move_pct": round(move / e.spot0 * 100, 3),
                    "hit": move > 0,
                }
            )
        bid = book.bids[0][0] if book is not None and book.bids else 0.0
        if e.opt_ask0 > 0 and bid <= 0:
            res["opt_missing"] = "no quote for the vehicle"
        if e.opt_ask0 > 0 and bid > 0:
            qty = e.lot
            day = now.astimezone(IST).date()
            charges = (
                fno_charges(
                    self.book,
                    day,
                    segment="options",
                    side="buy",
                    turnover_inr=e.opt_ask0 * qty,
                    broker=self.broker_name,
                ).total
                + fno_charges(
                    self.book,
                    day,
                    segment="options",
                    side="sell",
                    turnover_inr=bid * qty,
                    broker=self.broker_name,
                ).total
            )
            gross = (bid - e.opt_ask0) * qty
            res.update(
                {
                    "opt_bid": bid,
                    "opt_ret_pct": round((bid / e.opt_ask0 - 1) * 100, 2),
                    "opt_net_inr": round(gross - charges, 2),
                }
            )
        e.results[key] = res

    def open_vehicles(self) -> list[str]:
        """Options that open entries are still to be priced on, newest signal first: the
        cockpit pins them in the live feed so a vehicle that drifts from the money keeps a
        quote (the feed otherwise follows only the strikes near the current spot)."""
        out: dict[str, None] = {}
        for e in sorted(self.entries, key=lambda x: x.t0, reverse=True):
            if not e.done and e.vehicle:
                out.setdefault(e.vehicle, None)
        return list(out)

    def update(self, now: datetime, spots: Mapping[str, float], books: Mapping[str, Book]) -> None:
        for e in self.entries:
            if e.done:
                continue
            before = len(e.results)
            for h in self.horizons:
                key = f"{h}m"
                due = e.t0 + timedelta(minutes=h)
                if key in e.results:
                    continue
                if due > self.close_at:
                    if now >= self.close_at:  # the position would be squared off first
                        e.results[key] = {"skipped": "after square-off"}
                elif now >= due:
                    if (now - due).total_seconds() > self.late_s:
                        e.results[key] = {"skipped": "engine down at the horizon"}
                    else:
                        self._measure(e, key, now, spots.get(e.underlying), books.get(e.vehicle))
            if now >= self.close_at and "close" not in e.results:
                if (now - self.close_at).total_seconds() > self.late_s:
                    e.results["close"] = {"skipped": "engine down at the square-off"}
                else:
                    self._measure(e, "close", now, spots.get(e.underlying), books.get(e.vehicle))
            if all(f"{h}m" in e.results for h in self.horizons) and "close" in e.results:
                self._finish(e)
            elif len(e.results) != before:
                self._save(e)

    def finish(self, now: datetime, spots: Mapping[str, float], books: Mapping[str, Book]) -> None:
        """Session end: measure what is still open at the last prices, then write everything."""
        for e in self.entries:
            if e.done:
                continue
            if "close" not in e.results:
                self._measure(e, "close", now, spots.get(e.underlying), books.get(e.vehicle))
            for h in self.horizons:
                e.results.setdefault(f"{h}m", {"skipped": "session ended first"})
            self._finish(e)

    def _finish(self, e: StudyEntry) -> None:
        e.done = True
        self.done_today.append(e.row())
        self._save(e)

    # ------------------------------------------------------------------ view

    def view(self) -> dict[str, Any]:
        rows = [e.row() for e in self.entries if not e.done] + list(reversed(self.done_today))
        keys = [f"{h}m" for h in self.horizons] + ["close"]
        today = [*self.done_today, *[e.row() for e in self.entries if not e.done]]
        firsts = episode_firsts(today)
        return {
            "horizons": keys,
            "summary": summarize(today, keys),
            "signals": len(today),
            "episodes": len(firsts),
            "episode_summary": summarize(firsts, keys),
            "rows": rows[:60],
        }


def episode_firsts(
    rows: Sequence[Mapping[str, Any]], minutes: int = EPISODE_MIN
) -> list[dict[str, Any]]:
    """The first signal of each episode, in time order, with ``episode_signals`` = how many
    signals the episode held. An episode = signals of one underlying and direction within
    ``minutes`` of its first signal; it is measured on that first signal, the one lot a trader
    would actually have taken."""
    out: list[dict[str, Any]] = []
    open_ep: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda x: datetime.fromisoformat(str(x["t0"]))):
        t = datetime.fromisoformat(str(r["t0"]))
        key = (str(r.get("underlying", "")), str(r.get("direction", "")))
        cur = open_ep.get(key)
        if cur is not None and t - cur[0] <= timedelta(minutes=minutes):
            cur[1]["episode_signals"] += 1
            continue
        first = {**r, "episode_signals": 1}
        open_ep[key] = (t, first)
        out.append(first)
    return out


def summarize(rows: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
    """Per horizon: signals measured, share moving the signal's way, average move, option net."""
    out = []
    for k in keys:
        rs = [r["results"][k] for r in rows if isinstance(r.get("results", {}).get(k), dict)]
        moved = [x for x in rs if "move_pts" in x]
        opt = [x["opt_net_inr"] for x in rs if "opt_net_inr" in x]
        out.append(
            {
                "horizon": k,
                "n": len(moved),
                "hit_pct": round(100.0 * sum(1 for x in moved if x["hit"]) / len(moved), 1)
                if moved
                else None,
                "avg_move_pts": round(sum(x["move_pts"] for x in moved) / len(moved), 2)
                if moved
                else None,
                "avg_move_pct": round(sum(x["move_pct"] for x in moved) / len(moved), 3)
                if moved
                else None,
                "opt_n": len(opt),
                "opt_net_inr": round(sum(opt), 2) if opt else None,
                "opt_avg_inr": round(sum(opt) / len(opt), 2) if opt else None,
            }
        )
    return out
