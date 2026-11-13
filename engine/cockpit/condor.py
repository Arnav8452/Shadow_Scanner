"""SPEC §5.1 on paper: iron condor / credit spreads on Nifty, with order flow as a veto.

Paper only; every number in ``cockpit.condor`` is a HYPOTHESIS until the paper record says
otherwise. What the strategy does, in the order it checks:

1. **Window and expiry.** Entries between ``first_entry`` and ``last_entry`` on the nearest
   weekly expiry with at least ``min_days_to_expiry`` calendar days left. One structure a day.
2. **Premium is rich.** Our ATM IV percentile (252 sessions) at least ``min_iv_percentile``.
3. **No event window.** No RBI / Budget / CPI / FOMC blackout and no heavy results cluster
   (``blackout_status``); a scale factor of 0 blocks.
4. **Order-flow veto** (``SetupTracker.trend`` over ``veto_lookback_min``):
   - confirmed setups both ways -> no trade (two-way tape);
   - confirmed UP only -> bull put spread (sell the put side only), DOWN only -> bear call
     spread, when ``one_sided_on_trend``; otherwise no trade;
   - a setup still forming -> wait until it confirms or fails;
   - nothing -> iron condor.
5. **Strikes.** Shorts at about ``short_delta`` (Black-76 on our forward and each strike's own
   IV from the 1-minute chain); wings ``wing_widths`` points further out, the widest whose
   worst-case loss fits the per-trade limit. The credit must be at least
   ``min_credit_pct_of_width`` of the width. The four contracts are pinned into the live feed
   and the structure is traded only once all of them have a live book.
6. **Hedge first** (CLAUDE.md rule 1): buy the wings; only if both fill, sell the shorts; if a
   short fails, the filled legs are unwound. On exit the shorts are bought back first.

Exits (checked every publish): take profit at ``take_profit_pct`` of the credit; stop when the
cost to close reaches ``stop_mult`` x the credit; the index touching a short strike; order flow
(a confirmed setup toward a short strike with the index within ``defend_within_pct`` of it);
and the time exit at ``exit_time`` on the trading day before expiry. The position is held
overnight (journal ``paper_condor.jsonl`` restores it).

Margin: SPAN for a hedged structure is not modelled; the worst-case loss is used for the risk
gate and as the margin proxy (UNVERIFIED).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from engine.backtest.costs import fno_charges
from engine.cockpit.paper import EXIT_PROTECTION_PCT, Book
from engine.core.config import RiskLimits
from engine.core.rules import IST, RuleBook
from engine.execution.leg_sequencer import SpreadLegIntent, sequence_entry_legs
from engine.execution.order_fsm import OrderRecord, OrderState
from engine.execution.paper_broker import PaperBroker
from engine.risk.limits import AccountRiskState, check_pre_trade_risk_gate


@dataclass(frozen=True)
class CondorConfig:
    enabled: bool = True
    underlying: str = "NIFTY"
    first_entry: str = "10:00"
    last_entry: str = "14:30"
    min_days_to_expiry: int = 2
    short_delta: float = 0.16
    wing_widths: tuple[float, ...] = (200.0, 150.0, 100.0)
    min_credit_pct_of_width: float = 10.0
    min_iv_percentile: float = 50.0
    veto_lookback_min: float = 30.0
    one_sided_on_trend: bool = True
    take_profit_pct: float = 50.0
    stop_mult: float = 2.0
    exit_on_short_touch: bool = True
    defend_within_pct: float = 0.5
    exit_time: str = "15:00"  # on the trading day before expiry
    max_per_day: int = 1
    book_wait_s: float = 60.0
    # SPEC 5.1: no RBI / Budget / election / heavyweight results cluster while the trade is on.
    hold_block_categories: tuple[str, ...] = ("RBI_MPC", "UNION_BUDGET", "ELECTION_EXIT_POLL")
    hold_earnings_weight_pct: float = 10.0


# ---------------------------------------------------------------------------- positions


@dataclass
class Leg:
    symbol: str
    kind: str  # CE / PE
    strike: float
    side: str  # BUY (wing) / SELL (short)
    qty: int
    entry_px: float = 0.0
    entry_charges: float = 0.0
    open: bool = True
    exit_px: float = 0.0
    exit_charges: float = 0.0


@dataclass
class SpreadPosition:
    id: str
    structure: str  # IRON_CONDOR / BULL_PUT / BEAR_CALL
    underlying: str
    expiry: date
    legs: list[Leg]
    qty: int
    credit_pts: float  # net premium received per unit at entry
    width_pts: float
    max_loss_inr: float
    opened_at: datetime
    spot0: float
    reason: str
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def short_put(self) -> float | None:
        return next((lg.strike for lg in self.legs if lg.side == "SELL" and lg.kind == "PE"), None)

    @property
    def short_call(self) -> float | None:
        return next((lg.strike for lg in self.legs if lg.side == "SELL" and lg.kind == "CE"), None)

    @property
    def entry_charges(self) -> float:
        return sum(lg.entry_charges for lg in self.legs)


@dataclass(frozen=True)
class ClosedSpread:
    id: str
    structure: str
    underlying: str
    expiry: date
    qty: int
    credit_pts: float
    debit_pts: float
    gross_inr: float
    charges_inr: float
    net_inr: float
    opened_at: datetime
    closed_at: datetime
    reason: str
    legs: tuple[dict[str, Any], ...] = ()
    context: dict[str, Any] = field(default_factory=dict)


def cost_to_close(pos: SpreadPosition, books: Mapping[str, Book]) -> float | None:
    """Per unit, at the touch: buy the open shorts at the ask, sell the open wings at the bid."""
    total = 0.0
    for lg in pos.legs:
        if not lg.open:
            continue
        b = books.get(lg.symbol)
        if b is None:
            return None
        if lg.side == "SELL":
            if not b.asks:
                return None
            total += b.asks[0][0]
        else:
            total -= b.bids[0][0] if b.bids else 0.0
    return total


# ---------------------------------------------------------------------------- account


class SpreadAccount:
    """Multi-leg paper account: defined risk only, hedge-first entry, shorts-first exit."""

    def __init__(
        self,
        name: str,
        *,
        book: RuleBook,
        limits: RiskLimits,
        broker_name: str,
        session_open: datetime,
        journal: Path | None,
        entry_block: Callable[[datetime], str | None] | None = None,
    ) -> None:
        self.name = name
        self.book = book
        self.limits = limits
        self.broker_name = broker_name
        self.session_open = session_open
        self.journal = journal
        # Extra entry rule outside the risk gate, e.g. a macro-event window (returns the reason).
        self.entry_block = entry_block
        self.broker = PaperBroker(book, broker_name=broker_name)
        self.positions: list[SpreadPosition] = []
        self.closed: list[ClosedSpread] = []
        self.refusals: list[dict[str, Any]] = []
        self.trades_today = 0
        self.weekly_prior_net = 0.0
        self._seq = 0

    # ------------------------------------------------------------------ state

    @property
    def realized_net(self) -> float:
        return round(sum(t.net_inr for t in self.closed), 2)

    def risk_state(self) -> AccountRiskState:
        losses = [t for t in self.closed if t.net_inr < 0]
        streak = 0
        for t in reversed(self.closed):
            if t.net_inr >= 0:
                break
            streak += 1
        open_risk = sum(p.max_loss_inr for p in self.positions)
        return AccountRiskState(
            realized_daily_pnl_inr=self.realized_net,
            realized_weekly_pnl_inr=self.weekly_prior_net + self.realized_net,
            open_risk_inr=open_risk,
            margin_used_inr=open_risk,  # proxy: SPAN not modelled (UNVERIFIED)
            trades_taken_today=self.trades_today,
            consecutive_losses_today=streak,
            last_loss_at=losses[-1].closed_at if losses else None,
        )

    def gate(self, max_loss_inr: float, now: datetime, lots: int = 1) -> tuple[bool, str]:
        g = check_pre_trade_risk_gate(
            self.limits,
            self.risk_state(),
            proposed_trade_max_loss_inr=max_loss_inr,
            proposed_margin_inr=max_loss_inr,
            proposed_lots=lots,
            strategy_kind="defined_risk_spread",
            now=now,
            session_open=self.session_open,
        )
        return g.allowed, "; ".join(g.reasons)

    def per_trade_limit(self) -> float:
        return self.limits.inr(self.limits.per_trade_max_loss_pct["defined_risk_spread"])

    # ------------------------------------------------------------------ orders

    def _fill(
        self,
        symbol: str,
        side: str,
        qty: int,
        book: Book,
        now: datetime,
        *,
        exit_: bool,
        kind: str = "CE",
    ) -> tuple[int, float, float, str]:
        """(filled qty, average price, charges, reason) for one leg against its book."""
        ladder = book.asks if side == "BUY" else book.bids
        if not ladder or ladder[0][0] <= 0:
            return 0, 0.0, 0.0, "no book"
        self._seq += 1
        order = OrderRecord(
            f"{self.name.upper()}-{now:%H%M%S}-{self._seq:04d}",
            symbol,
            side,
            "MARKET_PROTECTED",
            qty,
            ladder[0][0],
            protection_pct=EXIT_PROTECTION_PCT if exit_ else 0.5,
            created_at=now,
        )
        res = self.broker.execute_against_depth(
            order,
            bids=book.bids,
            asks=book.asks,
            now=now,
            trade_date=now.astimezone(IST).date(),
            segment="futures" if kind == "FUT" else "options",
            is_exit=exit_,
        )
        if res.order.state not in (OrderState.FILLED, OrderState.PARTIAL):
            return 0, 0.0, 0.0, res.order.reason or res.order.state.value
        return (
            res.order.filled_qty,
            res.order.avg_fill_price,
            round(res.charges.total, 2),
            "",
        )

    def open_spread(
        self,
        *,
        structure: str,
        underlying: str,
        expiry: date,
        legs: Sequence[Leg],
        width_pts: float,
        books: Mapping[str, Book],
        now: datetime,
        spot: float,
        reason: str,
        context: Mapping[str, Any] | None = None,
        lots: int = 1,
        max_loss_inr: float | None = None,
    ) -> tuple[bool, str]:
        """``max_loss_inr`` overrides the structure's worst case for the risk gate when a hard
        (spot-conditioned) stop defines the planned loss, e.g. a synthetic future."""
        qty = legs[0].qty
        # Defined risk only: refuse any short without a same-type wing (raises otherwise).
        intents = [
            SpreadLegIntent(
                lg.symbol,
                lg.side,
                lg.qty,
                1.0,
                lg.side == "BUY",
                option_type=lg.kind if lg.kind in ("CE", "PE") else None,
                is_future=lg.kind == "FUT",
            )
            for lg in legs
        ]
        sequence_entry_legs(intents)
        if self.entry_block is not None:
            why = self.entry_block(now)
            if why:
                return self._refuse(structure, now, why)
        wings = [lg for lg in legs if lg.side == "BUY"]
        shorts = [lg for lg in legs if lg.side == "SELL"]
        for lg in legs:
            b = books.get(lg.symbol)
            if b is None or not b.bids or not b.asks:
                return self._refuse(
                    structure, now, f"no two-sided book for {lg.strike:g} {lg.kind}"
                )
        est_credit = sum(books[s.symbol].bids[0][0] for s in shorts) - sum(
            books[w.symbol].asks[0][0] for w in wings
        )
        max_loss = worst_case(est_credit, width_pts) * qty if max_loss_inr is None else max_loss_inr
        ok, why = self.gate(max_loss, now, lots)
        if not ok:
            return self._refuse(structure, now, why)
        filled: list[Leg] = []
        # Stage 1: wings.
        for w in wings:
            q, px, ch, why = self._fill(
                w.symbol, "BUY", qty, books[w.symbol], now, exit_=False, kind=w.kind
            )
            if q < qty:
                self._unwind(filled + ([_part(w, q, px, ch)] if q else []), books, now)
                return self._refuse(structure, now, f"wing {w.strike:g} {w.kind} not filled: {why}")
            w.entry_px, w.entry_charges = round(px, 4), ch
            filled.append(w)
        # Stage 2: shorts, only now that their wings are held.
        for s in shorts:
            q, px, ch, why = self._fill(
                s.symbol, "SELL", qty, books[s.symbol], now, exit_=False, kind=s.kind
            )
            if q < qty:
                self._unwind(filled + ([_part(s, q, px, ch)] if q else []), books, now)
                return self._refuse(
                    structure, now, f"short {s.strike:g} {s.kind} not filled: {why}; wings unwound"
                )
            s.entry_px, s.entry_charges = round(px, 4), ch
            filled.append(s)
        credit = sum(s.entry_px for s in shorts) - sum(w.entry_px for w in wings)
        self._seq += 1
        pos = SpreadPosition(
            id=f"{structure}-{now.astimezone(IST):%Y%m%d-%H%M%S}",
            structure=structure,
            underlying=underlying,
            expiry=expiry,
            legs=list(filled),
            qty=qty,
            credit_pts=round(credit, 2),
            width_pts=width_pts,
            max_loss_inr=round(
                worst_case(credit, width_pts) * qty if max_loss_inr is None else max_loss_inr, 2
            ),
            opened_at=now,
            spot0=spot,
            reason=reason,
            context=dict(context or {}),
        )
        self.positions.append(pos)
        self.trades_today += 1
        self._write({"type": "open", "t": now.isoformat(), **_pos_json(pos)})
        what = f"credit {credit:.2f}" if credit >= 0 else f"debit {-credit:.2f}"
        return True, (
            f"opened {structure.replace('_', ' ').lower()} {_strikes(pos)}: {what} pts "
            f"(₹{abs(credit) * qty:,.0f}), worst case ₹{pos.max_loss_inr:,.0f}"
        )

    def _unwind(self, legs: Sequence[Leg], books: Mapping[str, Book], now: datetime) -> None:
        """Undo a half-built structure: buy back shorts first, then sell wings."""
        for lg in sorted(legs, key=lambda x: x.side != "SELL"):
            back = "BUY" if lg.side == "SELL" else "SELL"
            b = books.get(lg.symbol)
            if b is not None and lg.qty > 0:
                self._fill(lg.symbol, back, lg.qty, b, now, exit_=True, kind=lg.kind)

    def close(
        self, pos: SpreadPosition, books: Mapping[str, Book], now: datetime, reason: str
    ) -> tuple[bool, str]:
        """Shorts first (rule 1). A leg without a book stays open and is retried next time."""
        order = sorted((lg for lg in pos.legs if lg.open), key=lambda x: x.side != "SELL")
        for lg in order:
            if lg.side == "BUY" and any(x.open and x.side == "SELL" for x in pos.legs):
                return False, "shorts not all bought back yet; wings kept"
            b = books.get(lg.symbol)
            if b is None:
                return False, f"no book for {lg.strike:g} {lg.kind}; still open"
            back = "BUY" if lg.side == "SELL" else "SELL"
            q, px, ch, why = self._fill(lg.symbol, back, lg.qty, b, now, exit_=True, kind=lg.kind)
            if q < lg.qty:
                return False, f"{lg.strike:g} {lg.kind} exit not filled ({why}); still open"
            lg.open, lg.exit_px, lg.exit_charges = False, round(px, 4), ch
        debit = sum(lg.exit_px for lg in pos.legs if lg.side == "SELL") - sum(
            lg.exit_px for lg in pos.legs if lg.side == "BUY"
        )
        gross = (pos.credit_pts - debit) * pos.qty
        charges = sum(lg.entry_charges + lg.exit_charges for lg in pos.legs)
        trade = ClosedSpread(
            id=pos.id,
            structure=pos.structure,
            underlying=pos.underlying,
            expiry=pos.expiry,
            qty=pos.qty,
            credit_pts=pos.credit_pts,
            debit_pts=round(debit, 2),
            gross_inr=round(gross, 2),
            charges_inr=round(charges, 2),
            net_inr=round(gross - charges, 2),
            opened_at=pos.opened_at,
            closed_at=now,
            reason=reason,
            legs=tuple(asdict(lg) for lg in pos.legs),
            context=pos.context,
        )
        self.positions.remove(pos)
        self.closed.append(trade)
        self._write({"type": "close", "t": now.isoformat(), **_closed_json(trade)})
        return True, (
            f"closed {pos.structure.replace('_', ' ').lower()} {_strikes(pos)} ({reason}): "
            f"credit {pos.credit_pts:.2f}, paid {debit:.2f}, net ₹{trade.net_inr:,.0f}"
        )

    # ------------------------------------------------------------------ journal

    def _refuse(self, what: str, now: datetime, why: str) -> tuple[bool, str]:
        rec = {
            "type": "refused",
            "t": now.isoformat(),
            "time": now.astimezone(IST).strftime("%H:%M:%S"),
            "symbol": what,
            "why": why,
        }
        self.refusals.append(rec)
        self.refusals = self.refusals[-50:]
        self._write(rec)
        return False, f"refused {what.replace('_', ' ').lower()}: {why}"

    def _write(self, rec: dict[str, Any]) -> None:
        if self.journal is None:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"account": self.name, **rec}, default=str) + "\n")

    def load_journal(self, day: date) -> int:
        """Open structures from any earlier day (they are held overnight), and today's closed
        trades, refusals and counters."""
        if self.journal is None or not self.journal.exists():
            return 0
        iso = day.isocalendar()
        opens: dict[str, dict[str, Any]] = {}
        used = 0
        for line in self.journal.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                t = datetime.fromisoformat(rec["t"]).astimezone(IST)
            except (ValueError, KeyError, TypeError):
                continue
            if t.date() > day:
                continue
            kind = rec.get("type")
            if kind == "open":
                opens[rec["id"]] = rec
            elif kind == "close":
                opens.pop(rec["id"], None)
            if t.date() != day:
                if kind == "close" and t.date().isocalendar()[:2] == iso[:2]:
                    self.weekly_prior_net += float(rec.get("net_inr", 0.0))
                continue
            used += 1
            if kind == "open":
                self.trades_today += 1
            elif kind == "close":
                self.closed.append(_closed_from(rec))
            elif kind == "refused":
                self.refusals.append(rec)
        self.positions = [_pos_from(r) for r in opens.values()]
        return used

    # ------------------------------------------------------------------ view

    def view(self, books: Mapping[str, Book], now: datetime, spot: float | None) -> dict[str, Any]:
        out_pos = []
        unreal = 0.0
        day = now.astimezone(IST).date()
        for p in self.positions:
            ctc = cost_to_close(p, books)
            legs = []
            for lg in p.legs:
                b = books.get(lg.symbol)
                legs.append(
                    {
                        "symbol": lg.symbol,
                        "label": f"{lg.side} {lg.strike:g} {lg.kind}",
                        "side": lg.side,
                        "entry": lg.entry_px,
                        "bid": b.bids[0][0] if b is not None and b.bids else None,
                        "ask": b.asks[0][0] if b is not None and b.asks else None,
                        "open": lg.open,
                    }
                )
            net = None
            if ctc is not None:
                exit_ch = 0.0
                for lg in p.legs:
                    b = books.get(lg.symbol)
                    if not lg.open or b is None:
                        continue
                    lv = b.asks if lg.side == "SELL" else b.bids
                    px = lv[0][0] if lv else 0.0
                    exit_ch += fno_charges(
                        self.book, day, segment="futures" if lg.kind == "FUT" else "options",
                        side="buy" if lg.side == "SELL" else "sell",
                        turnover_inr=px * lg.qty, broker=self.broker_name,
                    ).total  # fmt: skip
                net = round((p.credit_pts - ctc) * p.qty - p.entry_charges - exit_ch, 2)
                unreal += net
            out_pos.append(
                {
                    "id": p.id,
                    "structure": p.structure,
                    "underlying": p.underlying,
                    "expiry": p.expiry.isoformat(),
                    "strikes": _strikes(p),
                    "qty": p.qty,
                    "credit": p.credit_pts,
                    "cost_to_close": round(ctc, 2) if ctc is not None else None,
                    "width": p.width_pts,
                    "max_loss": p.max_loss_inr,
                    "net": net,
                    "spot0": p.spot0,
                    "spot": spot,
                    "short_put": p.short_put,
                    "short_call": p.short_call,
                    "opened": p.opened_at.astimezone(IST).strftime("%d %b %H:%M"),
                    "reason": p.reason,
                    "legs": legs,
                }
            )
        return {
            "name": self.name,
            "positions": out_pos,
            "closed": [_closed_view(t) for t in self.closed[-20:]][::-1],
            "realized_net": self.realized_net,
            "unrealized_net": round(unreal, 2),
            "day_net": round(self.realized_net + unreal, 2),
            "trades_today": self.trades_today,
            "per_trade_limit": round(self.per_trade_limit(), 0),
            "refusals": self.refusals[-10:][::-1],
        }


def _part(lg: Leg, q: int, px: float, ch: float) -> Leg:
    return Leg(lg.symbol, lg.kind, lg.strike, lg.side, q, px, ch)


def worst_case(credit: float, width: float) -> float:
    """Per unit: a credit structure loses at most width − credit; a debit one (spread bought,
    straddle) at most what was paid."""
    return max(0.0, width - credit) if credit >= 0 else -credit


def _strikes(p: SpreadPosition) -> str:
    if p.structure not in ("IRON_CONDOR", "BULL_PUT", "BEAR_CALL"):
        return " / ".join(f"{lg.side[0]} {lg.strike:g} {lg.kind}" for lg in p.legs)
    sp, sc = p.short_put, p.short_call
    wp = next((lg.strike for lg in p.legs if lg.side == "BUY" and lg.kind == "PE"), None)
    wc = next((lg.strike for lg in p.legs if lg.side == "BUY" and lg.kind == "CE"), None)
    parts = []
    if sp is not None:
        parts.append(f"{wp:g}/{sp:g} PE")
    if sc is not None:
        parts.append(f"{sc:g}/{wc:g} CE")
    return " + ".join(parts)


def _pos_json(p: SpreadPosition) -> dict[str, Any]:
    d = asdict(p)
    d["expiry"] = p.expiry.isoformat()
    d["opened_at"] = p.opened_at.isoformat()
    return d


def _pos_from(r: Mapping[str, Any]) -> SpreadPosition:
    return SpreadPosition(
        id=str(r["id"]),
        structure=str(r["structure"]),
        underlying=str(r["underlying"]),
        expiry=date.fromisoformat(str(r["expiry"])),
        legs=[Leg(**lg) for lg in r["legs"]],
        qty=int(r["qty"]),
        credit_pts=float(r["credit_pts"]),
        width_pts=float(r["width_pts"]),
        max_loss_inr=float(r["max_loss_inr"]),
        opened_at=datetime.fromisoformat(str(r["opened_at"])),
        spot0=float(r.get("spot0", 0.0)),
        reason=str(r.get("reason", "")) + " (restored)",
        context=dict(r.get("context") or {}),
    )


def _closed_json(t: ClosedSpread) -> dict[str, Any]:
    d = asdict(t)
    d["expiry"] = t.expiry.isoformat()
    d["opened_at"] = t.opened_at.isoformat()
    d["closed_at"] = t.closed_at.isoformat()
    return d


def _closed_from(r: Mapping[str, Any]) -> ClosedSpread:
    return ClosedSpread(
        id=str(r["id"]),
        structure=str(r["structure"]),
        underlying=str(r["underlying"]),
        expiry=date.fromisoformat(str(r["expiry"])),
        qty=int(r["qty"]),
        credit_pts=float(r["credit_pts"]),
        debit_pts=float(r["debit_pts"]),
        gross_inr=float(r["gross_inr"]),
        charges_inr=float(r["charges_inr"]),
        net_inr=float(r["net_inr"]),
        opened_at=datetime.fromisoformat(str(r["opened_at"])),
        closed_at=datetime.fromisoformat(str(r["closed_at"])),
        reason=str(r.get("reason", "")),
        legs=tuple(r.get("legs") or ()),
        context=dict(r.get("context") or {}),
    )


def _closed_view(t: ClosedSpread) -> dict[str, Any]:
    return {
        "id": t.id,
        "structure": t.structure,
        "credit": t.credit_pts,
        "debit": t.debit_pts,
        "gross": t.gross_inr,
        "charges": t.charges_inr,
        "net": t.net_inr,
        "opened": t.opened_at.astimezone(IST).strftime("%d %b %H:%M"),
        "closed": t.closed_at.astimezone(IST).strftime("%d %b %H:%M"),
        "reason": t.reason,
    }


# ---------------------------------------------------------------------------- strategy


@dataclass
class Plan:
    structure: str
    expiry: date
    legs: list[Leg]
    width: float
    credit_est: float
    made_at: datetime
    spot: float
    reason: str


@dataclass
class Check:
    name: str
    ok: bool | None  # None = not checked / information only
    detail: str


@dataclass
class CondorStrategy:
    cfg: CondorConfig
    account: SpreadAccount
    # Hooks set by the cockpit core
    lot_size: Callable[[str], int]
    option_symbol: Callable[[str, date, float, str], str | None]  # und, expiry, strike, CE/PE
    pin: Callable[[list[str]], Any]
    trading_day_before: Callable[[date], date]
    context: Callable[[], dict[str, Any]] = dict  # market state at entry (set by the core)
    plan: Plan | None = None
    checks: list[Check] = field(default_factory=list)
    decision: str = "not evaluated yet"
    _next_eval: datetime | None = None

    def legs_in_use(self) -> list[str]:
        syms = [lg.symbol for p in self.account.positions for lg in p.legs if lg.open]
        if self.plan is not None:
            syms += [lg.symbol for lg in self.plan.legs]
        return list(dict.fromkeys(syms))

    # ------------------------------------------------------------------ entry

    def evaluate(
        self,
        now: datetime,
        *,
        strip: Mapping[str, Any],
        chain: Mapping[str, Any] | None,
        trend: Mapping[str, Any],
        event_scale: float | None,
        event_names: Sequence[str],
        events: Sequence[Any] = (),
    ) -> list[Check]:
        """The entry checklist; sets ``self.plan`` when everything passes."""
        c = self.cfg
        checks: list[Check] = []
        hhmm = now.astimezone(IST).strftime("%H:%M")
        checks.append(
            Check(
                "Entry window",
                c.first_entry <= hhmm <= c.last_entry,
                f"{c.first_entry}–{c.last_entry} (now {hhmm})",
            )
        )
        opened_today = self.account.trades_today
        checks.append(
            Check(
                "One structure a day",
                opened_today < c.max_per_day and not self.account.positions,
                "already holding one" if self.account.positions else f"{opened_today} opened today",
            )
        )
        exp_s = (chain or {}).get("expiry") or strip.get("expiry")
        expiry = date.fromisoformat(str(exp_s)) if exp_s else None
        days = (expiry - now.astimezone(IST).date()).days if expiry else None
        checks.append(
            Check(
                "Expiry far enough",
                days is not None and days >= c.min_days_to_expiry,
                f"{expiry} ({days} days; need ≥ {c.min_days_to_expiry})" if expiry else "no chain",
            )
        )
        if expiry is not None:
            exit_at = datetime.combine(
                self.trading_day_before(expiry),
                time.fromisoformat(c.exit_time),
                tzinfo=IST,
            )
            major, weight = self.events_before(events, now, exit_at)
            checks.append(
                Check(
                    "No major event before exit",
                    not major and weight < c.hold_earnings_weight_pct,
                    "; ".join(major)
                    or (
                        f"results of {weight:.1f}% of index weight before {exit_at:%d %b %H:%M}"
                        if weight
                        else f"none before {exit_at:%d %b %H:%M}"
                    ),
                )
            )
        ivp = strip.get("iv_percentile")
        checks.append(
            Check(
                "IV percentile high",
                ivp is not None and float(ivp) >= c.min_iv_percentile,
                f"{ivp:.0f} (need ≥ {c.min_iv_percentile:.0f})"
                if ivp is not None
                else "no IV history yet (needs ≥ 20 sessions)",
            )
        )
        if event_scale is None:
            checks.append(Check("No event window", True, "calendar not loaded"))
        else:
            checks.append(
                Check(
                    "No event window",
                    event_scale > 0,
                    "inside " + ", ".join(event_names) if event_scale <= 0 else "clear",
                )
            )
        up, down, forming = (
            int(trend.get("up", 0)),
            int(trend.get("down", 0)),
            trend.get("forming", []),
        )
        structure = "IRON_CONDOR"
        if up and down:
            flow_ok, flow = False, f"two-way tape: {up} UP and {down} DOWN confirmed setups"
        elif forming:
            flow_ok, flow = (
                False,
                f"setup forming ({', '.join(forming)}): wait for it to confirm or fail",
            )
        elif up or down:
            if c.one_sided_on_trend:
                structure = "BULL_PUT" if up else "BEAR_CALL"
                flow_ok = True
                side = "put" if up else "call"
                flow = f"confirmed {'UP' if up else 'DOWN'} setup: sell only the {side} side"
            else:
                flow_ok, flow = False, f"confirmed {'UP' if up else 'DOWN'} setup: trending tape"
        else:
            flow_ok, flow = True, "no confirmed setup: range-bound tape, both sides"
        checks.append(Check(f"Order-flow veto ({c.veto_lookback_min:.0f} min)", flow_ok, flow))
        gex = (chain or {}).get("gex") or {}
        checks.append(
            Check(
                "Dealer gamma (information)",
                None,
                f"{gex.get('regime', 'unknown')} (flip {gex.get('flip', '—')}); HYPOTHESIS",
            )
        )
        self.plan = None
        if all(ch.ok is not False for ch in checks) and expiry is not None:
            plan, why = self._make_plan(now, structure, expiry, strip, chain)
            checks.append(Check("Strikes and credit", plan is not None, why))
            if plan is not None:
                ok, gate_why = self.account.gate(
                    max(0.0, plan.width - plan.credit_est) * plan.legs[0].qty, now
                )
                checks.append(Check("Risk gate", ok, gate_why or "passes"))
                if ok:
                    self.plan = plan
                    self.pin(self.legs_in_use())
        self.checks = checks
        failed = [ch for ch in checks if ch.ok is False]
        if self.account.positions:
            self.decision = (
                "managing exits (take profit, stop, short-strike touch, order flow, time)"
            )
        elif self.plan is not None:
            self.decision = f"ready: {self.plan.reason}; waiting for live books on all legs"
        else:
            self.decision = "no trade: " + "; ".join(
                f"{f.name.lower()} ({f.detail})" for f in failed
            )
        return checks

    def events_before(
        self, events: Sequence[Any], now: datetime, until: datetime
    ) -> tuple[list[str], float]:
        """(major events, results index weight %) scheduled between now and the exit."""
        major: list[str] = []
        weight = 0.0
        for ce in events:
            ev = getattr(ce, "event", ce)
            start = ev.start_ts
            if not now <= start <= until:
                continue
            cat = str(getattr(ev.category, "value", ev.category))
            if cat in self.cfg.hold_block_categories:
                major.append(f"{ev.title} {start.astimezone(IST):%d %b %H:%M}")
            elif cat == "CONSTITUENT_EARNINGS":
                weight += float(ev.index_weight_pct or 0.0)
        return major, weight

    def _make_plan(
        self,
        now: datetime,
        structure: str,
        expiry: date,
        strip: Mapping[str, Any],
        chain: Mapping[str, Any] | None,
    ) -> tuple[Plan | None, str]:
        c = self.cfg
        rows = (chain or {}).get("rows") or []
        f = strip.get("forward") or strip.get("spot")
        if not rows or not f:
            return None, "no 1-minute chain with IVs yet"
        f = float(f)
        puts = [r for r in rows if r["k"] < f and (r.get("pe") or {}).get("delta") is not None]
        calls = [r for r in rows if r["k"] > f and (r.get("ce") or {}).get("delta") is not None]
        want_put = structure in ("IRON_CONDOR", "BULL_PUT")
        want_call = structure in ("IRON_CONDOR", "BEAR_CALL")
        sp = min(puts, key=lambda r: abs(abs(r["pe"]["delta"]) - c.short_delta)) if puts else None
        sc = min(calls, key=lambda r: abs(r["ce"]["delta"] - c.short_delta)) if calls else None
        if (want_put and sp is None) or (want_call and sc is None):
            return None, "no strike near the target delta in the chain"
        by_k = {r["k"]: r for r in rows}
        und = c.underlying
        limit = self.account.per_trade_limit()
        tried = []
        for width in c.wing_widths:
            legs: list[Leg] = []
            credit = 0.0
            ok = True
            for side_on, short, kind, wing_k in (
                (want_put, sp, "PE", (sp["k"] - width) if sp else 0.0),
                (want_call, sc, "CE", (sc["k"] + width) if sc else 0.0),
            ):
                if not side_on or short is None:
                    continue
                s_sym = self.option_symbol(und, expiry, short["k"], kind)
                w_sym = self.option_symbol(und, expiry, wing_k, kind)
                key = kind.lower()
                w_row = by_k.get(wing_k, {}).get(key) or {}
                s_bid = short[key].get("bid") or 0.0
                w_ask = w_row.get("ask") or 0.0
                if s_sym is None or w_sym is None or s_bid <= 0 or w_ask <= 0:
                    ok = False
                    break
                lot = max(1, self.lot_size(s_sym))
                legs.append(Leg(w_sym, kind, wing_k, "BUY", lot))
                legs.append(Leg(s_sym, kind, short["k"], "SELL", lot))
                credit += s_bid - w_ask
            if not ok or not legs:
                tried.append(f"{width:g}: wing not in chain")
                continue
            qty = legs[0].qty
            max_loss = max(0.0, width - credit) * qty
            if credit < width * c.min_credit_pct_of_width / 100.0:
                tried.append(
                    f"{width:g}: credit {credit:.1f} < {c.min_credit_pct_of_width:g}% of width"
                )
                continue
            if max_loss > limit:
                tried.append(f"{width:g}: worst case ₹{max_loss:,.0f} > ₹{limit:,.0f}")
                continue
            shorts = "/".join(f"{lg.strike:g}{lg.kind}" for lg in legs if lg.side == "SELL")
            plan = Plan(
                structure=structure,
                expiry=expiry,
                legs=legs,
                width=width,
                credit_est=round(credit, 2),
                made_at=now,
                spot=float(strip.get("spot") or f),
                reason=f"{structure.replace('_', ' ').lower()} short {shorts}, wings {width:g} "
                f"(est. credit {credit:.1f} pts, worst case ₹{max_loss:,.0f})",
            )
            return plan, plan.reason
        return None, "; ".join(tried) or "no width fits"

    def try_execute(self, now: datetime, books: Mapping[str, Book]) -> str | None:
        """Trade the plan once every leg has a live book; drop it if they never arrive."""
        p = self.plan
        if p is None:
            return None
        if (now - p.made_at).total_seconds() > self.cfg.book_wait_s:
            self.plan = None
            self.pin(self.legs_in_use())
            return "condor plan dropped: leg books did not arrive in time"
        if not all(lg.symbol in books and books[lg.symbol].bids and books[lg.symbol].asks
                   for lg in p.legs):  # fmt: skip
            return None
        self.plan = None
        ok, text = self.account.open_spread(
            structure=p.structure,
            underlying=self.cfg.underlying,
            expiry=p.expiry,
            legs=p.legs,
            width_pts=p.width,
            books=books,
            now=now,
            spot=p.spot,
            reason=p.reason,
            context=self.context(),
        )
        self.decision = (
            "managing exits (take profit, stop, short-strike touch, order flow, time)"
            if ok
            else f"entry refused: {text}"
        )
        self.pin(self.legs_in_use())
        return text

    # ------------------------------------------------------------------ exits

    def check_exits(
        self,
        now: datetime,
        books: Mapping[str, Book],
        spot: float | None,
        trend: Mapping[str, Any],
    ) -> list[str]:
        out: list[str] = []
        for pos in list(self.account.positions):
            why = self.exit_reason(pos, now, books, spot, trend)
            if why is None:
                continue
            ok, text = self.account.close(pos, books, now, why)
            out.append(text if ok else f"condor exit ({why}) pending: {text}")
            if ok:
                self.pin(self.legs_in_use())
        return out

    def exit_reason(
        self,
        pos: SpreadPosition,
        now: datetime,
        books: Mapping[str, Book],
        spot: float | None,
        trend: Mapping[str, Any],
    ) -> str | None:
        c = self.cfg
        local = now.astimezone(IST)
        exit_day = self.trading_day_before(pos.expiry)
        if local.date() > exit_day or (
            local.date() == exit_day and local.strftime("%H:%M") >= c.exit_time
        ):
            return f"time exit before expiry ({exit_day:%d %b} {c.exit_time})"
        ctc = cost_to_close(pos, books)
        if ctc is not None and pos.credit_pts > 0:
            if ctc <= pos.credit_pts * (1 - c.take_profit_pct / 100.0):
                return f"take profit ({c.take_profit_pct:.0f}% of credit)"
            if ctc >= pos.credit_pts * c.stop_mult:
                return f"stop ({c.stop_mult:g}x credit)"
        if spot:
            sp, sc = pos.short_put, pos.short_call
            if c.exit_on_short_touch and ((sp and spot <= sp) or (sc and spot >= sc)):
                return "index touched a short strike"
            near = c.defend_within_pct / 100.0
            if sc and trend.get("up") and spot >= sc * (1 - near):
                return (
                    f"order flow: confirmed UP setup, index within {c.defend_within_pct:g}% "
                    "of the short call"
                )
            if sp and trend.get("down") and spot <= sp * (1 + near):
                return (
                    f"order flow: confirmed DOWN setup, index within {c.defend_within_pct:g}% "
                    "of the short put"
                )
        return None

    def view(self) -> dict[str, Any]:
        c = self.cfg
        return {
            "decision": self.decision,
            "checks": [asdict(ch) for ch in self.checks],
            "plan": None
            if self.plan is None
            else {
                "structure": self.plan.structure,
                "legs": [f"{lg.side} {lg.strike:g} {lg.kind}" for lg in self.plan.legs],
                "credit_est": self.plan.credit_est,
                "width": self.plan.width,
            },
            "rules": {
                "enabled": c.enabled,
                "underlying": c.underlying,
                "window": f"{c.first_entry}-{c.last_entry}",
                "short_delta": c.short_delta,
                "wing_widths": list(c.wing_widths),
                "min_credit_pct": c.min_credit_pct_of_width,
                "min_ivp": c.min_iv_percentile,
                "take_profit_pct": c.take_profit_pct,
                "stop_mult": c.stop_mult,
                "defend_within_pct": c.defend_within_pct,
                "exit_time": c.exit_time,
                "veto_lookback_min": c.veto_lookback_min,
            },
        }


def previous_trading_day(day: date, is_trading_day: Callable[[date], bool] | None) -> date:
    d = day - timedelta(days=1)
    for _ in range(10):
        if is_trading_day is None:
            if d.weekday() < 5:
                return d
        elif is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return d
