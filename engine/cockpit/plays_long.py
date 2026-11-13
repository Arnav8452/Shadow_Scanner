"""Long-option playbook strategies from the owner's notes (``new/``, 12 Nov 2026). Paper only.

Short holds are allowed here only through the owner's charges rule: an entry needs the
expected 3-minute option move to clear ``impulse_mult`` x the charges breakeven (Gate 1), and
exits use the breakeven ratchet with the 180-s scratch (Gate 2). All thresholds HYPOTHESES.

- ``PutPanicPlay`` (spec Scalp 1): index breaks the 30-min opening-range low or rejects VWAP,
  futures CVD z <= -2.5, ATM put IV up > 0.4 pts in 3 min -> 0.55-0.60 delta put, 2-5 days to
  expiry; stop 20% of premium (rupee cap), not +10% in 12 min -> out.
- ``OiIgnitionPlay`` (spec Scalp 2 + breaking_retail "second mouse"): index crosses the
  highest-OI strike while that strike's OI falls > 3% in 6 min, 1-min futures volume > 2x and
  CVD rising; optional retest entry (pullback to the strike within 3 min with opposite-side OI
  building and the option above its VWAP); absorption and heavyweight filters; after +25% the
  stop trails the option's 1-min 9-EMA.
- ``ZeroDtePlay`` (spec Scalp 3 + breaking_retail): expiry day; 11:00-13:30 the index stays
  inside ATM strike +- straddle; 13:30-14:30 a 1-min close outside on > 3x futures volume ->
  2 lots ATM in that direction, about Rs 11 per unit at risk, exit at delta 0.90 or 14:45;
  skipped when 0DTE IV rises while next-expiry IV falls.
- ``BreakthroughPlay`` (rules.md 5-pillar score): order book (decay-weighted OBI), volume
  delta and relative volume, writer pain (option vs its 9:20 / VWAP anchor) with OI unwinding,
  gamma impulse vs breakeven, IV velocity; 20/25/25/15/15 weights; score x0.4 unless the
  impulse clears 2.5x breakeven. 65-79 -> 1 lot, 80+ -> 2 lots with an EMA trail.
- ``SpoofFadePlay`` (spoofing notes): a pulled (spoofed) wall or an eaten wall on an index
  future or option -> trade the side it reveals, 1 lot, short-hold gates.
- ``ZoneRiderPlay`` (ride_wave.md): a futures absorption floor (or ceiling) cleared by 10 pts
  with 5-min CVD agreeing, or a 30-min CVD absorption divergence -> 0.60 delta option with >= 5
  days left; stop when the index crosses back through the zone; hold to the square-off.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from engine.cockpit.gates import absorption, heavyweights_aligned, off_cycle
from engine.cockpit.plays_base import LongPlay, Market, fmt, hhmm
from engine.core.rules import IST


def _pick(
    m: Market, und: str, kind: str, delta: float, min_days: int = 0
) -> tuple[str, dict[str, Any]] | None:
    """Contract near ``delta`` on the nearest expiry with at least ``min_days`` left."""
    rows = None
    if min_days > 0:
        today = m.now.astimezone(IST).date()
        for exp, t in sorted(m.tables(und).items()):
            from datetime import date as _d

            if (_d.fromisoformat(exp) - today).days >= min_days:
                rows = t.get("rows")
                break
        if rows is None:
            return None
    return m.by_delta(und, kind, delta, rows)


# ------------------------------------------------------------------------------ scalp 1


@dataclass
class PutPanicPlay(LongPlay):
    scalp: bool = True
    window: tuple[str, str] = ("09:45", "14:00")
    cvd_z: float = -2.5
    iv_rise: float = 0.4
    delta: float = 0.575
    min_days: int = 2
    impulse_mult: float = 3.5
    stop_pct: float = 20.0
    max_risk_inr: float = 1500.0

    def look(self, now: datetime, m: Market) -> list[str] | None:
        if not self.in_window(now, self.window):
            return None
        if not off_cycle(now):
            self.note("no entries in the first 45 s of a round minute")
            return None
        out = []
        for und in m.underlyings():
            if any(mg.und == und for mg in self.managed.values()):
                continue
            t = m.tape(und)
            bars, orng, vw = t.done(), t.opening_range(30), t.vwap()
            if len(bars) < 2 or orng is None:
                self.note(f"{und}: waiting for the 30-min opening range")
                continue
            last = bars[-1]
            broke = last.close < orng[1] <= bars[-2].close
            rejected = vw is not None and last.high >= vw > last.close and bars[-2].close < vw
            z, ivc = t.cvd_z(), t.iv_change("pe_atm", 3)
            self.note(
                f"{und}: no breakdown yet (index {fmt(last.close, 0)}, OR low {fmt(orng[1], 0)}, "
                f"VWAP {fmt(vw, 0)}); CVD z {fmt(z)} (needs <= {self.cvd_z:g}), "
                f"put IV {fmt(ivc, 2, True)} in 3 min (needs > +{self.iv_rise:g})"
            )
            if not (broke or rejected):
                continue
            if z is None or z > self.cvd_z or ivc is None or ivc <= self.iv_rise:
                self.renote(
                    f"{und}: {'OR-low break' if broke else 'VWAP rejection'}, but CVD z {fmt(z)} "
                    f"(needs <= {self.cvd_z:g}) / put IV {fmt(ivc, 2, True)} (needs > "
                    f"+{self.iv_rise:g})"
                )
                continue
            pick = _pick(m, und, "PE", self.delta, self.min_days)
            if pick is None:
                self.renote(f"{und}: trigger met, but no {self.min_days}+ day put near 0.575 delta")
                continue
            ok, why, _, _ = m.scalp_gate(pick[0], m.lot(pick[0]), self.impulse_mult, und)
            if not ok:
                self.renote(f"{und}: trigger met, charges gate refused ({why})")
                continue
            bk = m.book(pick[0])
            ask = bk.asks[0][0] if bk is not None and bk.asks else 0.0
            # 20% of premium, but never more than max_risk_inr on the lot.
            per_unit = min(self.max_risk_inr / m.lot(pick[0]), ask * self.stop_pct / 100)
            why_txt = "OR low" if broke else "VWAP reject"
            out.append(
                self.enter(
                    m,
                    und,
                    pick[0],
                    lots=1,
                    bullish=False,
                    stop_inr=per_unit or None,
                    reason=f"{und} put panic ({why_txt}, CVD z {z:.1f}, put IV +{ivc:.1f})",
                    ratchet=True,
                    time_stop=(12, 10),
                    candle_stop_min=15,
                )
            )
        return out


# ------------------------------------------------------------------------------ scalp 2


@dataclass
class OiIgnitionPlay(LongPlay):
    scalp: bool = True
    window: tuple[str, str] = ("09:30", "14:30")
    near_pts: float = 15.0
    oi_drop_pct: float = 3.0
    vol_mult: float = 2.0
    delta: float = 0.60  # protocol band 0.55-0.65
    impulse_mult: float = 3.5
    second_mouse: bool = True
    armed: dict[str, dict[str, Any]] = field(default_factory=dict)

    def look(self, now: datetime, m: Market) -> list[str] | None:
        if not self.in_window(now, self.window):
            return None
        out = []
        for und in m.underlyings():
            if any(mg.und == und for mg in self.managed.values()):
                continue
            t = m.tape(und)
            bars = t.done()
            rows = m.chain_rows(und)
            if not rows:
                self.note(f"{und}: waiting for the 1-min option chain (recorder)")
                continue
            if len(bars) < 22:
                self.note(f"{und}: building the tape ({len(bars)}/22 one-minute bars)")
                continue
            last, prev = bars[-1], bars[-2]
            call_k = max(rows, key=lambda r: (r.get("ce") or {}).get("oi") or 0)["k"]
            put_k = max(rows, key=lambda r: (r.get("pe") or {}).get("oi") or 0)["k"]
            rv = t.rvol()
            slope = sum(b.delta for b in bars[-3:])
            arm = self.armed.get(und)
            if arm is not None:  # second-mouse retest
                secs = (now - arm["t"]).total_seconds()
                self.note(
                    f"{und}: ignition through {arm['k']:g}; waiting for a retest that holds "
                    f"({secs:.0f} of 180 s)"
                )
                out += self._retest(now, m, und, arm, last)
                continue
            self.note(
                f"{und}: no wall crossed (index {fmt(last.close, 0)}; call wall {float(call_k):g}, "
                f"put wall {float(put_k):g}); needs a cross with that OI -{self.oi_drop_pct:g}% "
                f"in 6 min on {self.vol_mult:g}x volume (now {fmt(rv)}x)"
            )
            for bull, k, kind in ((True, float(call_k), "CE"), (False, float(put_k), "PE")):
                crossed = prev.close < k <= last.close if bull else prev.close > k >= last.close
                if not crossed or abs(last.close - k) > self.near_pts * 2:
                    continue
                d = t.oi_change_pct(k, kind, 6)
                if d is None or d > -self.oi_drop_pct or rv is None or rv < self.vol_mult:
                    self.renote(
                        f"{und} crossed {k:g}: OI {fmt(d, 1, True)}% in 6 min (needs "
                        f"-{self.oi_drop_pct:g}%), volume {fmt(rv)}x (needs {self.vol_mult:g}x)"
                    )
                    continue
                if (slope > 0) != bull:
                    self.renote(f"{und} crossed {k:g}, but CVD is moving the other way")
                    continue
                trap = absorption(last, rv, t.atr(), bull)
                hw = heavyweights_aligned(m.day_returns(), bull) if und == "NIFTY" else None
                if trap or hw is False:
                    self.renote(f"{und} crossed {k:g}: {trap or 'heavyweights not aligned'}")
                    continue
                if self.second_mouse:
                    opp = "PE" if bull else "CE"
                    self.armed[und] = {
                        "k": k,
                        "bull": bull,
                        "t": now,
                        "opp": opp,
                        "opp_oi": (t.oi[-1][1].get((k, opp)) if t.oi else None),
                    }
                    self.renote(f"{und}: ignition at {k:g}; waiting up to 3 min for the retest")
                else:
                    out.append(self._buy(m, und, bull, f"{und} OI ignition through {k:g}"))
        return out

    def _retest(
        self, now: datetime, m: Market, und: str, arm: dict[str, Any], last: Any
    ) -> list[str]:
        if (now - arm["t"]).total_seconds() > 180:
            del self.armed[und]
            return []
        if (now - arm["t"]).total_seconds() < 60:
            return []
        t = m.tape(und)
        atr = t.atr() or 0.0
        k, bull = arm["k"], arm["bull"]
        touched = last.low <= k + 0.25 * atr if bull else last.high >= k - 0.25 * atr
        opp_now = t.oi[-1][1].get((k, arm["opp"])) if t.oi else None
        building = arm["opp_oi"] is not None and opp_now is not None and opp_now > arm["opp_oi"]
        pick = _pick(m, und, "CE" if bull else "PE", self.delta)
        above_vwap = False
        if pick is not None:
            vw = m.otape(pick[0]).vwap()
            b = m.book(pick[0])
            above_vwap = bool(vw and b and b.bids and b.bids[0][0] >= vw)
        if touched and building and above_vwap:
            del self.armed[und]
            return [self._buy(m, und, bull, f"{und} retest of {k:g} held (opposite OI building)")]
        return []

    def _buy(self, m: Market, und: str, bull: bool, reason: str) -> str:
        pick = _pick(m, und, "CE" if bull else "PE", self.delta)
        if pick is None:
            return f"{self.title}: no {self.delta} delta option"
        ok, why, _, _ = m.scalp_gate(pick[0], m.lot(pick[0]), self.impulse_mult, und)
        if not ok:
            self.renote(f"{und}: {reason}, charges gate refused ({why})")
            return f"{self.title}: {reason}, refused by charges gate ({why})"
        return self.enter(
            m,
            und,
            pick[0],
            lots=1,
            bullish=bull,
            reason=reason,
            ratchet=True,
            ema_after_pct=25,
            candle_stop_min=15,
        )


# ------------------------------------------------------------------------------ scalp 3


@dataclass
class ZeroDtePlay(LongPlay):
    band_from: str = "11:00"
    band_to: str = "13:30"
    last_entry: str = "14:30"
    vol_mult: float = 3.0
    lots: int = 2
    risk_per_unit: float = 11.0
    impulse_mult: float = 3.5
    bands: dict[str, dict[str, Any]] = field(default_factory=dict)

    def look(self, now: datetime, m: Market) -> list[str] | None:
        out = []
        h = hhmm(now)
        for und in m.underlyings():
            if any(mg.und == und for mg in self.managed.values()):
                continue
            if not m.expiry_today(und):
                nxt_exp = min(m.tables(und), default=None)
                self.note(
                    f"{und}: expiry days only" + (f" (next expiry {nxt_exp})" if nxt_exp else "")
                )
                continue
            t = m.tape(und)
            bd = self.bands.setdefault(und, {"inside": True, "band": None, "done": False})
            if h < self.band_from:
                self.note(f"{und}: expiry day; the pin band forms {self.band_from}-{self.band_to}")
                continue
            if self.band_from <= h < self.band_to:
                strad = t.bars[-1].straddle if t.bars else 0.0
                atm = t.atm_strike
                if strad and atm:
                    bd["band"] = (atm - strad, atm + strad)
                    if bd["band"] and not bd["band"][0] <= m.spot(und) <= bd["band"][1]:
                        bd["inside"] = False
                band = bd["band"]
                if band is None:
                    self.note(f"{und}: waiting for the ATM straddle")
                else:
                    inside = "index inside so far" if bd["inside"] else "index left it: no pin"
                    self.note(f"{und}: pin band {fmt(band[0], 0)}-{fmt(band[1], 0)}, {inside}")
                continue
            if bd["done"]:
                self.note(f"{und}: done for this expiry")
                continue
            if h > self.last_entry or not bd["band"]:
                self.note(
                    f"{und}: entries {self.band_to}-{self.last_entry}"
                    + ("" if bd["band"] else "; no pin band was formed")
                )
                continue
            if not bd["inside"]:
                self.note(f"{und}: index left the straddle band before 13:30, no pin")
                continue
            bars = t.done()
            if not bars:
                continue
            last = bars[-1]
            lo, hi = bd["band"]
            up, down = last.close > hi, last.close < lo
            rv = t.rvol()
            self.note(
                f"{und}: waiting for a 1-min close outside {fmt(lo, 0)}-{fmt(hi, 0)} on "
                f"{self.vol_mult:g}x volume (last close {fmt(last.close, 0)}, {fmt(rv)}x)"
            )
            if not (up or down) or rv is None or rv < self.vol_mult:
                continue
            near, nxt = t.iv_change("atm", 3), t.iv_change("next_atm", 3)
            if near is not None and nxt is not None and near > 0 > nxt:
                self.renote(f"{und}: 0DTE IV up while next-expiry IV falls, retail being faded")
                bd["done"] = True
                continue
            pick = m.by_delta(und, "CE" if up else "PE", 0.5)
            if pick is None:
                continue
            ok, why, _, _ = m.scalp_gate(
                pick[0], m.lot(pick[0]) * self.lots, self.impulse_mult, und, delta_band=None
            )
            if not ok:
                self.renote(f"{und}: break seen, charges gate refused ({why})")
                continue
            bd["done"] = True
            out.append(
                self.enter(
                    m,
                    und,
                    pick[0],
                    lots=self.lots,
                    bullish=up,
                    stop_inr=self.risk_per_unit,
                    reason=f"{und} 0DTE break {'above' if up else 'below'} straddle band "
                    f"{lo:,.0f}-{hi:,.0f} on {rv:.1f}x volume",
                    delta_exit=0.90,
                )
            )
        return out


def vacuum_score(
    m: Market, und: str, sym: str, bull: bool, charges_mult: float = 2.5
) -> dict[str, Any] | None:
    """rules.md 5-pillar confidence (the protocol's high-velocity vacuum score, 0-100):
        book 20, volume delta 25, writer pain + OI unwind 25, gamma impulse vs breakeven 15, IV
    velocity 15; x0.4 unless the expected impulse clears ``charges_mult`` x breakeven."""
    t = m.tape(und)
    b = m.book(sym)
    g = m.detail(sym)
    if b is None or not b.bids or not b.asks or not g:
        return None
    w = [math.exp(-0.3 * i) for i in range(min(5, len(b.bids), len(b.asks)))]
    wb = sum(x * b.bids[i][1] for i, x in enumerate(w))
    wa = sum(x * b.asks[i][1] for i, x in enumerate(w))
    obi = (wb - wa) / (wb + wa + 1e-9)
    s_ob = min(max((obi + 0.1) / 0.6, 0), 1) * 100
    z = t.cvd_z() or 0.0
    z = z if bull else -z
    rv = t.rvol() or 1.0
    s_vol = (0.65 * min(max(z / 3, 0), 1) + 0.35 * min(max((rv - 1) / 2.5, 0), 1)) * 100
    ot = m.otape(sym)
    anchor = max(ot.open_920 or 0.0, ot.vwap() or 0.0)
    ltp = (b.bids[0][0] + b.asks[0][0]) / 2
    pain = (ltp - anchor) / anchor if anchor else 0.0
    k = float(m.core.master.get(sym).strike) if sym in m.core.master else 0.0
    kind = "CE" if bull else "PE"
    oi5 = t.oi_change_pct(k, kind, 5) or 0.0
    s_oi = (0.5 * min(max((pain - 0.10) / 0.25, 0), 1) + 0.5 * min(max(-oi5 / 4, 0), 1)) * 100
    qty = m.lot(sym)
    _, be = m.breakeven(b.asks[0][0], qty)
    atr = t.atr() or 0.0
    move = atr * math.sqrt(3)
    imp = abs(g.get("delta") or 0) * move + 0.5 * abs(g.get("gamma") or 0) * move * move
    ratio = imp / max(be, 0.5)
    s_gr = min(max((ratio - 1.5) / 2.5, 0), 1) * 100
    ivc = t.iv_change("ce_atm" if bull else "pe_atm", 3) or 0.0
    s_iv = min(max((ivc + 0.1) / 0.6, 0), 1) * 100
    conf = 0.20 * s_ob + 0.25 * s_vol + 0.25 * s_oi + 0.15 * s_gr + 0.15 * s_iv
    passes = ratio >= charges_mult
    # Where 25% / 30% writer stops sit in index terms (delta-gamma inversion).
    clusters = {}
    d_, gm = max(abs(g.get("delta") or 0), 0.05), max(abs(g.get("gamma") or 0), 1e-6)
    for pct in (25, 30):
        dp = anchor * (1 + pct / 100) - ltp if anchor else 0.0
        ds = (-d_ + math.sqrt(d_ * d_ + 2 * gm * dp)) / gm if dp > 0 else 0.0
        spot = m.spot(und)
        clusters[f"sl_{pct}"] = round(spot + ds if bull else spot - ds, 1) if spot else None
    return {
        "symbol": sym,
        "label": m.core.label(sym),
        "score": round(conf if passes else conf * 0.4, 1),
        "passes_charges_gate": passes,
        "sub": {
            "book": round(s_ob),
            "volume": round(s_vol),
            "oi": round(s_oi),
            "gamma": round(s_gr),
            "iv": round(s_iv),
        },
        "writer_pain_pct": round(pain * 100, 1),
        "impulse_pts": round(imp, 2),
        "breakeven_pts": be,
        **clusters,
    }


# ------------------------------------------------------------------------------ rules.md


@dataclass
class BreakthroughPlay(LongPlay):
    scalp: bool = True
    window: tuple[str, str] = ("09:30", "14:30")
    delta: float = 0.60  # protocol band 0.55-0.65
    min_score: float = 65.0
    two_lot_score: float = 80.0
    # 13 Nov 2026 (owner): the writer-pain + OI-unwinding pillar is the thesis; a score made of
    # book, volume and the direction-blind gamma pillar alone does not qualify.
    min_oi_pillar: float = 30.0
    charges_mult: float = 2.5
    last: dict[str, Any] = field(default_factory=dict)

    def score(self, m: Market, und: str, sym: str, bull: bool) -> dict[str, Any] | None:
        return vacuum_score(m, und, sym, bull, self.charges_mult)

    def look(self, now: datetime, m: Market) -> list[str] | None:
        if not self.in_window(now, self.window):
            return None
        if not off_cycle(now):
            self.note("no entries in the first 45 s of a round minute")
            return None
        out = []
        for und in m.underlyings():
            if any(mg.und == und for mg in self.managed.values()):
                continue
            best = None
            for bull, kind in ((True, "CE"), (False, "PE")):
                pick = m.by_delta(und, kind, self.delta)
                if pick is None:
                    continue
                s = self.score(m, und, pick[0], bull)
                if s is not None and (best is None or s["score"] > best[0]["score"]):
                    best = (s, bull)
            if best is None:
                self.note(f"{und}: no ~{self.delta:g} delta option with a book and greeks yet")
                continue
            self.last[und] = best[0] | {"direction": "UP" if best[1] else "DOWN"}
            s, bull = best
            self.note(
                f"{und}: best score {s['score']:.0f} {'UP' if bull else 'DOWN'} (needs "
                f"{self.min_score:g}; {self.two_lot_score:g}+ = 2 lots)"
                + ("" if s["passes_charges_gate"] else ", x0.4: impulse under 2.5x charges")
            )
            if s["score"] < self.min_score:
                continue
            if s["sub"]["oi"] < self.min_oi_pillar:
                self.renote(
                    f"{und}: score {s['score']:.0f}, but writer pain + OI unwinding is "
                    f"{s['sub']['oi']} (needs {self.min_oi_pillar:g})"
                )
                continue
            d = abs(float(m.detail(s["symbol"]).get("delta") or 0))
            if not 0.55 <= d <= 0.65:
                self.renote(
                    f"{und}: score {s['score']:.0f}, but delta {d:.2f} is outside 0.55-0.65"
                )
                continue
            t = m.tape(und)
            bars = t.done()
            trap = absorption(bars[-1], t.rvol(), t.atr(), bull) if bars else None
            if trap:
                self.renote(
                    f"{und}: score {s['score']:.0f}, vetoed by the absorption filter ({trap})"
                )
                continue
            lots = 2 if s["score"] >= self.two_lot_score else 1
            out.append(
                self.enter(
                    m,
                    und,
                    s["symbol"],
                    lots=lots,
                    bullish=bull,
                    ratchet=True,
                    ema_after_pct=25 if lots == 2 else None,
                    reason=f"{und} breakthrough {s['score']:.0f} ({'UP' if bull else 'DOWN'})",
                )
            )
        return out

    def view(self) -> dict[str, Any]:
        return super().view() | {"scores": self.last}


# ------------------------------------------------------------------------------ spoofing


@dataclass
class SpoofFadePlay(LongPlay):
    scalp: bool = True
    window: tuple[str, str] = ("09:30", "14:30")
    delta: float = 0.60  # protocol band 0.55-0.65
    impulse_mult: float = 3.5
    min_confidence: float = 65.0  # protocol rule 2: the high-velocity vacuum score
    seen: set[tuple[str, float]] = field(default_factory=set)
    last_seen: str = ""

    def look(self, now: datetime, m: Market) -> list[str] | None:
        if not self.in_window(now, self.window):
            return None
        if not off_cycle(now):
            self.note("no entries in the first 45 s of a round minute")
            return None
        out = []
        self.note(
            f"trades a pulled (spoofed) or eaten wall within 5 s of it when the vacuum score is "
            f"{self.min_confidence:g}+, the move clears {self.impulse_mult:g}x charges and no "
            "absorption shows; "
            + (f"last: {self.last_seen}" if self.last_seen else "none seen yet today")
        )
        sc = m.runner.scanner
        for sym in list(getattr(m.runner, "subscribed", [])):
            ev = sc.spoof_view(sym) if hasattr(sc, "spoof_view") else None
            if not ev or now.timestamp() - float(ev["ts"]) > 5 or (sym, ev["ts"]) in self.seen:
                continue
            self.seen.add((sym, ev["ts"]))
            if sym not in m.core.master:
                continue
            self.last_seen = f"{ev['pattern']} on {m.core.label(sym)} at {hhmm(now)}"
            ins = m.core.master.get(sym)
            und = ins.underlying
            if und not in m.underlyings() or any(mg.und == und for mg in self.managed.values()):
                continue
            wait = self.retry_at.get(und)
            if wait is not None and now < wait:
                continue  # backing off after a refusal
            # Contract direction -> index direction (a SHORT reading on a put is bullish).
            long_contract = ev["direction"] == "LONG"
            bull = long_contract != (ins.kind.value == "PE")
            pick = m.by_delta(und, "CE" if bull else "PE", self.delta)
            if pick is None:
                continue
            ok, why, _, _ = m.scalp_gate(pick[0], m.lot(pick[0]), self.impulse_mult, und)
            if not ok:
                self.last_seen += f" (charges gate refused: {why})"
                continue
            vac = vacuum_score(m, und, pick[0], bull)
            if vac is None or vac["score"] < self.min_confidence:
                self.last_seen += (
                    f" (vacuum score {vac['score'] if vac else '—'} under {self.min_confidence:g})"
                )
                continue
            t = m.tape(und)
            bars = t.done()
            trap = absorption(bars[-1], t.rvol(), t.atr(), bull) if bars else None
            if trap:
                self.last_seen += f" (absorption filter: {trap})"
                continue
            out.append(
                self.enter(
                    m,
                    und,
                    pick[0],
                    lots=1,
                    bullish=bull,
                    ratchet=True,
                    reason=f"{ev['pattern']} on {m.core.label(sym)} at {ev['price']:g} "
                    f"(PCR {ev['pcr']})",
                )
            )
        return out


# ------------------------------------------------------------------------------ ride_wave.md


@dataclass
class ZoneRiderPlay(LongPlay):
    window: tuple[str, str] = ("09:45", "14:00")
    clear_pts: float = 10.0
    delta: float = 0.60
    min_days: int = 5
    used: set[tuple[str, float]] = field(default_factory=set)
    div_at: dict[str, datetime] = field(default_factory=dict)

    def look(self, now: datetime, m: Market) -> list[str] | None:
        if not self.in_window(now, self.window):
            return None
        out = []
        for und in m.underlyings():
            if any(mg.und == und for mg in self.managed.values()):
                continue
            wait = self.retry_at.get(und)
            if wait is not None and now < wait:
                continue
            t = m.tape(und)
            z = m.zones(und)
            f = m.fut(und)
            px = float(f.ltp) if f is not None and f.ltp else 0.0
            cvd5 = sum(b.delta for b in t.done()[-5:])
            scale = self.clear_pts * (3.0 if und == "SENSEX" else 1.0)
            parts = [
                f"floor {fl['low']:,.0f}-{fl['high']:,.0f}: needs future >= "
                f"{fl['high'] + scale:,.0f} with 5-min CVD > 0"
                for fl in z.get("floors", [])
            ] + [
                f"ceiling {ce['low']:,.0f}-{ce['high']:,.0f}: needs future <= "
                f"{ce['low'] - scale:,.0f} with 5-min CVD < 0"
                for ce in z.get("ceilings", [])
            ]
            n_bars = len(t.done())
            zone_txt = (
                "; ".join(parts) + f" (future {fmt(px, 0)}, 5-min CVD {cvd5:+,.0f})"
                if parts
                else "no absorption zone in the last 30 min"
            )
            div_txt = (
                "30-min CVD divergence: none"
                if n_bars >= 30
                else f"divergence check needs 30 bars ({n_bars})"
            )
            self.note(f"{und}: {zone_txt}; {div_txt}")
            trig = None
            for fl in z.get("floors", []):
                if px >= fl["high"] + scale and cvd5 > 0 and (und, fl["low"]) not in self.used:
                    trig = (
                        True,
                        fl["low"],
                        fl["stop_below"],
                        f"floor {fl['low']:,.0f}-{fl['high']:,.0f} ({fl['ratio']}x)",
                    )
            for ce in z.get("ceilings", []):
                if px <= ce["low"] - scale and cvd5 < 0 and (und, ce["low"]) not in self.used:
                    trig = (
                        False,
                        ce["low"],
                        ce["stop_above"],
                        f"ceiling {ce['low']:,.0f}-{ce['high']:,.0f} ({ce['ratio']}x)",
                    )
            div = divergence(t)
            last_div = self.div_at.get(und)
            if (
                trig is None
                and div is not None
                and (last_div is None or (now - last_div).total_seconds() >= 1800)
            ):
                # One trade per divergence episode (the pattern spans 30 minutes of bars).
                self.div_at[und] = now
                trig = (div == "bullish", 0.0, None, f"30-min CVD absorption divergence ({div})")
            if trig is None:
                continue
            bull, zkey, stop, why = trig
            self.used.add((und, zkey))
            pick = _pick(m, und, "CE" if bull else "PE", self.delta, self.min_days)
            if pick is None:
                self.renote(f"{und}: {why}, but no {self.min_days}+ day option in the chain")
                continue
            # The structural stop is on the future; translate to the index by the basis.
            basis = (px - m.spot(und)) if px else 0.0
            out.append(
                self.enter(
                    m,
                    und,
                    pick[0],
                    lots=1,
                    bullish=bull,
                    stop_pct=35,
                    spot_stop=(stop - basis) if stop is not None else None,
                    reason=f"{und} {'long' if bull else 'short'} wave: {why}",
                )
            )
        return out


def momentum_state(bars: list[Any], bullish: bool) -> dict[str, float] | None:
    """The Log-3 entry features on completed 1-minute index bars, signed for the direction:
    15-minute momentum %, distance from the day's extreme % (high for calls, low for puts) and
    the distance beyond the 15-minute opening range % (0 inside it)."""
    if len(bars) < 16:
        return None
    orb = [b for b in bars if b.start.astimezone(IST).strftime("%H:%M") < "09:30"]
    if not orb:
        return None
    sign = 1.0 if bullish else -1.0
    px, ref = bars[-1].close, bars[-16].close
    or_hi, or_lo = max(b.high for b in orb), min(b.low for b in orb)
    beyond = (px - or_hi) / or_hi * 100 if bullish else (or_lo - px) / or_lo * 100
    ext = max(b.high for b in bars) if bullish else min(b.low for b in bars)
    return {
        "mom15_pct": sign * (px - ref) / ref * 100,
        "from_extreme_pct": abs(ext - px) / px * 100,
        "beyond_or_pct": max(0.0, beyond),
    }


@dataclass
class Log3MomentumPlay(LongPlay):
    """Log-3 momentum (HYPOTHESIS from ``tasks.py log3-features``, 13 Nov 2026): the funded
    trader's entries came after a 15-minute push of >= ~0.05% in the trade's direction, near the
    day's extreme and beyond the opening range (93% of entries vs 29% of other minutes, 10
    sessions). 10:00-14:00: all three hold -> buy the ATM option (~0.50 delta) that way. Exits
    as the log: -10% stop, +30% target, the square-off. One entry per index and direction per
    30 minutes. A paper test of the mined rule, run beside the other directional accounts."""

    window: tuple[str, str] = ("10:00", "14:00")
    mom_min_pct: float = 0.05
    near_extreme_pct: float = 0.10
    delta: float = 0.50
    stop_pct: float = 10.0
    target_pct: float = 30.0
    gap_min: float = 30.0
    last_at: dict[tuple[str, bool], datetime] = field(default_factory=dict)

    def look(self, now: datetime, m: Market) -> list[str] | None:
        if not self.in_window(now, self.window):
            return None
        out: list[str] = []
        for und in m.underlyings():
            if any(mg.und == und for mg in self.managed.values()):
                continue
            bars = m.tape(und).done()
            parts = []
            for bull in (True, False):
                f = momentum_state(bars, bull)
                if f is None:
                    parts = ["needs 16 minutes of bars and the opening range"]
                    break
                ok = (
                    f["mom15_pct"] >= self.mom_min_pct
                    and f["beyond_or_pct"] > 0
                    and f["from_extreme_pct"] <= self.near_extreme_pct
                )
                parts.append(
                    f"{'up' if bull else 'down'}: 15-min move {f['mom15_pct']:+.2f}% its way "
                    f"(needs >= "
                    f"+{self.mom_min_pct:g}), {f['from_extreme_pct']:.2f}% off the "
                    f"{'high' if bull else 'low'} (needs <= {self.near_extreme_pct:g}), "
                    f"{'beyond' if f['beyond_or_pct'] > 0 else 'inside'} the opening range"
                )
                last = self.last_at.get((und, bull))
                if not ok or (last is not None and now - last < timedelta(minutes=self.gap_min)):
                    continue
                pick = m.by_delta(und, "CE" if bull else "PE", self.delta)
                if pick is None:
                    parts.append("no ~0.5 delta option in the chain")
                    continue
                self.last_at[(und, bull)] = now
                out.append(
                    self.enter(
                        m,
                        und,
                        pick[0],
                        lots=1,
                        bullish=bull,
                        stop_pct=self.stop_pct,
                        target_pct=self.target_pct,
                        reason=(
                            f"{und} Log-3 momentum {'up' if bull else 'down'}: 15-min "
                            f"{f['mom15_pct']:+.2f}%, {f['from_extreme_pct']:.2f}% off the "
                            f"{'high' if bull else 'low'}, beyond the opening range"
                        ),
                    )
                )
                break
            self.note(f"{und}: " + "; ".join(parts))
        return out


def divergence(t: Any, minutes: int = 30) -> str | None:
    """ride_wave.md's 30-minute filter: a higher low in price with a lower low in CVD is bullish
    absorption (sellers absorbed); a lower high with a higher high in CVD is bearish."""
    bars = t.done()[-minutes:]
    if len(bars) < minutes:
        return None
    cvd, run = [], 0.0
    for b in bars:
        run += b.delta
        cvd.append(run)
    half = minutes // 2
    lo1 = min(range(half), key=lambda i: bars[i].low)
    lo2 = min(range(half, minutes), key=lambda i: bars[i].low)
    if bars[lo2].low > bars[lo1].low and cvd[lo2] < cvd[lo1]:
        return "bullish"
    hi1 = max(range(half), key=lambda i: bars[i].high)
    hi2 = max(range(half, minutes), key=lambda i: bars[i].high)
    if bars[hi2].high < bars[hi1].high and cvd[hi2] > cvd[hi1]:
        return "bearish"
    return None


__all__ = [
    "BreakthroughPlay",
    "OiIgnitionPlay",
    "PutPanicPlay",
    "SpoofFadePlay",
    "ZeroDtePlay",
    "ZoneRiderPlay",
    "divergence",
    "timedelta",
    "vacuum_score",
]
