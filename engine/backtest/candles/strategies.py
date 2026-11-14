"""Strategy simulators on 1-minute candles, and the registry of what may run on candles.

Allowed: strategies whose triggers use only price, 1-minute volume, IV and OI. Built so far:

- ``condor`` (SPEC 5.1, ``cockpit.condor`` settings via ``CondorConfig``): the live rules
  except the order-flow veto and the event filter, which need data that does not exist
  historically (no depth; ``config/events`` only from Nov 2026). Always an iron condor.
- ``log3`` (SPEC 5.2, ``Log3MomentumPlay`` / ``momentum_state``): the mined momentum rule on
  index bars, ATM option of the nearest expiry, -10% / +30%, 15:20 square-off.

Refused: order-book strategies (no historical depth, so OBI, RER and air pockets cannot be
computed) and the futures-CVD ones, OI ignition (S2) and OI credit (spec B): a CVD needs each
trade's side from the bid/ask (Lee-Ready), which candles do not have.
Allowed but not built yet: expiry gamma (5.10), 0DTE gamma, compression debit
spread (spec D), straddle-VWAP iron fly (spec A).

Every fill is the next bar's open with the haircut (``fills.Haircut``). Exit rules are
decided on last trades (``trigger_on_haircut: false``, default) and every exit FILL pays the
haircut: marking the triggers with a ₹1.50 floor on ₹5-15 condor sides fired the 2.2x side stop
on the spread model itself (first condor run, 14 Nov 2026). A long option's stop or target that
triggers inside a bar fills at the level less the haircut, or at the open less the haircut when
the bar opens beyond it; when a stop and a target are both inside one bar, the stop is assumed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from engine.backtest.candles.data import Bar, Contract
from engine.backtest.candles.fills import Costs, Haircut, table_margin
from engine.backtest.candles.market import IvHistory, Market, Series, hhmm, in_window
from engine.cockpit.condor import CondorConfig
from engine.cockpit.plays_long import Log3MomentumPlay, momentum_state
from engine.cockpit.stress import gap_pnl, worst
from engine.core.rules import IST

REFUSED: dict[str, str] = {
    "score": "order-flow score needs 30-level depth (OBI, RER, air pockets)",
    "breakthrough": "five-pillar scoring needs live depth and the order book",
    "spoof_fade": "spoof walls exist only in the order book",
    "zone_rider": "absorption zones need signed trade flow from depth",
    "oi_ignition": "spec S2 triggers on futures CVD z-scores, which need each trade's side from "
    "the bid/ask (Lee-Ready); candles have none",
    "oi_credit": "spec B triggers on futures CVD z-scores, which need each trade's side from "
    "the bid/ask (Lee-Ready); candles have none",
}
NOT_BUILT: dict[str, str] = {
    "gamma": "SPEC 5.10 expiry-afternoon gamma (needs futures volume candles)",
    "zero_dte": "spec S3 0DTE afternoon gamma",
    "compression": "spec D compression debit spread",
    "iron_fly": "spec A straddle-VWAP iron fly",
}


class StrategyRefused(ValueError):
    """The strategy cannot be tested on candles (or is not built)."""


@dataclass
class LegTrade:
    label: str
    kind: str
    strike: float
    side: str  # entry side: BUY or SELL
    qty: int
    entry_at: datetime
    entry_ref: float  # the bar open the fill came from
    entry_px: float  # after the haircut
    exit_at: datetime | None = None
    exit_ref: float = 0.0
    exit_px: float = 0.0
    charges: float = 0.0

    @property
    def open(self) -> bool:
        return self.exit_at is None

    @property
    def symbol(self) -> str:  # for engine.cockpit.stress.gap_pnl
        return self.label

    def pnl(self) -> float:
        sign = 1.0 if self.side == "BUY" else -1.0
        return sign * (self.exit_px - self.entry_px) * self.qty

    def haircut_inr(self) -> float:
        paid = abs(self.entry_px - self.entry_ref)
        if not self.open:
            paid += abs(self.exit_px - self.exit_ref)
        return paid * self.qty


@dataclass
class CandleTrade:
    strategy: str
    structure: str
    expiry: date
    entry_at: datetime
    reason_in: str
    legs: list[LegTrade]
    exit_at: datetime | None = None
    reason_out: str = ""
    credit_pts: float = 0.0  # credit structures: per unit, at entry fills
    width: float = 0.0
    margin_inr: float | None = None
    flags: list[str] = field(default_factory=list)
    marks: dict[date, float] = field(default_factory=dict)  # MTM net at each session end
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def gross(self) -> float:
        return sum(lg.pnl() for lg in self.legs if not lg.open)

    @property
    def charges(self) -> float:
        return sum(lg.charges for lg in self.legs)

    @property
    def net(self) -> float:
        return self.gross - self.charges

    @property
    def haircut_inr(self) -> float:
        return sum(lg.haircut_inr() for lg in self.legs)

    @property
    def exit_day(self) -> date:
        return (self.exit_at or self.entry_at).astimezone(IST).date()


@dataclass
class Ctx:
    market: Market
    haircut: Haircut
    costs: Costs
    calendar: Any
    capital_inr: float
    risk: dict[str, Any]
    run_date: date
    fill_within_min: float = 3.0  # an entry leg must trade within this after the decision
    stale_entry_min: float = 5.0  # plan prices older than this are not used
    stale_mark_min: float = 15.0  # marks older than this are flagged
    eval_every_min: int = 5
    trigger_on_haircut: bool = False  # exits decided on haircut marks (True) or last trades
    iv_history_from: date | None = None
    iv_min_history: int = 20  # sessions of closing ATM IV before the percentile is used
    log: Callable[[str], None] = lambda _s: None


@dataclass
class SimOut:
    trades: list[CandleTrade]
    skips: Counter[str]
    sessions: list[date]
    iv_days: int = 0


def _fill_leg(
    ctx: Ctx, c: Contract, side: str, qty: int, after: datetime, within: float | None
) -> LegTrade | None:
    b = ctx.market.series(c).next_after(after, within)
    if b is None:
        return None
    px = ctx.haircut.fill(side, b.open)
    lg = LegTrade(f"{c.strike:g}{c.kind}", c.kind, c.strike, side, qty, b.start, b.open, px)
    lg.charges += ctx.costs.order(side, px, qty, b.start.date())
    return lg


def _exit_leg(ctx: Ctx, tr: CandleTrade, lg: LegTrade, c: Contract, after: datetime) -> None:
    """Close ``lg`` at the next bar's open; with no later bar, at the last close (flagged)."""
    side = "SELL" if lg.side == "BUY" else "BUY"
    s = ctx.market.series(c)
    b = s.next_after(after)
    if b is None or b.start.date() > c.expiry:
        last = s.last(after)
        ref = last.close if last is not None else lg.entry_ref
        tr.flags.append(f"{lg.label}: no bar after {hhmm(after)}; closed at the last trade")
        at = after + timedelta(minutes=1)
    else:
        ref, at = b.open, b.start
    lg.exit_at, lg.exit_ref = at, ref
    lg.exit_px = ctx.haircut.fill(side, ref)
    lg.charges += ctx.costs.order(side, lg.exit_px, lg.qty, at.date())


