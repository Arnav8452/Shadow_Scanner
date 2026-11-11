"""Black-76 Option Pricing, Dual-Clock Time-to-Expiry, and Bracketed Brent's Method IV Solver.

Implements SPEC §4.2:
1. **Dual Clock**:
   - ``t_cal``: calendar time to expiry in years (ACT/365) for discounting ``exp(-r * t_cal)``.
   - ``t_var``: trading-minute variance clock in years (counting actual IST trading minutes from
     ``now`` to ``15:30``/``15:40`` on expiry day, divided by ``252 * session_minutes``) so weekend
     and holiday theta decay does not artificially crush Friday/Monday IVs.
   - Near-expiry floor: ``min_trading_minutes = 15.0`` prevents ``T -> 0`` numerical explosion on
     expiry afternoon.
2. **Bracketed Brent's Method (``brentq``) Solver**:
   - Pure Python stdlib implementation of Brent's root-finding algorithm over ``[1e-4, 5.0]``
     (guaranteed convergence without Newton-Raphson wing blowups when vega is near zero).
   - Strict no-arbitrage bound validation before solving.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

SQRT_2 = math.sqrt(2.0)
SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT_2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


@dataclass(frozen=True, slots=True)
class DualExpiryClock:
    t_cal: float  # Calendar years for discounting exp(-r * t_cal)
    t_var: float  # Trading-time variance years for sigma * sqrt(t_var)
    remaining_trading_minutes: float


def compute_dual_clock(
    now: datetime,
    expiry_ts: datetime,
    *,
    trading_days_between: int | None = None,
    session_minutes_per_day: float = 385.0,  # NSE F&O 09:15-15:40 = 385 mins (375 for 09:15-15:30)
    min_trading_minutes: float = 15.0,
) -> DualExpiryClock:
    """Compute calendar discounting time ``t_cal`` and trading variance time ``t_var``.

    APPROXIMATE (calendar-day ratio, fixed session length, no holidays). For real chains use
    :func:`engine.analytics.expiry_clock.expiry_clock`, which reads session times and holidays
    from config/rules (15:30 -> 15:40 close on 3 Aug 2026).
    """
    if now.tzinfo is None or expiry_ts.tzinfo is None:
        raise ValueError("now and expiry_ts must be timezone-aware")
    delta_s = max(0.0, (expiry_ts - now).total_seconds())
    t_cal = max(delta_s / (365.0 * 86400.0), min_trading_minutes / (365.0 * 1440.0))

    if trading_days_between is not None:
        # Intraday minutes left today + full trading days remaining
        same_day_mins = min(session_minutes_per_day, max(0.0, (delta_s % 86400.0) / 60.0))
        rem_mins = max(
            min_trading_minutes,
            max(0, trading_days_between) * session_minutes_per_day + same_day_mins,
        )
    else:
        # Approximate trading minutes from calendar days (5/7 business ratio)
        cal_days = delta_s / 86400.0
        if cal_days <= 1.0:
            rem_mins = max(min_trading_minutes, min(session_minutes_per_day, delta_s / 60.0))
        else:
            rem_mins = max(
                min_trading_minutes,
                cal_days * (252.0 / 365.0) * session_minutes_per_day,
            )

    t_var = rem_mins / (252.0 * session_minutes_per_day)
    return DualExpiryClock(
        t_cal=t_cal,
        t_var=t_var,
        remaining_trading_minutes=round(rem_mins, 2),
    )


def black76_price(
    forward: float,
    strike: float,
    t_var: float,
    sigma: float,
    *,
    is_call: bool,
    t_cal: float | None = None,
    risk_free_rate: float = 0.068,
) -> float:
    """Black-76 European option price on forward ``F`` with dual-clock support."""
    if forward <= 0 or strike <= 0:
        raise ValueError("forward and strike must be > 0")
    t_disc = t_var if t_cal is None else t_cal
    df = math.exp(-risk_free_rate * max(t_disc, 0.0))
    if t_var <= 0 or sigma <= 0:
        intrinsic = max(0.0, forward - strike) if is_call else max(0.0, strike - forward)
        return df * intrinsic

    vol_sqrt_t = sigma * math.sqrt(t_var)
    d1 = (math.log(forward / strike) + 0.5 * sigma * sigma * t_var) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    if is_call:
        return df * (forward * norm_cdf(d1) - strike * norm_cdf(d2))
    return df * (strike * norm_cdf(-d2) - forward * norm_cdf(-d1))


def brentq(
    fn: object,
    a: float,
    b: float,
    *,
    xtol: float = 1e-7,
    max_iter: int = 100,
) -> float:
    """Pure-Python Brent's root-finding algorithm on a bracketed interval ``[a, b]``."""
    f = fn  # callable(float) -> float
    fa: float = f(a)  # type: ignore[operator]
    fb: float = f(b)  # type: ignore[operator]
    if fa * fb > 0:
        raise ValueError(f"Root not bracketed: f({a})={fa}, f({b})={fb}")
    if abs(fa) < abs(fb):
        a, b = b, a
        fa, fb = fb, fa

    c = a
    fc = fa
    mflag = True
    d = 0.0

    for _ in range(max_iter):
        if abs(fb) <= 1e-11 or abs(b - a) <= xtol:
            return b
        if abs(fa - fc) > 1e-12 and abs(fb - fc) > 1e-12:
            # Inverse quadratic interpolation
            s = (
                a * fb * fc / ((fa - fb) * (fa - fc))
                + b * fa * fc / ((fb - fa) * (fb - fc))
                + c * fa * fb / ((fc - fa) * (fc - fb))
            )
        else:
            # Secant method
            s = b - fb * (b - a) / (fb - fa)

        cond1 = not ((3.0 * a + b) / 4.0 < s < b or b < s < (3.0 * a + b) / 4.0)
        cond2 = mflag and abs(s - b) >= abs(b - c) / 2.0
        cond3 = (not mflag) and abs(s - b) >= abs(c - d) / 2.0
        cond4 = mflag and abs(b - c) < xtol
        cond5 = (not mflag) and abs(c - d) < xtol

        if cond1 or cond2 or cond3 or cond4 or cond5:
            s = 0.5 * (a + b)
            mflag = True
        else:
            mflag = False

        fs: float = f(s)  # type: ignore[operator]
        d = c
        c = b
        fc = fb

        if fa * fs < 0:
            b = s
            fb = fs
        else:
            a = s
            fa = fs

        if abs(fa) < abs(fb):
            a, b = b, a
            fa, fb = fb, fa

    return b


