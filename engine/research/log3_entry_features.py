"""SPEC 5.2 research task ``log3_entry_features``: what did the funded trader's entries (Log 3)
have in common, measured on the index at the minute of entry?

Log 3's exits are known (about -10% stop, +30% target, trail); its entry trigger is not. For
each trade idea (fills in the same direction within ``group_min`` minutes are one idea, the
scale-ins), this pulls the index's 1-minute candles for that session and measures, in the
idea's direction (+ = with the trade):

- time of day (minutes after 09:15);
- distance from the session's running VWAP proxy (time-weighted mean of 1-min closes; the index
  has no volume), %;
- position vs the 15-minute opening range: above, below, inside; distance from its edge, %;
- trend since the open, %; momentum over the last 15 minutes, %;
- distance from the day's extreme in the trade's direction (high for calls, low for puts), %,
  and whether the last 5 minutes made a new extreme (breakout) or not (pullback).

The same features at every other minute of those sessions (10:00-14:00) are the control. A
one- or two-condition rule is chosen to separate entries from the control; with ~10 sessions it
is a HYPOTHESIS to test on new sessions, never a result. Outputs stay in ``data/private``
(derived from a private log): no names or account IDs, only times and index features.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

FEATURES = (
    "minutes_after_open",
    "vs_vwap_pct",
    "vs_or_pct",
    "trend_pct",
    "mom15_pct",
    "from_extreme_pct",
    "new_extreme_5m",
)


@dataclass(frozen=True)
class Idea:
    day: date
    t: time
    bullish: bool
    fills: int
    net_inr: float


def group_ideas(rows: Sequence[Mapping[str, Any]], group_min: float = 10.0) -> list[Idea]:
    """Fills (trade_date, time_in, instrument ending CE/PE, net_pnl_inr) -> ideas."""
    fills = sorted(
        (
            date.fromisoformat(str(r["trade_date"])),
            time.fromisoformat(str(r["time_in"])),
            str(r["instrument"]).endswith("CE"),
            float(r.get("net_pnl_inr") or 0),
        )
        for r in rows
    )
    ideas: list[Idea] = []
    for d, t, bull, net in fills:
        last = ideas[-1] if ideas else None
        mins = (
            (t.hour * 60 + t.minute) - (last.t.hour * 60 + last.t.minute)
            if last is not None and last.day == d
            else 1e9
        )
        if last is not None and last.day == d and last.bullish == bull and 0 <= mins <= group_min:
            ideas[-1] = Idea(d, last.t, bull, last.fills + 1, last.net_inr + net)
        else:
            ideas.append(Idea(d, t, bull, 1, net))
    return ideas


def features_at(
    candles: Sequence[Sequence[Any]], at: time, bullish: bool
) -> dict[str, float] | None:
    """Features of the index at the last completed minute before ``at``."""
    bars = [
        (datetime.fromisoformat(str(c[0])).time(), float(c[1]), float(c[2]), float(c[3]),
         float(c[4]))
        for c in sorted(candles, key=lambda c: str(c[0]))
    ]  # fmt: skip
    past = [b for b in bars if b[0] < at.replace(second=0)]
    if len(past) < 16:
        return None
    sign = 1.0 if bullish else -1.0
    o = past[0][1]
    px = past[-1][4]
    closes = [b[4] for b in past]
    vwap = statistics.fmean(closes)
    orb = [b for b in past if b[0] < time(9, 30)]
    or_hi, or_lo = max(b[2] for b in orb), min(b[3] for b in orb)
    if px > or_hi:
        vs_or = (px - or_hi) / or_hi * 100
    elif px < or_lo:
        vs_or = (px - or_lo) / or_lo * 100
    else:
        vs_or = 0.0
    hi, lo = max(b[2] for b in past), min(b[3] for b in past)
    ext = hi if bullish else lo
    prev_ext = max(b[2] for b in past[:-5]) if bullish else min(b[3] for b in past[:-5])
    new_ext = (hi > prev_ext) if bullish else (lo < prev_ext)
    return {
        "minutes_after_open": at.hour * 60 + at.minute - (9 * 60 + 15),
        "vs_vwap_pct": sign * (px - vwap) / vwap * 100,
        "vs_or_pct": sign * vs_or,
        "trend_pct": sign * (px - o) / o * 100,
        "mom15_pct": sign * (px - past[-16][4]) / past[-16][4] * 100,
        "from_extreme_pct": abs(ext - px) / px * 100,
        "new_extreme_5m": 1.0 if new_ext else 0.0,
    }


def _hit(v: float, thr: float, op: str) -> bool:
    return v >= thr if op == ">=" else v <= thr


def _rule_search(
    pos: list[dict[str, float]], neg: list[dict[str, float]]
) -> list[tuple[float, str]]:
    """Single-feature thresholds ranked by Youden's J (recall on entries - false-positive rate
    on the control): the simplest rule a person could follow."""
    out: list[tuple[float, str]] = []
    for f in FEATURES:
        vals = sorted({round(x[f], 3) for x in pos + neg})
        for thr in vals:
            for op in (">=", "<="):
                tpr = sum(_hit(x[f], thr, op) for x in pos) / len(pos)
                fpr = sum(_hit(x[f], thr, op) for x in neg) / len(neg)
                out.append(
                    (tpr - fpr, f"{f} {op} {thr:g} (entries {tpr:.0%}, other minutes {fpr:.0%})")
                )
    out.sort(key=lambda x: -x[0])
    return out[:5]


def analyze(
    ideas: Sequence[Idea],
    candles_for: Callable[[date], Sequence[Sequence[Any]]],
) -> dict[str, Any]:
    pos: list[dict[str, float]] = []
    neg: list[dict[str, float]] = []
    by_day: dict[date, Sequence[Sequence[Any]]] = {}
    for i in ideas:
        if i.day not in by_day:
            by_day[i.day] = candles_for(i.day)
        f = features_at(by_day[i.day], i.t, i.bullish)
        if f is not None:
            pos.append(f | {"win": 1.0 if i.net_inr > 0 else 0.0})
    for d, cs in by_day.items():
        dirs = {i.bullish for i in ideas if i.day == d}
        for m in range(10 * 60, 14 * 60, 5):
            t = time(m // 60, m % 60)
            if any(i.day == d and abs((i.t.hour * 60 + i.t.minute) - m) < 10 for i in ideas):
                continue
            for bull in dirs:
                f = features_at(cs, t, bull)
                if f is not None:
                    neg.append(f)
    med = {
        f: (
            statistics.median(x[f] for x in pos) if pos else None,
            statistics.median(x[f] for x in neg) if neg else None,
        )
        for f in FEATURES
    }
    return {
        "ideas": len(ideas),
        "measured": len(pos),
        "control": len(neg),
        "medians": med,
        "rules": _rule_search(pos, neg) if pos and neg else [],
        "winners": sum(x["win"] for x in pos),
    }


def render(res: dict[str, Any]) -> str:
    lines = [
        "# Log 3 entry features (SPEC 5.2 `log3_entry_features`)",
        "",
        f"{res['ideas']} trade ideas (scale-ins grouped), {res['measured']} measured on 1-minute "
        f"index candles, {res['winners']:.0f} of them winners; control: {res['control']} other "
        "minutes (10:00-14:00, every 5 min) of the same sessions, same direction.",
        "",
        "Features are signed in the trade's direction (+ = with the trade).",
        "",
        "| Feature | Entries (median) | Other minutes (median) |",
        "|---|---|---|",
    ]
    for f, (a, b) in res["medians"].items():
        lines.append(
            f"| {f} | {a:.3f} | {b:.3f} |"
            if a is not None and b is not None
            else f"| {f} | — | — |"
        )
    lines += [
        "",
        "Best single-condition rules (Youden's J = entries caught - other minutes caught):",
        "",
    ]
    lines += [f"- J {j:+.2f}: {r}" for j, r in res["rules"]]
    lines += [
        "",
        "HYPOTHESIS: about 10 sessions. A rule found here must be tested on sessions the log "
        "never saw (paper, or replay on recorded days) before it replaces the 5.2 trigger.",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["FEATURES", "Idea", "analyze", "features_at", "group_ideas", "render"]