def _is_trading_day(cal: Any, d: date) -> bool:
    try:
        return bool(cal.is_trading_day(d))
    except Exception:
        return d.weekday() < 5


def _trading_day_before(cal: Any, d: date) -> date:
    x = d - timedelta(days=1)
    while not _is_trading_day(cal, x):
        x -= timedelta(days=1)
    return x


def build_iv_history(ctx: Ctx, sessions: Sequence[date], before: date) -> IvHistory:
    """Closing ATM IVs of the warm-up sessions (from ``iv_history_from``) before ``before``."""
    h = IvHistory(min_history=ctx.iv_min_history)
    if ctx.iv_history_from is None:
        return h
    src = ctx.market.src
    for d in src.sessions(ctx.iv_history_from, before - timedelta(days=1)):
        h.add(d, ctx.market.daily_atm_iv(d))
    return h


# =============================================================================== condor


def _contracts_of(ctx: Ctx, tr: CandleTrade) -> dict[str, Contract]:
    out = {}
    for lg in tr.legs:
        c = ctx.market.contract(tr.expiry, lg.strike, lg.kind)
        if c is not None:
            out[lg.label] = c
    return out


def _marks(
    ctx: Ctx, tr: CandleTrade, cons: dict[str, Contract], at: datetime
) -> dict[str, float] | None:
    """Each open leg's last trade at ``at`` (None if a leg never traded)."""
    out = {}
    for lg in tr.legs:
        if not lg.open:
            continue
        b = ctx.market.series(cons[lg.label]).last(at)
        if b is None:
            return None
        if (at - b.start) > timedelta(minutes=ctx.stale_mark_min):
            tr.context["stale_marks"] = int(tr.context.get("stale_marks", 0)) + 1
        out[lg.label] = b.close
    return out


def _ctc(ctx: Ctx, tr: CandleTrade, px: dict[str, float], kind: str | None = None) -> float:
    """Cost to close the open legs (per unit) for the exit rules: shorts bought, wings sold,
    at last trades (or at the haircut with ``trigger_on_haircut``)."""
    tot = 0.0
    for lg in tr.legs:
        if not lg.open or (kind and lg.kind != kind):
            continue
        p = px[lg.label]
        if ctx.trigger_on_haircut:
            tot += ctx.haircut.buy(p) if lg.side == "SELL" else -ctx.haircut.sell(p)
        else:
            tot += p if lg.side == "SELL" else -p
    return tot


def _tin(ctx: Ctx, lg: LegTrade) -> float:
    """A leg's entry price for the exit rules: the trade price it filled from (the mid-like
    reference, ``entry_ref``), or the haircut fill with ``trigger_on_haircut``. Exit rules read
    last trades, so their credit must be at last trades too; P&L always uses the fills."""
    return lg.entry_px if ctx.trigger_on_haircut else lg.entry_ref