def implied_vol_black76(
    price: float,
    forward: float,
    strike: float,
    t_var: float,
    *,
    is_call: bool,
    t_cal: float | None = None,
    risk_free_rate: float = 0.068,
    vol_low: float = 1e-4,
    vol_high: float = 5.0,
) -> float | None:
    """Solve for Black-76 implied volatility using bracketed Brent's method.

    Returns ``None`` if ``price`` violates no-arbitrage bounds or lies outside ``[1e-4, 5.0]``.
    """
    if price <= 0 or forward <= 0 or strike <= 0 or t_var <= 0:
        return None
    t_disc = t_var if t_cal is None else t_cal
    df = math.exp(-risk_free_rate * max(t_disc, 0.0))
    intrinsic = df * (max(0.0, forward - strike) if is_call else max(0.0, strike - forward))
    upper_bound = df * (forward if is_call else strike)
    if price < intrinsic - 1e-6 or price >= upper_bound:
        return None

    low_px = black76_price(
        forward,
        strike,
        t_var,
        vol_low,
        is_call=is_call,
        t_cal=t_disc,
        risk_free_rate=risk_free_rate,
    )
    high_px = black76_price(
        forward,
        strike,
        t_var,
        vol_high,
        is_call=is_call,
        t_cal=t_disc,
        risk_free_rate=risk_free_rate,
    )
    if price <= low_px:
        return vol_low if abs(price - low_px) < 1e-3 else None
    if price >= high_px:
        return None

    def objective(sigma: float) -> float:
        return (
            black76_price(
                forward,
                strike,
                t_var,
                sigma,
                is_call=is_call,
                t_cal=t_disc,
                risk_free_rate=risk_free_rate,
            )
            - price
        )

    return brentq(objective, vol_low, vol_high)
