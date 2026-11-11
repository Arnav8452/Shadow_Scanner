"""Block-Bootstrap Monte Carlo Drawdown & Ruin Simulator (M6, SPEC §8).

Preserves autocorrelation clusters in volatility by resampling contiguous blocks of size
``block_size`` across ``n_paths`` simulated equity curves.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MonteCarloSummary:
    n_paths: int
    median_max_drawdown_inr: float
    p95_max_drawdown_inr: float
    p99_max_drawdown_inr: float
    prob_ruin_pct: float  # % of paths where drawdown exceeds ruin_threshold_inr


def run_block_bootstrap_mc(
    pnl_series: Sequence[float],
    *,
    n_paths: int = 500,
    block_size: int = 5,
    ruin_threshold_inr: float = 40_000.0,  # e.g., 20% of ₹2L capital
    seed: int = 42,
) -> MonteCarloSummary:
    """Estimate 95th/99th percentile Maximum Drawdown and ruin probability via block bootstrap."""
    n = len(pnl_series)
    if n == 0:
        return MonteCarloSummary(0, 0.0, 0.0, 0.0, 0.0)

    rng = random.Random(seed)
    blk = max(1, min(block_size, n))
    mdds: list[float] = []
    ruined = 0

    for _ in range(n_paths):
        cum = 0.0
        peak = 0.0
        mdd = 0.0
        sampled = 0
        while sampled < n:
            start = rng.randint(0, n - blk)
            for k in range(blk):
                if sampled >= n:
                    break
                cum += pnl_series[start + k]
                peak = max(peak, cum)
                mdd = max(mdd, peak - cum)
                sampled += 1
        mdds.append(mdd)
        if mdd >= ruin_threshold_inr:
            ruined += 1

    mdds.sort()
    med = mdds[len(mdds) // 2]
    p95 = mdds[min(len(mdds) - 1, int(0.95 * len(mdds)))]
    p99 = mdds[min(len(mdds) - 1, int(0.99 * len(mdds)))]

    return MonteCarloSummary(
        n_paths=n_paths,
        median_max_drawdown_inr=round(med, 2),
        p95_max_drawdown_inr=round(p95, 2),
        p99_max_drawdown_inr=round(p99, 2),
        prob_ruin_pct=round(ruined / n_paths * 100.0, 2),
    )
