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
from datetime import datetime, timedelta
from typing import Any

from engine.cockpit.condor import Leg, SpreadAccount, _strikes, cost_to_close
from engine.cockpit.gates import BreakevenRatchet, breakeven, impulse_ok
from engine.cockpit.paper import Book, PaperAccount
from engine.cockpit.tape import IndexTape, OptionTape
from engine.core.rules import IST
from engine.data.reference.instrument_master import Kind


def hhmm(now: datetime) -> str:
    return now.astimezone(IST).strftime("%H:%M")


def fmt(x: float | None, d: int = 1, sign: bool = False) -> str:
    """A number for a status line ("—" when not known yet)."""
    if x is None:
        return "—"
    return f"{x:+.{d}f}" if sign else f"{x:,.{d}f}"


class Status:
    """What a strategy is waiting for, rebuilt every step: one note per index (or condition)
    with the live value next to the threshold it needs. The page shows the joined notes."""

    notes: list[str]
    decision: str

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.decision = " · ".join(self.notes)

    def renote(self, text: str) -> None:
        """Replace the latest note (a more specific reason found further down the checks)."""
        if self.notes:
            self.notes[-1] = text
        else:
            self.notes.append(text)
        self.decision = " · ".join(self.notes)

    def in_window(self, now: datetime, window: tuple[str, str]) -> bool:
        h = hhmm(now)
        if window[0] <= h <= window[1]:
            return True
        self.note(
            f"entries {window[0]}–{window[1]}"
            + (" (opens later)" if h < window[0] else " (closed for today)")
        )
        return False


class Market:
    """Read-only view of the cockpit core for strategies (one per step)."""

    def __init__(self, core: Any, now: datetime) -> None:
        self.core = core
        self.now = now
        self._books: dict[str, Book] | None = None
        # Chain picks that are not in the live feed yet (by_delta); a long play pins them.
        self.want_feed: list[str] = []

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
        self,
        und: str,
        kind: str,
        target: float,
        rows: list[dict[str, Any]] | None = None,
        live_band: float = 0.05,
    ) -> tuple[str, dict[str, Any]] | None:
        """Contract of ``kind`` whose |delta| is closest to ``target``.

        A contract already in the live feed (book + live greeks) within ``live_band`` of the
        target wins: strategies score and trade off its live book. Otherwise the chain's pick is
        returned and added to ``want_feed`` so the strategy can pin it (the live feed carries
        only ~50 contracts; Sensex often has just +-2 strikes in it)."""
        key = kind.lower()
        best = None
        live = None
        for r in rows if rows is not None else self.chain_rows(und):
            side = r.get(key) or {}
            d, sym = side.get("delta"), side.get("sym")
            if d is None or not sym:
                continue
            gap = abs(abs(float(d)) - target)
            row = side | {"k": float(r["k"])}
            if best is None or gap < best[0]:
                best = (gap, str(sym), row)
            b, g = self.book(str(sym)), self.detail(str(sym))
            if b is not None and b.bids and b.asks and g.get("delta") is not None:
                lgap = abs(abs(float(g["delta"])) - target)
                # Ties (equal live gaps) go to the contract the chain also ranks closer.
                if lgap <= live_band and (live is None or (lgap, gap) < live[0]):
                    live = ((lgap, gap), str(sym), row | {"delta": g["delta"]})
        if live is not None:
            return live[1], live[2]
        if best is None:
            return None
        want: list[str] = self.__dict__.setdefault("want_feed", [])
        if best[1] not in want:
            want.append(best[1])
        return best[1], best[2]

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
        self,
        sym: str,
        qty: int,
        mult: float,
        und: str,
        delta_band: tuple[float, float] | None = (0.55, 0.65),
    ) -> tuple[bool, str, float, float]:
        """Owner rule: a short-hold trade only on a 0.55-0.65 delta option (``delta_band``;
        intrinsic movement must dominate theta) whose expected 3-minute move is at least
        ``mult`` x its charges breakeven. Returns (ok, why, true_be, be_points)."""
        b = self.book(sym)
        if b is None or not b.asks:
            return False, "no book", 0.0, 0.0
        entry = b.asks[0][0]
        true_be, be_pts = self.breakeven(entry, qty)
        g = self.detail(sym)
        d = g.get("delta") if g else None
        if (
            delta_band is not None
            and d is not None
            and not delta_band[0] <= abs(float(d)) <= delta_band[1]
        ):
            return (
                False,
                f"delta {abs(float(d)):.2f} outside {delta_band[0]:g}-{delta_band[1]:g}",
                true_be,
                be_pts,
            )
        ok, imp = impulse_ok(self.tape(und).atr(), g.get("delta"), g.get("gamma"), be_pts, mult)
        why = (
            f"impulse {imp:.2f} pts, needs {mult * be_pts:.2f} ({mult:g}x breakeven {be_pts:.2f})"
            if g
            else "no greeks for the contract"
        )
        return ok, why, true_be, be_pts

    def pin(self, owner: str, syms: list[str]) -> list[str] | None:
        """Add ``syms`` to the live feed for ``owner``; returns the ones accepted."""
        got = self.core._pin(owner, syms)
        return list(got) if got is not None else None


