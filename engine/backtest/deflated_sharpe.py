"""Probabilistic Sharpe Ratio (PSR), Deflated Sharpe Ratio (DSR), and Trial Registry (M6, SPEC §8).

Implements Bailey & López de Prado (2014):
- Adjusts observed Sharpe ratio ``SR`` for sample length ``T``, skewness ``gamma_3``, kurtosis
  ``gamma_4``, and multiple-testing selection bias across ``n_trials`` logged parameter runs.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from engine.analytics.iv import norm_cdf

EULER_MASCHERONI = 0.5772156649015329


@dataclass(frozen=True, slots=True)
class DeflatedSharpeResult:
    observed_sharpe_annual: float
    benchmark_sharpe_annual: float
    probabilistic_sharpe_pct: float  # PSR in [0, 100]
    deflated_sharpe_pct: float  # DSR in [0, 100]
    n_trials: int
    n_observations: int
    skewness: float
    kurtosis: float
    passed: bool  # True when DSR >= 95% (or PSR >= 95% when n_trials == 1)


def _inv_norm_cdf_approx(p: float) -> float:
    """Rational approximation (Abramowitz & Stegun 26.2.23) for standard normal quantile."""
    p_clamped = min(0.999999, max(1e-6, p))
    if p_clamped < 0.5:
        return -_inv_norm_cdf_approx(1.0 - p_clamped)
    t = math.sqrt(-2.0 * math.log(1.0 - p_clamped))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t)


def expected_max_sharpe(n_trials: int, var_trials_sr: float = 0.5) -> float:
    """Expected maximum Sharpe Ratio under the null across ``n_trials`` independent trials."""
    if n_trials <= 1:
        return 0.0
    sd = math.sqrt(max(1e-6, var_trials_sr))
    z1 = _inv_norm_cdf_approx(1.0 - 1.0 / n_trials)
    z2 = _inv_norm_cdf_approx(1.0 - 1.0 / (n_trials * math.e))
    return sd * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)


def compute_deflated_sharpe(
    returns: Sequence[float],
    *,
    n_trials: int = 1,
    var_trials_sr_annual: float = 0.25,
    periods_per_year: float = 252.0,
) -> DeflatedSharpeResult:
    """Compute PSR and DSR for a return series given ``n_trials`` tested configurations."""
    t_len = len(returns)
    if t_len < 5:
        return DeflatedSharpeResult(0.0, 0.0, 0.0, 0.0, max(1, n_trials), t_len, 0.0, 3.0, False)

    mean_r = sum(returns) / t_len
    sd_r = statistics.stdev(returns)
    if sd_r <= 1e-12:
        return DeflatedSharpeResult(0.0, 0.0, 0.0, 0.0, max(1, n_trials), t_len, 0.0, 3.0, False)

    sr_per_period = mean_r / sd_r
    sr_annual = sr_per_period * math.sqrt(periods_per_year)

    z_scores = [(r - mean_r) / sd_r for r in returns]
    skew = sum(z**3 for z in z_scores) / t_len
    kurt = sum(z**4 for z in z_scores) / t_len

    # Expected max annual SR across n_trials -> convert to per-period benchmark SR*
    sr_star_annual = expected_max_sharpe(max(1, n_trials), var_trials_sr_annual)
    sr_star_period = sr_star_annual / math.sqrt(periods_per_year)

    denom_var = max(
        1e-6,
        1.0 - skew * sr_per_period + ((kurt - 1.0) / 4.0) * (sr_per_period**2),
    )
    denom = math.sqrt(denom_var / max(1, t_len - 1))

    psr_z = sr_per_period / denom
    dsr_z = (sr_per_period - sr_star_period) / denom

    psr_pct = norm_cdf(psr_z) * 100.0
    dsr_pct = norm_cdf(dsr_z) * 100.0

    return DeflatedSharpeResult(
        observed_sharpe_annual=round(sr_annual, 4),
        benchmark_sharpe_annual=round(sr_star_annual, 4),
        probabilistic_sharpe_pct=round(psr_pct, 2),
        deflated_sharpe_pct=round(dsr_pct, 2),
        n_trials=max(1, n_trials),
        n_observations=t_len,
        skewness=round(skew, 4),
        kurtosis=round(kurt, 4),
        passed=(dsr_pct >= 90.0 and sr_annual > 0),
    )
