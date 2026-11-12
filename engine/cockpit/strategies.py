"""More paper strategies driven by the same signals (each with its own account and journal).

All are paper only, one lot, defined risk, on the approved risk gate; every number is a
HYPOTHESIS to be measured, not a fitted value.

- **Debit spreads on confirmed setups** (``cockpit.debit``): the same confirmed order-flow
  setups the 5.2 directional strategy buys a naked ATM option on, expressed instead as a bull
  call / bear put spread (buy ATM, sell ``width_steps`` strikes further out). Same signal,
  different vehicle: an A/B test of naked vs spread, which matters most when IV is rich.
  Exits: spread value reaches ``target_pct_of_max`` of the maximum value, falls to
  ``stop_pct_of_debit`` of what was paid, or the 15:20 square-off.
- **Expiry-afternoon gamma breakout** (SPEC 5.10, ``cockpit.gamma``): on an index's weekly
  expiry day, 13:45–15:15, after a quiet morning (09:15–13:30 range < ``compression_pct`` of
  spot), a completed 5-minute candle closes beyond the morning high/low and the futures VWAP on
  ``vol_mult`` x the day's average 5-minute futures volume, and OI at the breached ATM strike
  fell since 13:30 (short covering). Buys 1 lot ATM; 30% premium stop; one lot cannot be
  halved, so instead of "book half at 1.8x" the stop moves up to +40% once the premium is
  1.8x; square-off 15:20; one attempt per expiry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from engine.cockpit.condor import Leg, SpreadAccount, cost_to_close
from engine.cockpit.paper import Book, PaperAccount
from engine.core.rules import IST

Kind_T = str  # "CE" / "PE"

# ---------------------------------------------------------------------------- debit spreads


@dataclass(frozen=True)
class DebitConfig:
    enabled: bool = True
    underlyings: tuple[str, ...] = ("NIFTY", "SENSEX")
    min_score: float = 80.0
    first_entry: str = "09:30"
    last_entry: str = "14:00"
    width_steps: int = 2  # short leg this many strikes further out (Nifty 100, Sensex 200)
    target_pct_of_max: float = 60.0
    stop_pct_of_debit: float = 50.0
    square_off: str = "15:20"
    max_entries_per_day: int = 2
    book_wait_s: float = 60.0
    expiry_day_exit: str = "14:45"
    expiry_day_last_entry: str = "14:30"


@dataclass
class _Plan:
    und: str
    bullish: bool
    legs: list[Leg]
    width: float
    made_at: datetime
    ref: str
    expiry: date


@dataclass
class DebitSpreadStrategy:
    cfg: DebitConfig
    account: SpreadAccount
    strikes_of: Callable[[str, Kind_T], tuple[date, list[float]] | None]
    option_symbol: Callable[[str, date, float, str], str | None]
    lot_size: Callable[[str], int]
    pin: Callable[[list[str]], Any]
    context: Callable[[str], dict[str, Any]] = lambda und: {}
    plan: _Plan | None = None
    today: dict[str, dict[str, Any]] = field(default_factory=dict)
    decision: str = "waiting for a confirmed setup"

    name = "debit"
    title = "Debit spreads on confirmed setups"
    ref = "5.2 variant"

    def legs_in_use(self) -> list[str]:
        syms = [lg.symbol for p in self.account.positions for lg in p.legs if lg.open]
        if self.plan is not None:
            syms += [lg.symbol for lg in self.plan.legs]
        return list(dict.fromkeys(syms))

    def on_confirmed(
        self, *, und: str, bullish: bool, score: float, ref: str, spot: float, now: datetime
    ) -> str | None:
        c = self.cfg
        if not c.enabled or und not in c.underlyings or score < c.min_score or spot <= 0:
            return None
        hhmm = now.astimezone(IST).strftime("%H:%M")
        if not c.first_entry <= hhmm <= c.last_entry:
            return None
        st = self.today.setdefault(und, {"bullish": None, "entries": 0})
        if st["entries"] >= c.max_entries_per_day or (
            st["bullish"] is not None and st["bullish"] != bullish
        ):
            return None
        if any(p.underlying == und for p in self.account.positions) or self.plan is not None:
            return None
        kind = "CE" if bullish else "PE"
        grid = self.strikes_of(und, kind)
        if grid is None:
            return f"debit spread: no option chain for {und}"
        expiry, strikes = grid
        if expiry == now.astimezone(IST).date() and hhmm >= c.expiry_day_last_entry:
            return None
        atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        far_i = atm_i + c.width_steps if bullish else atm_i - c.width_steps
        if not 0 <= far_i < len(strikes):
            return None
        k_long, k_short = strikes[atm_i], strikes[far_i]
        s_long = self.option_symbol(und, expiry, k_long, kind)
        s_short = self.option_symbol(und, expiry, k_short, kind)
        if s_long is None or s_short is None:
            return None
        lot = max(1, self.lot_size(s_long))
        self.plan = _Plan(
            und=und,
            bullish=bullish,
            legs=[Leg(s_long, kind, k_long, "BUY", lot), Leg(s_short, kind, k_short, "SELL", lot)],
            width=abs(k_short - k_long),
            made_at=now,
            ref=ref,
            expiry=expiry,
        )
        self.pin(self.legs_in_use())
        self.decision = (
            f"ready: {'bull call' if bullish else 'bear put'} {k_long:g}/{k_short:g} {kind} "
            "(waiting for both books)"
        )
        return None

    def step(self, now: datetime, books: Mapping[str, Book]) -> list[str]:
        out: list[str] = []
        p = self.plan
        if p is not None:
            if (now - p.made_at).total_seconds() > self.cfg.book_wait_s:
                self.plan = None
                self.pin(self.legs_in_use())
                out.append("debit spread plan dropped: leg books did not arrive in time")
            elif all(lg.symbol in books and books[lg.symbol].bids and books[lg.symbol].asks
                     for lg in p.legs):  # fmt: skip
                self.plan = None
                ok, text = self.account.open_spread(
                    structure="BULL_CALL" if p.bullish else "BEAR_PUT",
                    underlying=p.und,
                    expiry=p.expiry,
                    legs=p.legs,
                    width_pts=p.width,
                    books=books,
                    now=now,
                    spot=0.0,
                    reason=f"confirmed {'UP' if p.bullish else 'DOWN'} setup {p.ref}",
                    context=self.context(p.und),
                )
                if ok:
                    st = self.today.setdefault(p.und, {"bullish": None, "entries": 0})
                    st["bullish"], st["entries"] = p.bullish, st["entries"] + 1
                self.decision = "holding; exits: target, stop, 15:20" if ok else text
                self.pin(self.legs_in_use())
                out.append(text)
        for pos in list(self.account.positions):
            why = self.exit_reason(pos, now, books)
            if why is None:
                continue
            ok, text = self.account.close(pos, books, now, why)
            out.append(text if ok else f"debit spread exit ({why}) pending: {text}")
            if ok:
                self.pin(self.legs_in_use())
                self.decision = "waiting for a confirmed setup"
        return out

    def exit_reason(self, pos: Any, now: datetime, books: Mapping[str, Book]) -> str | None:
        c = self.cfg
        hhmm = now.astimezone(IST).strftime("%H:%M")
        if hhmm >= c.square_off:
            return "square-off"
        if pos.expiry == now.astimezone(IST).date() and hhmm >= c.expiry_day_exit:
            return f"expiry-day exit {c.expiry_day_exit}"
        ctc = cost_to_close(pos, books)
        if ctc is None:
            return None
        debit = -pos.credit_pts
        value = -ctc  # what selling the spread back would bring, per unit
        if debit <= 0:
            return None
        if value >= debit + c.target_pct_of_max / 100.0 * (pos.width_pts - debit):
            return f"target ({c.target_pct_of_max:.0f}% of max value)"
        if value <= debit * (1 - c.stop_pct_of_debit / 100.0):
            return f"stop ({c.stop_pct_of_debit:.0f}% of debit)"
        return None

    def view(self) -> dict[str, Any]:
        c = self.cfg
        return {
            "decision": self.decision,
            "rules": (
                f"Entries {c.first_entry}–{c.last_entry} on confirmed setups (best reading ≥ "
                f"{c.min_score:g}) in {', '.join(c.underlyings)}; buy ATM, sell {c.width_steps} "
                f"strikes out; target {c.target_pct_of_max:g}% of max value, stop at "
                f"{c.stop_pct_of_debit:g}% of the debit, square-off {c.square_off}; one idea per "
                f"index per day, at most {c.max_entries_per_day} entries."
            ),
        }


# ---------------------------------------------------------------------------- gamma breakout


@dataclass(frozen=True)
class GammaConfig:
    enabled: bool = True
    underlyings: tuple[str, ...] = ("NIFTY", "SENSEX")
    morning_end: str = "13:30"
    first_entry: str = "13:45"
    last_entry: str = "14:30"  # SPEC said 15:15; settlement averaging from 15:00 (12 Nov review)
    compression_pct: float = 0.45
    vol_mult: float = 1.8
    stop_pct: float = 30.0
    trail: tuple[tuple[float, float], ...] = ((80.0, 40.0),)
    candle_min: int = 5


@dataclass
class _Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    vol: float = 0.0
    pv: float = 0.0  # futures price x volume, for VWAP


def absorption_trap(bar: _Bar, avg_vol: float, up: bool) -> str | None:
    """Effort without result: heavy volume but a small body, or a long wick against the break
    (passive orders absorbing it). HYPOTHESIS thresholds from the 12 Nov review notes."""
    rng = max(bar.high - bar.low, 1e-9)
    body = abs(bar.close - bar.open)
    rvol = bar.vol / avg_vol if avg_vol else 0.0
    if rvol >= 2.5 and body / rng < 0.35:
        return f"absorption: {rvol:.1f}x volume but the body is {body / rng:.0%} of the range"
    wick = (bar.high - max(bar.open, bar.close)) if up else (min(bar.open, bar.close) - bar.low)
    if rvol >= 2.0 and wick / rng > 0.55:
        return f"absorption: rejection wick {wick / rng:.0%} of the range against the break"
    return None


@dataclass
class _Day:
    day: date
    bars: list[_Bar] = field(default_factory=list)
    morning_hi: float = 0.0
    morning_lo: float = 0.0
    fut_vol_last: float | None = None
    oi_1330: dict[tuple[float, str], int] = field(default_factory=dict)
    attempted: bool = False
    why: str = ""


@dataclass
class GammaBreakoutStrategy:
    cfg: GammaConfig
    account: PaperAccount
    expiry_today: Callable[[str, date], bool]
    atm_option: Callable[[str, str], str | None]
    chain_rows: Callable[[str], list[dict[str, Any]]]
    context: Callable[[str], dict[str, Any]] = lambda und: {}
    days: dict[str, _Day] = field(default_factory=dict)

    name = "gamma"
    title = "Expiry-afternoon gamma breakout"
    ref = "5.10"

    def on_tick(self, now: datetime, und: str, spot: float, fut: float, fut_cum_vol: float) -> None:
        """Build 5-minute bars of the index with the front future's volume and VWAP."""
        local = now.astimezone(IST)
        d = self.days.get(und)
        if d is None or d.day != local.date():
            d = self.days[und] = _Day(local.date())
        if spot <= 0:
            return
        vol = 0.0
        if fut_cum_vol > 0:
            if d.fut_vol_last is not None and fut_cum_vol >= d.fut_vol_last:
                vol = fut_cum_vol - d.fut_vol_last
            d.fut_vol_last = fut_cum_vol
        m = (local.hour * 60 + local.minute) // self.cfg.candle_min * self.cfg.candle_min
        start = local.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0)
        if not d.bars or d.bars[-1].start != start:
            d.bars.append(_Bar(start, spot, spot, spot, spot))
        b = d.bars[-1]
        b.high, b.low, b.close = max(b.high, spot), min(b.low, spot), spot
        b.vol += vol
        b.pv += vol * (fut or spot)
        hhmm = local.strftime("%H:%M")
        if "09:15" <= hhmm < self.cfg.morning_end:
            d.morning_hi = max(d.morning_hi, spot) if d.morning_hi else spot
            d.morning_lo = min(d.morning_lo, spot) if d.morning_lo else spot
        if hhmm >= self.cfg.morning_end and not d.oi_1330:
            for r in self.chain_rows(und):
                for key, kind in (("ce", "CE"), ("pe", "PE")):
                    side = r.get(key) or {}
                    if side.get("oi") is not None:
                        d.oi_1330[(float(r["k"]), kind)] = int(side["oi"])

    def check(self, now: datetime, und: str, books: Mapping[str, Book]) -> str | None:
        """Entry on the first completed bar that meets all three SPEC conditions."""
        c = self.cfg
        d = self.days.get(und)
        local = now.astimezone(IST)
        if not c.enabled or d is None or d.attempted or und not in c.underlyings:
            return None
        if not self.expiry_today(und, local.date()):
            d.why = "not an expiry day"
            return None
        hhmm = local.strftime("%H:%M")
        if not c.first_entry <= hhmm <= c.last_entry or len(d.bars) < 3:
            d.why = f"window {c.first_entry}–{c.last_entry}"
            return None
        if not (d.morning_hi and d.morning_lo):
            d.why = "no morning range recorded (engine started late?)"
            return None
        rng = (d.morning_hi - d.morning_lo) / d.morning_lo * 100
        if rng >= c.compression_pct:
            d.why = f"morning range {rng:.2f}% ≥ {c.compression_pct}%: no compression"
            return None
        bar = d.bars[-2]  # the last completed bar
        vols = [b.vol for b in d.bars[:-1] if b.vol > 0]
        avg = sum(vols) / len(vols) if vols else 0.0
        tot_v = sum(b.vol for b in d.bars[:-1])
        vwap = sum(b.pv for b in d.bars[:-1]) / tot_v if tot_v else None
        up = bar.close > d.morning_hi and (vwap is None or bar.close > vwap)
        down = bar.close < d.morning_lo and (vwap is None or bar.close < vwap)
        if not (up or down):
            d.why = (
                f"compressed ({rng:.2f}%); waiting for a 5-min close outside "
                f"{d.morning_lo:,.0f}–{d.morning_hi:,.0f}"
            )
            return None
        if not avg or bar.vol < c.vol_mult * avg:
            d.why = f"breakout bar volume {bar.vol:,.0f} < {c.vol_mult}x average {avg:,.0f}"
            return None
        trap = absorption_trap(bar, avg, up)
        if trap:
            d.why = trap
            return None
        kind = "CE" if up else "PE"
        atm = (
            min(d.oi_1330, key=lambda kk: abs(kk[0] - bar.close), default=None)
            if d.oi_1330
            else None
        )
        if atm is None:
            d.why = "no 13:30 OI snapshot for the short-covering check"
            return None
        strike = atm[0]
        now_oi = None
        for r in self.chain_rows(und):
            if float(r["k"]) == strike:
                now_oi = (r.get(kind.lower()) or {}).get("oi")
        before = d.oi_1330.get((strike, kind))
        if now_oi is None or before is None or now_oi >= before:
            d.why = f"no short covering at {strike:g} {kind} (OI {before} → {now_oi})"
            return None
        d.attempted = True
        sym = self.atm_option(und, kind)
        if sym is None or sym not in books:
            return f"gamma breakout {und}: no ATM {kind} book"
        ok, text = self.account.open_long_option(
            sym,
            books[sym],
            now=now,
            lots=1,
            stop_pct=c.stop_pct,
            trail=c.trail,
            reason=f"5.10 {und} {'UP' if up else 'DOWN'} breakout ({rng:.2f}% morning, "
            f"{bar.vol / avg:.1f}x volume, {kind} OI {before}→{now_oi})",
            context=self.context(und),
        )
        d.why = "attempted today" if ok else text
        return text

    def view(self) -> dict[str, Any]:
        c = self.cfg
        return {
            "decision": "; ".join(f"{u}: {d.why or 'watching'}" for u, d in self.days.items())
            or "waiting for data",
            "rules": (
                f"Expiry days only, {c.first_entry}–{c.last_entry}: morning (to {c.morning_end}) "
                f"range < {c.compression_pct}% of spot; a {c.candle_min}-min close beyond the "
                f"morning high/low and futures VWAP on ≥ {c.vol_mult}x average volume; OI at the "
                f"breached ATM strike lower than at {c.morning_end}. 1 lot ATM, stop "
                f"{c.stop_pct:g}% of premium, at +80% the stop moves to +40% (one lot cannot be "
                "halved), square-off 15:20, one attempt per expiry."
            ),
        }