def _tout(ctx: Ctx, lg: LegTrade) -> float:
    return lg.exit_px if ctx.trigger_on_haircut else lg.exit_ref


def _rule_credit(
    ctx: Ctx, tr: CandleTrade, kind: str | None = None, open_only: bool = False
) -> float:
    """Per-unit credit the exit rules compare against (all legs, or one side's open legs)."""
    return sum(
        (_tin(ctx, lg) if lg.side == "SELL" else -_tin(ctx, lg))
        for lg in tr.legs
        if (kind is None or lg.kind == kind) and (lg.open or not open_only)
    )


def _booked_debit(ctx: Ctx, tr: CandleTrade) -> float:
    """Per unit, what closing already-closed legs cost (as ``booked_debit`` live)."""
    return sum(
        (_tout(ctx, lg) if lg.side == "SELL" else -_tout(ctx, lg)) for lg in tr.legs if not lg.open
    )


def _mtm(tr: CandleTrade, ctx: Ctx, px: dict[str, float]) -> float:
    """Net P&L if every open leg were closed at the haircut now (exit charges excluded)."""
    total = tr.gross - tr.charges
    for lg in tr.legs:
        if lg.open:
            p = px[lg.label]
            exit_px = ctx.haircut.sell(p) if lg.side == "BUY" else ctx.haircut.buy(p)
            sign = 1.0 if lg.side == "BUY" else -1.0
            total += sign * (exit_px - lg.entry_px) * lg.qty
    return total


@dataclass(frozen=True)
class CondorVariant:
    """A condor rule to test: the live ``CondorConfig`` plus the structure. ``TREND_SPREAD``
    sells only the side the trend favours (a bull put spread when the index rose over the last
    ``trend_lookback_min`` minutes, a bear call spread when it fell): the candle stand-in for
    the live rule's one-sided spread on a confirmed order-flow setup."""

    cfg: CondorConfig = field(default_factory=CondorConfig)
    structure: str = "IRON_CONDOR"  # or "TREND_SPREAD"
    trend_lookback_min: int = 30
    skip_pre_closure: bool = False  # no entries on the last session before a market closure
    label: str = "live config"


def condor_variants(cfg: CondorConfig) -> list[CondorVariant]:
    """Main session, 13 Nov 2026: short delta 0.16 / 0.20 x stop 2.0x / 1.5x credit x iron
    condor / trend-side 2-leg spread (take-profit as configured), the live config first; then
    the live config without the per-side stop, and without entries before a closure."""
    from dataclasses import replace

    out = []
    for structure in ("IRON_CONDOR", "TREND_SPREAD"):
        for delta in (cfg.short_delta, 0.20 if cfg.short_delta != 0.20 else 0.16):
            for stop in (cfg.stop_mult, 1.5 if cfg.stop_mult != 1.5 else 2.0):
                name = "iron condor" if structure == "IRON_CONDOR" else "trend-side spread"
                label = f"{name}, delta {delta:.2f}, stop {stop:g}x"
                live = structure == "IRON_CONDOR" and delta == cfg.short_delta
                live = live and stop == cfg.stop_mult
                out.append(
                    CondorVariant(
                        replace(cfg, short_delta=delta, stop_mult=stop),
                        structure,
                        label=label + (" (live config)" if live else ""),
                    )
                )
    # Owner 14 Nov: the live config without the 2.2x per-side stop; main session 14 Nov: no
    # entries on a Friday / pre-holiday session (v1 had 44 Friday entries, 19 booked that
    # afternoon by the weekend rule).
    out.append(
        CondorVariant(replace(cfg, side_stop_mult=0.0), label="iron condor, no per-side stop")
    )
    out.append(
        CondorVariant(cfg, skip_pre_closure=True, label="iron condor, no entry before a closure")
    )
    return out


