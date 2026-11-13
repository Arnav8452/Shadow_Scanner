"""Paper accounts for the cockpit: Track B (bot) and Track C (manual).

Both go through the same path: long options only (V2 rule), the approved pre-trade risk gate
(1 lot, trades per day, cool-off, loss limits, first-15-minute block, 14:00 directional cut-off),
``PaperBroker`` fills that walk the recorded book, and charges from ``config/rules`` for the
configured brokerage plan. Each position carries a paper stop on premium so its maximum loss is
known before the gate is asked (a long option with no stop risks the whole premium, which is
above the per-trade limit for a Nifty lot).

Every open, close and refusal is appended to a JSONL journal; on restart the day's open
positions and loss streak are rebuilt from it.

Track A (every signal, no limits) is the scanner's own ledger; see ``engine/shadow/scanner.py``.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from engine.backtest.costs import fno_charges
from engine.core.config import RiskLimits
from engine.core.rules import IST, RuleBook
from engine.data.adapters.base import Level
from engine.execution.order_fsm import OrderRecord, OrderState
from engine.execution.paper_broker import PaperBroker
from engine.risk.limits import AccountRiskState, check_pre_trade_risk_gate

OPTION_RE = re.compile(r"\d(CE|PE)(?:_[A-Z0-9]+)?$")
EXIT_PROTECTION_PCT = 5.0  # exits may walk up to 5% through the book to get out


@dataclass(frozen=True)
class Book:
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]


@dataclass
class PaperPosition:
    symbol: str
    qty: int
    avg_price: float
    stop_price: float
    entry_charges: float
    opened_at: datetime
    signal_id: str
    reason: str
    target_price: float | None = None
    # (gain % that arms it, gain % the stop is raised to), e.g. ((15, 7), (25, 12))
    trail: tuple[tuple[float, float], ...] = ()
    context: dict[str, Any] = field(default_factory=dict)  # market state at entry
    # When the bid first sat at/below the stop while the model price disagreed (air pocket?).
    stop_held_since: datetime | None = None
    vacuum_since: datetime | None = None  # when a stop first met a liquidity vacuum


class LiquidityMonitor:
    """SPEC 7 liquidity-vacuum guard. Every contract's spread (% of mid) and top-5 depth are
    sampled each step for ``window_s``; a book whose spread is ``spread_mult`` x its recent
    median (and over ``min_spread_pct``), or whose depth fell under ``depth_frac`` of it, is in
    a vacuum: market makers pulled quotes (news, a spike). Entries wait; stop exits wait a few
    seconds instead of chasing, unless a hard risk limit is hit."""

    def __init__(
        self,
        *,
        window_s: float = 60.0,
        spread_mult: float = 2.0,
        depth_frac: float = 0.4,
        min_spread_pct: float = 1.0,
        min_samples: int = 20,
    ) -> None:
        self.window_s, self.spread_mult, self.depth_frac = window_s, spread_mult, depth_frac
        self.min_spread_pct, self.min_samples = min_spread_pct, min_samples
        self._hist: dict[str, deque[tuple[float, float, float]]] = {}
        self._last: dict[str, float] = {}

    @staticmethod
    def _measure(book: Book) -> tuple[float, float] | None:
        if not book.bids or not book.asks:
            return None
        b, a = book.bids[0][0], book.asks[0][0]
        mid = (a + b) / 2
        if mid <= 0 or a < b:
            return None
        depth = sum(q for _, q, *_ in book.bids[:5]) + sum(q for _, q, *_ in book.asks[:5])
        return (a - b) / mid * 100, float(depth)

    def observe(self, books: Mapping[str, Book], now: datetime) -> None:
        ts = now.timestamp()
        for sym, book in books.items():
            if ts - self._last.get(sym, 0.0) < 0.5:
                continue
            m = self._measure(book)
            if m is None:
                continue
            self._last[sym] = ts
            h = self._hist.setdefault(sym, deque())
            h.append((ts, m[0], m[1]))
            while h and ts - h[0][0] > self.window_s:
                h.popleft()

    def vacuum(self, sym: str, book: Book) -> str | None:
        h = self._hist.get(sym)
        m = self._measure(book)
        if not h or len(h) < self.min_samples or m is None:
            return None
        spreads = sorted(x[1] for x in h)
        depths = sorted(x[2] for x in h)
        ms, md = spreads[len(spreads) // 2], depths[len(depths) // 2]
        if m[0] > max(self.min_spread_pct, self.spread_mult * ms):
            return f"liquidity vacuum: spread {m[0]:.1f}% vs {ms:.1f}% median of the last minute"
        if md > 0 and m[1] < self.depth_frac * md:
            return f"liquidity vacuum: top-5 depth {m[1]:,.0f} vs {md:,.0f} median"
        return None


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    qty: int
    entry_price: float
    exit_price: float
    gross_inr: float
    charges_inr: float
    net_inr: float
    opened_at: datetime
    closed_at: datetime
    reason: str
    signal_id: str
    context: dict[str, Any] = field(default_factory=dict)


def is_option_symbol(symbol: str, label: str = "") -> bool:
    return bool(OPTION_RE.search(symbol.upper()) or OPTION_RE.search(label.upper()))


class PaperAccount:
    def __init__(
        self,
        name: str,
        *,
        book: RuleBook,
        limits: RiskLimits,
        broker_name: str,
        session_open: datetime,
        square_off_at: datetime,
        lot_size_of: Callable[[str], int],
        is_option: Callable[[str], bool],
        journal: Path | None,
        entry_block: Callable[[datetime], str | None] | None = None,
        expiry_of: Callable[[str], date | None] | None = None,
        expiry_day_last_entry: str = "14:30",
        expiry_day_exit: str = "14:50",
        stop_confirm: Callable[[str, float, float], bool] | None = None,
    ) -> None:
        self.name = name
        self.book = book
        self.limits = limits
        self.broker_name = broker_name
        self.session_open = session_open
        self.square_off_at = square_off_at
        self.lot_size_of = lot_size_of
        self.is_option = is_option
        self.journal = journal
        # Extra entry rule outside the risk gate, e.g. a macro-event window (returns the reason).
        self.entry_block = entry_block
        # Long options expiring today: settlement averaging takes over in the last half hour.
        self.expiry_of = expiry_of
        self.expiry_day_last_entry = expiry_day_last_entry
        self.expiry_day_exit = expiry_day_exit
        # breaking_retail.md: a stop fires only if the option's model price (index + smoothed
        # IV) also says the stop is reached, so a quote air-pocket alone does not stop us out.
        self.stop_confirm = stop_confirm
        # A model-price hold is for quote air pockets, which last a few seconds. If the bid stays
        # at/below the stop longer than this, falls past ``stop_override_mult`` x the stop
        # distance, or the open loss reaches the per-trade limit, the stop fires anyway.
        self.stop_hold_max_s = 5.0
        self.stop_override_mult = 1.5
        # Orders fill against the first book at least this long after the decision (order
        # latency to the exchange); 0 = the decision's own book (tests, research).
        self.fill_latency_s = 0.0
        self.pending: dict[str, dict[str, Any]] = {}
        self.liquidity: LiquidityMonitor | None = None  # shared, set by the cockpit
        self.vol_sizing: Any = None  # SPEC 5.7 VolSizing, set by the cockpit
        self.vacuum_wait_s = 3.0  # a stop exit waits this long for quotes to come back
        # Net P&L of open positions at the bid (after entry charges), refreshed every step:
        # an open loss counts toward the daily and weekly loss limits like a realized one.
        self.open_net = 0.0
        self.broker = PaperBroker(book, broker_name=broker_name)
        self.positions: dict[str, PaperPosition] = {}
        self.closed: list[ClosedTrade] = []
        self.refusals: list[dict[str, Any]] = []
        self.trades_today = 0
        self.weekly_prior_net = 0.0
        self._seq = 0

    # ------------------------------------------------------------------ state

    @property
    def realized_net(self) -> float:
        return round(sum(t.net_inr for t in self.closed), 2)

    def _consecutive_losses(self) -> int:
        n = 0
        for t in reversed(self.closed):
            if t.net_inr < 0:
                n += 1
            else:
                break
        return n

    def risk_state(self) -> AccountRiskState:
        losses = [t for t in self.closed if t.net_inr < 0]
        open_loss = min(0.0, self.open_net)  # open gains do not widen the limit
        return AccountRiskState(
            realized_daily_pnl_inr=self.realized_net + open_loss,
            realized_weekly_pnl_inr=self.weekly_prior_net + self.realized_net + open_loss,
            # Orders still on their way count as if filled at the price they were sent at.
            open_risk_inr=sum(
                max(0.0, p.avg_price - p.stop_price) * p.qty for p in self.positions.values()
            )
            + sum(o["decided_ask"] * o["stop_pct"] / 100 * o["qty"] for o in self.pending.values()),
            margin_used_inr=sum(p.avg_price * p.qty for p in self.positions.values())
            + sum(o["decided_ask"] * o["qty"] for o in self.pending.values()),
            trades_taken_today=self.trades_today + len(self.pending),
            consecutive_losses_today=self._consecutive_losses(),
            last_loss_at=losses[-1].closed_at if losses else None,
        )

    # ------------------------------------------------------------------ orders

    def open_long_option(
        self,
        symbol: str,
        book: Book,
        *,
        now: datetime,
        lots: int,
        stop_pct: float,
        signal_id: str = "",
        reason: str = "",
        target_pct: float | None = None,
        trail: tuple[tuple[float, float], ...] = (),
        context: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str]:
        if not self.is_option(symbol):
            return self._refuse(symbol, now, "only long options may be paper-traded", signal_id)
        if symbol in self.positions:
            return self._refuse(symbol, now, "position already open", signal_id)
        if not book.asks or not book.bids or book.asks[0][0] <= 0:
            return self._refuse(symbol, now, "no order book", signal_id)
        if not 1.0 <= stop_pct <= 90.0:
            return self._refuse(symbol, now, "stop must be 1-90% of premium", signal_id)
        if self.entry_block is not None:
            why = self.entry_block(now)
            if why:
                return self._refuse(symbol, now, why, signal_id)
        if self.liquidity is not None:
            vac = self.liquidity.vacuum(symbol, book)
            if vac:
                return self._refuse(symbol, now, vac, signal_id)
        if self.vol_sizing is not None:
            # SPEC 5.7: smaller (never larger) when recent realized vol is high.
            lots, note, vctx = self.vol_sizing.size(symbol, lots, now.astimezone(IST).date())
            if lots <= 0:
                return self._refuse(symbol, now, note or "vol-managed sizing", signal_id)
            context = {**dict(context or {}), **vctx, **({"vol_note": note} if note else {})}
        if self._expires_today(symbol, now) and (
            now.astimezone(IST).strftime("%H:%M") >= self.expiry_day_last_entry
        ):
            return self._refuse(
                symbol, now, f"expires today: no new entries after {self.expiry_day_last_entry}",
                signal_id,
            )  # fmt: skip
        lot = max(1, self.lot_size_of(symbol))
        qty = lots * lot
        ask = book.asks[0][0]
        max_loss = ask * stop_pct / 100.0 * qty
        gate = check_pre_trade_risk_gate(
            self.limits,
            self.risk_state(),
            proposed_trade_max_loss_inr=max_loss,
            proposed_margin_inr=ask * qty,
            proposed_lots=lots,
            strategy_kind="directional",
            now=now,
            session_open=self.session_open,
        )
        if not gate.allowed:
            return self._refuse(symbol, now, "; ".join(gate.reasons), signal_id)
        spec = {
            "symbol": symbol,
            "qty": qty,
            "stop_pct": stop_pct,
            "signal_id": signal_id,
            "reason": reason,
            "target_pct": target_pct,
            "trail": tuple(trail),
            "context": dict(context or {}),
            "decided": now,
            "decided_ask": ask,
        }
        if self.fill_latency_s > 0:
            if symbol in self.pending:
                return self._refuse(symbol, now, "an order is already on its way", signal_id)
            spec["due"] = now.timestamp() + self.fill_latency_s
            self.pending[symbol] = spec
            return True, (
                f"order sent: buy {qty} {symbol} near {ask:.2f} "
                f"(fills against the book {self.fill_latency_s * 1000:.0f} ms later)"
            )
        return self._execute_open(spec, book, now)

    def _execute_open(self, spec: dict[str, Any], book: Book, now: datetime) -> tuple[bool, str]:
        symbol, qty, signal_id = spec["symbol"], int(spec["qty"]), str(spec["signal_id"])
        stop_pct, target_pct, reason = spec["stop_pct"], spec["target_pct"], spec["reason"]
        trail, context = spec["trail"], spec["context"]
        if not book.asks or not book.bids or book.asks[0][0] <= 0:
            return self._refuse(symbol, now, "no order book when the order arrived", signal_id)
        ask = book.asks[0][0]
        self._seq += 1
        order = OrderRecord(
            f"{self.name.upper()}-{now:%H%M%S}-{self._seq:04d}",
            symbol,
            "BUY",
            "MARKET_PROTECTED",
            qty,
            ask,
            created_at=now,
        )
        res = self.broker.execute_against_depth(
            order,
            bids=book.bids,
            asks=book.asks,
            now=now,
            trade_date=now.astimezone(IST).date(),
            segment="options",
        )
        if (
            res.order.state not in (OrderState.FILLED, OrderState.PARTIAL)
            or res.order.filled_qty <= 0
        ):
            return self._refuse(symbol, now, res.order.reason or res.order.state.value, signal_id)
        fill = res.order.avg_fill_price
        pos = PaperPosition(
            symbol=symbol,
            qty=res.order.filled_qty,
            avg_price=round(fill, 4),
            stop_price=round(fill * (1.0 - stop_pct / 100.0), 2),
            entry_charges=round(res.charges.total, 2),
            opened_at=now,
            signal_id=signal_id,
            reason=reason,
            target_price=round(fill * (1.0 + target_pct / 100.0), 2) if target_pct else None,
            trail=tuple(trail),
            context=dict(context or {}),
        )
        self.positions[symbol] = pos
        self.trades_today += 1
        slip = round(fill - float(spec.get("decided_ask") or fill), 2)
        self._write(
            {
                "type": "open",
                "t": now.isoformat(),
                "symbol": symbol,
                "qty": pos.qty,
                "price": pos.avg_price,
                "decided_ask": spec.get("decided_ask"),
                "latency_slip": slip,
                "stop": pos.stop_price,
                "target": pos.target_price,
                "trail": [list(t) for t in pos.trail],
                "charges": pos.entry_charges,
                "signal_id": signal_id,
                "reason": reason,
                "context": pos.context,
            }
        )
        lag = (
            f" (asked {float(spec['decided_ask']):.2f} at the decision, {slip:+.2f} later)"
            if spec.get("decided_ask") is not None and self.fill_latency_s > 0
            else ""
        )
        return True, f"bought {pos.qty} {symbol} @ {fill:.2f}{lag}, stop {pos.stop_price:.2f}"

    def fill_pending(self, books: Mapping[str, Book], now: datetime) -> list[str]:
        """Orders whose latency has passed fill against the book they now meet."""
        out: list[str] = []
        for sym, spec in list(self.pending.items()):
            if now.timestamp() < float(spec["due"]):
                continue
            del self.pending[sym]
            book = books.get(sym)
            if book is None:
                out.append(self._refuse(sym, now, "no book when the order arrived", "")[1])
                continue
            out.append(self._execute_open(spec, book, now)[1])
        return out

    def close(self, symbol: str, book: Book, *, now: datetime, reason: str) -> tuple[bool, str]:
        pos = self.positions.get(symbol)
        if pos is None:
            return False, "no open position"
        if not book.bids or book.bids[0][0] <= 0:
            return False, "no bid to sell into; still open"
        self._seq += 1
        order = OrderRecord(
            f"{self.name.upper()}-{now:%H%M%S}-{self._seq:04d}",
            symbol,
            "SELL",
            "MARKET_PROTECTED",
            pos.qty,
            book.bids[0][0],
            protection_pct=EXIT_PROTECTION_PCT,
            created_at=now,
        )
        res = self.broker.execute_against_depth(
            order,
            bids=book.bids,
            asks=book.asks,
            now=now,
            trade_date=now.astimezone(IST).date(),
            segment="options",
            is_exit=True,
        )
        filled = res.order.filled_qty
        if filled <= 0:
            return False, f"exit not filled ({res.order.reason or res.order.state.value})"
        exit_px = res.order.avg_fill_price
        entry_charges = pos.entry_charges * filled / pos.qty
        gross = (exit_px - pos.avg_price) * filled
        charges = entry_charges + res.charges.total
        trade = ClosedTrade(
            symbol=symbol,
            qty=filled,
            entry_price=pos.avg_price,
            exit_price=round(exit_px, 4),
            gross_inr=round(gross, 2),
            charges_inr=round(charges, 2),
            net_inr=round(gross - charges, 2),
            opened_at=pos.opened_at,
            closed_at=now,
            reason=reason,
            signal_id=pos.signal_id,
            context=pos.context,
        )
        self.closed.append(trade)
        if filled >= pos.qty:
            del self.positions[symbol]
        else:  # the rest stays open with its share of the entry charges
            pos.qty -= filled
            pos.entry_charges = round(pos.entry_charges - entry_charges, 2)
        self._write({"type": "close", **_trade_json(trade)})
        return True, f"sold {filled} {symbol} @ {exit_px:.2f}: net ₹{trade.net_inr:,.2f}"

    def check_exits(self, books: Mapping[str, Book], now: datetime) -> list[str]:
        """Stops on premium and the square-off time. Returns what happened."""
        out: list[str] = self.fill_pending(books, now)
        self.open_net = sum(
            (b.bids[0][0] - p.avg_price) * p.qty - p.entry_charges
            for s, p in self.positions.items()
            if (b := books.get(s)) is not None and b.bids
        )
        for symbol in list(self.positions):
            book = books.get(symbol)
            if book is None or not book.bids:
                continue
            pos = self.positions[symbol]
            bid = book.bids[0][0]
            # Trailing: once the bid shows the arming gain, raise the stop to lock the given gain.
            for arm_pct, lock_pct in pos.trail:
                if bid >= pos.avg_price * (1 + arm_pct / 100.0):
                    pos.stop_price = max(
                        pos.stop_price, round(pos.avg_price * (1 + lock_pct / 100.0), 2)
                    )
            if now >= self.square_off_at:
                ok, text = self.close(symbol, book, now=now, reason="square-off")
            elif self._expires_today(symbol, now) and (
                now.astimezone(IST).strftime("%H:%M") >= self.expiry_day_exit
            ):
                ok, text = self.close(
                    symbol, book, now=now, reason=f"expiry-day exit {self.expiry_day_exit}"
                )
            elif pos.target_price is not None and bid >= pos.target_price:
                ok, text = self.close(symbol, book, now=now, reason="target on premium")
            elif bid <= pos.stop_price:
                why = "trailing stop" if pos.stop_price > pos.avg_price else "stop on premium"
                vac = self.liquidity.vacuum(symbol, book) if self.liquidity is not None else None
                if vac and not self._hard_limit(pos, bid):
                    # SPEC 7: no chasing into an empty book; give quotes a few seconds back.
                    pos.vacuum_since = pos.vacuum_since or now
                    if (now - pos.vacuum_since).total_seconds() < self.vacuum_wait_s:
                        continue
                    why += f" (after waiting {self.vacuum_wait_s:g} s in a {vac})"
                if self.stop_confirm is not None and not self.stop_confirm(
                    symbol, bid, pos.stop_price
                ):
                    override = self._stop_override(pos, bid, now)
                    if override is None:
                        continue
                    why += f" (model price disagreed; override: {override})"
                ok, text = self.close(symbol, book, now=now, reason=why)
            else:
                pos.stop_held_since = None
                continue
            out.append(text if ok else f"{symbol}: {text}")
        return out

    def _hard_limit(self, pos: PaperPosition, bid: float) -> bool:
        """Past 1.5x the stop distance or at the per-trade loss limit: exit regardless."""
        dist = pos.avg_price - pos.stop_price
        if dist > 0 and pos.avg_price - bid >= self.stop_override_mult * dist:
            return True
        cap = self.limits.inr(self.limits.per_trade_max_loss_pct.get("directional", 0.0))
        return cap > 0 and (pos.avg_price - bid) * pos.qty >= cap

    def _stop_override(self, pos: PaperPosition, bid: float, now: datetime) -> str | None:
        """Why a model-price hold must give way now (None: keep holding for a moment)."""
        if pos.stop_held_since is None:
            pos.stop_held_since = now
        held = (now - pos.stop_held_since).total_seconds()
        if held >= self.stop_hold_max_s:
            return f"bid at/below the stop for {held:.0f} s"
        dist = pos.avg_price - pos.stop_price
        if dist > 0 and pos.avg_price - bid >= self.stop_override_mult * dist:
            return f"bid {bid:.2f} beyond {self.stop_override_mult:g}x the stop distance"
        cap = self.limits.inr(self.limits.per_trade_max_loss_pct.get("directional", 0.0))
        if cap > 0 and (pos.avg_price - bid) * pos.qty >= cap:
            return f"open loss at the per-trade limit Rs {cap:,.0f}"
        return None

    def flatten(self, books: Mapping[str, Book], now: datetime, reason: str) -> list[str]:
        out = []
        for symbol in list(self.positions):
            book = books.get(symbol)
            ok, text = (
                self.close(symbol, book, now=now, reason=reason)
                if book is not None
                else (False, "no book")
            )
            out.append(text if ok else f"{symbol}: {text}")
        return out

    def _expires_today(self, symbol: str, now: datetime) -> bool:
        if self.expiry_of is None:
            return False
        exp = self.expiry_of(symbol)
        return exp is not None and exp == now.astimezone(IST).date()

    # ------------------------------------------------------------------ view

    def view(self, books: Mapping[str, Book], now: datetime) -> dict[str, Any]:
        positions = []
        unreal_net = 0.0
        for p in self.positions.values():
            book = books.get(p.symbol)
            mark = book.bids[0][0] if book is not None and book.bids else 0.0
            gross = (mark - p.avg_price) * p.qty if mark > 0 else 0.0
            exit_charges = (
                fno_charges(
                    self.book,
                    now.astimezone(IST).date(),
                    segment="options",
                    side="sell",
                    turnover_inr=mark * p.qty,
                    broker=self.broker_name,
                ).total
                if mark > 0
                else 0.0
            )
            net = gross - p.entry_charges - exit_charges
            unreal_net += net
            positions.append(
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "avg": round(p.avg_price, 2),
                    "mark": mark,
                    "stop": p.stop_price,
                    "target": p.target_price,
                    "gross": round(gross, 2),
                    "charges": round(p.entry_charges + exit_charges, 2),
                    "net": round(net, 2),
                    "opened": p.opened_at.astimezone(IST).strftime("%H:%M:%S"),
                    "reason": p.reason,
                    # A long option needs only its premium (plus charges): no SPAN, no exposure.
                    "margin": {
                        "final": round(p.avg_price * p.qty + p.entry_charges, 2),
                        "source": "premium paid + entry charges (long option: no SPAN)",
                    },
                }
            )
        st = self.risk_state()
        day_limit = self.limits.inr(self.limits.daily_loss_limit_pct)
        cool_until = None
        if st.last_loss_at is not None and self.limits.cooloff_after_loss_min > 0:
            end = st.last_loss_at.timestamp() + self.limits.cooloff_after_loss_min * 60
            if end > now.timestamp():
                cool_until = datetime.fromtimestamp(end, tz=IST).strftime("%H:%M")
        gross_r = sum(t.gross_inr for t in self.closed)
        charges_r = sum(t.charges_inr for t in self.closed)
        return {
            "name": self.name,
            "broker": self.broker_name,
            "positions": positions,
            "closed": [_trade_view(t) for t in self.closed[-30:]][::-1],
            "realized_gross": round(gross_r, 2),
            "realized_charges": round(charges_r, 2),
            "realized_net": self.realized_net,
            "unrealized_net": round(unreal_net, 2),
            "day_net": round(self.realized_net + unreal_net, 2),
            "trades_today": self.trades_today,
            "max_trades": self.limits.max_trades_per_day,
            "wins": sum(1 for t in self.closed if t.net_inr > 0),
            "losses": sum(1 for t in self.closed if t.net_inr < 0),
            "consecutive_losses": st.consecutive_losses_today,
            "cooloff_until": cool_until,
            "daily_loss_limit": round(day_limit, 2),
            "per_trade_limit": round(
                self.limits.inr(self.limits.per_trade_max_loss_pct.get("directional", 0.0)), 2
            ),
            "loss_room_left": round(day_limit + self.realized_net + min(0.0, unreal_net), 2),
            "refusals": self.refusals[-10:][::-1],
        }

    # ------------------------------------------------------------------ journal

    def _refuse(self, symbol: str, now: datetime, why: str, signal_id: str) -> tuple[bool, str]:
        # The same refusal again within a minute (numbers aside, e.g. the cool-off countdown)
        # is not journaled again: a persistent trigger would otherwise write lines every step.
        kind = (symbol, re.sub(r"[0-9.]+", "#", why))
        last = getattr(self, "_last_refusal", None)
        if last is not None and last[0] == kind and (now - last[1]).total_seconds() < 60:
            return False, f"refused {symbol}: {why}"
        self._last_refusal = (kind, now)
        rec = {
            "type": "refused",
            "t": now.isoformat(),
            "time": now.astimezone(IST).strftime("%H:%M:%S"),
            "symbol": symbol,
            "why": why,
            "signal_id": signal_id,
        }
        self.refusals.append(rec)
        self.refusals = self.refusals[-50:]
        self._write(rec)
        return False, f"refused {symbol}: {why}"

    def _write(self, rec: dict[str, Any]) -> None:
        if self.journal is None:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"account": self.name, **rec}, default=str) + "\n")

    def load_journal(self, day: date) -> int:
        """Rebuild today's positions, closed trades and counters; returns lines used."""
        if self.journal is None or not self.journal.exists():
            return 0
        iso = day.isocalendar()
        used = 0
        opens: dict[str, dict[str, Any]] = {}
        for line in self.journal.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                t = datetime.fromisoformat(rec["t"]).astimezone(IST)
            except (ValueError, KeyError, TypeError):
                continue
            d = t.date()
            if d != day:
                if rec.get("type") == "close" and d < day and d.isocalendar()[:2] == iso[:2]:
                    self.weekly_prior_net += float(rec.get("net_inr", 0.0))
                continue
            used += 1
            kind = rec.get("type")
            if kind == "open":
                opens[rec["symbol"]] = rec
                self.trades_today += 1
            elif kind == "close":
                self.closed.append(
                    ClosedTrade(
                        symbol=rec["symbol"],
                        qty=int(rec["qty"]),
                        entry_price=float(rec["entry_price"]),
                        exit_price=float(rec["exit_price"]),
                        gross_inr=float(rec["gross_inr"]),
                        charges_inr=float(rec["charges_inr"]),
                        net_inr=float(rec["net_inr"]),
                        opened_at=datetime.fromisoformat(rec["opened_at"]),
                        closed_at=datetime.fromisoformat(rec["closed_at"]),
                        reason=str(rec.get("reason", "")),
                        signal_id=str(rec.get("signal_id", "")),
                        context=dict(rec.get("context") or {}),
                    )
                )
                opened = opens.get(rec["symbol"])
                if opened is not None:
                    opened["qty"] = int(opened["qty"]) - int(rec["qty"])
                    if opened["qty"] <= 0:
                        opens.pop(rec["symbol"], None)
            elif kind == "refused":
                self.refusals.append(rec)
        for sym, rec in opens.items():
            self.positions[sym] = PaperPosition(
                symbol=sym,
                qty=int(rec["qty"]),
                avg_price=float(rec["price"]),
                stop_price=float(rec["stop"]),
                entry_charges=float(rec.get("charges", 0.0)),
                opened_at=datetime.fromisoformat(rec["t"]),
                signal_id=str(rec.get("signal_id", "")),
                reason=str(rec.get("reason", "")) + " (restored)",
                target_price=float(rec["target"]) if rec.get("target") else None,
                trail=tuple((float(a), float(b)) for a, b in rec.get("trail") or ()),
                context=dict(rec.get("context") or {}),
            )
        return used


