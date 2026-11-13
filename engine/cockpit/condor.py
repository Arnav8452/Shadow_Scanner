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
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from engine.backtest.costs import fno_charges
from engine.cockpit.paper import EXIT_PROTECTION_PCT, Book
from engine.cockpit.stress import DEFAULT_SCENARIOS, gap_pnl, worst
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
    # 13 Nov 2026 (owner): before a weekend or holiday, book a structure that has captured at
    # least this % of its take-profit (do not carry gap risk for the last few rupees).
    weekend_book_pct: float = 80.0
    weekend_book_time: str = "15:10"
    # ...and close the whole condor if either side's cost to close reaches this x that side's
    # credit (a combined stop lets the winning side hide the tested one).
    side_stop_mult: float = 2.2
    # SPEC 5.1 adjustments: book the untested side once it has decayed this much while the
    # other side is tested (costs at least its credit to close); at most one adjustment.
    untested_decay_pct: float = 85.0
    # SPEC 5.1 gap rule: no stop exits in the first 15-minute candle (a gap through a short
    # strike is judged after it); the time exit still applies.
    first_candle_end: str = "09:30"


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
    # Margin to put the same legs on in the market (engine/cockpit/margin.py), once quoted.
    margin: dict[str, Any] = field(default_factory=dict)

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
    margin: dict[str, Any] = field(default_factory=dict)


def booked_debit(pos: SpreadPosition) -> float:
    """Per unit, what closed legs cost to close (shorts bought back minus wings sold)."""
    return sum(lg.exit_px for lg in pos.legs if not lg.open and lg.side == "SELL") - sum(
        lg.exit_px for lg in pos.legs if not lg.open and lg.side == "BUY"
    )