def _condor_plan(
    ctx: Ctx,
    cfg: CondorConfig,
    at: datetime,
    expiry: date,
    spot: float,
    ivh: IvHistory,
    sides: tuple[str, ...] = ("PE", "CE"),
) -> tuple[dict[str, Any] | None, str]:
    m = ctx.market
    fi = m.forward_iv(at, expiry, spot)
    if fi is None:
        return None, "no forward/IV (fewer than 3 synchronous near-ATM pairs)"
    ivp, n = ivh.percentile(at.date(), fi.atm_iv)
    if ivp is None:
        return None, f"IV history {n} < {ivh.min_history} sessions"
    if ivp < cfg.min_iv_percentile:
        return None, "IV percentile below the minimum"
    picks = {}
    for kind in sides:
        got = m.pick_by_delta(
            expiry, kind, cfg.short_delta, fi, at, max_age_min=ctx.stale_entry_min, otm_only=True
        )
        if got is None:
            return None, "no strike near the target delta traded recently"
        picks[kind] = got
    limit = ctx.capital_inr * float(ctx.risk["per_trade_max_loss_pct"]["defined_risk_spread"]) / 100
    tried = []
    for width in cfg.wing_widths:
        legs = []
        credit = 0.0
        for kind, sign in (("PE", -1.0), ("CE", 1.0)):
            if kind not in sides:
                continue
            short, s_px, _d = picks[kind]
            wing = m.contract(expiry, short.strike + sign * width, kind)
            w_px = m.close(wing, at, ctx.stale_entry_min) if wing is not None else None
            if wing is None or not w_px:
                break
            legs.append((wing, "BUY"))
            legs.append((short, "SELL"))
            credit += ctx.haircut.sell(s_px) - ctx.haircut.buy(w_px)
        else:
            lot = legs[0][0].lot
            max_loss = max(0.0, width - credit) * lot
            if credit < width * cfg.min_credit_pct_of_width / 100:
                tried.append("credit below the minimum % of width")
                continue
            if max_loss > limit:
                tried.append("worst case above the per-trade limit")
                continue
            return {
                "legs": legs,
                "width": width,
                "credit_est": credit,
                "ivp": ivp,
                "atm_iv": fi.atm_iv,
                "forward": fi.forward,
                "approx_clock": fi.approx_clock,
            }, "ok"
        tried.append("wing not traded recently")
    return None, tried[-1] if tried else "no width fits"


def _gap_worst(ctx: Ctx, tr: CandleTrade, at: datetime, spot: float) -> tuple[str, float] | None:
    """Worst +-5/8% gap P&L of the open legs (``engine/cockpit/stress.py``) on candle IVs."""
    m = ctx.market
    fi = m.forward_iv(at, tr.expiry, spot)
    if fi is None:
        return None
    detail: dict[str, dict[str, Any]] = {}
    for lg in tr.legs:
        if not lg.open:
            continue
        c = m.contract(tr.expiry, lg.strike, lg.kind)
        got = m.iv_delta(c, at, fi, ctx.stale_mark_min) if c is not None else None
        if got is None:
            return None
        detail[lg.label] = {
            "iv": got[1] * 100,
            "forward": fi.forward,
            "strike": lg.strike,
            "t_var": fi.t_var,
        }
    return worst(gap_pnl([lg for lg in tr.legs if lg.open], detail, spot))


def _condor_exit(
    ctx: Ctx, cfg: CondorConfig, tr: CandleTrade, bar: Bar, px: dict[str, float]
) -> str | None:
    dec = bar.start + timedelta(minutes=1)
    if hhmm(dec) < cfg.first_candle_end:
        return None
    ctc = _ctc(ctx, tr, px) + _booked_debit(ctx, tr)
    cr = _rule_credit(ctx, tr)
    if cr > 0:
        if ctc <= cr * (1 - cfg.take_profit_pct / 100):
            return f"take profit ({cfg.take_profit_pct:g}% of credit)"
        if ctc >= cr * cfg.stop_mult:
            return f"stop ({cfg.stop_mult:g}x credit)"
        d = dec.astimezone(IST).date()
        target = cr * cfg.take_profit_pct / 100
        if (
            not _is_trading_day(ctx.calendar, d + timedelta(days=1))
            and hhmm(dec) >= cfg.weekend_book_time
            and target > 0
            and cr - ctc >= target * cfg.weekend_book_pct / 100
        ):
            return "booked before the market closure (take-profit nearly in hand)"
        got = cr - ctc
        ratio = float(getattr(cfg, "weekend_reward_gap_ratio", 0.0))
        if (
            ratio > 0
            and not _is_trading_day(ctx.calendar, d + timedelta(days=1))
            and hhmm(dec) >= cfg.weekend_book_time
            and target > 0
            and got >= target * 0.25
        ):
            w = _gap_worst(ctx, tr, bar.start, bar.close)
            left = (target - got) * tr.legs[0].qty
            if w is not None and w[1] < 0 and left < ratio * -w[1]:
                return "booked before the market closure (little left vs the gap risk)"
    for kind, name in (("PE", "put"), ("CE", "call")):
        legs = [lg for lg in tr.legs if lg.kind == kind and lg.open]
        if len(legs) < 2:
            continue
        side_cr = _rule_credit(ctx, tr, kind, open_only=True)
        stop_x = cfg.side_stop_mult  # 0 or less = no per-side stop (a backtest variant)
        if stop_x > 0 and side_cr > 0 and _ctc(ctx, tr, px, kind) >= side_cr * stop_x:
            return f"{name} side stop ({cfg.side_stop_mult:g}x its credit)"
    if cfg.exit_on_short_touch:
        sp = [lg.strike for lg in tr.legs if lg.open and lg.side == "SELL" and lg.kind == "PE"]
        sc = [lg.strike for lg in tr.legs if lg.open and lg.side == "SELL" and lg.kind == "CE"]
        if (sp and bar.low <= sp[0]) or (sc and bar.high >= sc[0]):
            return "index touched a short strike"
    return None


