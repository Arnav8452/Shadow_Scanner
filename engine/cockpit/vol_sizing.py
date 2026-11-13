"""SPEC 5.7 volatility-managed sizing overlay (Moreira & Muir 2017): trade smaller when recent
realized volatility is high, within hard caps.

- **Factor:** median 20-day realized vol over the last year / the latest 20-day realized vol,
  clamped to [0.5, 1.5]. Daily index closes up to the previous session only (point in time).
- **Use:** it never increases size (the caps in config stay the caps). Below 1, a multi-lot entry
  is cut to ``floor(lots x factor)``, at least 1 lot; below ``skip_below`` (RV20 about 1.7x its
  median) new entries are skipped. Exits are never touched.
- **Underlying:** index options use their own index; stock options use Nifty (market vol).
- **Data:** Upstox daily candles, cached once a day in ``data/state/index_daily.json``. Without
  data (no token, no network, simulation) the factor is 1 and nothing changes.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import statistics
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}


def rolling_rv(closes: Sequence[float], n: int = 20) -> list[float]:
    """Annualised close-to-close realized vol over each window of ``n`` returns, %."""
    rets = [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]
    out = []
    for i in range(n, len(rets) + 1):
        w = rets[i - n : i]
        out.append(math.sqrt(sum(r * r for r in w) / n * 252) * 100)
    return out


@dataclass
class VolSizing:
    closes: dict[str, list[tuple[date, float]]] = field(default_factory=dict)
    rv_days: int = 20
    lookback_days: int = 250
    floor: float = 0.5
    cap: float = 1.5
    skip_below: float = 0.6
    underlying_of: Callable[[str], str] = lambda s: s
    _cache: dict[tuple[str, date], tuple[float, float, float] | None] = field(
        default_factory=dict, repr=False
    )

    def factor(self, underlying: str, day: date) -> tuple[float, float, float] | None:
        """(factor, RV20 now %, median RV20 %) from closes before ``day``; None without data."""
        und = underlying if underlying in self.closes else "NIFTY"
        key = (und, day)
        if key not in self._cache:
            cl = [px for d, px in sorted(self.closes.get(und, [])) if d < day]
            rvs = rolling_rv(cl[-(self.lookback_days + self.rv_days + 1) :], self.rv_days)
            if len(rvs) < 60:
                self._cache[key] = None
            else:
                now, med = rvs[-1], statistics.median(rvs[-self.lookback_days :])
                f = min(self.cap, max(self.floor, med / now)) if now > 0 else 1.0
                self._cache[key] = (round(f, 2), round(now, 1), round(med, 1))
        return self._cache[key]

    def size(self, symbol: str, lots: int, day: date) -> tuple[int, str | None, dict[str, Any]]:
        """(lots to trade, note or None, context). 0 lots = skip the entry."""
        und = self.underlying_of(symbol)
        f = self.factor(und, day)
        if f is None:
            return lots, None, {}
        k, now, med = f
        ctx = {"vol_factor": k, "rv20": now, "rv20_median": med}
        if k < self.skip_below:
            return (
                0,
                (
                    f"vol-managed sizing: {und} RV20 {now:.1f}% vs {med:.1f}% median "
                    f"(factor {k:.2f} < {self.skip_below:g}); entries paused"
                ),
                ctx,
            )
        if k < 1 and lots > 1:
            new = max(1, math.floor(lots * k))
            if new < lots:
                return (
                    new,
                    (
                        f"vol-managed sizing: {lots} -> {new} lots ({und} RV20 {now:.1f}% vs "
                        f"{med:.1f}% median)"
                    ),
                    ctx,
                )
        return lots, None, ctx


def _parse(candles: Sequence[Sequence[Any]]) -> list[tuple[date, float]]:
    return sorted((date.fromisoformat(str(c[0])[:10]), float(c[4])) for c in candles)


def load_daily_closes(
    cache: Path,
    today: date,
    fetch: Callable[[str, date, date], Sequence[Sequence[Any]]] | None,
    keys: Mapping[str, str] = INDEX_KEYS,
) -> dict[str, list[tuple[date, float]]]:
    """Daily closes per index, from the cache if it was written today, else fetched (about
    450 calendar days) and cached. Failures leave that index out (factor 1)."""
    try:
        doc = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    except (OSError, ValueError):
        doc = {}
    if doc.get("as_of") == today.isoformat() or fetch is None:
        data = doc.get("closes") or {}
        return {u: [(date.fromisoformat(d), float(p)) for d, p in v] for u, v in data.items()}
    out: dict[str, list[tuple[date, float]]] = {}
    for und, key in keys.items():
        try:
            out[und] = _parse(fetch(key, today - timedelta(days=450), today - timedelta(days=1)))
        except Exception as exc:  # network, auth: no overlay for this index
            log.warning("vol sizing: no daily candles for %s (%s)", und, type(exc).__name__)
    if out:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {"as_of": today.isoformat(),
                 "closes": {u: [[d.isoformat(), p] for d, p in v] for u, v in out.items()}}
            ),
            encoding="utf-8",
        )  # fmt: skip
    return out


def upstox_daily(token: str) -> Callable[[str, date, date], Sequence[Sequence[Any]]]:
    from engine.cockpit.backfill import _http_json

    def fetch(key: str, frm: date, to: date) -> Sequence[Sequence[Any]]:
        k = urllib.parse.quote(key, safe="")
        url = f"https://api.upstox.com/v3/historical-candle/{k}/days/1/{to.isoformat()}/{frm.isoformat()}"
        doc = _http_json(url, token)
        return list((doc.get("data") or {}).get("candles") or [])

    return fetch


__all__ = ["INDEX_KEYS", "VolSizing", "load_daily_closes", "rolling_rv", "upstox_daily"]
