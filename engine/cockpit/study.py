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

    def _load_today(self, close_at: datetime) -> list[dict[str, Any]]:
        if self.journal is None or not self.journal.exists():
            return []
        day = close_at.astimezone(IST).date().isoformat()
        out = []
        for line in self.journal.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if str(rec.get("t0", ""))[:10] == day:
                out.append(rec)
        return out

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

    def update(self, now: datetime, spots: Mapping[str, float], books: Mapping[str, Book]) -> None:
        for e in self.entries:
            if e.done:
                continue
            for h in self.horizons:
                key = f"{h}m"
                due = e.t0 + timedelta(minutes=h)
                if key in e.results:
                    continue
                if due > self.close_at:
                    if now >= self.close_at:  # the position would be squared off first
                        e.results[key] = {"skipped": "after square-off"}
                elif now >= due:
                    self._measure(e, key, now, spots.get(e.underlying), books.get(e.vehicle))
            if now >= self.close_at and "close" not in e.results:
                self._measure(e, "close", now, spots.get(e.underlying), books.get(e.vehicle))
            if all(f"{h}m" in e.results for h in self.horizons) and "close" in e.results:
                self._finish(e)

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
        row = e.row()
        self.done_today.append(row)
        if self.journal is not None:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")

    # ------------------------------------------------------------------ view

    def view(self) -> dict[str, Any]:
        rows = [e.row() for e in self.entries if not e.done] + list(reversed(self.done_today))
        return {
            "horizons": [f"{h}m" for h in self.horizons] + ["close"],
            "summary": summarize(
                [*self.done_today, *[e.row() for e in self.entries if not e.done]],
                [f"{h}m" for h in self.horizons] + ["close"],
            ),
            "rows": rows[:60],
        }


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