def _untested_side(
    ctx: Ctx, cfg: CondorConfig, tr: CandleTrade, bar: Bar, px: dict[str, float]
) -> str | None:
    if tr.context.get("adjustments", 0) >= 1:
        return None
    if hhmm(bar.start + timedelta(minutes=1)) < cfg.first_candle_end:
        return None
    sides = {}
    for kind in ("PE", "CE"):
        legs = [lg for lg in tr.legs if lg.kind == kind and lg.open]
        if len(legs) < 2:
            return None
        cr = _rule_credit(ctx, tr, kind, open_only=True)
        sides[kind] = (cr, _ctc(ctx, tr, px, kind))
    for k, other in (("PE", "CE"), ("CE", "PE")):
        cr, ctc = sides[k]
        ocr, octc = sides[other]
        if cr > 0 and ctc <= cr * (1 - cfg.untested_decay_pct / 100) and octc >= ocr:
            return k
    return None


def _economics(tr: CandleTrade, cfg: CondorConfig) -> dict[str, float | None]:
    """Main's per-trade economics on the actual fills and charges: net take-profit, net stop,
    the win rate that breaks even (net stop / (net TP + net stop)) and TP on margin."""
    qty = tr.legs[0].qty
    rt = tr.charges
    net_tp = tr.credit_pts * cfg.take_profit_pct / 100 * qty - rt
    net_stop = tr.credit_pts * (cfg.stop_mult - 1) * qty + rt
    return {
        "net_tp": net_tp,
        "net_stop": net_stop,
        "breakeven_win_pct": net_stop / (net_tp + net_stop) * 100
        if net_tp + net_stop > 0
        else None,
        "tp_on_margin_pct": net_tp / tr.margin_inr * 100 if tr.margin_inr else None,
    }


def simulate_condor(
    ctx: Ctx, sessions: Sequence[date], params: CondorVariant | CondorConfig
) -> SimOut:
    var = params if isinstance(params, CondorVariant) else CondorVariant(params)
    cfg = var.cfg
    m = ctx.market
    skips: Counter[str] = Counter()
    trades: list[CandleTrade] = []
    if not sessions:
        return SimOut(trades, skips, [])
    ivh = build_iv_history(ctx, sessions, sessions[0])
    warm = len(ivh.values)
    tr: CandleTrade | None = None
    cons: dict[str, Contract] = {}
    for d in sessions:
        bars = m.src.index_bars(d)
        if not bars:
            continue
        opened_today = 0
        day_skip: set[str] = set()
        exp = m.nearest_expiry(d)
        for i, bar in enumerate(bars):
            t = bar.start
            dec = t + timedelta(minutes=1)
            if tr is not None:
                exit_day = _trading_day_before(ctx.calendar, tr.expiry)
                why = None
                if d > exit_day or (d == exit_day and hhmm(dec) >= cfg.exit_time):
                    why = f"time exit before expiry ({cfg.exit_time} on {exit_day:%d %b})"
                px = _marks(ctx, tr, cons, t)
                if why is None and px is not None:
                    why = _condor_exit(ctx, cfg, tr, bar, px)
                    if why is None:
                        side = _untested_side(ctx, cfg, tr, bar, px)
                        if side is not None:
                            for lg in sorted(
                                (x for x in tr.legs if x.open and x.kind == side),
                                key=lambda x: x.side != "SELL",  # buy back the short first
                            ):
                                _exit_leg(ctx, tr, lg, cons[lg.label], t)
                            tr.context["adjustments"] = 1
                            tr.flags.append(f"booked the untested {side} side at {hhmm(dec)}")
                if why is not None:
                    for lg in sorted(
                        (x for x in tr.legs if x.open), key=lambda x: x.side != "SELL"
                    ):
                        _exit_leg(ctx, tr, lg, cons[lg.label], t)
                    tr.exit_at = max(lg.exit_at for lg in tr.legs if lg.exit_at is not None)
                    tr.reason_out = why
                    tr.context["economics"] = _economics(tr, cfg)
                    trades.append(tr)
                    tr = None
                continue
            if opened_today >= cfg.max_per_day or not in_window(
                dec, (cfg.first_entry, cfg.last_entry)
            ):
                continue
            if dec.minute % max(1, ctx.eval_every_min):
                continue
            if var.skip_pre_closure and not _is_trading_day(ctx.calendar, d + timedelta(days=1)):
                day_skip.add("last session before a market closure")
                continue
            if exp is None or (exp - d).days < cfg.min_days_to_expiry:
                day_skip.add("nearest expiry closer than min_days_to_expiry")
                continue
            structure = "IRON_CONDOR"
            sides: tuple[str, ...] = ("PE", "CE")
            if var.structure == "TREND_SPREAD":
                ref = bars[max(0, i - var.trend_lookback_min)].close
                if bar.close > ref:
                    structure, sides = "BULL_PUT", ("PE",)
                elif bar.close < ref:
                    structure, sides = "BEAR_CALL", ("CE",)
                else:
                    day_skip.add("no trend over the lookback")
                    continue
            plan, why = _condor_plan(ctx, cfg, t, exp, bar.close, ivh, sides)
            if plan is None:
                day_skip.add(why)
                continue
            legs: list[LegTrade] = []
            for c, side in plan["legs"]:  # wings (BUY) come first in the plan: hedge first
                got = _fill_leg(ctx, c, side, c.lot, t, ctx.fill_within_min)
                if got is None:
                    break
                legs.append(got)
            if len(legs) < len(plan["legs"]):
                day_skip.add("a leg did not trade within the fill window")
                continue
            credit = sum(lg.entry_px for lg in legs if lg.side == "SELL") - sum(
                lg.entry_px for lg in legs if lg.side == "BUY"
            )
            tr = CandleTrade(
                "condor",
                structure,
                exp,
                max(lg.entry_at for lg in legs),
                f"IVP {plan['ivp']:.0f}, ATM IV {plan['atm_iv'] * 100:.1f}%, wings "
                f"{plan['width']:g}, est. credit {plan['credit_est']:.1f}",
                legs,
                credit_pts=round(credit, 2),
                width=float(plan["width"]),
                margin_inr=table_margin(ctx.costs.book, structure, 1, ctx.run_date),
                context={"ivp": plan["ivp"], "atm_iv": plan["atm_iv"]},
            )
            if plan["approx_clock"]:
                tr.flags.append("expiry clock approximated (holidays not loaded)")
            cons = _contracts_of(ctx, tr)
            opened_today += 1
        if tr is not None:
            last = bars[-1].start
            px = _marks(ctx, tr, cons, last)
            if px is not None:
                tr.marks[d] = _mtm(tr, ctx, px)
        for s in day_skip:
            skips[s] += 1
        ivh.add(d, m.daily_atm_iv(d))  # known after the close: used from the next session
    if tr is not None:
        tr.flags.append("still open at the end of the range (not counted)")
        skips["open at the end of the range"] += 1
    return SimOut(trades, skips, list(sessions), warm)


