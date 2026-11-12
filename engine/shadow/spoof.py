"""Spoofing and wall tracking on a depth feed without order counts (owner's notes, 12 Nov 2026).

A wall is a level holding at least ``wall_mult`` x the median quantity on its side of the
visible book. Each wall is tracked by (side, price) across snapshots; when it loses most of its
peak size we ask whether it was *eaten* (volume traded at or next to its price) or *pulled*
(gone without trades):

- **Phantom cancel ratio** (PCR) = size that vanished without trading / peak size. A pulled
  wall (PCR >= ``pull_pcr``) while price had come close to it is a ``spoof rug-pull``: the side
  that displayed it was bait, so the trade direction is the opposite (a pulled bid wall reads
  bearish).
- A wall that mostly traded away (PCR <= ``eaten_pcr`` and traded volume >= 70% of its peak)
  is an ``eaten wall squeeze``: the side that took it out wins (an eaten ask wall reads
  bullish).
- **Touch vs deep book**: when levels 2-5 lean hard one way (deep imbalance beyond
  ``deep_imb``) while the touch leans the other (beyond ``touch_imb``), the deep wall is
  suspect bait and the book-shape pillar is multiplied by ``bait_multiplier``.

Volume is attributed to a wall when the last trade printed within ``near_ticks`` of its price.
Distances are in ticks so the same rules fit options, futures and stocks. HYPOTHESIS values.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SpoofConfig:
    wall_mult: float = 3.0
    pull_pcr: float = 0.75
    eaten_pcr: float = 0.20
    lost_frac: float = 0.70
    near_ticks: int = 2
    approach_ticks: int = 10
    deep_imb: float = 0.45
    touch_imb: float = 0.35
    bait_multiplier: float = 0.25


@dataclass
class _Wall:
    peak: float
    first_ts: float
    traded_near: float = 0.0


@dataclass
class SpoofTracker:
    cfg: SpoofConfig = field(default_factory=SpoofConfig)
    walls: dict[tuple[str, float], _Wall] = field(default_factory=dict)
    last_cum: float | None = None
    last_event: dict[str, Any] | None = None

    @staticmethod
    def _walls(levels: Any, mult: float) -> dict[float, float]:
        qs = [float(lv[1]) for lv in levels if lv[1] and lv[1] > 0]
        if not qs:
            return {}
        med = max(statistics.median(qs), 1.0)
        return {float(lv[0]): float(lv[1]) for lv in levels if lv[1] and lv[1] >= mult * med}

    def on_depth(
        self, *, ts: float, ltp: float, cum_volume: float, bids: Any, asks: Any, tick: float
    ) -> dict[str, Any]:
        c = self.cfg
        tick = tick or 0.05
        b1 = bids[0] if bids else (0.0, 0.0)
        a1 = asks[0] if asks else (0.0, 0.0)
        b1q, a1q = float(b1[1] or 0), float(a1[1] or 0)
        touch = (b1q - a1q) / max(b1q + a1q, 1.0)
        deep_b = sum(float(lv[1] or 0) for lv in list(bids)[1:5])
        deep_a = sum(float(lv[1] or 0) for lv in list(asks)[1:5])
        deep = (deep_b - deep_a) / max(deep_b + deep_a, 1.0)
        bait_bid = deep > c.deep_imb and touch < -c.touch_imb
        bait_ask = deep < -c.deep_imb and touch > c.touch_imb

        vol = 0.0
        if cum_volume > 0:
            if self.last_cum is not None and cum_volume >= self.last_cum:
                vol = cum_volume - self.last_cum
            self.last_cum = cum_volume
        book = {("BID", float(lv[0])): float(lv[1] or 0) for lv in bids}
        book |= {("ASK", float(lv[0])): float(lv[1] or 0) for lv in asks}
        for side, levels in (("BID", bids), ("ASK", asks)):
            for price, qty in self._walls(levels, c.wall_mult).items():
                w = self.walls.get((side, price))
                if w is None:
                    self.walls[(side, price)] = _Wall(qty, ts)
                else:
                    w.peak = max(w.peak, qty)
        events: list[dict[str, Any]] = []
        for key in list(self.walls):
            side, price = key
            w = self.walls[key]
            if vol > 0 and abs(ltp - price) <= c.near_ticks * tick + 1e-9:
                w.traded_near += vol
            lost = w.peak - book.get(key, 0.0)
            if lost < c.lost_frac * w.peak:
                continue
            if abs(ltp - price) <= c.approach_ticks * tick + 1e-9:
                pulled = max(0.0, lost - w.traded_near)
                pcr = pulled / max(w.peak, 1.0)
                if pcr >= c.pull_pcr:
                    events.append({
                        "pattern": "spoof rug-pull", "side": side, "price": price,
                        "peak": w.peak, "pcr": round(pcr, 2),
                        "direction": "SHORT" if side == "BID" else "LONG",
                    })  # fmt: skip
                elif pcr <= c.eaten_pcr and w.traded_near >= 0.70 * w.peak:
                    events.append({
                        "pattern": "eaten wall squeeze", "side": side, "price": price,
                        "peak": w.peak, "pcr": round(pcr, 2), "eaten": w.traded_near,
                        "direction": "SHORT" if side == "BID" else "LONG",
                    })  # fmt: skip
            del self.walls[key]
        if events:
            self.last_event = {**events[0], "ts": ts}
        return {
            "touch_imbalance": round(touch, 2),
            "deep_imbalance": round(deep, 2),
            "bait": "FAKE_BID_BAIT" if bait_bid else "FAKE_ASK_BAIT" if bait_ask else "CLEAN",
            "book_multiplier": c.bait_multiplier if (bait_bid or bait_ask) else 1.0,
            "events": events,
        }
