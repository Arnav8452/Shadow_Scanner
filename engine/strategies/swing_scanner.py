"""Nightly swing scanner on daily bars (SPEC §5.8). Paper only; every threshold is a HYPOTHESIS.

Setups on day ``T`` (bars known at 19:30 IST on ``T``; trades go in at ``T+1``'s open):

* ``BREAKOUT`` (long): close clearly above a resistance touched ``min_touches``+ times in the
  lookback, on ``vol_mult``x the 20-session average volume, the first close above it; optional
  coil filter (tight range before the break).
* ``BREAKDOWN`` (bearish mirror): close clearly below multi-touch support on volume. Puts only:
  retail cannot hold cash shorts overnight.
* ``SUPPORT_BOUNCE`` (long): slow up-trend (close above a rising 50-session average), the low
  tags multi-touch support, and a reversal candle closes in the upper part of its range, up on
  the day.

Instrument by IV rank: low IV rank -> the ATM option (delta ~0.5) of the nearest expiry at least
``min_option_days`` trading days away; high or unknown IV rank, results inside the window, the
F&O ban, or a premium larger than the risk budget -> the cash stock (longs) or no trade (bearish).

Stops: the line (a daily close back through it) plus a premium stop for options and a 5-7% stop
for cash; a time stop after ``time_stop_sessions`` without follow-through. Sizing: an option's
whole premium is its risk; cash quantity from the stop distance.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from engine.data.eod.bhavcopy import CashBhavcopyRow, FnOBhavcopyRow


class Setup(StrEnum):
    BREAKOUT = "BREAKOUT"
    BREAKDOWN = "BREAKDOWN"
    SUPPORT_BOUNCE = "SUPPORT_BOUNCE"


@dataclass(frozen=True)
class SwingConfig:
    """``swing`` in settings.yaml. HYPOTHESIS values (untested) unless stated."""

    lookback: int = 60
    pivot_span: int = 2  # a swing high/low is the extreme of +-2 sessions
    min_touches: int = 2
    touch_tol_pct: float = 0.75  # pivots within this % form one level
    clear_pct: float = 0.5  # "clearly above": close this % beyond the level
    vol_mult: float = 1.5
    vol_avg_days: int = 20
    coil_filter: bool = False
    coil_days: int = 10
    coil_max_range_pct: float = 8.0
    sma_days: int = 50
    bounce_close_in_range: float = 0.6  # close in the top 40% of the day's range
    min_price: float = 50.0
    min_turnover_cr: float = 10.0  # average daily traded value, ₹ crore
    iv_rank_low: float = 50.0  # at or below -> option; above -> cash
    min_option_days: int = 5  # trading days to expiry (SPEC §5.8)
    option_premium_stop_pct: float = 25.0  # SPEC: line stop capped at 20-25% of premium
    time_stop_sessions: int = 2  # SPEC
    cash_stop_pct: float = 6.0  # SPEC: 5-7%
    max_hold_sessions: int = 15
    option_max_premium_pct: float = 2.5  # of capital = settings.risk directional max loss
    cash_risk_pct: float = 2.5  # of capital
    max_cash_value_pct: float = 25.0  # of capital per position
    results_buffer_days: int = 10  # calendar days: no long option if results fall inside
    max_candidates: int = 10


@dataclass(frozen=True, slots=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_row(cls, r: CashBhavcopyRow) -> Bar:
        return cls(r.trade_date, r.open, r.high, r.low, r.close, float(r.traded_qty))


@dataclass(frozen=True)
class Candidate:
    symbol: str
    day: date
    setup: Setup
    direction: str  # LONG | SHORT (bearish view)
    level: float  # the line: a close back through it is the price stop
    close: float
    volume_mult: float
    touches: int
    coil: bool
    rationale: str


def _pivots(bars: Sequence[Bar], span: int, high: bool) -> list[float]:
    out: list[float] = []
    for i in range(span, len(bars) - span):
        win = bars[i - span : i + span + 1]
        v = bars[i].high if high else bars[i].low
        if v == (max(b.high for b in win) if high else min(b.low for b in win)):
            out.append(v)
    return out


def levels(
    bars: Sequence[Bar], *, span: int, tol_pct: float, min_touches: int, high: bool
) -> list[tuple[float, int]]:
    """Price levels touched ``min_touches``+ times: pivots within ``tol_pct`` of each other are
    one level (their mean). Returns (level, touches)."""
    piv = sorted(_pivots(bars, span, high))
    groups: list[list[float]] = []
    for p in piv:
        if groups and p <= (sum(groups[-1]) / len(groups[-1])) * (1 + tol_pct / 100.0):
            groups[-1].append(p)
        else:
            groups.append([p])
    return [(sum(g) / len(g), len(g)) for g in groups if len(g) >= min_touches]


def _sma(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def scan_symbol(symbol: str, bars: Sequence[Bar], cfg: SwingConfig) -> Candidate | None:
    """Evaluate the last bar of ``bars`` (oldest first). ``None`` when nothing qualifies."""
    need = max(cfg.lookback, cfg.sma_days + 10, cfg.vol_avg_days) + 1
    if len(bars) < need:
        return None
    t, prev = bars[-1], bars[-2]
    hist = bars[-cfg.lookback - 1 : -1]  # levels from the lookback, never including T
    avg_vol = _sma([b.volume for b in bars[-cfg.vol_avg_days - 1 : -1]])
    avg_value_cr = _sma([b.volume * b.close for b in bars[-cfg.vol_avg_days - 1 : -1]]) / 1e7
    if t.close < cfg.min_price or avg_value_cr < cfg.min_turnover_cr or avg_vol <= 0:
        return None
    vmult = t.volume / avg_vol
    tol = cfg.touch_tol_pct / 100.0
    clear = cfg.clear_pct / 100.0
    window = bars[-cfg.coil_days - 1 : -1]
    coil_range = (max(b.high for b in window) - min(b.low for b in window)) / prev.close * 100.0
    coil = coil_range <= cfg.coil_max_range_pct

    if vmult >= cfg.vol_mult and (coil or not cfg.coil_filter):
        res = [
            (lv, n)
            for lv, n in levels(hist, span=cfg.pivot_span, tol_pct=cfg.touch_tol_pct,
                                min_touches=cfg.min_touches, high=True)
            if prev.close <= lv * (1 + tol) and t.close >= lv * (1 + clear)
        ]  # fmt: skip
        if res:
            lv, n = max(res)
            return Candidate(
                symbol, t.day, Setup.BREAKOUT, "LONG", round(lv, 2), t.close, round(vmult, 2), n,
                coil, f"close {t.close:.2f} above {n}-touch resistance {lv:.2f} on {vmult:.1f}x "
                f"volume" + (f"; {coil_range:.1f}% coil" if coil else ""),
            )  # fmt: skip
        sup = [
            (lv, n)
            for lv, n in levels(hist, span=cfg.pivot_span, tol_pct=cfg.touch_tol_pct,
                                min_touches=cfg.min_touches, high=False)
            if prev.close >= lv * (1 - tol) and t.close <= lv * (1 - clear)
        ]  # fmt: skip
        if sup:
            lv, n = min(sup)
            return Candidate(
                symbol, t.day, Setup.BREAKDOWN, "SHORT", round(lv, 2), t.close, round(vmult, 2),
                n, coil, f"close {t.close:.2f} below {n}-touch support {lv:.2f} on {vmult:.1f}x "
                "volume",
            )  # fmt: skip

    closes = [b.close for b in bars]
    sma_now = _sma(closes[-cfg.sma_days :])
    sma_before = _sma(closes[-cfg.sma_days - 10 : -10])
    rng = t.high - t.low
    reversal = (
        t.close > t.open and t.close > prev.close and rng > 0
        and (t.close - t.low) / rng >= cfg.bounce_close_in_range
    )  # fmt: skip
    if t.close > sma_now >= sma_before and reversal:
        sup = [
            (lv, n)
            for lv, n in levels(hist, span=cfg.pivot_span, tol_pct=cfg.touch_tol_pct,
                                min_touches=cfg.min_touches, high=False)
            if t.low <= lv * (1 + tol) and t.close > lv
        ]  # fmt: skip
        if sup:
            lv, n = max(sup)
            return Candidate(
                symbol, t.day, Setup.SUPPORT_BOUNCE, "LONG", round(lv, 2), t.close,
                round(vmult, 2), n, coil, f"reversal candle off {n}-touch support {lv:.2f} in an "
                f"up-trend (close above rising {cfg.sma_days}-session average {sma_now:.2f})",
            )  # fmt: skip
    return None


# --------------------------------------------------------------------------- vehicle and size


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    expiry: date
    strike: float
    option_type: str  # CE | PE
    close: float
    lot: int
    contracts: int

    @property
    def contract(self) -> str:
        return f"{self.symbol} {self.expiry.isoformat()} {self.strike:g} {self.option_type}"


@dataclass(frozen=True)
class SwingPlan:
    candidate: Candidate
    vehicle: str  # OPTION | CASH | SKIP
    reason: str
    iv_rank: float | None
    option: OptionQuote | None = None
    qty: int = 0  # shares (cash) or contracts' underlying quantity (lot x lots)
    est_entry: float = 0.0
    stop: float = 0.0  # cash hard stop / option premium stop
    risk_inr: float = 0.0
    notes: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        c = self.candidate
        return {
            "symbol": c.symbol,
            "day": c.day.isoformat(),
            "setup": c.setup.value,
            "direction": c.direction,
            "level": c.level,
            "close": c.close,
            "volume_mult": c.volume_mult,
            "touches": c.touches,
            "coil": c.coil,
            "rationale": c.rationale,
            "vehicle": self.vehicle,
            "reason": self.reason,
            "iv_rank": self.iv_rank,
            "contract": self.option.contract if self.option else None,
            "lot": self.option.lot if self.option else None,
            "qty": self.qty,
            "est_entry": self.est_entry,
            "stop": self.stop,
            "risk_inr": round(self.risk_inr, 2),
            "notes": list(self.notes),
        }


def atm_option(
    fo_rows: Sequence[FnOBhavcopyRow],
    symbol: str,
    option_type: str,
    *,
    day: date,
    min_trading_days: int,
    trading_days_to: Any,
) -> OptionQuote | None:
    """ATM option (strike nearest the underlying) of the nearest expiry at least
    ``min_trading_days`` trading days after ``day`` that traded on ``day``."""
    opts = [
        r for r in fo_rows
        if r.symbol == symbol and r.instrument in ("STO", "OPTSTK") and r.option_type == option_type
        and r.trade_date == day
    ]  # fmt: skip
    for exp in sorted({r.expiry for r in opts}):
        if trading_days_to(day, exp) < min_trading_days:
            continue
        chain = [r for r in opts if r.expiry == exp and r.close > 0]
        und = next((r.underlying_price for r in chain if r.underlying_price > 0), 0.0)
        if not chain or und <= 0:
            continue
        best = min(chain, key=lambda r: abs(r.strike - und))
        return OptionQuote(
            symbol, exp, best.strike, option_type, best.close, max(1, best.lot_size), best.contracts
        )
    return None


def plan_trade(
    cand: Candidate,
    *,
    cfg: SwingConfig,
    capital: float,
    iv_rank: float | None,
    has_fno: bool,
    banned: bool,
    results_date: date | None,
    option: OptionQuote | None,
) -> SwingPlan:
    """Choose the instrument and size for a candidate (SPEC §5.8 rules)."""
    why_not_option: list[str] = []
    if not has_fno:
        why_not_option.append("no F&O")
    if banned:
        why_not_option.append("F&O ban")
    if results_date is not None:
        why_not_option.append(f"results on {results_date.isoformat()}")
    if iv_rank is None:
        why_not_option.append("IV rank unknown")
    elif iv_rank > cfg.iv_rank_low:
        why_not_option.append(f"IV rank {iv_rank:.0f} > {cfg.iv_rank_low:.0f}")
    if option is None and not why_not_option:
        why_not_option.append(f"no option >= {cfg.min_option_days} trading days out")
    budget = cfg.option_max_premium_pct / 100.0 * capital
    if option is not None and not why_not_option and option.close * option.lot > budget:
        why_not_option.append(
            f"premium ₹{option.close * option.lot:,.0f} for one lot > ₹{budget:,.0f} budget"
        )
    if not why_not_option and option is not None:
        stop = round(option.close * (1 - cfg.option_premium_stop_pct / 100.0), 2)
        return SwingPlan(
            cand, "OPTION", f"IV rank {iv_rank:.0f} <= {cfg.iv_rank_low:.0f}", iv_rank, option,
            qty=option.lot, est_entry=option.close, stop=stop,
            risk_inr=option.close * option.lot,  # premium = max loss (SPEC)
            notes=("1 lot", "paper only: stock F&O is deferred at ₹2L capital (settings)"),
        )  # fmt: skip
    if cand.direction != "LONG":
        return SwingPlan(
            cand, "SKIP", "bearish needs a put; " + "; ".join(why_not_option)
            + " (no overnight cash shorts)", iv_rank,
        )  # fmt: skip
    entry = cand.close
    stop = round(entry * (1 - cfg.cash_stop_pct / 100.0), 2)
    per_share = entry - stop
    qty = math.floor(cfg.cash_risk_pct / 100.0 * capital / per_share) if per_share > 0 else 0
    qty = min(qty, math.floor(cfg.max_cash_value_pct / 100.0 * capital / entry))
    if qty < 1:
        return SwingPlan(cand, "SKIP", "cash position below one share", iv_rank)
    return SwingPlan(
        cand, "CASH", "; ".join(why_not_option) or "cash", iv_rank, None,
        qty=qty, est_entry=entry, stop=stop, risk_inr=qty * per_share,
    )  # fmt: skip


def rank_candidates(cands: Sequence[Candidate], cfg: SwingConfig) -> list[Candidate]:
    order = {Setup.BREAKOUT: 0, Setup.SUPPORT_BOUNCE: 1, Setup.BREAKDOWN: 2}
    return sorted(cands, key=lambda c: (order[c.setup], -c.volume_mult, c.symbol))[
        : cfg.max_candidates
    ]


def swing_config(raw: Mapping[str, Any] | None) -> SwingConfig:
    if not raw:
        return SwingConfig()
    base = SwingConfig()
    kw: dict[str, Any] = {}
    for k, v in raw.items():
        if not hasattr(base, k):
            raise ValueError(f"swing.{k}: unknown setting")
        kw[k] = type(getattr(base, k))(v)
    return SwingConfig(**kw)
