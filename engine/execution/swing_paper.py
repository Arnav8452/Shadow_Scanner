"""Swing paper account (overnight holds): cash delivery and long stock options. Paper only.

Runs once a night on end-of-day data, after the scanner:

1. pending exits, then pending entries, fill at the day's open (buy at open + half-spread, sell
   at open - half-spread); an entry that opens through its stop is skipped;
2. stop orders work during the day: if the low reaches the stop, the exit fills at the stop (or
   the open, if it gapped through), less the half-spread;
3. after the close: the line stop (a close back through the setup's line), the time stop (no
   follow-through after N sessions), the stock-option expiry guard (out by E-2, SPEC §6), the
   maximum hold and trend-sleeve exits queue a sale for the next open;
4. positions are marked at the estimated bid (close - half-spread); breaching the daily or
   weekly loss limit queues every position out and blocks new entries (kill switch).

There is no recorded bid/ask for most stocks and stock options, so spreads are HYPOTHESIS
estimates from config and every fill and mark is labelled EOD-estimated. Costs come from
``config/rules`` (delivery, or intraday when entry and exit fall on the same day; options F&O).
Long only: no cash shorts (retail cannot hold them overnight) and no option selling.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from engine.backtest.costs import equity_delivery_charges, equity_intraday_charges, fno_charges
from engine.core.calendar import TradingCalendar
from engine.core.config import RiskLimits
from engine.core.rules import RuleBook


@dataclass(frozen=True)
class DayBar:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class SwingFillConfig:
    """HYPOTHESIS spread estimates (half of bid-ask, % of price) for EOD paper fills."""

    cash_half_spread_pct: float = 0.05
    option_half_spread_pct: float = 1.5


@dataclass
class Order:
    id: str
    strategy: str  # swing | trend
    symbol: str  # underlying stock / ETF
    instrument: str  # CASH | OPTION
    key: str  # bar key: the stock symbol, or the option contract
    qty: int
    est_price: float
    stop_pct: float  # hard stop below the fill (cash 5-7%, option premium 25%); 0 = none
    line: float = 0.0  # underlying level; a close below it exits (0 = none)
    setup_close: float = 0.0  # underlying close on the signal day (time-stop reference)
    expiry: str = ""  # option expiry ISO date
    reason: str = ""
    placed_on: str = ""


@dataclass
class Position:
    order: Order
    entry_price: float
    entry_day: str
    stop: float
    entry_charges: float
    sessions: int = 0
    mark: float = 0.0
    exit_reason: str = ""  # set when a sale is queued for the next open


@dataclass
class Closed:
    id: str
    strategy: str
    symbol: str
    instrument: str
    key: str
    qty: int
    entry_day: str
    entry_price: float
    exit_day: str
    exit_price: float
    gross: float
    charges: float
    net: float
    reason: str


@dataclass
class SwingState:
    capital: float
    cash: float
    pending: list[Order] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    closed: list[Closed] = field(default_factory=list)
    equity_marks: list[tuple[str, float]] = field(default_factory=list)  # (day, equity at bid)
    kill_until: str = ""  # no new entries placed on or before this day
    last_day: str = ""
    seq: int = 0


class SwingPaperAccount:
    def __init__(
        self,
        state_path: Path,
        *,
        limits: RiskLimits,
        book: RuleBook,
        calendar: TradingCalendar,
        broker: str,
        fills: SwingFillConfig | None = None,
        max_hold_sessions: int = 15,
        time_stop_sessions: int = 2,
    ) -> None:
        self.path = state_path
        self.journal = state_path.with_name("paper_swing.jsonl")
        self.limits = limits
        self.book = book
        self.cal = calendar
        self.broker = broker
        self.fills = fills or SwingFillConfig()
        self.max_hold = max_hold_sessions
        self.time_stop = time_stop_sessions
        self.state = self._load()

    # ------------------------------------------------------------------ persistence

    def _load(self) -> SwingState:
        if not self.path.exists():
            return SwingState(capital=self.limits.capital_inr, cash=self.limits.capital_inr)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return SwingState(
            capital=raw["capital"],
            cash=raw["cash"],
            pending=[Order(**o) for o in raw.get("pending", [])],
            positions=[
                Position(**{**p, "order": Order(**p["order"])}) for p in raw.get("positions", [])
            ],
            closed=[Closed(**c) for c in raw.get("closed", [])],
            equity_marks=[(d, float(v)) for d, v in raw.get("equity_marks", [])],
            kill_until=raw.get("kill_until", ""),
            last_day=raw.get("last_day", ""),
            seq=int(raw.get("seq", 0)),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _log(self, kind: str, **data: Any) -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": kind, **data}, default=str) + "\n")

    # ------------------------------------------------------------------ helpers

    def _hs(self, o: Order) -> float:
        pct = (
            self.fills.option_half_spread_pct
            if o.instrument == "OPTION"
            else (self.fills.cash_half_spread_pct)
        )
        return pct / 100.0

    def _charges(self, o: Order, side: str, value: float, day: date, intraday: bool) -> float:
        if o.instrument == "OPTION":
            return fno_charges(
                self.book, day, segment="options", side=side,  # type: ignore[arg-type]
                turnover_inr=value, broker=self.broker,
            ).total  # fmt: skip
        if intraday:
            return equity_intraday_charges(
                self.book, day, side=side, value_inr=value, broker=self.broker,  # type: ignore[arg-type]
                allow_unverified=True,
            ).total  # fmt: skip
        return equity_delivery_charges(
            self.book, day, side=side, value_inr=value, broker=self.broker,  # type: ignore[arg-type]
            allow_unverified=True,
        ).total  # fmt: skip

    def open_risk(self) -> float:
        risk = sum(max(0.0, (p.mark or p.entry_price) - p.stop) * p.order.qty
                   if p.stop > 0 else (p.mark or p.entry_price) * p.order.qty
                   for p in self.state.positions)  # fmt: skip
        return risk + sum(self._order_risk(o) for o in self.state.pending if o.qty > 0)

    def _order_risk(self, o: Order) -> float:
        if o.instrument == "OPTION" or o.stop_pct <= 0:
            return o.est_price * o.qty  # premium = max loss; no-stop sleeve: full value at risk
        return o.est_price * o.stop_pct / 100.0 * o.qty

    def exposure(self) -> float:
        return sum((p.mark or p.entry_price) * p.order.qty for p in self.state.positions) + sum(
            o.est_price * o.qty for o in self.state.pending if o.qty > 0
        )

    def equity(self) -> float:
        return self.state.cash + sum((p.mark or p.entry_price) * p.order.qty
                                     for p in self.state.positions)  # fmt: skip

    def holds(self, symbol: str) -> bool:
        return any(p.order.symbol == symbol for p in self.state.positions) or any(
            o.symbol == symbol and o.qty > 0 for o in self.state.pending
        )

    # ------------------------------------------------------------------ orders (night of T)

    def place_entry(self, order: Order, *, day: date, risk_budget_pct: float) -> tuple[bool, str]:
        """Queue an entry for the next open after risk checks (settings.risk, approved)."""
        lim = self.limits
        why: list[str] = []
        if self.state.kill_until and day.isoformat() <= self.state.kill_until:
            why.append(f"kill switch until {self.state.kill_until}")
        if self.holds(order.symbol):
            why.append("already held or ordered (no stacking or averaging)")
        placed_today = sum(1 for o in self.state.pending if o.placed_on == day.isoformat())
        if placed_today >= lim.max_trades_per_day:
            why.append(f"{lim.max_trades_per_day} entries already placed today")
        risk = self._order_risk(order)
        if risk > lim.inr(risk_budget_pct) + 1e-6:
            why.append(f"risk ₹{risk:,.0f} > ₹{lim.inr(risk_budget_pct):,.0f} per trade")
        if self.open_risk() + risk > lim.inr(lim.max_open_risk_pct) + 1e-6:
            why.append(f"open risk would exceed {lim.max_open_risk_pct:g}% of capital")
        value = order.est_price * order.qty
        if self.exposure() + value > lim.inr(lim.margin_headroom_max_used_pct) + 1e-6:
            why.append(f"capital in use would exceed {lim.margin_headroom_max_used_pct:g}%")
        if value > self.state.cash:
            why.append("not enough cash")
        if why:
            self._log("refused", day=day, order=asdict(order), reasons=why)
            return False, "; ".join(why)
        self.state.seq += 1
        order.id = order.id or f"SW-{self.state.seq:05d}"
        order.placed_on = day.isoformat()
        self.state.pending.append(order)
        self._log("order", day=day, order=asdict(order))
        return True, f"queued {order.instrument} {order.key} x{order.qty} for the next open"

    def queue_exit(self, symbol: str, reason: str) -> bool:
        for p in self.state.positions:
            if p.order.symbol == symbol and not p.exit_reason:
                p.exit_reason = reason
                return True
        return False

    # ------------------------------------------------------------------ the night's run

    def process_day(
        self, day: date, bars: Mapping[str, DayBar], underlying: Mapping[str, float]
    ) -> list[str]:
        """Apply ``day``'s bars (keys: stock symbols and option contracts) and ``underlying``
        closes (stock symbol -> close). Idempotent per day."""
        if self.state.last_day and day.isoformat() <= self.state.last_day:
            return [f"{day} already processed"]
        out: list[str] = []
        # 1. queued exits at the open
        for p in list(self.state.positions):
            if p.exit_reason:
                b = bars.get(p.order.key)
                if b is None or b.open <= 0:
                    out.append(f"{p.order.key}: no open price on {day}; exit stays queued")
                    continue
                out.append(self._close(p, b.open * (1 - self._hs(p.order)), day, p.exit_reason))
        # 2. entries at the open
        for o in list(self.state.pending):
            self.state.pending.remove(o)
            b = bars.get(o.key)
            if b is None or b.open <= 0:
                out.append(f"{o.key}: did not trade on {day}; entry cancelled")
                self._log("cancel", day=day, order=asdict(o), reason="no open")
                continue
            px = b.open * (1 + self._hs(o))
            stop = px * (1 - o.stop_pct / 100.0) if o.stop_pct > 0 else 0.0
            ub = bars.get(o.symbol)
            if o.line and ub is not None and 0 < ub.open < o.line:
                out.append(f"{o.key}: {o.symbol} opened back below the line; entry skipped")
                self._log("cancel", day=day, order=asdict(o), reason="opened below the line")
                continue
            value = px * o.qty
            if value > self.state.cash:
                out.append(f"{o.key}: not enough cash at the open; entry cancelled")
                continue
            ch = self._charges(o, "buy", value, day, intraday=False)
            self.state.cash -= value + ch
            pos = Position(o, round(px, 2), day.isoformat(), round(stop, 2), round(ch, 2))
            self.state.positions.append(pos)
            self._log("fill", day=day, side="buy", key=o.key, qty=o.qty, price=round(px, 2),
                      charges=round(ch, 2), est="EOD-estimated")  # fmt: skip
            out.append(f"bought {o.key} x{o.qty} @ {px:.2f} ({o.strategy})")
        # 3. stop orders during the day
        for p in list(self.state.positions):
            b = bars.get(p.order.key)
            if b is None or p.stop <= 0 or b.low > p.stop:
                continue
            fill = min(b.open, p.stop) if b.open > 0 else p.stop
            out.append(self._close(p, fill * (1 - self._hs(p.order)), day, "stop"))
        # 4. after the close
        for p in self.state.positions:
            b = bars.get(p.order.key)
            if b is not None and b.close > 0:
                p.mark = round(b.close * (1 - self._hs(p.order)), 2)
            p.sessions += 1 if p.entry_day != day.isoformat() else 0
            o = p.order
            und = underlying.get(o.symbol, 0.0)
            reason = ""
            if o.line and und and und < o.line:
                reason = f"close {und:.2f} back below the line {o.line:.2f}"
            elif o.setup_close and und and p.sessions >= self.time_stop and und <= o.setup_close:
                reason = f"time stop: no follow-through after {p.sessions} sessions"
            elif o.expiry:
                left = self.cal.trading_days_between(day, date.fromisoformat(o.expiry))
                if left <= self.limits.stock_fno_expiry_exit_days_before + 1:
                    reason = "expiry guard: stock options out by E-2 (physical settlement)"
            if not reason and o.strategy == "swing" and p.sessions >= self.max_hold:
                reason = f"maximum hold {self.max_hold} sessions"
            if reason and not p.exit_reason:
                p.exit_reason = reason
                out.append(f"{o.key}: exit queued for the next open ({reason})")
        # 5. marks and the loss limits
        eq = self.equity()
        prev = self.state.equity_marks[-1][1] if self.state.equity_marks else self.state.capital
        self.state.equity_marks.append((day.isoformat(), round(eq, 2)))
        week_base = self._prior_equity(day)
        day_loss = prev - eq
        week_loss = week_base - eq
        lim = self.limits
        if day_loss > lim.inr(lim.daily_loss_limit_pct) or week_loss > lim.inr(
            lim.weekly_loss_limit_pct
        ):
            for p in self.state.positions:
                p.exit_reason = p.exit_reason or "kill switch: loss limit"
            nxt = self.cal.next_trading_day(day)
            self.state.kill_until = nxt.isoformat()
            self.state.pending.clear()
            out.append(f"KILL SWITCH: loss ₹{day_loss:,.0f} today / ₹{week_loss:,.0f} this week; "
                       f"flattening at the next open, no entries until after {nxt}")  # fmt: skip
        self.state.last_day = day.isoformat()
        self._log("mark", day=day, equity=round(eq, 2), cash=round(self.state.cash, 2),
                  positions=len(self.state.positions))  # fmt: skip
        return out

    def _prior_equity(self, day: date) -> float:
        """Equity at the last mark before ``day``'s ISO week (or the starting capital)."""
        wk = day.isocalendar()[:2]
        before = [
            v for d, v in self.state.equity_marks if date.fromisoformat(d).isocalendar()[:2] < wk
        ]
        return before[-1] if before else self.state.capital

    def _close(self, p: Position, price: float, day: date, reason: str) -> str:
        o = p.order
        intraday = p.entry_day == day.isoformat() and o.instrument == "CASH"
        value = price * o.qty
        ch = self._charges(o, "sell", value, day, intraday=intraday)
        entry_ch = p.entry_charges
        if intraday:  # same-day round trip is an intraday cash trade, not delivery
            entry_ch = self._charges(o, "buy", p.entry_price * o.qty, day, intraday=True)
            self.state.cash += p.entry_charges - entry_ch
        self.state.cash += value - ch
        gross = (price - p.entry_price) * o.qty
        net = gross - ch - entry_ch
        self.state.positions.remove(p)
        c = Closed(o.id, o.strategy, o.symbol, o.instrument, o.key, o.qty, p.entry_day,
                   p.entry_price, day.isoformat(), round(price, 2), round(gross, 2),
                   round(ch + entry_ch, 2), round(net, 2), reason)  # fmt: skip
        self.state.closed.append(c)
        self._log("close", **asdict(c), est="EOD-estimated")
        return f"sold {o.key} x{o.qty} @ {price:.2f}: net ₹{net:,.0f} ({reason})"

    # ------------------------------------------------------------------ view

    def snapshot(self) -> dict[str, Any]:
        closed = self.state.closed
        nets = [c.net for c in closed]
        return {
            "capital": self.state.capital,
            "cash": round(self.state.cash, 2),
            "equity": round(self.equity(), 2),
            "open_risk": round(self.open_risk(), 2),
            "exposure": round(self.exposure(), 2),
            "kill_until": self.state.kill_until,
            "last_day": self.state.last_day,
            "positions": [
                {
                    "strategy": p.order.strategy,
                    "key": p.order.key,
                    "instrument": p.order.instrument,
                    "qty": p.order.qty,
                    "entry": p.entry_price,
                    "entry_day": p.entry_day,
                    "stop": p.stop,
                    "line": p.order.line,
                    "mark": p.mark,
                    "unrealised": round(
                        ((p.mark or p.entry_price) - p.entry_price) * p.order.qty, 2
                    ),
                    "sessions": p.sessions,
                    "exit_queued": p.exit_reason,
                }
                for p in self.state.positions
            ],
            "pending": [asdict(o) for o in self.state.pending],
            "closed": [asdict(c) for c in closed[-30:]],
            "summary": {
                "trades": len(closed),
                "net": round(sum(nets), 2),
                "wins": sum(1 for x in nets if x > 0),
                "cash_trades": sum(1 for c in closed if c.instrument == "CASH"),
                "option_trades": sum(1 for c in closed if c.instrument == "OPTION"),
            },
            "note": "Paper, end-of-day fills at open +- an estimated half-spread; marks at the "
            "estimated bid. Not market depth.",
        }


def option_key(symbol: str, expiry: date, strike: float, option_type: str) -> str:
    return f"{symbol} {expiry.isoformat()} {strike:g} {option_type}"
