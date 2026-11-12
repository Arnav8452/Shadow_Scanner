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
        return AccountRiskState(
            realized_daily_pnl_inr=self.realized_net,
            realized_weekly_pnl_inr=self.weekly_prior_net + self.realized_net,
            open_risk_inr=sum(
                max(0.0, p.avg_price - p.stop_price) * p.qty for p in self.positions.values()
            ),
            margin_used_inr=sum(p.avg_price * p.qty for p in self.positions.values()),
            trades_taken_today=self.trades_today,
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
        self._write(
            {
                "type": "open",
                "t": now.isoformat(),
                "symbol": symbol,
                "qty": pos.qty,
                "price": pos.avg_price,
                "stop": pos.stop_price,
                "target": pos.target_price,
                "trail": [list(t) for t in pos.trail],
                "charges": pos.entry_charges,
                "signal_id": signal_id,
                "reason": reason,
                "context": pos.context,
            }
        )
        return True, f"bought {pos.qty} {symbol} @ {fill:.2f}, stop {pos.stop_price:.2f}"

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
        out: list[str] = []
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
                ok, text = self.close(symbol, book, now=now, reason=why)
            else:
                continue
            out.append(text if ok else f"{symbol}: {text}")
        return out

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
            "loss_room_left": round(day_limit + self.realized_net + min(0.0, unreal_net), 2),
            "refusals": self.refusals[-10:][::-1],
        }

    # ------------------------------------------------------------------ journal

    def _refuse(self, symbol: str, now: datetime, why: str, signal_id: str) -> tuple[bool, str]:
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
    }


def books_from(states: Mapping[str, Any], symbols: Sequence[str] | None = None) -> dict[str, Book]:
    keys = symbols if symbols is not None else list(states)
    out: dict[str, Book] = {}
    for s in keys:
        st = states.get(s)
        if st is not None and (st.bids or st.asks):
            out[s] = Book(tuple(st.bids), tuple(st.asks))
    return out
