"""Multi-leg playbook strategies from the owner's notes (``new/``, 12 Nov 2026). Paper only.

Defined risk throughout (wings bought first, shorts bought back first); thresholds HYPOTHESES.

- ``IronFlyPlay`` (spec A + breaking_retail post-flush): after 09:45 on the configured days,
  30-min realized vol / ATM IV < 0.85 and the ATM straddle below its VWAP for 3 one-minute
  bars (and, if ``post_flush``, after a 9:20 straddle leg traded 25% above its 9:20 price and a
  1-minute selling candle printed) -> sell ATM call + put, buy wings ``wing_pts`` away (scaled
  to Sensex). Exit when the straddle closes above VWAP + 1.5 sigma, the net delta passes
  +-0.30 per unit, or the square-off.
- ``OiCreditPlay`` (spec B): index within ``near_pts`` of the call (put) OI wall, that wall's
  OI down > 4% in 10 min and futures CVD z >= +2 (<= -2) -> bull put (bear call) spread at the
  money, wing ``wing_pts`` out. Exits: 50% of credit, 2x credit, index back through the wall,
  square-off.
- ``CalendarPlay`` (spec C): near-expiry ATM IV minus far-expiry ATM IV > +2.5 vol pts and
  its z-score over the last 20 sessions > +2 -> sell the near ATM option, buy the far one at the
  same strike. Exit when the term spread is back to 0, the short leg's |delta| leaves
  0.25-0.75, or the day before the near expiry. Held overnight.
- ``CompressionDebitPlay`` (spec D): 15-min Bollinger width in the bottom 10% of the last 10
  sessions, a close out of the 60-min box with 25-delta skew moving the breakout's way and
  15-min futures volume > 2.5x -> buy ~0.55 delta, sell ~0.25 delta. Hold up to 2 days; exits:
  70% of max value, half the debit, back inside the box.
- ``ExpiryPinPlay`` (fix2.md): expiry day 13:45-14:45, fresh put writing at a strike (OI up
  >= 30% since 13:35) while call OI there unwinds (>= 20%), and that put spikes >= 50% above
  its 13:45 price -> sell it with a cheap wing below (credit spread). Exits: 90% of the credit
  or 2x credit; held into the close otherwise (15:20).
- ``SyntheticFuturePlay`` (futures strategy 1): on a confirmed setup, a delta-one position from
  options (buy the protective wing ``wing_pts`` away first, then long call / short put, or the
  mirror). Planned loss = the index stop distance x quantity. Intraday to 3 days.
- ``MarriedFuturePlay`` (futures strategy 2): a break of the previous day's high with futures
  OI +2% over 15 min and the annualized basis expanding -> buy the ATM put, then the future.
  Target +200 index points, stop back below the breakout by 0.5%, up to 5 days. Bullish only:
  the bearish mirror needs a short future, which the leg sequencer (defined risk) does not
  allow.
- ``McxMiniPlay`` (futures strategy 3): VWAP breakout with CVD z >= 2 and 2x volume on
  CRUDEOILM / NATGASMINI with the charges ratchet. Dormant: the engine has no MCX feed or
  instrument master, and MCX costs (CTT) are not in config/rules yet.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from engine.cockpit.condor import Leg, cost_to_close
from engine.cockpit.plays_base import Market, SpreadPlay, SpreadTrade, hhmm
from engine.core.rules import IST


def _atm(rows: list[dict[str, Any]], f: float) -> dict[str, Any] | None:
    return min(rows, key=lambda r: abs(float(r["k"]) - f)) if rows else None


def _row(rows: list[dict[str, Any]], k: float) -> dict[str, Any] | None:
    return next((r for r in rows if abs(float(r["k"]) - k) < 1e-6), None)


def _scale(und: str, pts: float, m: Market) -> float:
    """Point distances are written for Nifty; scale them to another index by spot."""
    if und == "NIFTY":
        return pts
    n, s = m.spot("NIFTY"), m.spot(und)
    step = m.strike_step(und)
    raw = pts * (s / n) if n and s else pts
    return max(step, round(raw / step) * step)


# ------------------------------------------------------------------------------ A


@dataclass
class IronFlyPlay(SpreadPlay):
    first_entry: str = "09:45"
    last_entry: str = "13:00"
    rv_iv_max: float = 0.85
    below_vwap_bars: int = 3
    wing_pts: float = 300.0
    days: dict[str, tuple[int, ...]] = field(
        default_factory=lambda: {"NIFTY": (0, 1), "SENSEX": (3,)}
    )
    post_flush: bool = True
    sigma_exit: float = 1.5
    delta_exit: float = 0.30

    def _flushed(self, m: Market, und: str) -> bool:
        if not self.post_flush:
            return True
        t = m.tape(und)
        rows = m.chain_rows(und)
        a = _atm(rows, m.spot(und))
        if a is None:
            return False
        for key in ("ce", "pe"):
            sym = (a.get(key) or {}).get("sym")
            if not sym:
                continue
            ot = m.otape(sym)
            hi = max((c for _, c in ot.closes), default=0.0)
            if ot.open_920 and hi >= 1.25 * ot.open_920:
                bars = t.done()
                if bars and bars[-1].delta < 0:  # a selling candle after the stop flush
                    return True
        return False

    def look(self, now: datetime, m: Market) -> list[str] | None:
        if (
            not self.first_entry <= hhmm(now) <= self.last_entry
            or self.entries_today >= self.max_entries
        ):
            return None
        if self.account.positions:
            return None
        for und in m.underlyings():
            if now.astimezone(IST).weekday() not in self.days.get(und, ()):
                continue
            t = m.tape(und)
            strip = m.strip(und)
            iv = strip.get("atm_iv_pct")
            rv = t.realized_vol(30, from_open=True)
            st = t.straddle_stats()
            if not iv or rv is None or st is None:
                self.decision = f"{und}: waiting for 30 min of tape and the straddle VWAP"
                continue
            ratio = rv / (float(iv) / 100)
            closes = [b.straddle for b in t.done()[-self.below_vwap_bars :] if b.straddle]
            below = len(closes) == self.below_vwap_bars and all(c < st[0] for c in closes)
            if ratio >= self.rv_iv_max or not below:
                self.decision = (
                    f"{und}: RV/IV {ratio:.2f} (need < {self.rv_iv_max}), "
                    f"straddle below VWAP {below}"
                )
                continue
            if not self._flushed(m, und):
                self.decision = f"{und}: waiting for the 9:20 straddle stop flush"
                continue
            rows = m.chain_rows(und)
            f = float(strip.get("forward") or m.spot(und))
            a = _atm(rows, f)
            if a is None:
                continue
            k = float(a["k"])
            w = _scale(und, self.wing_pts, m)
            wp, wc = _row(rows, k - w), _row(rows, k + w)
            syms = [
                (a.get("ce") or {}).get("sym"),
                (a.get("pe") or {}).get("sym"),
                ((wp or {}).get("pe") or {}).get("sym"),
                ((wc or {}).get("ce") or {}).get("sym"),
            ]
            if not all(syms):
                self.decision = f"{und}: wings {w:g} away not in the chain"
                continue
            lot = m.lot(str(syms[0]))
            legs = [
                Leg(str(syms[2]), "PE", k - w, "BUY", lot),
                Leg(str(syms[3]), "CE", k + w, "BUY", lot),
                Leg(str(syms[1]), "PE", k, "SELL", lot),
                Leg(str(syms[0]), "CE", k, "SELL", lot),
            ]
            tr = SpreadTrade(
                "", und, now, square_off="15:15", extra={"vwap": st[0], "sd": st[1], "atm": k}
            )
            self.plan(
                m, und, "IRON_FLY", legs, w, f"{und} iron fly {k:g} ±{w:g} (RV/IV {ratio:.2f})", tr
            )
            return []
        return None

    def extra_exit(self, pos: Any, tr: SpreadTrade, now: datetime, m: Market) -> str | None:
        t = m.tape(pos.underlying)
        st = t.straddle_stats()
        if st is not None and st[2] > st[0] + self.sigma_exit * st[1]:
            return f"straddle {st[2]:.1f} above VWAP + {self.sigma_exit}σ"
        net = 0.0
        for lg in pos.legs:
            d = m.detail(lg.symbol).get("delta")
            if d is None or not lg.open:
                continue
            net += float(d) * (1 if lg.side == "BUY" else -1)
        if abs(net) > self.delta_exit:
            return f"net delta {net:+.2f} per unit"
        return None


# ------------------------------------------------------------------------------ B


@dataclass
class OiCreditPlay(SpreadPlay):
    window: tuple[str, str] = ("09:45", "14:00")
    near_pts: float = 20.0
    oi_drop_pct: float = 4.0
    cvd_z: float = 2.0
    wing_pts: float = 250.0

    def look(self, now: datetime, m: Market) -> list[str] | None:
        if not self.window[0] <= hhmm(now) <= self.window[1] or self.entries_today >= 2:
            return None
        for und in m.underlyings():
            if any(p.underlying == und for p in self.account.positions):
                continue
            t, rows = m.tape(und), m.chain_rows(und)
            if not rows:
                continue
            spot = m.spot(und)
            near = _scale(und, self.near_pts, m)
            z = t.cvd_z()
            cw = max(rows, key=lambda r: (r.get("ce") or {}).get("oi") or 0)
            pw = max(rows, key=lambda r: (r.get("pe") or {}).get("oi") or 0)
            for bull, wall, kind in ((True, cw, "CE"), (False, pw, "PE")):
                k = float(wall["k"])
                if abs(spot - k) > near:
                    continue
                d = t.oi_change_pct(k, kind, 10)
                if (
                    d is None
                    or d > -self.oi_drop_pct
                    or z is None
                    or (z < self.cvd_z if bull else z > -self.cvd_z)
                ):
                    self.decision = f"{und} near {k:g}: {kind} OI {d}% in 10 min, CVD z {z}"
                    continue
                a = _atm(rows, spot)
                if a is None:
                    continue
                ka = float(a["k"])
                w = _scale(und, self.wing_pts, m)
                okind = "PE" if bull else "CE"
                wk = ka - w if bull else ka + w
                short_sym = (a.get(okind.lower()) or {}).get("sym")
                wrow = _row(rows, wk)
                wing_sym = ((wrow or {}).get(okind.lower()) or {}).get("sym")
                if not short_sym or not wing_sym:
                    continue
                lot = m.lot(short_sym)
                legs = [
                    Leg(wing_sym, okind, wk, "BUY", lot),
                    Leg(short_sym, okind, ka, "SELL", lot),
                ]
                tr = SpreadTrade(
                    "",
                    und,
                    now,
                    target_pct=50,
                    stop_mult=2.0,
                    square_off="15:20",
                    spot_stop=(k - near) if bull else (k + near),
                    bullish=bull,
                )
                self.plan(
                    m,
                    und,
                    "BULL_PUT" if bull else "BEAR_CALL",
                    legs,
                    w,
                    f"{und} {'call' if bull else 'put'} writers covering at {k:g} "
                    f"(OI {d:.1f}%, CVD z {z:.1f})",
                    tr,
                )
                return []
        return None


# ------------------------------------------------------------------------------ C


def term_spread_now(m: Market, und: str, min_far_days: int = 14) -> tuple[float, str, str] | None:
    """(near ATM IV - far ATM IV in vol pts, near expiry, far expiry) from the live chains."""
    tables = m.tables(und)
    today = m.now.astimezone(IST).date()
    exps = sorted(tables)
    if len(exps) < 2:
        return None
    near = exps[0]
    far = next((e for e in exps[1:] if (date.fromisoformat(e) - today).days >= min_far_days), None)
    if far is None:
        return None

    def atm_iv(t: dict[str, Any]) -> float | None:
        a = _atm(t.get("rows") or [], float(t.get("forward") or 0))
        if a is None:
            return None
        ivs = [x for x in ((a.get("ce") or {}).get("iv"), (a.get("pe") or {}).get("iv")) if x]
        return statistics.fmean(ivs) if ivs else None

    a, b = atm_iv(tables[near]), atm_iv(tables[far])
    return (a - b, near, far) if a is not None and b is not None else None


@dataclass
class CalendarPlay(SpreadPlay):
    window: tuple[str, str] = ("10:00", "14:30")
    min_spread: float = 2.5
    min_z: float = 2.0
    history_file: Path | None = None  # one term-spread value per session (written at the close)
    today_spread: float | None = None

    def history(self) -> list[float]:
        if self.history_file is None or not self.history_file.exists():
            return []
        out = []
        for line in self.history_file.read_text(encoding="utf-8").splitlines()[-20:]:
            try:
                out.append(float(json.loads(line)["spread"]))
            except (ValueError, KeyError):
                continue
        return out

    def record_close(self, now: datetime) -> None:
        if self.history_file is None or self.today_spread is None:
            return
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.history_file.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"day": now.astimezone(IST).date().isoformat(), "spread": self.today_spread}
                )
                + "\n"
            )

    def look(self, now: datetime, m: Market) -> list[str] | None:
        ts = term_spread_now(m, "NIFTY")
        if ts is None:
            self.decision = "needs the near and a far (14+ day) expiry in the recorded chain"
            return None
        spread, near, far = ts
        self.today_spread = round(spread, 2)
        hist = self.history()
        z = (
            (spread - statistics.fmean(hist)) / statistics.pstdev(hist)
            if len(hist) >= 10 and statistics.pstdev(hist) > 0
            else None
        )
        zs = "—" if z is None else f"{z:.2f}"
        self.decision = (
            f"term spread {spread:+.2f} vol pts (z {zs}; {len(hist)} sessions of history)"
        )
        if self.account.positions or not self.window[0] <= hhmm(now) <= self.window[1]:
            return None
        if spread <= self.min_spread or z is None or z <= self.min_z:
            return None
        tn, tf = m.tables("NIFTY")[near], m.tables("NIFTY")[far]
        a = _atm(tn.get("rows") or [], float(tn.get("forward") or 0))
        if a is None:
            return None
        k = float(a["k"])
        fr = _row(tf.get("rows") or [], k)
        s_sym, l_sym = (a.get("ce") or {}).get("sym"), ((fr or {}).get("ce") or {}).get("sym")
        if not s_sym or not l_sym:
            return None
        lot = m.lot(s_sym)
        legs = [Leg(l_sym, "CE", k, "BUY", lot), Leg(s_sym, "CE", k, "SELL", lot)]
        tr = SpreadTrade(
            "", "NIFTY", now, square_off=None, extra={"near": near, "far": far, "short": s_sym}
        )
        self.plan(
            m,
            "NIFTY",
            "CALENDAR",
            legs,
            0.0,
            f"NIFTY calendar {k:g} {near}/{far} (term {spread:+.1f}, z {z:.1f})",
            tr,
            expiry=date.fromisoformat(near),
        )
        return []

    def extra_exit(self, pos: Any, tr: SpreadTrade, now: datetime, m: Market) -> str | None:
        ts = term_spread_now(m, pos.underlying)
        if ts is not None and ts[0] <= 0.0:
            return f"term spread back to {ts[0]:+.2f}"
        short = tr.extra.get("short") or next(
            (lg.symbol for lg in pos.legs if lg.side == "SELL"), None
        )
        d = m.detail(short).get("delta") if short else None
        if d is not None and not 0.25 <= abs(float(d)) <= 0.75:
            return f"index left the tent (short leg delta {float(d):.2f})"
        local = now.astimezone(IST)
        if (pos.expiry - local.date()).days <= 1 and hhmm(now) >= "15:00":
            return "time exit before the near expiry"
        return None


# ------------------------------------------------------------------------------ D


@dataclass
class CompressionDebitPlay(SpreadPlay):
    window: tuple[str, str] = ("10:15", "14:30")
    pct: float = 10.0
    vol_mult: float = 2.5
    history_file: Path | None = None
    widths_today: dict[str, list[float]] = field(default_factory=dict)

    def _percentile(self, und: str, w: float) -> tuple[float | None, int]:
        hist: list[float] = []
        if self.history_file is not None and self.history_file.exists():
            for line in self.history_file.read_text(encoding="utf-8").splitlines()[-10:]:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                hist += [float(x) for x in rec.get(und, [])]
        hist += self.widths_today.get(und, [])
        if len(hist) < 20:
            return None, len(hist)
        return 100.0 * sum(1 for h in hist if h <= w) / len(hist), len(hist)

    def record_close(self, now: datetime) -> None:
        if self.history_file is None or not self.widths_today:
            return
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.history_file.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"day": now.astimezone(IST).date().isoformat(), **self.widths_today})
                + "\n"
            )

    def look(self, now: datetime, m: Market) -> list[str] | None:
        for und in m.underlyings():
            t = m.tape(und)
            w = t.bb_width(15)
            if w is not None:
                ws = self.widths_today.setdefault(und, [])
                if not ws or ws[-1] != w:
                    ws.append(w)
        if not self.window[0] <= hhmm(now) <= self.window[1] or self.entries_today >= 1:
            return None
        for und in m.underlyings():
            if any(p.underlying == und for p in self.account.positions):
                continue
            t = m.tape(und)
            w = t.bb_width(15)
            box = t.box(60)
            bars15 = t.bars_n(15)
            if w is None or box is None or len(bars15) < 6:
                continue
            p, n = self._percentile(und, w)
            if p is None or p > self.pct:
                self.decision = f"{und}: 15-min width percentile {p} ({n} widths of history)"
                continue
            last = t.done()[-1]
            up, down = last.close > box[0], last.close < box[1]
            avg15 = statistics.fmean(b.vol for b in bars15[:-1]) if len(bars15) > 1 else 0
            if not (up or down) or not avg15 or bars15[-1].vol < self.vol_mult * avg15:
                continue
            sk = t.iv_change("skew25", 15)
            if sk is None or (up and sk >= 0) or (down and sk <= 0):
                self.decision = f"{und}: box break without the skew move (Δskew {sk})"
                continue
            kind = "CE" if up else "PE"
            lng, sht = m.by_delta(und, kind, 0.55), m.by_delta(und, kind, 0.25)
            if lng is None or sht is None or lng[0] == sht[0]:
                continue
            lot = m.lot(lng[0])
            width = abs(sht[1]["k"] - lng[1]["k"])
            tr = SpreadTrade(
                "",
                und,
                now,
                target_pct=70,
                debit_stop_pct=50,
                square_off=None,
                bullish=up,
                extra={"box": box, "until": now.astimezone(IST).date().toordinal() + 2},
            )
            self.plan(
                m,
                und,
                "BULL_CALL" if up else "BEAR_PUT",
                [
                    Leg(lng[0], kind, lng[1]["k"], "BUY", lot),
                    Leg(sht[0], kind, sht[1]["k"], "SELL", lot),
                ],
                width,
                f"{und} compression break {'up' if up else 'down'} (width pct {p:.0f})",
                tr,
            )
            return []
        return None

    def extra_exit(self, pos: Any, tr: SpreadTrade, now: datetime, m: Market) -> str | None:
        box = tr.extra.get("box")
        spot = m.spot(pos.underlying)
        if box and spot and box[1] <= spot <= box[0] and (now - tr.opened).total_seconds() > 900:
            return "back inside the 60-min box"
        if (
            now.astimezone(IST).date().toordinal() >= int(tr.extra.get("until", 10**9))
            and hhmm(now) >= "15:15"
        ):
            return "two-day time stop"
        return None


# ------------------------------------------------------------------------------ fix2.md


@dataclass
class ExpiryPinPlay(SpreadPlay):
    start: str = "13:45"
    end: str = "14:45"
    put_oi_up_pct: float = 30.0
    call_oi_down_pct: float = 20.0
    spike_pct: float = 50.0
    wing_max_ask: float = 3.0
    base_px: dict[str, float] = field(default_factory=dict)

    def look(self, now: datetime, m: Market) -> list[str] | None:
        h = hhmm(now)
        for und in m.underlyings():
            if not m.expiry_today(und) or any(p.underlying == und for p in self.account.positions):
                continue
            t, rows = m.tape(und), m.chain_rows(und)
            if h == self.start:
                for r in rows:
                    pe = r.get("pe") or {}
                    if pe.get("sym") and pe.get("ltp"):
                        self.base_px.setdefault(str(pe["sym"]), float(pe["ltp"]))
            if not self.start < h <= self.end:
                continue
            mins = max(
                1,
                int(
                    (
                        now.astimezone(IST) - now.astimezone(IST).replace(hour=13, minute=35)
                    ).total_seconds()
                    // 60
                ),
            )
            for r in rows:
                k = float(r["k"])
                pe = r.get("pe") or {}
                pu, cd = t.oi_change_pct(k, "PE", mins), t.oi_change_pct(k, "CE", mins)
                if (
                    pu is None
                    or cd is None
                    or pu < self.put_oi_up_pct
                    or cd > -self.call_oi_down_pct
                ):
                    continue
                sym = pe.get("sym")
                base = self.base_px.get(str(sym)) if sym else None
                b = m.book(str(sym)) if sym else None
                if (
                    not base
                    or b is None
                    or not b.bids
                    or b.bids[0][0] < base * (1 + self.spike_pct / 100)
                ):
                    self.decision = (
                        f"{und} pin at {k:g} (put OI +{pu:.0f}%, call OI {cd:.0f}%): "
                        "waiting for a put spike"
                    )
                    continue
                wing = next(
                    (
                        w
                        for w in sorted(rows, key=lambda x: -float(x["k"]))
                        if float(w["k"]) < k
                        and 0 < ((w.get("pe") or {}).get("ask") or 0) <= self.wing_max_ask
                    ),
                    None,
                )
                wsym = ((wing or {}).get("pe") or {}).get("sym")
                if not wing or not wsym:
                    continue
                lot = m.lot(str(sym))
                legs = [
                    Leg(str(wsym), "PE", float(wing["k"]), "BUY", lot),
                    Leg(str(sym), "PE", k, "SELL", lot),
                ]
                tr = SpreadTrade("", und, now, target_pct=90, stop_mult=2.0, square_off="15:20")
                self.plan(
                    m,
                    und,
                    "BULL_PUT",
                    legs,
                    k - float(wing["k"]),
                    f"{und} expiry pin: sell the {k:g} put spike "
                    f"(put OI +{pu:.0f}%, call OI {cd:.0f}%)",
                    tr,
                )
                return []
        return None


# ------------------------------------------------------------------------------ futures


@dataclass
class SyntheticFuturePlay(SpreadPlay):
    window: tuple[str, str] = ("09:45", "14:00")
    wing_pts: float = 350.0
    stop_pct: float = 0.5  # index stop distance, % of spot -> planned loss for the gate
    hold_days: int = 3
    queue: list[dict[str, Any]] = field(default_factory=list)

    def on_setup(self, und: str, bullish: bool, ref: str) -> None:
        self.queue.append({"und": und, "bull": bullish, "ref": ref})

    def look(self, now: datetime, m: Market) -> list[str] | None:
        if not self.queue:
            return None
        q = self.queue.pop(0)
        if not self.window[0] <= hhmm(now) <= self.window[1] or self.account.positions:
            return None
        und, bull = q["und"], q["bull"]
        rows = m.chain_rows(und)
        spot = m.spot(und)
        a = _atm(rows, spot)
        if a is None or not spot:
            return None
        k = float(a["k"])
        w = _scale(und, self.wing_pts, m)
        wk = k - w if bull else k + w
        wr = _row(rows, wk)
        prot_kind = "PE" if bull else "CE"
        call, put = (a.get("ce") or {}).get("sym"), (a.get("pe") or {}).get("sym")
        wing = ((wr or {}).get(prot_kind.lower()) or {}).get("sym")
        if not call or not put or not wing:
            return None
        lot = m.lot(call)
        if bull:  # long synthetic: buy wing put, buy ATM call, sell ATM put
            legs = [
                Leg(wing, "PE", wk, "BUY", lot),
                Leg(call, "CE", k, "BUY", lot),
                Leg(put, "PE", k, "SELL", lot),
            ]
        else:  # short synthetic: buy wing call, buy ATM put, sell ATM call
            legs = [
                Leg(wing, "CE", wk, "BUY", lot),
                Leg(put, "PE", k, "BUY", lot),
                Leg(call, "CE", k, "SELL", lot),
            ]
        stop_pts = spot * self.stop_pct / 100
        tr = SpreadTrade(
            "",
            und,
            now,
            square_off=None,
            bullish=bull,
            spot_stop=spot - stop_pts if bull else spot + stop_pts,
            extra={"until": now.astimezone(IST).date().toordinal() + self.hold_days},
        )
        self.plan(
            m,
            und,
            "SYNTHETIC_LONG" if bull else "SYNTHETIC_SHORT",
            legs,
            w,
            f"{und} synthetic {'long' if bull else 'short'} on confirmed setup {q['ref']}",
            tr,
            max_loss_inr=stop_pts * lot,
        )
        return []

    def extra_exit(self, pos: Any, tr: SpreadTrade, now: datetime, m: Market) -> str | None:
        if (
            now.astimezone(IST).date().toordinal() >= int(tr.extra.get("until", 10**9))
            and hhmm(now) >= "15:15"
        ):
            return "holding-period exit"
        if (pos.expiry - now.astimezone(IST).date()).days <= 0 and hhmm(now) >= "14:45":
            return "expiry-day exit"
        return None


@dataclass
class MarriedFuturePlay(SpreadPlay):
    window: tuple[str, str] = ("10:00", "14:30")
    fut_oi_up_pct: float = 2.0
    target_pts: float = 200.0
    stop_pct: float = 0.5
    hold_days: int = 5
    fut_oi: dict[str, list[tuple[datetime, int]]] = field(default_factory=dict)
    basis: dict[str, list[float]] = field(default_factory=dict)

    def look(self, now: datetime, m: Market) -> list[str] | None:
        for und in m.underlyings():
            f = m.fut(und)
            s = m.spot(und)
            if f is None or not f.ltp or not s:
                continue
            series = self.fut_oi.setdefault(und, [])
            if f.oi_now and (not series or (now - series[-1][0]).total_seconds() >= 60):
                series.append((now, int(f.oi_now)))
                ins = m.core.master.get(f.symbol) if f.symbol in m.core.master else None
                dte = (
                    max(1, (ins.expiry - now.astimezone(IST).date()).days)
                    if ins and ins.expiry
                    else 30
                )
                self.basis.setdefault(und, []).append((f.ltp - s) / s * 365 / dte * 100)
        if not self.window[0] <= hhmm(now) <= self.window[1] or self.account.positions:
            return None
        for und in m.underlyings():
            f = m.fut(und)
            st = (
                m.runner.scanner.states.get(m.runner.spot_symbol.get(und, "")) if m.runner else None
            )
            series, basis = self.fut_oi.get(und, []), self.basis.get(und, [])
            if f is None or st is None or not st.pdh or len(series) < 16 or len(basis) < 30:
                continue
            spot = m.spot(und)
            oi_chg = (series[-1][1] - series[-16][1]) / max(series[-16][1], 1) * 100
            expanding = basis[-1] > 0 and basis[-1] > statistics.fmean(basis[-30:-15])
            if not (spot > st.pdh and oi_chg > self.fut_oi_up_pct and expanding):
                continue
            a = _atm(m.chain_rows(und), spot)
            put = ((a or {}).get("pe") or {}).get("sym")
            if not put or a is None:
                continue
            lot = m.lot(put)
            legs = [
                Leg(put, "PE", float(a["k"]), "BUY", lot),
                Leg(f.symbol, "FUT", 0.0, "BUY", lot),
            ]
            stop = st.pdh * (1 - self.stop_pct / 100)
            tr = SpreadTrade(
                "",
                und,
                now,
                square_off=None,
                spot_stop=stop,
                bullish=True,
                extra={
                    "entry_spot": spot,
                    "until": now.astimezone(IST).date().toordinal() + self.hold_days,
                },
            )
            # Worst case: future falls to the put strike plus the put premium.
            put_ask = m.book(put).asks[0][0] if m.book(put) and m.book(put).asks else 0.0  # type: ignore[union-attr]
            worst = (max(0.0, f.ltp - float(a["k"])) + put_ask) * lot
            self.plan(
                m,
                und,
                "MARRIED_FUTURE",
                legs,
                0.0,
                f"{und} above PDH {st.pdh:,.0f}, futures OI +{oi_chg:.1f}%, "
                f"basis {basis[-1]:.1f}% p.a. rising",
                tr,
                max_loss_inr=worst,
            )
            return []
        return None

    def extra_exit(self, pos: Any, tr: SpreadTrade, now: datetime, m: Market) -> str | None:
        spot = m.spot(pos.underlying)
        if spot and spot >= float(tr.extra.get("entry_spot", 1e12)) + self.target_pts:
            return f"target +{self.target_pts:g} index points"
        if (
            now.astimezone(IST).date().toordinal() >= int(tr.extra.get("until", 10**9))
            and hhmm(now) >= "15:15"
        ):
            return "holding-period exit"
        return None


@dataclass
class McxMiniPlay(SpreadPlay):
    symbols: tuple[str, ...] = ("CRUDEOILM", "NATGASMINI")

    def look(self, now: datetime, m: Market) -> list[str] | None:
        found = [
            s for s in self.symbols if any(s in str(k) for k in getattr(m.runner, "subscribed", []))
        ]
        self.decision = (
            "dormant: no MCX feed or instrument master in this engine, and MCX costs (CTT) are not "
            "in config/rules yet"
            if not found
            else "MCX data present: entries need verified MCX cost rows"
        )
        return None


__all__ = [
    "CalendarPlay",
    "CompressionDebitPlay",
    "ExpiryPinPlay",
    "IronFlyPlay",
    "MarriedFuturePlay",
    "McxMiniPlay",
    "OiCreditPlay",
    "SyntheticFuturePlay",
    "cost_to_close",
    "term_spread_now",
]
