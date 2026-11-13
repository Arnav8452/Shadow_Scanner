"""Variance-time clock weights (SPEC §4): how much variance an overnight and a non-trading day
add, in units of one trading session's variance, measured from index daily candles.

- **Session variance:** mean of squared open-to-close log returns.
- **Overnight weight:** mean squared close-to-open return across ONE night (consecutive
  calendar days), divided by the session variance.
- **Non-trading-day weight:** the extra mean squared close-to-open return per weekend or holiday
  day (nights spanning k calendar days vs one), divided by the session variance.

Squared returns are trimmed (top ``trim`` share dropped) so a few event days (elections, Covid)
do not set the clock for every ordinary night. Days whose open equals the previous close
exactly are dropped: an index with no pre-open print reports that and it is not a measurement.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class VarianceWeights:
    overnight: float  # one night's variance / one session's
    non_trading_day: float  # each weekend/holiday day's extra variance / one session's
    sessions: int
    nights_1: int
    nights_k: int


def _trimmed_mean(xs: Sequence[float], trim: float) -> float:
    s = sorted(xs)
    keep = s[: max(1, int(len(s) * (1 - trim)))]
    return sum(keep) / len(keep)


def calibrate(candles: Sequence[Sequence[Any]], *, trim: float = 0.01) -> VarianceWeights:
    """``candles``: [timestamp, open, high, low, close, ...] daily, any order."""
    rows = sorted(
        ((date.fromisoformat(str(c[0])[:10]), float(c[1]), float(c[4])) for c in candles),
        key=lambda r: r[0],
    )
    intra: list[float] = []
    one: list[float] = []
    multi: list[tuple[int, float]] = []
    for (d0, _o0, c0), (d1, o1, c1) in itertools.pairwise(rows):
        if min(c0, o1, c1) <= 0:
            continue
        intra.append(math.log(c1 / o1) ** 2)
        if o1 == c0:
            continue
        gap = (d1 - d0).days
        r2 = math.log(o1 / c0) ** 2
        if gap == 1:
            one.append(r2)
        elif 1 < gap <= 5:
            multi.append((gap, r2))
    v_s = _trimmed_mean(intra, trim)
    v_1 = _trimmed_mean(one, trim) if one else 0.0
    extra = [(r2 - v_1) / (gap - 1) for gap, r2 in multi]
    v_nt = max(0.0, _trimmed_mean(extra, trim)) if extra else 0.0
    return VarianceWeights(
        overnight=round(v_1 / v_s, 4) if v_s > 0 else 0.0,
        non_trading_day=round(v_nt / v_s, 4) if v_s > 0 else 0.0,
        sessions=len(intra),
        nights_1=len(one),
        nights_k=len(multi),
    )


__all__ = ["VarianceWeights", "calibrate"]