# ================================================================================ Log-3


@dataclass(frozen=True)
class Log3Params:
    window: tuple[str, str] = Log3MomentumPlay.window
    mom_min_pct: float = Log3MomentumPlay.mom_min_pct
    near_extreme_pct: float = Log3MomentumPlay.near_extreme_pct
    delta: float = Log3MomentumPlay.delta
    stop_pct: float = Log3MomentumPlay.stop_pct
    target_pct: float = Log3MomentumPlay.target_pct
    gap_min: float = Log3MomentumPlay.gap_min
    max_entries: int = 3  # LongPlay.max_entries
    lots: int = 1
    square_off: str = "15:20"
    min_fit_stop_pct: float = 10.0  # LongPlay.min_fit_stop_pct


def _long_exit(
    ctx: Ctx, s: Series, entry: LegTrade, stop: float, target: float, square_off: str
) -> tuple[datetime, float, float, str]:
    """Walk the option's bars from the entry bar: (time, ref price, fill, reason)."""
    h = ctx.haircut
    day = entry.entry_at.astimezone(IST).date()
    sq = datetime.combine(day, time.fromisoformat(square_off), tzinfo=IST)
    last: Bar | None = None
    for b in s.between(entry.entry_at - timedelta(seconds=1), sq + timedelta(hours=12)):
        if b.start.astimezone(IST).date() != day:
            break
        if b.start >= sq:
            return b.start, b.open, h.sell(b.open), f"square-off {square_off}"
        if ctx.trigger_on_haircut:  # the level is a bid proxy and fills as it is
            o, lo, hi = h.sell(b.open), h.sell(b.low), h.sell(b.high)
            s_ref, s_px = stop + h.half(stop), stop
            t_ref, t_px = target + h.half(target), target
        else:  # the level is a last trade; the fill pays the haircut
            o, lo, hi = b.open, b.low, b.high
            s_ref, s_px = stop, h.sell(stop)
            t_ref, t_px = target, h.sell(target)
        if b.start > entry.entry_at:
            if o <= stop:
                return b.start, b.open, h.sell(b.open), "stop (opened through it)"
            if o >= target:
                return b.start, b.open, h.sell(b.open), "target (opened through it)"
        if lo <= stop:
            return b.start, s_ref, s_px, f"stop {ctx_pct(stop, entry.entry_px)}"
        if hi >= target:
            return b.start, t_ref, t_px, "target"
        last = b
    ref = last.close if last is not None else entry.entry_ref
    at = (last.start if last is not None else entry.entry_at) + timedelta(minutes=1)
    return at, ref, h.sell(ref), "no bar at the square-off: last trade"


def ctx_pct(level: float, entry: float) -> str:
    return f"({(level / entry - 1) * 100:+.0f}%)" if entry else ""


