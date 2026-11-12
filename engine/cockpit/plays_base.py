"""Shared machinery for the playbook strategies (owner's notes in ``new/``, 12 Nov 2026).

``Market`` gives a strategy read access to what the cockpit core knows (spot, front future,
minute tapes, live option books and greeks, the 1-minute chains, setups, zones, events) plus
helpers (strike by delta, charges breakeven, feed pins, entry context). ``LongPlay`` manages
long-option trades with the exits the notes ask for:

- Gate 2 breakeven ratchet (initial stop, stop to charges-breakeven at 2x breakeven points,
  then trailed) with the 180-s scratch rule, for the short-hold strategies;
- a velocity time stop (e.g. not +10% within 12 minutes) and the candle-count rule (still below
  breakeven after 15 one-minute candles);
- a 1-minute 9-EMA trail on the option once it is up a given %, and an exit on a delta target.

Every number lives in the strategy's config and is a HYPOTHESIS. Paper only.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from engine.cockpit.condor import Leg, SpreadAccount, cost_to_close
from engine.cockpit.gates import BreakevenRatchet, breakeven, impulse_ok
from engine.cockpit.paper import Book, PaperAccount
from engine.cockpit.tape import IndexTape, OptionTape
from engine.core.rules import IST
from engine.data.reference.instrument_master import Kind


def hhmm(now: datetime) -> str:
    return now.astimezone(IST).strftime("%H:%M")


class Market:
    """Read-only view of the cockpit core for strategies (one per step)."""

    def __init__(self, core: Any, now: datetime) -> None:
        self.core = core
        self.now = now
        self._books: dict[str, Book] | None = None

    # ---------------------------------------------------------------- prices
    @property
    def runner(self) -> Any:
        return self.core.runner

    def underlyings(self) -> list[str]:
        return list(getattr(self.runner, "spot_symbol", {}))

    def spot(self, und: str) -> float:
        return float(self.runner.spots().get(und, 0.0))

    def fut(self, und: str) -> Any:
        return self.core._front_future(und)

    def books(self) -> dict[str, Book]:
        if self._books is None:
            self._books = self.core.books()
        return self._books

    def book(self, sym: str) -> Book | None:
        return self.books().get(sym)

    def tape(self, und: str) -> IndexTape:
        t: IndexTape = self.core.tapes.setdefault(und, IndexTape(und))
        return t

    def otape(self, sym: str) -> OptionTape:
        self.core.otape_watch.add(sym)
        o: OptionTape = self.core.otapes.setdefault(sym, OptionTape())
        return o

    # ---------------------------------------------------------------- analytics
    def strip(self, und: str) -> dict[str, Any]:
        a = self.core.analytics
        return dict(a.strip.get(und, {})) if a is not None else {}

    def chain(self, und: str) -> dict[str, Any]:
        a = self.core.analytics
        return dict((a.chain.get(und) or {}) if a is not None else {})

    def chain_rows(self, und: str) -> list[dict[str, Any]]:
        return list(self.chain(und).get("rows") or [])

    def tables(self, und: str) -> dict[str, dict[str, Any]]:
        a = self.core.analytics
        return dict((a.chains.get(und) or {}) if a is not None else {})

    def detail(self, sym: str) -> dict[str, Any]:
        a = self.core.analytics
        return dict((a.detail.get(sym) or {}) if a is not None else {})

    def ivp(self, und: str) -> float | None:
        v = self.strip(und).get("iv_percentile")
        return float(v) if v is not None else None

    def by_delta(
        self, und: str, kind: str, target: float, rows: list[dict[str, Any]] | None = None
    ) -> tuple[str, dict[str, Any]] | None:
        """Contract of ``kind`` whose |delta| is closest to ``target`` (from the chain)."""
        key = kind.lower()
        best = None
        for r in rows if rows is not None else self.chain_rows(und):
            side = r.get(key) or {}
            d, sym = side.get("delta"), side.get("sym")
            if d is None or not sym:
                continue
            gap = abs(abs(float(d)) - target)
            if best is None or gap < best[0]:
                best = (gap, str(sym), side | {"k": float(r["k"])})
        return (best[1], best[2]) if best else None

    def strike_step(self, und: str) -> float:
        ks = sorted(float(r["k"]) for r in self.chain_rows(und))
        steps = [b - a for a, b in itertools.pairwise(ks) if b > a]
        return min(steps) if steps else (100.0 if und == "SENSEX" else 50.0)

    def option_symbol(self, und: str, expiry: Any, strike: float, kind: str) -> str | None:
        sym = self.core.option_symbol(und, expiry, strike, kind)
        return str(sym) if sym else None

    def expiry_today(self, und: str) -> bool:
        return bool(self.core._expiry_today(und, self.now.astimezone(IST).date()))

    def lot(self, sym: str) -> int:
        m = self.core.master
        return max(1, m.get(sym).lot_size) if sym in m else 1

    def trend(self, und: str) -> dict[str, Any]:
        return dict(self.core._trend(und, self.now))

    def zones(self, und: str) -> dict[str, Any]:
        f = self.fut(und)
        return self.core.zones.zones(und, f.ltp) if f is not None and f.ltp else {}

    def day_returns(self) -> dict[str, float] | None:
        d = getattr(self.core.analytics, "dispersion", None) if self.core.analytics else None
        return dict(d.get("day_returns_pct") or {}) if d else None

    def context(self, und: str) -> dict[str, Any]:
        return dict(self.core.market_context(und, self.now))

    def event_block(self) -> str | None:
        ev = self.core.calendar_events
        if not ev:
            return None
        from engine.data.events.calendar_file import blackout_status

        st = blackout_status(ev, self.now)
        return (
            f"event window: {', '.join(st.active_blackout_events)}"
            if st.in_macro_blackout
            else None
        )

    # ---------------------------------------------------------------- costs and gates
    def breakeven(self, entry: float, qty: int) -> tuple[float, float]:
        acct = self.core.bot
        return breakeven(acct.book, acct.broker_name, self.now.astimezone(IST).date(), entry, qty)

    def scalp_gate(
        self, sym: str, qty: int, mult: float, und: str
    ) -> tuple[bool, str, float, float]:
        """Owner rule: a short-hold trade only when the expected 3-minute option move is at
        least ``mult`` x its charges breakeven. Returns (ok, why, true_be, be_points)."""
        b = self.book(sym)
        if b is None or not b.asks:
            return False, "no book", 0.0, 0.0
        entry = b.asks[0][0]
        true_be, be_pts = self.breakeven(entry, qty)
        g = self.detail(sym)
        ok, imp = impulse_ok(self.tape(und).atr(), g.get("delta"), g.get("gamma"), be_pts, mult)
        why = (
            f"impulse {imp:.2f} pts vs {mult}x breakeven {be_pts:.2f}"
            if g
            else "no greeks for the contract"
        )
        return ok, why, true_be, be_pts

    def pin(self, owner: str, syms: list[str]) -> None:
        self.core._pin(owner, syms)


# ------------------------------------------------------------------------------ long plays


@dataclass
class Managed:
    sym: str
    und: str
    opened: datetime
    entry: float
    true_be: float
    be_pts: float
    ratchet: BreakevenRatchet | None = None
    time_stop: tuple[float, float] | None = None  # (minutes, needed gain %)
    candle_stop_min: float | None = None
    ema_after_pct: float | None = None
    delta_exit: float | None = None
    spot_stop: float | None = None  # exit when the index crosses this (direction-aware)
    bullish: bool = True


@dataclass
class LongPlay:
    key: str
    title: str
    ref: str
    account: PaperAccount
    enabled: bool = True
    decision: str = "watching"
    rules: str = ""
    managed: dict[str, Managed] = field(default_factory=dict)
    day: Any = None
    entries_today: int = 0
    max_entries: int = 3
    hiv_route: Any = None  # HighIvRouter for the naked-buying rule 4

    def _new_day(self, now: datetime) -> None:
        d = now.astimezone(IST).date()
        if self.day != d:
            self.day, self.entries_today = d, 0

    def step(self, now: datetime, m: Market) -> list[str]:
        self._new_day(now)
        out = self.manage(now, m)
        if self.enabled:
            out += self.look(now, m) or []
        return out

    def look(self, now: datetime, m: Market) -> list[str] | None:  # pragma: no cover - abstract
        return None

    def enter(
        self,
        m: Market,
        und: str,
        sym: str,
        *,
        lots: int,
        reason: str,
        bullish: bool,
        stop_pct: float = 20.0,
        ratchet: bool = False,
        time_stop: tuple[float, float] | None = None,
        candle_stop_min: float | None = None,
        ema_after_pct: float | None = None,
        delta_exit: float | None = None,
        target_pct: float | None = None,
        spot_stop: float | None = None,
        stop_inr: float | None = None,
    ) -> str:
        now = m.now
        if self.entries_today >= self.max_entries:
            return f"{self.title}: {self.max_entries} entries today already"
        ivp = m.ivp(und)
        if self.hiv_route is not None and ivp is not None and ivp >= self.hiv_route.ivp_min:
            # Rule 4: IV in its top band -> no naked buying; the signal becomes a debit spread.
            self.entries_today += 1
            return str(self.hiv_route.route(m, und, bullish, f"{self.title}: {reason}"))
        b = m.book(sym)
        if b is None or not b.asks:
            return f"{self.title}: no book for {m.core.label(sym)}"
        lot = m.lot(sym)
        qty = lots * lot
        entry = b.asks[0][0]
        if stop_inr is not None and entry > 0:  # rupee risk per unit -> % of premium
            stop_pct = max(1.0, min(90.0, stop_inr / entry * 100))
        ok, text = self.account.open_long_option(
            sym,
            b,
            now=now,
            lots=lots,
            stop_pct=stop_pct,
            target_pct=target_pct,
            reason=f"{self.ref} {reason}",
            context=m.context(und),
        )
        if not ok:
            return text
        self.entries_today += 1
        pos = self.account.positions[sym]
        true_be, be_pts = m.breakeven(pos.avg_price, qty)
        mg = Managed(
            sym,
            und,
            now,
            pos.avg_price,
            true_be,
            be_pts,
            bullish=bullish,
            time_stop=time_stop,
            candle_stop_min=candle_stop_min,
            ema_after_pct=ema_after_pct,
            delta_exit=delta_exit,
            spot_stop=spot_stop,
        )
        if ratchet:
            mg.ratchet = BreakevenRatchet.start(pos.avg_price, qty, true_be, be_pts, now)
            pos.stop_price = max(pos.stop_price, mg.ratchet.stop)
        self.managed[sym] = mg
        m.otape(sym)
        return text.replace(sym, m.core.label(sym))

    def manage(self, now: datetime, m: Market) -> list[str]:
        out: list[str] = []
        for sym, mg in list(self.managed.items()):
            pos = self.account.positions.get(sym)
            if pos is None:
                del self.managed[sym]
                continue
            b = m.book(sym)
            if b is None or not b.bids:
                continue
            bid = b.bids[0][0]
            why = None
            held_min = (now - mg.opened).total_seconds() / 60
            if mg.ratchet is not None:
                why = mg.ratchet.on_bid(bid, now)
                pos.stop_price = max(pos.stop_price, mg.ratchet.stop)
            if why is None and mg.time_stop and held_min >= mg.time_stop[0]:
                if bid < mg.entry * (1 + mg.time_stop[1] / 100):
                    why = f"time stop: not +{mg.time_stop[1]:g}% in {mg.time_stop[0]:g} min"
                else:
                    mg.time_stop = None  # it made the move in time: the velocity check is done
            if (
                why is None
                and mg.candle_stop_min
                and held_min >= mg.candle_stop_min
                and bid < mg.true_be
            ):
                why = f"still below charges breakeven after {mg.candle_stop_min:g} candles"
            if mg.ema_after_pct is not None and bid >= mg.entry * (1 + mg.ema_after_pct / 100):
                e = m.otape(sym).ema(9)
                if e:
                    pos.stop_price = max(pos.stop_price, round(e, 2))
            if why is None and mg.delta_exit is not None:
                d = m.detail(sym).get("delta")
                if d is not None and abs(float(d)) >= mg.delta_exit:
                    why = f"delta reached {abs(float(d)):.2f}"
            if why is None and mg.spot_stop is not None:
                s = m.spot(mg.und)
                if s and (
                    (mg.bullish and s < mg.spot_stop) or (not mg.bullish and s > mg.spot_stop)
                ):
                    why = f"index through the structural stop {mg.spot_stop:,.1f}"
            if why:
                ok, text = self.account.close(sym, b, now=now, reason=why)
                out.append(text.replace(sym, m.core.label(sym)) if ok else f"{sym}: {text}")
                if ok:
                    self.managed.pop(sym, None)
        return out

    def view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "ref": self.ref,
            "enabled": self.enabled,
            "decision": self.decision,
            "rules": self.rules,
            "kind": "long",
        }


# ------------------------------------------------------------------------------ spread helper


@dataclass
class SpreadTrade:
    """Exit rules attached to one open structure of a spread play."""

    pos_id: str
    und: str
    opened: datetime
    target_pct: float | None = None  # of credit captured (credit) or of max value (debit)
    stop_mult: float | None = None  # cost to close >= stop_mult x credit (credit structures)
    debit_stop_pct: float | None = None  # value <= this % of the debit (debit structures)
    square_off: str | None = "15:20"
    spot_stop: float | None = None
    bullish: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


def spread_exit(
    pos: Any, tr: SpreadTrade, books: Mapping[str, Book], now: datetime, spot: float
) -> str | None:
    if tr.square_off and hhmm(now) >= tr.square_off:
        return f"square-off {tr.square_off}"
    ctc = cost_to_close(pos, books)
    if ctc is not None:
        if pos.credit_pts > 0:  # credit structure
            if tr.target_pct and ctc <= pos.credit_pts * (1 - tr.target_pct / 100):
                return f"take profit ({tr.target_pct:g}% of credit)"
            if tr.stop_mult and ctc >= pos.credit_pts * tr.stop_mult:
                return f"stop ({tr.stop_mult:g}x credit)"
        else:  # debit structure: value now = -ctc
            debit, value = -pos.credit_pts, -ctc
            width = pos.width_pts
            if (
                tr.target_pct
                and width > debit
                and value >= debit + tr.target_pct / 100 * (width - debit)
            ):
                return f"target ({tr.target_pct:g}% of max value)"
            if tr.debit_stop_pct and value <= debit * tr.debit_stop_pct / 100:
                return f"stop (worth {tr.debit_stop_pct:g}% of the debit)"
    if (
        tr.spot_stop is not None
        and spot
        and ((tr.bullish and spot < tr.spot_stop) or (not tr.bullish and spot > tr.spot_stop))
    ):
        return f"index through the structural stop {tr.spot_stop:,.1f}"
    return None


@dataclass
class SpreadPlay:
    key: str
    title: str
    ref: str
    account: SpreadAccount
    enabled: bool = True
    decision: str = "watching"
    rules: str = ""
    trades: dict[str, SpreadTrade] = field(default_factory=dict)
    day: Any = None
    entries_today: int = 0
    max_entries: int = 1
    pending: dict[str, Any] | None = None  # a plan waiting for leg books
    overnight: bool = False  # held across sessions (no 15:20 square-off, restored on restart)

    def step(self, now: datetime, m: Market) -> list[str]:
        d = now.astimezone(IST).date()
        if self.day != d:
            self.day, self.entries_today = d, 0
        out = self.manage(now, m)
        out += self.try_pending(now, m)
        if self.enabled and self.pending is None:
            out += self.look(now, m) or []
        return out

    def look(self, now: datetime, m: Market) -> list[str] | None:  # pragma: no cover - abstract
        return None

    def extra_exit(self, pos: Any, tr: SpreadTrade, now: datetime, m: Market) -> str | None:
        return None

    def plan(
        self,
        m: Market,
        und: str,
        structure: str,
        legs: list[Leg],
        width: float,
        reason: str,
        trade: SpreadTrade,
        *,
        lots: int = 1,
        max_loss_inr: float | None = None,
        expiry: Any = None,
    ) -> None:
        self.pending = {
            "und": und,
            "structure": structure,
            "legs": legs,
            "width": width,
            "reason": reason,
            "trade": trade,
            "lots": lots,
            "made": m.now,
            "max_loss": max_loss_inr,
            "expiry": expiry,
        }
        m.pin(self.key, self.legs_in_use())
        self.decision = f"ready: {reason} (waiting for leg books)"

    def legs_in_use(self) -> list[str]:
        syms = [lg.symbol for p in self.account.positions for lg in p.legs if lg.open]
        if self.pending:
            syms += [lg.symbol for lg in self.pending["legs"]]
        return list(dict.fromkeys(syms))

    def try_pending(self, now: datetime, m: Market) -> list[str]:
        p = self.pending
        if p is None:
            return []
        books = m.books()
        if (now - p["made"]).total_seconds() > 60:
            self.pending = None
            m.pin(self.key, self.legs_in_use())
            return [f"{self.title}: plan dropped (leg books did not arrive)"]
        if not all(
            lg.symbol in books and books[lg.symbol].bids and books[lg.symbol].asks
            for lg in p["legs"]
        ):
            return []
        self.pending = None
        exp = p["expiry"] or next(
            (
                m.core.master.get(lg.symbol).expiry
                for lg in p["legs"]
                if lg.symbol in m.core.master and m.core.master.get(lg.symbol).expiry
            ),
            now.date(),
        )
        ok, text = self.account.open_spread(
            structure=p["structure"],
            underlying=p["und"],
            expiry=exp,
            legs=p["legs"],
            width_pts=p["width"],
            books=books,
            now=now,
            spot=m.spot(p["und"]),
            reason=f"{self.ref} {p['reason']}",
            context=m.context(p["und"]),
            lots=p["lots"],
            max_loss_inr=p["max_loss"],
        )
        if ok:
            self.entries_today += 1
            tr = p["trade"]
            tr.pos_id = self.account.positions[-1].id
            tr.opened = now
            self.trades[tr.pos_id] = tr
            self.decision = "holding; managing exits"
        else:
            self.decision = text
        m.pin(self.key, self.legs_in_use())
        return [text]

    def manage(self, now: datetime, m: Market) -> list[str]:
        out: list[str] = []
        books = m.books()
        for pos in list(self.account.positions):
            tr = self.trades.get(pos.id) or SpreadTrade(
                pos.id,
                pos.underlying,
                pos.opened_at,
                square_off=None if self.overnight else "15:20",
            )
            why = spread_exit(pos, tr, books, now, m.spot(pos.underlying)) or self.extra_exit(
                pos, tr, now, m
            )
            if why is None:
                continue
            ok, text = self.account.close(pos, books, now, why)
            out.append(text if ok else f"{self.title} exit ({why}) pending: {text}")
            if ok:
                self.trades.pop(pos.id, None)
                m.pin(self.key, self.legs_in_use())
                self.decision = "watching"
        return out

    def view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "ref": self.ref,
            "enabled": self.enabled,
            "decision": self.decision,
            "rules": self.rules,
            "kind": "spread",
        }


@dataclass
class HighIvRouter:
    """Naked-buying rule 4: with ATM IV at or above its ``ivp_min`` percentile, a naked-buy
    signal is placed as a vertical debit spread (buy ~0.55 delta, sell ~0.25 delta)."""

    play: SpreadPlay
    ivp_min: float = 80.0

    def route(self, m: Market, und: str, bullish: bool, reason: str) -> str:
        if self.play.pending is not None or self.play.account.positions:
            return f"{reason}: IV rich, routed spread skipped (one already open)"
        kind = "CE" if bullish else "PE"
        long_ = m.by_delta(und, kind, 0.55)
        short = m.by_delta(und, kind, 0.25)
        if long_ is None or short is None or long_[0] == short[0]:
            return f"{reason}: IV rich, no strikes for a debit spread"
        lot = m.lot(long_[0])
        width = abs(short[1]["k"] - long_[1]["k"])
        self.play.plan(
            m,
            und,
            "BULL_CALL" if bullish else "BEAR_PUT",
            [
                Leg(long_[0], kind, long_[1]["k"], "BUY", lot),
                Leg(short[0], kind, short[1]["k"], "SELL", lot),
            ],
            width,
            f"IV ≥ {self.ivp_min:g}th pct: {reason}",
            SpreadTrade(
                "",
                und,
                m.now,
                target_pct=60,
                debit_stop_pct=50,
                square_off="15:20",
                bullish=bullish,
            ),
        )
        return (
            f"{reason}: IV rich (≥ {self.ivp_min:g}th pct) -> debit spread instead of a naked buy"
        )


__all__ = ["HighIvRouter", "Kind", "LongPlay", "Managed", "Market", "SpreadPlay", "SpreadTrade"]