# ------------------------------------------------------------------------------ long plays


def last_bar_new(mg: Any, ot: Any) -> bool:
    """True once per completed 1-minute bar (the 9-EMA exit judges closes, not ticks)."""
    if len(ot.closes) < 2:
        return False
    stamp = ot.closes[-2][0]
    if getattr(mg, "last_bar", None) == stamp:
        return False
    mg.last_bar = stamp
    return True


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
    last_bar: Any = None


@dataclass
class LongPlay(Status):
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
    off_reason: str = "off (cockpit.plays in settings.yaml)"
    notes: list[str] = field(default_factory=list)
    retry_s: float = 60.0  # after a refused entry, wait this long before retrying that index
    retry_at: dict[str, datetime] = field(default_factory=dict)
    _pin_after: datetime = field(default=datetime.min.replace(tzinfo=IST), repr=False)
    # A stop tightened to fit the per-trade limit must stay at least this % of premium.
    min_fit_stop_pct: float = 10.0
    # Short-hold strategies: positions found open after a restart are put back under the
    # charges ratchet (initial stop, breakeven lock, 180-s scratch) and the candle rule.
    scalp: bool = False
    gate2_mode: str = "volatility"  # cockpit.gate2_mode: "volatility" or "charges"
    awaiting: dict[str, dict[str, Any]] = field(default_factory=dict)  # orders not yet filled

    def _new_day(self, now: datetime) -> None:
        d = now.astimezone(IST).date()
        if self.day != d:
            self.day, self.entries_today = d, 0

    def _adopt(self, now: datetime, m: Market) -> None:
        """Open positions without a manager (the engine restarted after the entry) get one,
        so the in-trade rules keep applying instead of only the account's premium stop."""
        for sym, spec in list(self.awaiting.items()):
            if sym in self.account.positions:
                del self.awaiting[sym]
                self._manage_new(m, sym, self.account.positions[sym], spec)
            elif (
                sym not in getattr(self.account, "pending", {})
                and (now - spec["sent"]).total_seconds() > 30
            ):
                del self.awaiting[sym]  # the order was refused when it arrived
        for sym, pos in list(self.account.positions.items()):
            if sym in self.managed:
                continue
            ins = m.core.master.get(sym) if sym in m.core.master else None
            und = ins.underlying if ins is not None else ""
            bullish = not (ins is not None and ins.kind is Kind.PUT)
            true_be, be_pts = m.breakeven(pos.avg_price, pos.qty)
            mg = Managed(sym, und, pos.opened_at, pos.avg_price, true_be, be_pts, bullish=bullish)
            if self.scalp:
                mg.ratchet = BreakevenRatchet.start(
                    pos.avg_price, pos.qty, true_be, be_pts, pos.opened_at,
                    mode=self.gate2_mode, atr=m.otape(sym).atr(),
                )  # fmt: skip
                mg.candle_stop_min = 15
                pos.stop_price = max(pos.stop_price, mg.ratchet.stop)
            self.managed[sym] = mg

    def step(self, now: datetime, m: Market) -> list[str]:
        self._new_day(now)
        self._adopt(now, m)
        out = self.manage(now, m)
        self.notes = []
        if not self.enabled:
            self.decision = self.off_reason
            return out
        if self.entries_today >= self.max_entries:
            self.note(f"{self.max_entries} entries today: done for the day")
        else:
            m.want_feed = []
            out += self.look(now, m) or []
            self._feed_candidates(now, m)
        held = [m.core.label(s) for s in self.managed]
        head = [f"holding {', '.join(held)}"] if held else []
        waits = [
            f"{u}: retrying after {t.astimezone(IST):%H:%M:%S} (last entry refused)"
            for u, t in self.retry_at.items()
            if now < t
        ]
        self.decision = " · ".join(head + self.notes + waits) or "watching"
        return [x for x in out if x]

    def look(self, now: datetime, m: Market) -> list[str] | None:  # pragma: no cover - abstract
        return None

    def _feed_candidates(self, now: datetime, m: Market) -> None:
        """Pin the contracts this play picked from the chain but could not score (no live book),
        so the next look can; at most every 30 s (a pin re-centres the feed)."""
        pins = getattr(m.runner, "pins", None)
        if not isinstance(pins, dict):
            return
        want = list(dict.fromkeys([*self.managed, *self.awaiting, *m.want_feed]))
        if set(want) == set(pins.get(self.key, [])) or now < self._pin_after:
            return
        self._pin_after = now + timedelta(seconds=30)
        got = m.pin(self.key, want) or []
        miss = [s for s in m.want_feed if s not in got]
        if miss:
            self.note(f"live feed full: could not add {', '.join(m.core.label(s) for s in miss)}")

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
        acct = self.account
        if acct.trades_today + len(acct.pending) >= acct.limits.max_trades_per_day:
            # The account's daily trade cap: no order would pass the gate, so don't send one
            # (and don't journal a refusal) on every trigger for the rest of the day.
            self.note(f"daily trade cap reached ({acct.limits.max_trades_per_day} trades)")
            return ""
        wait = self.retry_at.get(und)
        if wait is not None and now < wait:
            return ""  # backing off after a refusal: no new attempt, no new event line
        ivp = m.ivp(und)
        if self.hiv_route is not None and ivp is not None and ivp >= self.hiv_route.ivp_min:
            # Rule 4: IV in its top band -> no naked buying; the signal becomes a debit spread.
            self.entries_today += 1
            return str(self.hiv_route.route(m, und, bullish, f"{self.title}: {reason}"))
        b = m.book(sym)
        if b is None or not b.asks:
            # Chosen from the 1-minute chain but not in the 30-level feed: ask for it, and try
            # again once its book can have arrived instead of on every step.
            m.pin(self.key, [*self.managed, *self.awaiting, sym])
            self.retry_at[und] = now + timedelta(seconds=10)
            self.note(f"{und}: adding {m.core.label(sym)} to the live feed; retrying in 10 s")
            return f"{self.title}: no book for {m.core.label(sym)} yet; added it to the live feed"
        lot = m.lot(sym)
        qty = lots * lot
        entry = b.asks[0][0]
        if stop_inr is not None and entry > 0:  # rupee risk per unit -> % of premium
            stop_pct = max(1.0, min(90.0, stop_inr / entry * 100))
        if ratchet and entry > 0:
            # Gate 2, initial risk window: -12% of premium or Rs 1,200 on the position (and in
            # volatility mode within 2 x the option's 1-minute ATR, never tighter than its
            # charges breakeven), booked at entry so the risk gate sees the real planned loss.
            dist = min(entry * 0.12, 1200.0 / max(qty, 1))
            atr = m.otape(sym).atr() if self.gate2_mode == "volatility" else None
            if atr:
                dist = max(min(dist, 2.0 * atr), m.breakeven(entry, qty)[1])
            stop_pct = max(1.0, min(stop_pct, dist / entry * 100))
        cap = float(acct.limits.inr(acct.limits.per_trade_max_loss_pct.get("directional", 0.0)))
        if cap > 0 and entry > 0 and entry * qty * stop_pct / 100 > cap:
            # The planned loss would breach the per-trade limit (a Sensex 0.6-delta option at
            # Rs 900 x 20 with a 35% stop is Rs 6,300 > 5,000): tighten the stop to fit, as long
            # as it stays a real stop; otherwise skip the contract instead of being refused.
            fit = cap * 0.98 / (entry * qty) * 100
            if fit < self.min_fit_stop_pct:
                self.retry_at[und] = now + timedelta(seconds=self.retry_s)
                self.note(
                    f"{und}: {m.core.label(sym)} too dear for the Rs {cap:,.0f} per-trade limit "
                    f"(a fitting stop would be {fit:.0f}% of premium, under "
                    f"{self.min_fit_stop_pct:g}%)"
                )
                return ""
            reason += f" (stop {fit:.0f}% of premium, was {stop_pct:.0f}%, to fit the limit)"
            stop_pct = fit
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
            # The gate (cool-off, loss limits...) or the book refused: don't hammer it on every
            # step while the trigger persists.
            self.retry_at[und] = now + timedelta(seconds=self.retry_s)
            return text
        self.retry_at.pop(und, None)
        self.entries_today += 1
        spec = {
            "und": und,
            "bullish": bullish,
            "ratchet": ratchet,
            "time_stop": time_stop,
            "candle_stop_min": candle_stop_min,
            "ema_after_pct": ema_after_pct,
            "delta_exit": delta_exit,
            "spot_stop": spot_stop,
            "sent": now,
        }
        m.otape(sym)
        pos = self.account.positions.get(sym)
        if pos is None:  # the order is on its way (fill latency): managed once it fills
            self.awaiting[sym] = spec
        else:
            self._manage_new(m, sym, pos, spec)
        return text.replace(sym, m.core.label(sym))

    def _manage_new(self, m: Market, sym: str, pos: Any, spec: dict[str, Any]) -> None:
        """Attach this strategy's exit rules to a filled position (charges breakeven from the
        actual fill; the ratchet's clock starts at the fill)."""
        true_be, be_pts = m.breakeven(pos.avg_price, pos.qty)
        mg = Managed(
            sym,
            spec["und"],
            pos.opened_at,
            pos.avg_price,
            true_be,
            be_pts,
            bullish=spec["bullish"],
            time_stop=spec["time_stop"],
            candle_stop_min=spec["candle_stop_min"],
            ema_after_pct=spec["ema_after_pct"],
            delta_exit=spec["delta_exit"],
            spot_stop=spec["spot_stop"],
        )
        if spec["ratchet"]:
            mg.ratchet = BreakevenRatchet.start(
                pos.avg_price, pos.qty, true_be, be_pts, pos.opened_at,
                mode=self.gate2_mode, atr=m.otape(sym).atr(),
            )  # fmt: skip
            pos.stop_price = max(pos.stop_price, mg.ratchet.stop)
        self.managed[sym] = mg

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
                ot = m.otape(sym)
                why = mg.ratchet.on_bid(bid, now, atr=ot.atr())
                pos.stop_price = max(pos.stop_price, mg.ratchet.stop)
                if (
                    why is None
                    and mg.ratchet.mode == "volatility"
                    and mg.ratchet.state == "BREAKEVEN_LOCKED"
                ):
                    e, last = ot.ema(9), ot.last_close()
                    if e and last is not None and last < e and last_bar_new(mg, ot):
                        why = f"runner: 1-min close {last:.2f} under the 9-EMA {e:.2f}"
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
class SpreadPlay(Status):
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
    off_reason: str = "off (cockpit.plays in settings.yaml)"
    notes: list[str] = field(default_factory=list)
    last_result: str = ""  # the latest entry attempt's outcome (refusals stay visible)
    retry_s: float = 60.0  # after a refused or dropped plan, wait this long before the next
    retry_at: datetime | None = None
    # A structure whose short leg expires today is closed by this time, before the 15:10-15:40
    # closing-VWAP window that sets its settlement (cockpit.spread_expiry_exit).
    expiry_exit: str = "15:00"

    def step(self, now: datetime, m: Market) -> list[str]:
        d = now.astimezone(IST).date()
        if self.day != d:
            self.day, self.entries_today = d, 0
        out = self.manage(now, m)
        out += self.try_pending(now, m)
        self.notes = []
        if not self.enabled:
            self.decision = self.off_reason
            return out
        if self.retry_at is not None and now < self.retry_at:
            self.note(
                f"retrying after {self.retry_at.astimezone(IST):%H:%M:%S} (last plan refused)"
            )
        elif self.account.executions:
            self.note("order on its way to the exchange (legs filling one by one)")
        elif self.pending is None:
            out += self.look(now, m) or []
        head = [
            f"holding {p.structure.replace('_', ' ').lower()} {_strikes(p)}"
            for p in self.account.positions
        ]
        if self.pending is not None:
            head.append(f"placing: {self.pending['reason']} (waiting for every leg's book)")
        tail = [f"last attempt: {self.last_result}"] if self.last_result else []
        self.decision = " · ".join(head + self.notes + tail) or "watching"
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
        self.account.prefetch_margin(legs)
        want = self.legs_in_use()
        got = m.pin(self.key, want)
        if got is not None and len(got) < len(want):
            # The live feed holds 50 contracts and strategies may add 10 between them.
            self.last_result = (
                f"only {len(got)} of {len(want)} legs fit in the live feed "
                "(strategy slots full); the plan will lapse in 60 s"
            )

    def legs_in_use(self) -> list[str]:
        syms = [lg.symbol for p in self.account.positions for lg in p.legs if lg.open]
        syms += self.account.executing_symbols()
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
            self.last_result = f"{p['reason']}: dropped at {hhmm(now)}, leg books did not arrive"
            self.retry_at = now + timedelta(seconds=self.retry_s)
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
            take_profit_pct=(p["trade"].target_pct if p["trade"].target_pct else None),
            on_done=lambda done, txt, pos: self._on_open(done, txt, pos, p, m),
        )
        if ok:
            self.entries_today += 1
        m.pin(self.key, self.legs_in_use())
        return [text]

    def _on_open(self, ok: bool, text: str, pos: Any, p: dict[str, Any], m: Market) -> None:
        """The order completed (now, or legs later with fill latency)."""
        now = m.now
        if ok and pos is not None:
            tr = p["trade"]
            tr.pos_id = pos.id
            tr.opened = now
            self.trades[tr.pos_id] = tr
            self.last_result = ""
        elif not ok:
            self.last_result = f"{hhmm(now)} refused: {text}"
            self.retry_at = now + timedelta(seconds=self.retry_s)
        m.pin(self.key, self.legs_in_use())

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
            shorts_expiring = (
                any(lg.open and lg.side == "SELL" and lg.kind != "FUT" for lg in pos.legs)
                and pos.expiry == now.astimezone(IST).date()
            )
            why = (
                f"expiry-day exit {self.expiry_exit} (before the 15:10 settlement window)"
                if shorts_expiring and hhmm(now) >= self.expiry_exit
                else None
            )
            why = (
                why
                or spread_exit(pos, tr, books, now, m.spot(pos.underlying))
                or (self.extra_exit(pos, tr, now, m))
            )
            if why is None:
                continue
            ok, text = self.account.close(pos, books, now, why)
            out.append(text if ok else f"{self.title} exit ({why}) pending: {text}")
            if ok:
                self.trades.pop(pos.id, None)
                m.pin(self.key, self.legs_in_use())
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
class HighIvPlay(SpreadPlay):
    """Rule 4's account: receives naked-buy signals when IV is rich (see ``HighIvRouter``)."""

    ivp_min: float = 80.0

    def look(self, now: datetime, m: Market) -> list[str] | None:
        ivps = ", ".join(f"{u} {fmt(m.ivp(u), 0)}" for u in m.underlyings())
        self.note(
            f"takes naked-buy signals (put-panic, OI ignition, breakthrough) as debit spreads "
            f"when ATM IV is at or above the {self.ivp_min:g}th percentile; now {ivps}"
        )
        return None


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


__all__ = [
    "HighIvPlay",
    "HighIvRouter",
    "Kind",
    "LongPlay",
    "Managed",
    "Market",
    "SpreadPlay",
    "SpreadTrade",
    "Status",
    "fmt",
]
