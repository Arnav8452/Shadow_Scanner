"""Raw SVI smile fit (Gatheral 2004) for one expiry: total variance as a function of
log-moneyness, ``w(k) = a + b * (rho * (k - m) + sqrt((k - m)**2 + sigma**2))``.

Fitted to out-of-the-money implied volatilities computed from our own Black-76 IVs on the
synthetic forward (``k = ln(K / F)``, ``w = iv**2 * t``). It smooths noisy illiquid strikes so
Greeks and skew read off a curve without arbitrage-looking kinks. Pure Python: a bounded
Nelder–Mead on a handful of points every minute is cheap; no new dependency.

Constraints kept by penalty: ``b >= 0``, ``|rho| < 1``, ``sigma > 0``, minimum variance
``a + b*sigma*sqrt(1 - rho**2) >= 0`` (no negative variance), and Lee's wing bound
``b * (1 + |rho|) <= 4 / t`` (a necessary no-arbitrage condition on the wings).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SviFit:
    a: float
    b: float
    rho: float
    m: float
    sigma: float
    t: float
    rmse_vol_pts: float  # fit error in implied-vol points (percent)
    n_points: int

    def total_variance(self, k: float) -> float:
        return self.a + self.b * (
            self.rho * (k - self.m) + math.sqrt((k - self.m) ** 2 + self.sigma**2)
        )

    def iv(self, k: float) -> float:
        return math.sqrt(max(self.total_variance(k), 1e-12) / self.t)


def _nelder_mead(
    f: Callable[[list[float]], float], x0: list[float], step: list[float], iters: int = 600
) -> list[float]:
    n = len(x0)
    pts = [x0] + [[x0[j] + (step[j] if j == i else 0.0) for j in range(n)] for i in range(n)]
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        pts = [pts[i] for i in order]
        vals = [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < 1e-14:
            break
        centroid = [sum(p[j] for p in pts[:-1]) / n for j in range(n)]
        refl = [centroid[j] + (centroid[j] - pts[-1][j]) for j in range(n)]
        fr = f(refl)
        if fr < vals[0]:
            exp = [centroid[j] + 2.0 * (centroid[j] - pts[-1][j]) for j in range(n)]
            fe = f(exp)
            pts[-1], vals[-1] = (exp, fe) if fe < fr else (refl, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = refl, fr
        else:
            con = [centroid[j] + 0.5 * (pts[-1][j] - centroid[j]) for j in range(n)]
            fc = f(con)
            if fc < vals[-1]:
                pts[-1], vals[-1] = con, fc
            else:
                best = pts[0]
                pts = [best] + [
                    [best[j] + 0.5 * (p[j] - best[j]) for j in range(n)] for p in pts[1:]
                ]
                vals = [vals[0]] + [f(p) for p in pts[1:]]
    return pts[min(range(n + 1), key=lambda i: vals[i])]


def fit_svi(ks: Sequence[float], ivs: Sequence[float], t: float) -> SviFit | None:
    """Fit raw SVI to (log-moneyness, implied vol) points of one expiry; None if too few."""
    pts = [(k, v) for k, v in zip(ks, ivs, strict=False) if v is not None and 0 < v < 5]
    if len(pts) < 5 or t <= 0:
        return None
    w_obs = [(k, v * v * t) for k, v in pts]
    wmin = min(w for _, w in w_obs)
    k_at_min = min(w_obs, key=lambda x: x[1])[0]
    lee = 4.0 / t

    def params(x: list[float]) -> tuple[float, float, float, float, float]:
        a, b, rho, m, s = x
        return a, abs(b), math.tanh(rho), m, abs(s) + 1e-4

    def loss(x: list[float]) -> float:
        a, b, rho, m, s = params(x)
        err = 0.0
        for k, w in w_obs:
            model = a + b * (rho * (k - m) + math.sqrt((k - m) ** 2 + s * s))
            err += (model - w) ** 2
        penalty = 0.0
        min_var = a + b * s * math.sqrt(1.0 - rho * rho)
        if min_var < 0:
            penalty += 1e3 * min_var * min_var
        if b * (1 + abs(rho)) > lee:
            penalty += 1e3 * (b * (1 + abs(rho)) - lee) ** 2
        return err / len(w_obs) + penalty

    # Nelder-Mead finds local minima: start from a small grid of shapes, keep the best, and
    # restart once from it.
    step = [wmin * 0.5 + 1e-4, 0.05, 0.3, 0.05, 0.05]
    best: list[float] | None = None
    best_loss = math.inf
    for rho0 in (-0.9, -0.3, 0.3):  # atanh space: about -0.7, -0.3, +0.3
        for sig0 in (0.02, 0.1):
            for b0 in (0.05, 0.2):
                x = _nelder_mead(loss, [wmin * 0.5, b0, rho0, k_at_min, sig0], step, iters=400)
                lx = loss(x)
                if lx < best_loss:
                    best, best_loss = x, lx
    assert best is not None
    best = _nelder_mead(loss, best, [v * 0.2 for v in step], iters=800)
    a, b, rho, m, s = params(best)
    fit = SviFit(a, b, rho, m, s, t, 0.0, len(pts))
    rmse = math.sqrt(sum((fit.iv(k) - v) ** 2 for k, v in pts) / len(pts)) * 100.0
    return SviFit(a, b, rho, m, s, t, round(rmse, 3), len(pts))
