"""Structural order-flow zones on the front future (15-60 minute institutional footprints).

Not a scalping signal: it aggregates the futures tape into price zones (``zone_pts`` wide, e.g.
10 Nifty points) over a rolling ``window_min`` and asks, per zone, whether far more aggressive
volume traded there than was ever shown at the touch (a reloading passive order, an
"iceberg"), and whether price has since left the zone so the aggressive side is trapped:

- **bullish floor**: aggressive selling (trades at the bid) of at least ``iceberg_ratio`` x the
  largest bid ever displayed in the zone, over at least ``min_battle_min`` minutes, net delta
  negative, and price now above the zone (sellers underwater);
- **bearish ceiling**: the mirror image with aggressive buying absorbed by offers.

Trade side is read from each sample (the cockpit's step, ~0.1-0.2 s): volume added since the
last sample is a buy if the last trade printed at or above the ask, a sell at or below the bid,
otherwise by which side of the mid it printed. This is coarser than tick-by-tick data, so the
zones are context and a confirmation input (shown on the page, stored with every trade for
Research), not an entry trigger. Thresholds are HYPOTHESES (12 Nov 2026 review notes).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from engine.cockpit.tape import trade_side


@dataclass(frozen=True)
class ZoneConfig:
    enabled: bool = True
    window_min: float = 30.0
    min_battle_min: float = 8.0
    iceberg_ratio: float = 12.0
    zone_pts: tuple[tuple[str, float], ...] = (("NIFTY", 10.0), ("SENSEX", 30.0))

    def width(self, und: str) -> float:
        return dict(self.zone_pts).get(und, 10.0)


@dataclass
class _Sample:
    t: datetime
    zone: float
    buy: float
    sell: float
    bid_q: float
    ask_q: float


@dataclass
class _Tape:
    samples: deque[_Sample] = field(default_factory=deque)
    last_cum: float | None = None
    prev_bid: float = 0.0
    prev_ask: float = 0.0
    prev_px: float = 0.0


class ZoneTracker:
    def __init__(self, cfg: ZoneConfig) -> None:
        self.cfg = cfg
        self.tapes: dict[str, _Tape] = {}

    def on_sample(
        self,
        und: str,
        now: datetime,
        *,
        ltp: float,
        cum_volume: float,
        bid: float,
        ask: float,
        bid_qty: float,
        ask_qty: float,
    ) -> None:
        if not self.cfg.enabled or ltp <= 0:
            return
        tape = self.tapes.setdefault(und, _Tape())
        vol = 0.0
        if cum_volume > 0:
            if tape.last_cum is not None and cum_volume >= tape.last_cum:
                vol = cum_volume - tape.last_cum
            tape.last_cum = cum_volume
        w = self.cfg.width(und)
        zone = math.floor(ltp / w) * w
        # Against the quote before the trade (see engine.cockpit.tape.trade_side).
        buy, sell = trade_side(vol, ltp, tape.prev_bid or bid, tape.prev_ask or ask, tape.prev_px)
        tape.prev_bid, tape.prev_ask, tape.prev_px = bid, ask, ltp
        # Displayed size only counts for the zone the touch sits in.
        tape.samples.append(_Sample(now, zone, buy, sell, bid_qty, ask_qty))
        cutoff = now - timedelta(minutes=self.cfg.window_min)
        while tape.samples and tape.samples[0].t < cutoff:
            tape.samples.popleft()

    def zones(self, und: str, spot_like: float) -> dict[str, Any]:
        """Zones with a sustained absorption battle, and which of them trap a side now."""
        tape = self.tapes.get(und)
        if tape is None or not tape.samples:
            return {"floors": [], "ceilings": [], "zones": 0}
        w = self.cfg.width(und)
        agg: dict[float, dict[str, Any]] = {}
        for s in tape.samples:
            z = agg.setdefault(
                s.zone,
                {
                    "buy": 0.0,
                    "sell": 0.0,
                    "max_bid": 1.0,
                    "max_ask": 1.0,
                    "first": s.t,
                    "last": s.t,
                },
            )
            z["buy"] += s.buy
            z["sell"] += s.sell
            z["max_bid"] = max(z["max_bid"], s.bid_q)
            z["max_ask"] = max(z["max_ask"], s.ask_q)
            z["last"] = s.t
        floors, ceilings = [], []
        for zp, z in agg.items():
            mins = (z["last"] - z["first"]).total_seconds() / 60.0
            if mins < self.cfg.min_battle_min:
                continue
            net = z["buy"] - z["sell"]
            bid_ratio = z["sell"] / z["max_bid"]
            ask_ratio = z["buy"] / z["max_ask"]
            if bid_ratio >= self.cfg.iceberg_ratio and net < 0 and spot_like > zp + w + w / 2:
                floors.append(
                    {
                        "low": zp,
                        "high": zp + w,
                        "ratio": round(bid_ratio, 1),
                        "trapped_volume": round(z["sell"]),
                        "minutes": round(mins, 1),
                        "stop_below": zp - w / 2,
                    }
                )
            if ask_ratio >= self.cfg.iceberg_ratio and net > 0 and spot_like < zp - w / 2:
                ceilings.append(
                    {
                        "low": zp,
                        "high": zp + w,
                        "ratio": round(ask_ratio, 1),
                        "trapped_volume": round(z["buy"]),
                        "minutes": round(mins, 1),
                        "stop_above": zp + w + w / 2,
                    }
                )
        floors.sort(key=lambda x: -x["trapped_volume"])
        ceilings.sort(key=lambda x: -x["trapped_volume"])
        return {"floors": floors[:3], "ceilings": ceilings[:3], "zones": len(agg)}

    def view(self, prices: dict[str, float]) -> dict[str, Any]:
        c = self.cfg
        return {
            "rules": (
                f"{c.window_min:g}-min window; a zone needs ≥ {c.min_battle_min:g} min of trading "
                f"and aggressive volume ≥ {c.iceberg_ratio:g}x the largest size ever shown at the "
                "touch there; a floor traps sellers once price is above it, a ceiling traps buyers "
                "once price is below it. HYPOTHESIS; context only."
            ),
            "by_underlying": {u: self.zones(u, p) for u, p in prices.items()},
        }
