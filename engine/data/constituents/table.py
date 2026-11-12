"""Index constituents from ``config/reference/<index>_constituents.csv`` (point in time).

Membership comes from NSE's published list (``status = primary``). Weights are only filled where
known; rows whose weights have no source are ``weight_status = UNVERIFIED``; weights estimated from
free-float market cap are ``derived`` (``engine.data.constituents.weights``). Neither counts as
verified, and the analytics that use them say so. Earlier membership history is not loaded, so queries before a row's
``effective_from`` return nothing rather than today's list (no survivorship bias).
"""

from __future__ import annotations

import csv
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from engine.data.constituents.nifty_members import IndexConstituent


@dataclass(frozen=True, slots=True)
class ConstituentRow:
    index: str
    symbol: str
    isin: str
    industry: str
    weight_pct: float | None
    weight_verified: bool
    effective_from: date
    effective_to: date | None
    source_url: str
    weight_status: str = ""  # "" verified | derived | UNVERIFIED

    def active(self, on: date) -> bool:
        return self.effective_from <= on and (self.effective_to is None or on < self.effective_to)


def load_constituent_table(path: Path) -> list[ConstituentRow]:
    rows: list[ConstituentRow] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            w = (r.get("weight_pct") or "").strip()
            rows.append(
                ConstituentRow(
                    index=r["index"].strip().upper(),
                    symbol=r["symbol"].strip(),
                    isin=r["isin"].strip(),
                    industry=(r.get("industry") or "").strip(),
                    weight_pct=float(w) if w else None,
                    # "derived" (our free-float estimate) and UNVERIFIED are not verified;
                    # only an empty status (checked against the NSE factsheet) is.
                    weight_verified=bool(w)
                    and (r.get("weight_status") or "").strip() not in ("UNVERIFIED", "derived"),
                    effective_from=date.fromisoformat(r["effective_from"].strip()),
                    effective_to=(
                        date.fromisoformat(r["effective_to"].strip())
                        if (r.get("effective_to") or "").strip()
                        else None
                    ),
                    source_url=(r.get("source_url") or "").strip(),
                    weight_status=(r.get("weight_status") or "").strip(),
                )
            )
    return rows


def members_on(rows: Sequence[ConstituentRow], index: str, on: date) -> list[ConstituentRow]:
    return [r for r in rows if r.index == index.upper() and r.active(on)]


def heavyweights(
    rows: Sequence[ConstituentRow], index: str, on: date, n: int
) -> list[ConstituentRow]:
    """The ``n`` largest members by weight on ``on`` (members without a weight are skipped)."""
    weighted = [r for r in members_on(rows, index, on) if r.weight_pct is not None]
    weighted.sort(key=lambda r: r.weight_pct or 0.0, reverse=True)
    return weighted[:n]


def broker_key(row: ConstituentRow, broker: str) -> str:
    if broker == "upstox":
        return f"NSE_EQ|{row.isin}"
    if broker == "fyers":
        return f"NSE:{row.symbol}-EQ"
    if broker == "sim":
        return f"NSE:{row.symbol}-EQ"
    raise ValueError(f"no equity key format for broker {broker!r}")


def as_index_constituents(rows: Sequence[ConstituentRow]) -> list[IndexConstituent]:
    return [
        IndexConstituent(r.symbol, r.industry or "?", r.weight_pct or 0.0, r.effective_from)
        for r in rows
    ]


def log_returns(prices: Sequence[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(prices, prices[1:], strict=False) if a > 0 and b > 0]


def _corr(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = min(len(x), len(y))
    if n < 10:
        return None
    x, y = x[-n:], y[-n:]
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=False)) / math.sqrt(sxx * syy)


def weighted_average_correlation(
    returns: Mapping[str, Sequence[float]], weights: Mapping[str, float]
) -> tuple[float | None, int]:
    """Weight-pair-averaged pairwise correlation of members' returns (realized correlation).

    High (near 1): members move together, an index move is broad (macro). Low: moves are
    stock-specific and tend to offset (dispersion). Returns (value, pairs used).
    """
    syms = [s for s in returns if weights.get(s, 0.0) > 0]
    num = den = 0.0
    pairs = 0
    for i, a in enumerate(syms):
        for b in syms[i + 1 :]:
            c = _corr(returns[a], returns[b])
            if c is None:
                continue
            w = weights[a] * weights[b]
            num += w * c
            den += w
            pairs += 1
    return (round(num / den, 3) if den > 0 else None), pairs