def simulate_log3(ctx: Ctx, sessions: Sequence[date], p: Log3Params) -> SimOut:
    m = ctx.market
    skips: Counter[str] = Counter()
    trades: list[CandleTrade] = []
    cap = ctx.capital_inr * float(ctx.risk["per_trade_max_loss_pct"]["directional"]) / 100
    daily_limit = ctx.capital_inr * float(ctx.risk["daily_loss_limit_pct"]) / 100
    max_trades = int(ctx.risk.get("max_trades_per_day", 5))
    cool = timedelta(minutes=float(ctx.risk.get("cooloff_after_loss_min", 15)))
    for d in sessions:
        bars = m.src.index_bars(d)
        if not bars:
            continue
        exp = m.nearest_expiry(d)
        entries = 0
        realized = 0.0
        busy_until: datetime | None = None
        cool_until: datetime | None = None
        last_at: dict[bool, datetime] = {}
        day_skip: set[str] = set()
        for i, bar in enumerate(bars):
            t = bar.start
            dec = t + timedelta(minutes=1)
            if busy_until is not None and t < busy_until:
                continue
            if not in_window(dec, p.window) or entries >= min(p.max_entries, max_trades):
                continue
            if realized <= -daily_limit:
                day_skip.add("daily loss limit")
                continue
            if cool_until is not None and dec < cool_until:
                continue
            for bull in (True, False):
                f = momentum_state(list(bars[: i + 1]), bull)
                if f is None:
                    break
                ok = (
                    f["mom15_pct"] >= p.mom_min_pct
                    and f["beyond_or_pct"] > 0
                    and f["from_extreme_pct"] <= p.near_extreme_pct
                )
                prev = last_at.get(bull)
                if not ok or (prev is not None and dec - prev < timedelta(minutes=p.gap_min)):
                    continue
                if exp is None:
                    day_skip.add("no expiry listed")
                    continue
                fi = m.forward_iv(t, exp, bar.close)
                if fi is None:
                    day_skip.add("no forward/IV at the signal")
                    continue
                kind = "CE" if bull else "PE"
                pick = m.pick_by_delta(
                    exp, kind, p.delta, fi, t, max_age_min=ctx.stale_entry_min, n=4
                )
                if pick is None:
                    day_skip.add("no ~0.5 delta option traded recently")
                    continue
                c = pick[0]
                last_at[bull] = dec
                qty = c.lot * p.lots
                leg = _fill_leg(ctx, c, "BUY", qty, t, ctx.fill_within_min)
                if leg is None:
                    day_skip.add("option did not trade within the fill window")
                    continue
                stop_pct = p.stop_pct
                if leg.entry_px * qty * stop_pct / 100 > cap:
                    fit = cap * 0.98 / (leg.entry_px * qty) * 100
                    if fit < p.min_fit_stop_pct:
                        day_skip.add("premium too dear for the per-trade limit")
                        continue
                    stop_pct = fit
                # Levels from the entry's trade price (last-trade triggers) or from the fill
                # (``trigger_on_haircut``); either way the fill pays the haircut.
                basis = leg.entry_px if ctx.trigger_on_haircut else leg.entry_ref
                stop = round(basis * (1 - stop_pct / 100), 2)
                target = round(basis * (1 + p.target_pct / 100), 2)
                at, ref, px, why = _long_exit(ctx, m.series(c), leg, stop, target, p.square_off)
                leg.exit_at, leg.exit_ref, leg.exit_px = at, ref, px
                leg.charges += ctx.costs.order("SELL", px, qty, at.date())
                tr = CandleTrade(
                    "log3",
                    "LONG_CALL" if bull else "LONG_PUT",
                    exp,
                    leg.entry_at,
                    f"15-min {f['mom15_pct']:+.2f}%, {f['from_extreme_pct']:.2f}% off the "
                    f"{'high' if bull else 'low'}, beyond the OR; delta {pick[2]:+.2f}",
                    [leg],
                    exit_at=at,
                    reason_out=why,
                    context={"stop_pct": stop_pct, "atm_iv": fi.atm_iv},
                )
                if fi.approx_clock:
                    tr.flags.append("expiry clock approximated (holidays not loaded)")
                trades.append(tr)
                entries += 1
                realized += tr.net
                busy_until = at
                if tr.net < 0:
                    cool_until = at + cool
                break
        for s in day_skip:
            skips[s] += 1
    return SimOut(trades, skips, list(sessions))


def _coerce(old: Any, raw: str) -> Any:
    if isinstance(old, bool):
        if raw.lower() not in ("true", "false", "1", "0"):
            raise ValueError(f"expected true/false, got {raw!r}")
        return raw.lower() in ("true", "1")
    if isinstance(old, int):
        return int(raw)
    if isinstance(old, float):
        return float(raw)
    if isinstance(old, tuple):
        return tuple(float(x) for x in raw.split("/") if x)
    return raw