def _trade_json(t: ClosedTrade) -> dict[str, Any]:
    d = asdict(t)
    d["opened_at"] = t.opened_at.isoformat()
    d["closed_at"] = t.closed_at.isoformat()
    d["t"] = t.closed_at.isoformat()
    return d


def _trade_view(t: ClosedTrade) -> dict[str, Any]:
    return {
        "symbol": t.symbol,
        "qty": t.qty,
        "entry": round(t.entry_price, 2),
        "exit": round(t.exit_price, 2),
        "gross": t.gross_inr,
        "charges": t.charges_inr,
        "net": t.net_inr,
        "opened": t.opened_at.astimezone(IST).strftime("%H:%M:%S"),
        "closed": t.closed_at.astimezone(IST).strftime("%H:%M:%S"),
        "reason": t.reason,
        "margin": {
            "final": round(t.entry_price * t.qty, 2),
            "source": "premium paid (long option: no SPAN)",
        },
    }


def books_from(states: Mapping[str, Any], symbols: Sequence[str] | None = None) -> dict[str, Book]:
    keys = symbols if symbols is not None else list(states)
    out: dict[str, Book] = {}
    for s in keys:
        st = states.get(s)
        if st is not None and (st.bids or st.asks):
            out[s] = Book(tuple(st.bids), tuple(st.asks))
    return out
