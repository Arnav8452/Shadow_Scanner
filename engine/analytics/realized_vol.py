"""Realized Volatility Estimators (SPEC §4.4): CC, Parkinson, Garman-Klass, RS, and Yang-Zhang."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

TRADING_DAYS_PER_YEAR = 252.0


@dataclass(frozen=True, slots=True)
class OHLCBar:
    open: float
    high: float
    low: float
    close: float
    prev_close: float | None = None


@dataclass(frozen=True, slots=True)
class RealizedVolSuite:
    close_to_close: float
    parkinson: float
    garman_klass: float
    rogers_satchell: float
    yang_zhang: float
    n_bars: int


def rv_close_to_close(
    bars: Sequence[OHLCBar], *, annualize: float = TRADING_DAYS_PER_YEAR
) -> float:
    if len(bars) < 2:
        return 0.0
    rets: list[float] = []
    for i in range(1, len(bars)):
        prev_c = bars[i].prev_close or bars[i - 1].close
        if prev_c > 0 and bars[i].close > 0:
            rets.append(math.log(bars[i].close / prev_c))
    if len(rets) < 2:
        return 0.0
    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(0.0, var * annualize))


def rv_parkinson(bars: Sequence[OHLCBar], *, annualize: float = TRADING_DAYS_PER_YEAR) -> float:
    valid = [b for b in bars if b.high > 0 and b.low > 0 and b.high >= b.low]
    if not valid:
        return 0.0
    factor = 1.0 / (4.0 * len(valid) * math.log(2.0))
    sq_sum = sum(math.log(b.high / b.low) ** 2 for b in valid)
    return math.sqrt(max(0.0, factor * sq_sum * annualize))


def rv_garman_klass(bars: Sequence[OHLCBar], *, annualize: float = TRADING_DAYS_PER_YEAR) -> float:
    valid = [b for b in bars if b.open > 0 and b.high > 0 and b.low > 0 and b.close > 0]
    if not valid:
        return 0.0
    k = 2.0 * math.log(2.0) - 1.0
    acc = 0.0
    for b in valid:
        hl = math.log(b.high / b.low)
        co = math.log(b.close / b.open)
        acc += 0.5 * hl * hl - k * co * co
    return math.sqrt(max(0.0, (acc / len(valid)) * annualize))


def rv_rogers_satchell(
    bars: Sequence[OHLCBar], *, annualize: float = TRADING_DAYS_PER_YEAR
) -> float:
    valid = [b for b in bars if b.open > 0 and b.high > 0 and b.low > 0 and b.close > 0]
    if not valid:
        return 0.0
    acc = 0.0
    for b in valid:
        hc = math.log(b.high / b.close)
        ho = math.log(b.high / b.open)
        lc = math.log(b.low / b.close)
        lo = math.log(b.low / b.open)
        acc += hc * ho + lc * lo
    return math.sqrt(max(0.0, (acc / len(valid)) * annualize))


def rv_yang_zhang(bars: Sequence[OHLCBar], *, annualize: float = TRADING_DAYS_PER_YEAR) -> float:
    """Minimum-variance unbiased estimator combining overnight jumps, open-to-close, and RS."""
    if len(bars) < 2:
        return rv_garman_klass(bars, annualize=annualize)

    overnight: list[float] = []
    open_to_close: list[float] = []
    rs_terms: list[float] = []

    for i in range(1, len(bars)):
        b = bars[i]
        prev_c = b.prev_close or bars[i - 1].close
        if prev_c <= 0 or b.open <= 0 or b.high <= 0 or b.low <= 0 or b.close <= 0:
            continue
        overnight.append(math.log(b.open / prev_c))
        open_to_close.append(math.log(b.close / b.open))
        rs_terms.append(
            math.log(b.high / b.close) * math.log(b.high / b.open)
            + math.log(b.low / b.close) * math.log(b.low / b.open)
        )

    n = len(overnight)
    if n < 2:
        return rv_garman_klass(bars, annualize=annualize)

    mean_o = sum(overnight) / n
    var_o = sum((x - mean_o) ** 2 for x in overnight) / (n - 1)

    mean_c = sum(open_to_close) / n
    var_c = sum((x - mean_c) ** 2 for x in open_to_close) / (n - 1)

    var_rs = sum(rs_terms) / n
    k = 0.34 / (1.34 + (n + 1.0) / (n - 1.0))
    var_yz = var_o + k * var_c + (1.0 - k) * var_rs
    return math.sqrt(max(0.0, var_yz * annualize))


def compute_realized_vol_suite(bars: Sequence[OHLCBar]) -> RealizedVolSuite:
    return RealizedVolSuite(
        close_to_close=round(rv_close_to_close(bars), 6),
        parkinson=round(rv_parkinson(bars), 6),
        garman_klass=round(rv_garman_klass(bars), 6),
        rogers_satchell=round(rv_rogers_satchell(bars), 6),
        yang_zhang=round(rv_yang_zhang(bars), 6),
        n_bars=len(bars),
    )


def rv_intraday_from_prices(
    prices: Sequence[float],
    *,
    bars_per_session: int = 75,  # e.g. 5-min bars in 375-min session
) -> float:
    """Annualized intraday realized volatility from equally spaced intraday prices."""
    if len(prices) < 2:
        return 0.0
    sq_sum = 0.0
    count = 0
    for i in range(1, len(prices)):
        if prices[i - 1] > 0 and prices[i] > 0:
            r = math.log(prices[i] / prices[i - 1])
            sq_sum += r * r
            count += 1
    if count == 0:
        return 0.0
    session_var = sq_sum * (bars_per_session / count)
    return math.sqrt(max(0.0, session_var * TRADING_DAYS_PER_YEAR))