def _side_state(
    pos: SpreadPosition, kind: str, books: Mapping[str, Book]
) -> tuple[float, float] | None:
    """(credit, cost to close now) of one open side of a structure, per unit."""
    legs = [lg for lg in pos.legs if lg.kind == kind and lg.open]
    sh = [lg for lg in legs if lg.side == "SELL"]
    wg = [lg for lg in legs if lg.side == "BUY"]
    if not sh or not wg:
        return None
    bs, bw = [books.get(lg.symbol) for lg in sh], [books.get(lg.symbol) for lg in wg]
    if any(b is None or not b.asks for b in bs) or any(b is None or not b.bids for b in bw):
        return None
    credit = sum(lg.entry_px for lg in sh) - sum(lg.entry_px for lg in wg)
    ctc = sum(b.asks[0][0] for b in bs if b is not None) - sum(
        b.bids[0][0] for b in bw if b is not None
    )
    return credit, ctc


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
        self.margin_quoter: Any = None  # engine.cockpit.margin.MarginQuoter (live engine only)
        self._mcache: dict[tuple[tuple[str, int, str], ...], tuple[float, float]] = {}
        # Orders reach the exchange after ``fill_latency_s``; legs go one at a time,
        # ``leg_gap_s`` apart (wings first). 0 = all legs against the decision's books.
        self.fill_latency_s = 0.0
        self.leg_gap_s = 0.0
        self.executions: list[dict[str, Any]] = []
        self.open_net = 0.0  # open structures' net at the cost to close, refreshed every step
        self.liquidity: Any = None  # shared LiquidityMonitor, set by the cockpit
        self.vol_sizing: Any = None  # SPEC 5.7 VolSizing (spreads: skip only), set by the cockpit
        # SPEC 6 gap stress: LiveAnalytics (IV detail, spots), the scenarios, and whether this
        # account's structures are held overnight (then a structure whose worst gap loss exceeds
        # the per-trade spread limit is closed at ``gap_carry_time`` instead of carried).
        self.analytics: Any = None
        self.gap_scenarios: tuple[tuple[float, float], ...] = DEFAULT_SCENARIOS
        self.holds_overnight = False
        self.gap_carry: bool = True
        self.gap_carry_time = "15:10"
        self._carry_said: dict[str, date] = {}
        # Credit structures: the take-profit must be worth this many round-trips of charges
        # (0 = off). Set from cockpit.credit_charges_mult.
        self.charges_mult = 0.0
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
        open_risk = sum(p.max_loss_inr for p in self.positions) + sum(
            float(x["max_loss"]) for x in self.executions
        )
        open_loss = min(0.0, self.open_net)  # an open loss counts toward the loss limits
        return AccountRiskState(
            realized_daily_pnl_inr=self.realized_net + open_loss,
            realized_weekly_pnl_inr=self.weekly_prior_net + self.realized_net + open_loss,
            open_risk_inr=open_risk,
            # Broker margin: the live quote for a position's legs, else the paper margin table.
            margin_used_inr=sum(self.margin_of(p) for p in self.positions)
            + sum(float(x["margin"]) for x in self.executions),
            trades_taken_today=self.trades_today + len(self.executions),
            consecutive_losses_today=streak,
            last_loss_at=losses[-1].closed_at if losses else None,
        )

    def gate(
        self, max_loss_inr: float, now: datetime, lots: int = 1, margin_inr: float | None = None
    ) -> tuple[bool, str]:
        g = check_pre_trade_risk_gate(
            self.limits,
            self.risk_state(),
            proposed_trade_max_loss_inr=max_loss_inr,
            proposed_margin_inr=max_loss_inr if margin_inr is None else margin_inr,
            proposed_lots=lots,
            strategy_kind="defined_risk_spread",
            now=now,
            session_open=self.session_open,
        )
        return g.allowed, "; ".join(g.reasons)

    # ------------------------------------------------------------------ margin

    @staticmethod
    def _mkey(legs: Sequence[Leg]) -> tuple[tuple[str, int, str], ...]:
        return tuple(sorted((lg.symbol, int(lg.qty), lg.side) for lg in legs))

    def table_margin(self, structure: str, lots: int, day: date) -> float:
        """Paper margin table (config/rules/margin_rules.yaml, from broker quotes + buffer)."""
        cls = _MARGIN_CLASS.get(structure, "unknown")
        try:
            row = self.book.lookup(
                "margin_rules", day, allow_unverified=True, name="paper_margin_per_lot"
            )
            per_lot = float(row.params.get(cls) or row.params.get("unknown") or 0.0)
        except Exception:
            per_lot = 86500.0
        return per_lot * max(1, lots)

    def margin_for(self, structure: str, legs: Sequence[Leg], lots: int, day: date) -> float:
        """Margin to put these legs on: a live quote (prefetched) when fresh, else the table."""
        hit = self._mcache.get(self._mkey(legs))
        if hit is not None and datetime.now(IST).timestamp() - hit[0] < 900:
            return hit[1]
        return self.table_margin(structure, lots, day)

    def prefetch_margin(self, legs: Sequence[Leg]) -> None:
        """Ask the broker's calculator for these legs now, so the gate has a live figure by
        the time the plan executes (plans wait for leg books anyway)."""
        if self.margin_quoter is None or not legs:
            return
        key = self._mkey(legs)
        hit = self._mcache.get(key)
        if hit is not None and datetime.now(IST).timestamp() - hit[0] < 900:
            return

        def done(res: dict[str, Any]) -> None:
            if res.get("final"):
                self._mcache[key] = (datetime.now(IST).timestamp(), float(res["final"]))

        self.margin_quoter.request([(s, q, side) for s, q, side in key], done)

    def margin_of(self, pos: SpreadPosition) -> float:
        final = (pos.margin or {}).get("final")
        if final:
            return float(final)
        return self.table_margin(
            pos.structure, max(1, pos.qty // max(1, pos.legs[0].qty)), pos.opened_at.date()
        )

    def round_trip_charges(
        self, legs: Sequence[Leg], books: Mapping[str, Book], day: date
    ) -> float:
        """Entry charges at the touch, doubled for the exit (a conservative round trip)."""
        total = 0.0
        for lg in legs:
            b = books.get(lg.symbol)
            lv = (b.asks if lg.side == "BUY" else b.bids) if b is not None else ()
            px = lv[0][0] if lv else 0.0
            total += fno_charges(
                self.book, day, segment="futures" if lg.kind == "FUT" else "options",
                side="buy" if lg.side == "BUY" else "sell",
                turnover_inr=px * lg.qty, broker=self.broker_name,
            ).total  # fmt: skip
        return round(2 * total, 2)

    def executing_symbols(self) -> list[str]:
        return [lg.symbol for x in self.executions for lg in x["legs"]]

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
        take_profit_pct: float | None = None,
        on_done: Any = None,
    ) -> tuple[bool, str]:
        """``max_loss_inr`` overrides the structure's worst case for the risk gate when a hard
        (spot-conditioned) stop defines the planned loss, e.g. a synthetic future.
        ``take_profit_pct`` (credit structures) is checked against round-trip charges.
        ``on_done(ok, text, position | None)`` runs when the order completes, which with
        ``fill_latency_s`` > 0 is a few steps later (the call then returns "order sent")."""
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
        if self.liquidity is not None:
            for lg in legs:
                vac = self.liquidity.vacuum(lg.symbol, books[lg.symbol])
                if vac:
                    return self._refuse(structure, now, f"{lg.strike:g} {lg.kind}: {vac}")
        if self.vol_sizing is not None:
            # SPEC 5.7: a 1-lot structure cannot be cut, so high realized vol skips it.
            n, note, vctx = self.vol_sizing.size(legs[0].symbol, 1, now.astimezone(IST).date())
            if n <= 0:
                return self._refuse(structure, now, note or "vol-managed sizing")
            context = {**dict(context or {}), **vctx}
        est_credit = sum(books[s.symbol].bids[0][0] for s in shorts) - sum(
            books[w.symbol].asks[0][0] for w in wings
        )
        max_loss = worst_case(est_credit, width_pts) * qty if max_loss_inr is None else max_loss_inr
        day = now.astimezone(IST).date()
        if self.executions:
            return self._done(
                on_done, *self._refuse(structure, now, "an order is already on its way")
            )
        if take_profit_pct and est_credit > 0 and self.charges_mult > 0:
            rt = self.round_trip_charges(legs, books, day)
            target = est_credit * take_profit_pct / 100 * qty
            if target < self.charges_mult * rt:
                return self._done(on_done, *self._refuse(
                    structure, now,
                    f"charges: take-profit Rs {target:,.0f} ({take_profit_pct:g}% of a "
                    f"{est_credit:.1f}-pt credit) is under {self.charges_mult:g}x the round-trip "
                    f"charges Rs {rt:,.0f}",
                ))  # fmt: skip
        margin = self.margin_for(structure, legs, lots, day)
        ok, why = self.gate(max_loss, now, lots, margin_inr=margin)
        if not ok:
            return self._done(on_done, *self._refuse(structure, now, why))
        ex: dict[str, Any] = {
            "structure": structure, "underlying": underlying, "expiry": expiry,
            "legs": [*wings, *shorts], "todo": [*wings, *shorts], "filled": [],
            "qty": qty, "width": width_pts, "spot": spot, "reason": reason,
            "context": dict(context or {}), "max_loss_override": max_loss_inr,
            "max_loss": max_loss, "margin": margin, "on_done": on_done,
            "decided": now, "est_credit": est_credit,
        }  # fmt: skip
        if self.fill_latency_s > 0:
            ex["next_at"] = now.timestamp() + self.fill_latency_s
            self.executions.append(ex)
            return True, (
                f"order sent: {structure.replace('_', ' ').lower()} "
                f"({len(ex['legs'])} legs, wings first; {self.fill_latency_s * 1000:.0f} ms to "
                f"the exchange, {self.leg_gap_s * 1000:.0f} ms between legs)"
            )
        while ex["todo"]:
            ok, text = self._fill_next(ex, books, now)
            if not ok:
                return False, text
        return self._finish(ex, now)

    def process_executions(self, books: Mapping[str, Book], now: datetime) -> list[str]:
        """Advance orders on their way: at most one leg per order per step once its time has
        come, against the books of that moment."""
        out: list[str] = []
        net = 0.0
        for p in self.positions:
            ctc = cost_to_close(p, books)
            if ctc is not None:
                net += (p.credit_pts - ctc) * p.qty - p.entry_charges
        self.open_net = net
        for ex in list(self.executions):
            if now.timestamp() < float(ex["next_at"]):
                continue
            ok, text = self._fill_next(ex, books, now)
            if not ok:
                self.executions.remove(ex)
                out.append(text)
                continue
            if ex["todo"]:
                ex["next_at"] = now.timestamp() + self.leg_gap_s
                continue
            self.executions.remove(ex)
            out.append(self._finish(ex, now)[1])
        return out

    def _fill_next(
        self, ex: dict[str, Any], books: Mapping[str, Book], now: datetime
    ) -> tuple[bool, str]:
        lg = ex["todo"][0]
        qty = int(ex["qty"])
        b = books.get(lg.symbol)
        if b is None or not b.bids or not b.asks:
            q, px, ch, why = 0, 0.0, 0.0, "no book"
        else:
            q, px, ch, why = self._fill(lg.symbol, lg.side, qty, b, now, exit_=False, kind=lg.kind)
        if q < qty:
            self._unwind(ex["filled"] + ([_part(lg, q, px, ch)] if q else []), books, now)
            what = "wing" if lg.side == "BUY" else "short"
            ok, text = self._refuse(
                ex["structure"], now,
                f"{what} {lg.strike:g} {lg.kind} not filled: {why}"
                + ("; wings unwound" if lg.side == "SELL" else ""),
            )  # fmt: skip
            self._done(ex["on_done"], ok, text)
            return False, text
        lg.entry_px, lg.entry_charges = round(px, 4), ch
        ex["filled"].append(lg)
        ex["todo"].pop(0)
        return True, ""

    def _done(self, on_done: Any, ok: bool, text: str, pos: Any = None) -> tuple[bool, str]:
        if on_done is not None:
            on_done(ok, text, pos)
        return ok, text

    def _finish(self, ex: dict[str, Any], now: datetime) -> tuple[bool, str]:
        structure, qty, width_pts = ex["structure"], int(ex["qty"]), float(ex["width"])
        max_loss_inr, filled = ex["max_loss_override"], ex["filled"]
        shorts = [lg for lg in filled if lg.side == "SELL"]
        wings = [lg for lg in filled if lg.side == "BUY"]
        underlying, expiry, spot = ex["underlying"], ex["expiry"], ex["spot"]
        reason, context = ex["reason"], ex["context"]
        credit = sum(s.entry_px for s in shorts) - sum(w.entry_px for w in wings)
        sold = sum(s.entry_px for s in shorts)
        if sold > 0 and wings:
            # SPEC 5.1: wing cost as % of the premium sold (far wings are often overpriced).
            context = {**dict(context or {}), "wing_cost_pct": round(
                sum(w.entry_px for w in wings) / sold * 100, 1)}  # fmt: skip
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
        self._quote_margin(pos)
        what = f"credit {credit:.2f}" if credit >= 0 else f"debit {-credit:.2f}"
        lag = (
            f"; estimated {float(ex['est_credit']):.2f} at the decision"
            if self.fill_latency_s > 0
            else ""
        )
        return self._done(
            ex["on_done"],
            True,
            f"opened {structure.replace('_', ' ').lower()} {_strikes(pos)}: {what} pts "
            f"(₹{abs(credit) * qty:,.0f}{lag}), worst case ₹{pos.max_loss_inr:,.0f}",
            pos,
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
            margin=dict(pos.margin),
        )
        self.positions.remove(pos)
        self.closed.append(trade)
        self._write({"type": "close", "t": now.isoformat(), **_closed_json(trade)})
        return True, (
            f"closed {pos.structure.replace('_', ' ').lower()} {_strikes(pos)} ({reason}): "
            f"credit {pos.credit_pts:.2f}, paid {debit:.2f}, net ₹{trade.net_inr:,.0f}"
        )

    # ------------------------------------------------------------------ gap stress

    def gap_stress(self, legs: Sequence[Leg], underlying: str) -> dict[str, float] | None:
        a = self.analytics
        if a is None:
            return None
        spot = (getattr(a, "strip", {}).get(underlying) or {}).get("spot")
        return gap_pnl(legs, getattr(a, "detail", {}) or {}, spot, self.gap_scenarios)

    def gap_cap(self) -> float:
        return float(
            self.limits.inr(self.limits.per_trade_max_loss_pct.get("defined_risk_spread", 0.0))
        )

    def carry_check(self, books: Mapping[str, Book], now: datetime) -> list[str]:
        """SPEC 6: from ``gap_carry_time``, a structure held overnight whose worst +-5/8% gap
        loss exceeds the per-trade spread limit is closed rather than carried (e.g. a synthetic
        future, whose stop-based planned loss does not bound a gap past the stop)."""
        if not (self.holds_overnight and self.gap_carry and self.positions):
            return []
        if now.astimezone(IST).strftime("%H:%M") < self.gap_carry_time:
            return []
        cap = self.gap_cap()
        out: list[str] = []
        for pos in list(self.positions):
            if any(lg.symbol in self.executing_symbols() for lg in pos.legs):
                continue
            w = worst(self.gap_stress(pos.legs, pos.underlying))
            if w is None or cap <= 0 or -w[1] <= cap:
                continue
            ok, text = self.close(
                pos, books, now,
                f"gap stress: a {w[0]} overnight gap would lose Rs {-w[1]:,.0f} > the "
                f"Rs {cap:,.0f} per-trade limit; not carried",
            )  # fmt: skip
            if ok or self._carry_said.get(pos.id) != now.astimezone(IST).date():
                out.append(text)
                self._carry_said[pos.id] = now.astimezone(IST).date()
        return out

    def close_side(
        self, pos: SpreadPosition, kind: str, books: Mapping[str, Book], now: datetime, reason: str
    ) -> tuple[bool, str]:
        """Close one side (CE or PE legs) of a structure, short first; the rest stays open.
        The booked legs keep their exit prices, so the final close totals every leg."""
        legs = sorted(
            (lg for lg in pos.legs if lg.open and lg.kind == kind), key=lambda x: x.side != "SELL"
        )
        if not legs:
            return False, "nothing open on that side"
        for lg in legs:
            b = books.get(lg.symbol)
            if b is None:
                return False, f"no book for {lg.strike:g} {lg.kind}"
            back = "BUY" if lg.side == "SELL" else "SELL"
            q, px, ch, why = self._fill(lg.symbol, back, lg.qty, b, now, exit_=True, kind=lg.kind)
            if q < lg.qty:
                return False, f"{lg.strike:g} {lg.kind} exit not filled ({why})"
            lg.open, lg.exit_px, lg.exit_charges = False, round(px, 4), ch
        pos.context = {**pos.context, "adjustments": int(pos.context.get("adjustments", 0)) + 1}
        self._write({"type": "adjust", "t": now.isoformat(), "id": pos.id, "reason": reason,
                     "legs": [asdict(lg) for lg in pos.legs], "context": pos.context})  # fmt: skip
        paid = sum(lg.exit_px for lg in legs if lg.side == "SELL") - sum(
            lg.exit_px for lg in legs if lg.side == "BUY"
        )
        return True, f"booked the {kind} side of {_strikes(pos)} ({reason}): paid {paid:.2f}"

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

    def attach_margin(self, quoter: Any) -> None:
        """Use ``quoter`` for new positions and quote any restored one that has no margin yet."""
        self.margin_quoter = quoter
        for p in self.positions:
            if not p.margin:
                self._quote_margin(p)

    def _quote_margin(self, pos: SpreadPosition) -> None:
        """Margin to put these legs on in the market, quoted in the background (the trade never
        waits for it). Without a quoter the worst-case loss stays the margin proxy."""
        if self.margin_quoter is None:
            pos.margin = {"error": "no margin quote in this run (simulation or replay)"}
            return
        pos.margin = {"pending": True}
        legs = [(lg.symbol, int(lg.qty), lg.side) for lg in pos.legs if lg.open]

        def done(res: dict[str, Any]) -> None:
            # Journal first, so a restart right after the in-memory update still finds it.
            self._write({"type": "margin", "t": datetime.now(IST).isoformat(), "id": pos.id, **res})
            pos.margin = res
            for i, t in enumerate(self.closed):  # closed before the answer came back
                if t.id == pos.id:
                    self.closed[i] = replace(t, margin=res)

        self.margin_quoter.request(legs, done)

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
        margins: dict[str, dict[str, Any]] = {}
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
            if kind == "margin":
                margins[str(rec.get("id"))] = {
                    k: v for k, v in rec.items() if k not in ("account", "type", "t", "id")
                }
                continue
            if kind == "open":
                opens[rec["id"]] = rec
            elif kind == "adjust" and rec.get("id") in opens:
                opens[rec["id"]] = {**opens[rec["id"]], "legs": rec["legs"],
                                    "context": rec.get("context") or {}}  # fmt: skip
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
        for p in self.positions:
            p.margin = margins.get(p.id, {})
        self.closed = [
            c if c.margin or c.id not in margins else replace(c, margin=margins[c.id])
            for c in self.closed
        ]
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
                    "margin": p.margin,
                    "stress": self.gap_stress(p.legs, p.underlying),
                    "stress_cap": self.gap_cap(),
                    "context": {
                        k: p.context[k] for k in ("wing_cost_pct", "adjustments") if k in p.context
                    },
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


