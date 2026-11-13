"""Event IV crush study (pre-registered 14 Nov 2026, docs/research/mechanisms.md).

For each event (a stock's results board meeting, or an index event such as an RBI policy
decision) on day D, with D-1 the last session before D and D+1 the first session after it:

- **IV change:** ATM IV at the close of D+1 minus at the close of D-1, both from our EOD IV
  tables (Black-76 on the parity forward, variance clock), and only when both rows price the
  **same expiry** and that expiry is after D+1;
- **implied event move:** the variance the D-1 option carried beyond what the D+1 option
  still carries, sqrt(max(0, IV-1^2 * T-1 - IV+1^2 * T+1)) (T in variance-clock years);
- **realized move:** |ln(S+1 / S-1)|.

Control: the same names' IV change over every other two-session window that does not come
within ``exclude_days`` trading days of one of their events. Research only: never imported by
a live path. Every split looked at afterwards is a new trial.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

IvRow = Mapping[str, Any]  # trade_date, underlying, expiry, spot, atm_iv (fraction)


@dataclass(frozen=True)
class EventMeasure:
    underlying: str
    day: date
    before: date
    after: date
    iv_before: float  # %
    iv_after: float  # %
    implied_move: float | None  # fraction (log units)
    realized_move: float  # fraction (log units)

    @property
    def iv_change(self) -> float:
        return self.iv_after - self.iv_before


def measure_window(
    rows: Mapping[tuple[str, date], IvRow],
    und: str,
    before: date,
    after: date,
    t_var: Callable[[date, date], float],
    *,
    event_day: date | None = None,
) -> EventMeasure | None:
    a, b = rows.get((und, before)), rows.get((und, after))
    if a is None or b is None:
        return None
    if a["expiry"] != b["expiry"] or a["expiry"] <= after:
        return None  # a roll or an expiry inside the window: not comparable
    iv0, iv1 = float(a["atm_iv"]), float(b["atm_iv"])
    s0, s1 = float(a["spot"] or 0), float(b["spot"] or 0)
    if iv0 <= 0 or iv1 <= 0 or s0 <= 0 or s1 <= 0:
        return None
    t0, t1 = t_var(before, a["expiry"]), t_var(after, b["expiry"])
    var = iv0 * iv0 * t0 - iv1 * iv1 * t1
    return EventMeasure(
        und, event_day or after, before, after, iv0 * 100, iv1 * 100,
        math.sqrt(var) if var > 0 else None, abs(math.log(s1 / s0)),
    )  # fmt: skip


def run_study(
    rows: Mapping[tuple[str, date], IvRow],
    events: Iterable[tuple[str, date]],
    sessions: Sequence[date],
    t_var: Callable[[date, date], float],
    *,
    exclude_days: int = 3,
) -> dict[str, Any]:
    """``sessions``: every trading day in the sample, sorted. Events on a non-trading day use
    the sessions around it."""
    idx = {d: i for i, d in enumerate(sessions)}

    def around(d: date) -> tuple[int, int] | None:
        prev = [i for i, s in enumerate(sessions) if s < d]
        nxt = [i for i, s in enumerate(sessions) if s > d]
        return (prev[-1], nxt[0]) if prev and nxt else None

    ev_list = sorted(set(events))
    measures: list[EventMeasure] = []
    blocked: dict[str, set[int]] = {}
    for und, d in ev_list:
        w = around(d)
        if w is None:
            continue
        i0, i1 = w
        centre = idx.get(d, i0)
        blocked.setdefault(und, set()).update(
            range(centre - exclude_days, centre + exclude_days + 1)
        )
        m = measure_window(rows, und, sessions[i0], sessions[i1], t_var, event_day=d)
        if m is not None:
            measures.append(m)
    unds = {u for u, _ in rows}
    control: list[float] = []
    for und in unds:
        bad = blocked.get(und, set())
        for i in range(len(sessions) - 2):
            if any(j in bad for j in range(i, i + 3)):
                continue
            m = measure_window(rows, und, sessions[i], sessions[i + 2], t_var)
            if m is not None:
                control.append(m.iv_change)
    return {"events": measures, "control": control}


def summarize(res: Mapping[str, Any]) -> dict[str, Any]:
    ev: list[EventMeasure] = list(res["events"])
    ctl: list[float] = list(res["control"])
    out: dict[str, Any] = {"n_events": len(ev), "n_control": len(ctl)}
    if ev:
        ch = [m.iv_change for m in ev]
        out.update(
            fell_pct=round(100 * sum(c < 0 for c in ch) / len(ch), 1),
            median_change=round(statistics.median(ch), 2),
            mean_change=round(statistics.fmean(ch), 2),
        )
    if ctl:
        out.update(
            control_median=round(statistics.median(ctl), 2),
            control_fell_pct=round(100 * sum(c < 0 for c in ctl) / len(ctl), 1),
        )
    pairs = [(m.realized_move, m.implied_move) for m in ev if m.implied_move]
    if pairs:
        ratio = [r / i for r, i in pairs]
        logs = [math.log(max(r, 1e-4) / i) for r, i in pairs]
        sd = statistics.stdev(logs) if len(logs) > 1 else 0.0
        out.update(
            n_h2=len(pairs),
            realized_below_pct=round(100 * sum(x < 1 for x in ratio) / len(ratio), 1),
            mean_ratio=round(statistics.fmean(ratio), 3),
            median_ratio=round(statistics.median(ratio), 3),
            t_log_ratio=round(statistics.fmean(logs) / (sd / math.sqrt(len(logs))), 2)
            if sd > 0
            else None,
        )
    return out


def verdicts(s: Mapping[str, Any]) -> dict[str, str]:
    """The thresholds pre-registered in docs/research/mechanisms.md, unchanged."""
    h1 = (
        s.get("n_events", 0) > 0
        and s.get("fell_pct", 0) >= 70
        and s.get("median_change", 0) <= -3
        and abs(s.get("control_median", 99)) <= 0.5
    )
    t = s.get("t_log_ratio")
    h2 = (
        s.get("n_h2", 0) > 0
        and s.get("realized_below_pct", 0) > 55
        and s.get("mean_ratio", 9) < 0.9
        and t is not None
        and t <= -2
    )
    return {"H1": "PASS" if h1 else "FAIL", "H2": "PASS" if h2 else "FAIL"}


__all__ = ["EventMeasure", "measure_window", "run_study", "summarize", "verdicts"]
