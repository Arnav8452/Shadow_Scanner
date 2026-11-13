"""Results iron fly on bhavcopy closes: the trade test (H4) of the event IV crush.

Pre-registered in docs/research/mechanisms.md (14 Nov 2026, commit dd3c26a) before it ran:
sell the ATM call and put at the close of D-1 (the session before a results meeting), buy
wings W away, exit at the close of D+1; nearest expiry after D+1; W is the widest wing whose
max loss (W - credit) x lot is at most the cap; haircut per leg per side; full charges; at
most ``budget`` of summed max loss open at once. Research only, never imported by live code.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

Haircut = Callable[[float], float]  # price -> half-spread paid per side
Charges = Callable[[date, str, float], float]  # (day, "buy"/"sell", turnover) -> rupees


def base_haircut(px: float, mult: float = 1.0) -> float:
    return max(0.20, 0.015 * px) * mult


@dataclass(frozen=True)
class FlyLeg:
    kind: str  # CE / PE
    strike: float
    side: str  # SELL (body) / BUY (wing)
    px_in: float  # close of D-1
    px_out: float  # close of D+1


@dataclass
class FlyTrade:
    symbol: str
    day: date
    before: date
    after: date
    expiry: date
    lot: int
    spot_in: float
    spot_out: float
    atm: float
    wing: float
    legs: list[FlyLeg] = field(default_factory=list)

    def credit(self, hc: Haircut) -> float:
        """Net credit per unit at entry fills (sells at close - haircut, buys at close + it)."""
        return sum(
            (lg.px_in - hc(lg.px_in)) if lg.side == "SELL" else -(lg.px_in + hc(lg.px_in))
            for lg in self.legs
        )

    def max_loss(self, hc: Haircut) -> float:
        return (self.wing - self.credit(hc)) * self.lot

    def exit_cost_close(self) -> float:
        """Cost to close at the D+1 closes, per unit (before the haircut)."""
        return sum(lg.px_out if lg.side == "SELL" else -lg.px_out for lg in self.legs)

    def gross(self, hc: Haircut) -> float:
        """Rupees, entry and exit fills both paying the haircut. An iron fly never costs more
        than its wing to close: closes that say so are stale (deep in-the-money legs trade
        rarely; 14 Nov: LAURUSLABS 1570/1590 calls closed 31.6 apart), so the excess is removed."""
        tot = 0.0
        for lg in self.legs:
            if lg.side == "SELL":
                tot += (lg.px_in - hc(lg.px_in)) - (lg.px_out + hc(lg.px_out))
            else:
                tot += (lg.px_out - hc(lg.px_out)) - (lg.px_in + hc(lg.px_in))
        tot += max(0.0, self.exit_cost_close() - self.wing)
        return tot * self.lot

    def charges(self, charges: Charges, hc: Haircut) -> float:
        tot = 0.0
        for lg in self.legs:
            open_side, close_side = ("sell", "buy") if lg.side == "SELL" else ("buy", "sell")
            p_in = lg.px_in - hc(lg.px_in) if lg.side == "SELL" else lg.px_in + hc(lg.px_in)
            p_out = lg.px_out + hc(lg.px_out) if lg.side == "SELL" else lg.px_out - hc(lg.px_out)
            tot += charges(self.before, open_side, max(0.0, p_in) * self.lot)
            tot += charges(self.after, close_side, max(0.0, p_out) * self.lot)
        return tot

    def net(self, charges: Charges, hc: Haircut) -> float:
        return self.gross(hc) - self.charges(charges, hc)


Quote = Mapping[str, Any]  # close, contracts


def build_fly(
    symbol: str,
    day: date,
    before: date,
    after: date,
    expiry: date,
    lot: int,
    spot_in: float,
    spot_out: float,
    q_in: Mapping[tuple[float, str], Quote],
    q_out: Mapping[tuple[float, str], Quote],
    *,
    cap_inr: float,
    hc: Haircut,
) -> FlyTrade | None:
    """The widest symmetric iron fly whose max loss fits ``cap_inr``; every leg must have traded
    on both days (a stale close voids the event)."""

    def ok(k: float, kind: str) -> bool:
        a, b = q_in.get((k, kind)), q_out.get((k, kind))
        return bool(
            a and b and a.get("contracts", 0) > 0 and b.get("contracts", 0) > 0
            and a["close"] > 0 and b["close"] > 0
        )  # fmt: skip

    strikes = sorted({k for k, _ in q_in})
    body = [k for k in strikes if ok(k, "CE") and ok(k, "PE")]
    if not body or lot <= 0:
        return None
    atm = min(body, key=lambda k: abs(k - spot_in))
    best: FlyTrade | None = None
    for w in sorted({abs(k - atm) for k in strikes if k != atm}):
        if not (ok(atm - w, "PE") and ok(atm + w, "CE")):
            continue
        legs = [
            FlyLeg("CE", atm, "SELL", q_in[(atm, "CE")]["close"], q_out[(atm, "CE")]["close"]),
            FlyLeg("PE", atm, "SELL", q_in[(atm, "PE")]["close"], q_out[(atm, "PE")]["close"]),
            FlyLeg("CE", atm + w, "BUY", q_in[(atm + w, "CE")]["close"],
                   q_out[(atm + w, "CE")]["close"]),
            FlyLeg("PE", atm - w, "BUY", q_in[(atm - w, "PE")]["close"],
                   q_out[(atm - w, "PE")]["close"]),
        ]  # fmt: skip
        t = FlyTrade(symbol, day, before, after, expiry, lot, spot_in, spot_out, atm, w, legs)
        if t.credit(hc) > 0 and 0 < t.max_loss(hc) <= cap_inr:
            best = t  # widest that fits so far (w ascending)
    return best


def select_concurrent(trades: Sequence[FlyTrade], hc: Haircut, budget: float) -> list[FlyTrade]:
    """At most ``budget`` of summed max loss open at once, in date then symbol order."""
    chosen: list[FlyTrade] = []
    for t in sorted(trades, key=lambda x: (x.before, x.symbol)):
        live = [c for c in chosen if c.before <= t.before < c.after or c.before == t.before]
        if sum(c.max_loss(hc) for c in live) + t.max_loss(hc) <= budget:
            chosen.append(t)
    return chosen


def stats(nets: Sequence[float]) -> dict[str, Any]:
    n = len(nets)
    if not n:
        return {"n": 0}
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    sd = statistics.stdev(nets) if n > 1 else 0.0
    return {
        "n": n,
        "win_pct": round(100 * len(wins) / n, 1),
        "total": round(sum(nets), 0),
        "mean": round(statistics.fmean(nets), 1),
        "t": round(statistics.fmean(nets) / (sd / math.sqrt(n)), 2) if sd > 0 else None,
        "avg_win": round(statistics.fmean(wins), 0) if wins else None,
        "avg_loss": round(statistics.fmean(losses), 0) if losses else None,
        "worst": round(min(nets), 0),
        "best": round(max(nets), 0),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
    }


__all__ = ["FlyLeg", "FlyTrade", "base_haircut", "build_fly", "select_concurrent", "stats"]