_MARGIN_CLASS = {
    "BULL_PUT": "credit_2leg",
    "BEAR_CALL": "credit_2leg",
    "BULL_CALL": "debit_2leg",
    "BEAR_PUT": "debit_2leg",
    "IRON_CONDOR": "four_leg",
    "IRON_FLY": "four_leg",
    "CALENDAR": "calendar",
    "SYNTHETIC_LONG": "synthetic",
    "SYNTHETIC_SHORT": "synthetic",
    "MARRIED_FUTURE": "married_future",
    "MARRIED_SHORT_FUTURE": "married_future",
}


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
        margin=dict(r.get("margin") or {}),
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
        "margin": t.margin,
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
        syms += self.account.executing_symbols()
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
                opened_today < c.max_per_day
                and not self.account.positions
                and not self.account.executions,
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
                self.account.prefetch_margin(plan.legs)
                ok, gate_why = self.account.gate(
                    max(0.0, plan.width - plan.credit_est) * plan.legs[0].qty,
                    now,
                    margin_inr=self.account.margin_for(
                        plan.structure, plan.legs, 1, now.astimezone(IST).date()
                    ),
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
            take_profit_pct=self.cfg.take_profit_pct,
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
            side = self.untested_side(pos, now, books)
            if side is not None:
                ok, text = self.account.close_side(pos, side[0], books, now, side[1])
                out.append(text)
                if ok:
                    self.pin(self.legs_in_use())
            why = self.exit_reason(pos, now, books, spot, trend)
            if why is None:
                continue
            ok, text = self.account.close(pos, books, now, why)
            out.append(text if ok else f"condor exit ({why}) pending: {text}")
            if ok:
                self.pin(self.legs_in_use())
        return out

    def untested_side(
        self, pos: SpreadPosition, now: datetime, books: Mapping[str, Book]
    ) -> tuple[str, str] | None:
        """SPEC 5.1 adjustment: (side to book, reason) when one side of an iron condor has
        decayed ``untested_decay_pct`` while the other costs at least its credit to close."""
        c = self.cfg
        if pos.structure != "IRON_CONDOR" or int(pos.context.get("adjustments", 0)) >= 1:
            return None
        if now.astimezone(IST).strftime("%H:%M") < c.first_candle_end:
            return None
        sides = {k: _side_state(pos, k, books) for k in ("PE", "CE")}
        if any(v is None for v in sides.values()):
            return None
        for k, other in (("PE", "CE"), ("CE", "PE")):
            cr, ctc = sides[k]  # type: ignore[misc]
            ocr, octc = sides[other]  # type: ignore[misc]
            if cr > 0 and ctc <= cr * (1 - c.untested_decay_pct / 100) and octc >= ocr:
                return k, (
                    f"untested {'put' if k == 'PE' else 'call'} side decayed "
                    f"{(1 - ctc / cr) * 100:.0f}% while the other side is tested"
                )
        return None

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
        if local.strftime("%H:%M") < c.first_candle_end:
            return None  # SPEC 5.1: a gap is judged after the first 15-minute candle
        ctc = cost_to_close(pos, books)
        if ctc is not None:
            ctc += booked_debit(pos)  # a side already booked counts at what it cost
        if ctc is not None and pos.credit_pts > 0:
            if ctc <= pos.credit_pts * (1 - c.take_profit_pct / 100.0):
                return f"take profit ({c.take_profit_pct:.0f}% of credit)"
            if ctc >= pos.credit_pts * c.stop_mult:
                return f"stop ({c.stop_mult:g}x credit)"
            target = pos.credit_pts * c.take_profit_pct / 100.0
            got = pos.credit_pts - ctc
            tomorrow_closed = self.trading_day_before(local.date() + timedelta(days=2)) == (
                local.date()
            )
            if (
                tomorrow_closed
                and local.strftime("%H:%M") >= c.weekend_book_time
                and target > 0
                and got >= target * c.weekend_book_pct / 100.0
            ):
                return (
                    f"booked before the market closure: {got / target * 100:.0f}% of the "
                    f"take-profit captured (>= {c.weekend_book_pct:g}%)"
                )
        for side, kind in (("put", "PE"), ("call", "CE")):
            legs = [lg for lg in pos.legs if lg.kind == kind and lg.open]
            sh = [lg for lg in legs if lg.side == "SELL"]
            wg = [lg for lg in legs if lg.side == "BUY"]
            if not sh or not wg:
                continue
            credit = sum(lg.entry_px for lg in sh) - sum(lg.entry_px for lg in wg)
            bs = [books.get(lg.symbol) for lg in sh]
            bw = [books.get(lg.symbol) for lg in wg]
            if (
                credit <= 0
                or any(b is None or not b.asks for b in bs)
                or any(b is None or not b.bids for b in bw)
            ):
                continue
            side_ctc = sum(b.asks[0][0] for b in bs if b is not None) - sum(
                b.bids[0][0] for b in bw if b is not None
            )
            if side_ctc >= credit * c.side_stop_mult:
                return (
                    f"{side} side stop: costs {side_ctc:.2f} to close vs {credit:.2f} credit "
                    f"(>= {c.side_stop_mult:g}x)"
                )
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