def apply_overrides(params: Any, text: str) -> Any:
    """``"side_stop_mult=0,short_delta=0.2"`` -> a copy of ``params`` with those fields set
    (on ``.cfg`` for a ``CondorVariant``; tuples as ``200/150``). Every override is a new rule,
    so the trial registry counts it."""
    from dataclasses import fields, replace

    pairs = [x.split("=", 1) for x in text.split(",") if x.strip()]
    target = params.cfg if isinstance(params, CondorVariant) else params
    names = {f.name for f in fields(target)}
    changes: dict[str, Any] = {}
    for key, raw in pairs:
        key = key.strip()
        if key not in names:
            raise ValueError(f"unknown parameter {key!r} for {type(target).__name__}")
        changes[key] = _coerce(getattr(target, key), raw.strip())
    new = replace(target, **changes)
    if isinstance(params, CondorVariant):
        return replace(params, cfg=new, label=f"{params.label}; {text}")
    return new


# ============================================================================= registry


@dataclass(frozen=True)
class StrategySpec:
    key: str
    title: str
    ref: str
    run: Callable[[Ctx, Sequence[date], Any], SimOut]
    params: Callable[[Any], Any]  # cockpit config -> parameters (also hashed as the trial)
    deviations: tuple[str, ...] = ()
    variants: Callable[[Any], list[Any]] | None = None  # parameter sets compared (each a trial)


COMMON_DEVIATIONS = (
    "Fills at the next 1-minute bar's open +/- the haircut, not at a recorded bid/ask; a quiet "
    "leg's last trade can be minutes old (marks older than 15 min are counted).",
    "IV from last trades, not mids: synthetic forward from 3-5 near-ATM strikes whose call and "
    "put both traded within 2 minutes.",
    "No fill latency, partial fills, liquidity gate or liquidity-vacuum guard (no depth).",
)

STRATEGIES: dict[str, StrategySpec] = {
    "condor": StrategySpec(
        "condor",
        "Iron condor (SPEC 5.1)",
        "SPEC 5.1",
        simulate_condor,
        lambda ck: CondorVariant(ck.condor if ck is not None else CondorConfig()),
        variants=lambda ck: condor_variants(ck.condor if ck is not None else CondorConfig()),
        deviations=(
            "No order-flow veto: confirmed setups need the live tape and depth. The iron "
            "condor variants always sell both sides; the trend-side variants sell only the side "
            "the last 30 minutes of the index favour (a stand-in for a confirmed setup).",
            "No event filter (RBI / Budget / results): config/events holds no history before "
            "Nov 2026, so the live blackout cannot be replayed point in time.",
            "IV percentile against this runner's own closing ATM IVs (14:45-15:15 median, from "
            "Oct 2024), not the EOD bhavcopy history the cockpit loads; >= 20 sessions needed.",
            "Entry checked every 5 minutes (live: every update); the plan's prices must be "
            "<= 5 min old and every leg must trade within 3 min of the decision.",
            "Exit rules (take-profit, stops, adjustments, weekend booking) compare the cost to "
            "close at last trades with the credit at trade prices; the live rules read the ask "
            "of shorts and the bid of wings against the credit actually received. Every fill "
            "pays the haircut.",
            "Margin is the 13 Nov 2026 broker-quote table, not the margin of the day.",
        ),
    ),
    "log3": StrategySpec(
        "log3",
        "Log-3 momentum (mined rule)",
        "SPEC 5.2 / log3_entry_features",
        simulate_log3,
        lambda _ck: Log3Params(),
        deviations=(
            "Nifty only (the live account also trades Sensex).",
            "Stop and target are set from the entry's trade price, trigger on the bar's low/high "
            "(last trades) and fill at the level less the haircut, or at the open less the "
            "haircut when a bar opens beyond it; a bar that touches both counts as the stop.",
            "The 2.5% per-trade limit, 2% daily loss limit, 15-min cool-off after a loss and the "
            "5-trade day cap are applied; the weekly loss limit and loss-streak sizing are not.",
        ),
    ),
}
ALIASES = {"log3_mom": "log3", "iron_condor": "condor"}


def resolve(name: str) -> StrategySpec:
    key = ALIASES.get(name.strip().lower(), name.strip().lower())
    if key in REFUSED:
        raise StrategyRefused(
            f"{key}: refused on candles: {REFUSED[key]}. Order-book strategies are tested only "
            "on recorded depth (tasks.py replay)."
        )
    if key in NOT_BUILT:
        raise StrategyRefused(f"{key}: allowed on candles but not built yet ({NOT_BUILT[key]})")
    if key not in STRATEGIES:
        raise StrategyRefused(f"{key}: unknown strategy (built: {', '.join(sorted(STRATEGIES))})")
    return STRATEGIES[key]


__all__ = [
    "COMMON_DEVIATIONS",
    "NOT_BUILT",
    "REFUSED",
    "STRATEGIES",
    "CandleTrade",
    "CondorVariant",
    "Ctx",
    "LegTrade",
    "Log3Params",
    "SimOut",
    "StrategyRefused",
    "StrategySpec",
    "apply_overrides",
    "condor_variants",
    "resolve",
    "simulate_condor",
    "simulate_log3",
]
