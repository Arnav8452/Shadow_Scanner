"""Rolling Walk-Forward Optimization (WFO) & Out-of-Sample Decay Gate (M6, SPEC §8).

Enforces:
- Rolling in-sample (IS) training window followed by strictly out-of-sample (OOS) test window.
- Flags strategies where OOS Sharpe decays by more than ``max_sharpe_decay_pct`` (default 40%)
  relative to IS Sharpe.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_index: int
    in_sample_sharpe: float
    out_of_sample_sharpe: float
    out_of_sample_net_pnl: float


@dataclass(frozen=True, slots=True)
class WalkForwardSummary:
    folds: tuple[WalkForwardFold, ...]
    mean_is_sharpe: float
    mean_oos_sharpe: float
    sharpe_decay_pct: float
    passed: bool


def _sharpe(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    sd = statistics.stdev(xs)
    return (m / sd * math.sqrt(252.0)) if sd > 1e-6 else 0.0


def evaluate_walk_forward(
    daily_returns: Sequence[float],
    *,
    train_size: int = 30,
    test_size: int = 10,
    max_sharpe_decay_pct: float = 40.0,
) -> WalkForwardSummary:
    """Run rolling walk-forward folds over ``daily_returns`` and check OOS Sharpe retention."""
    n = len(daily_returns)
    if n < train_size + test_size:
        return WalkForwardSummary((), 0.0, 0.0, 100.0, False)

    folds: list[WalkForwardFold] = []
    start = 0
    idx = 0
    while start + train_size + test_size <= n:
        is_slice = daily_returns[start : start + train_size]
        oos_slice = daily_returns[start + train_size : start + train_size + test_size]
        folds.append(
            WalkForwardFold(
                fold_index=idx,
                in_sample_sharpe=round(_sharpe(is_slice), 4),
                out_of_sample_sharpe=round(_sharpe(oos_slice), 4),
                out_of_sample_net_pnl=round(sum(oos_slice), 2),
            )
        )
        start += test_size
        idx += 1

    mean_is = sum(f.in_sample_sharpe for f in folds) / len(folds)
    mean_oos = sum(f.out_of_sample_sharpe for f in folds) / len(folds)
    if mean_is <= 0:
        decay = 100.0
        passed = False
    else:
        decay = max(0.0, (mean_is - mean_oos) / mean_is * 100.0)
        passed = mean_oos > 0 and decay <= max_sharpe_decay_pct

    return WalkForwardSummary(
        folds=tuple(folds),
        mean_is_sharpe=round(mean_is, 4),
        mean_oos_sharpe=round(mean_oos, 4),
        sharpe_decay_pct=round(decay, 2),
        passed=passed,
    )
