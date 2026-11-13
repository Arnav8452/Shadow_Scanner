"""Cockpit hook inside the engine loop (P2 live, P4 replay).

The runner calls :meth:`CockpitCore.on_step` after each batch of feed events. All state changes
happen here, on the engine thread: paper orders from the page arrive as queued commands and are
applied against the current order book. A snapshot is published every ``publish_interval_ms``
(sooner after a fill); the publisher's own threads do the sending.

Any exception in here is caught by the runner, so the cockpit can never stop 30-level recording.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from engine.cockpit.analytics_live import LiveAnalytics
from engine.cockpit.bus import EnginePublisher
from engine.cockpit.config import CockpitConfig
from engine.cockpit.paper import Book, PaperAccount, books_from
from engine.core.clock import NS_PER_S, from_ns
from engine.core.rules import IST
from engine.data.reference.instrument_master import Instrument, InstrumentMaster, Kind
from engine.shadow.models import ShadowLedgerOutcome, ShadowSignal, ShadowSignalStatus

if TYPE_CHECKING:
    from engine.shadow.live import ShadowLiveRunner

log = logging.getLogger(__name__)


class ReplayControl(Protocol):
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def set_speed(self, speed: float) -> None: ...
    def progress(self) -> dict[str, Any]: ...


def instrument_label(ins: Instrument) -> str:
    if ins.kind is Kind.INDEX:
        return ins.description or ins.underlying
    exp = ins.expiry.strftime("%d%b").upper() if ins.expiry else ""
    if ins.kind is Kind.FUTURE:
        return f"{ins.underlying} FUT {exp}"
    if ins.kind.is_option:
        return f"{ins.underlying} {ins.strike:g} {ins.kind.value} {exp}"
    if ins.kind is Kind.EQUITY:  # cash stock: the trading symbol, not the broker's ISIN key
        return f"{ins.underlying or ins.description or ins.symbol} EQ"
    return ins.symbol


def _levels(levels: Any, n: int = 30) -> list[list[float]]:
    """[price, quantity, orders] per level (orders 0 when the feed does not give them:
    Upstox ``full_d30`` sends None)."""
    return [
        [float(lv[0]), float(lv[1]), float(lv[2] or 0) if len(lv) > 2 else 0.0]
        for lv in list(levels)[:n]
    ]


def _sig_row(sig: ShadowSignal) -> dict[str, Any]:
    return {
        "id": sig.signal_id,
        "score": round(sig.confidence_score, 1),
        "grade": sig.grade.value,
        "direction": sig.direction,
        "wave": sig.wave.value,
        "status": sig.status.value,
        "entry": sig.entry_price,
        "sl": sig.stop_loss,
        "t1": sig.target_1,
        "t2": sig.target_2,
        "rr1": sig.rr_t1,
        "obi": round(sig.obi_l5, 2),
        "rer": round(sig.real_execution_ratio, 2),
        "air": round(sig.air_pocket_ratio, 2),
        "cvd_z": round(sig.cvd_z_score, 2),
        "oi_quadrant": sig.futures_oi_quadrant,
        "at": sig.created_at.astimezone(IST).strftime("%H:%M:%S"),
        "breakdown": {
            "location": round(sig.breakdown.structural_location_pts, 1),
            "flow": round(sig.breakdown.order_flow_anti_spoof_pts, 1),
            "book": round(sig.breakdown.obi_air_pocket_pts, 1),
            "derivatives": round(sig.breakdown.cross_asset_derivative_pts, 1),
        },
    }


def _outcome_row(o: ShadowLedgerOutcome, label: str) -> dict[str, Any]:
    return {
        "id": o.signal_id,
        "symbol": o.symbol,
        "label": label,
        "direction": o.direction,
        "wave": o.wave.value,
        "score": round(o.confidence_score, 1),
        "status": o.status.value,
        "entry": o.entry_price,
        "exit": o.exit_price,
        "qty": o.qty,
        "gross": o.gross_pnl_inr,
        "charges": o.charges_inr,
        "net": o.net_pnl_inr,
        "net_2x": o.net_pnl_2x_slippage_inr,
        "r": o.r_multiple_net,
        "tradable": o.tradable,
        "thin": o.thin_book,
        "opened": o.opened_at.astimezone(IST).strftime("%H:%M:%S"),
        "closed": o.closed_at.astimezone(IST).strftime("%H:%M:%S"),
        "reason": o.exit_reason,
    }


class CockpitCore:
    def __init__(
        self,
        cfg: CockpitConfig,
        *,
        source: str,
        bot: PaperAccount,
        manual: PaperAccount,
        analytics: LiveAnalytics | None,
        publisher: EnginePublisher | None,
        replay: ReplayControl | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.source = source
        self.bot = bot
        self.manual = manual
        self.analytics = analytics
        self.publisher = publisher
        self.replay = replay
        self.on_stop = on_stop
        self.runner: ShadowLiveRunner | None = None
        self.events: deque[dict[str, str]] = deque(maxlen=cfg.events_kept)
        self._seen_triggered: set[str] = set()
        self._bot_links: dict[str, str] = {}  # signal_id -> option the bot bought
        self._outcomes_seen = 0
        self._next_publish = 0.0  # wall clock (monotonic s): replays may sit on one event time
        self._next_analytics = 0
        self._urgent = False
        self.calendar_events: list[Any] = []  # CalendarEvent rows (set by build_cockpit)
        self.study: Any = None  # SignalStudy (set by build_cockpit)
        self._directional_today: dict[str, dict[str, Any]] = {}  # underlying -> idea state
        self.setups: Any = None  # SetupTracker (set by build_cockpit)
        self.spread: Any = None  # SpreadAccount for SPEC 5.1 (set by build_cockpit)
        self.trading_calendar: Any = None
        self.condor: Any = None  # CondorStrategy (made in attach, needs the runner)
        self._next_condor_eval = 0.0  # monotonic s
        self._score_hist: dict[str, deque[tuple[float, float]]] = {}
        self._oi_first: dict[str, tuple[int, str]] = {}
        self._search_index: list[tuple[str, str, str, int]] | None = None
        self.user_pins: list[str] = []
        self.score_acct: PaperAccount | None = None  # original A+ score bot (own account)
        self.gamma_acct: PaperAccount | None = None  # SPEC 5.10
        self.debit_acct: Any = None  # SpreadAccount for debit spreads
        self.debit: Any = None  # DebitSpreadStrategy (made in attach)
        self.gamma: Any = None  # GammaBreakoutStrategy (made in attach)
        from engine.cockpit.tape import IndexTape, OptionTape
        from engine.cockpit.zones import ZoneTracker

        self.plays: list[Any] = []  # playbook strategies (build_cockpit)
        self.tapes: dict[str, IndexTape] = {}
        self._event_seen: dict[tuple[str, str], datetime] = {}
        self.otapes: dict[str, OptionTape] = {}
        self.otape_watch: set[str] = set()
        self._chain_rev_seen = -1
        self._iv_hist: dict[str, deque[tuple[float, float]]] = {}

        self.zones = ZoneTracker(cfg.zones)
        self.feed_frozen = False  # no fresh quotes in market hours: entries and exits wait
        self.feed_age_s: float | None = None

    # ------------------------------------------------------------------ wiring

    def attach(self, runner: ShadowLiveRunner) -> None:
        self.runner = runner
        if self.analytics is not None:
            self.analytics.start_chain_worker()
        if self.spread is not None:
            from engine.cockpit.condor import CondorStrategy, previous_trading_day

            cal = self.trading_calendar

            def is_trading_day(d: Any) -> bool:
                try:
                    return bool(cal.is_trading_day(d))
                except Exception:
                    return bool(d.weekday() < 5)

            self.condor = CondorStrategy(
                cfg=self.cfg.condor,
                account=self.spread,
                lot_size=lambda sym: self.master.get(sym).lot_size if sym in self.master else 1,
                option_symbol=self.option_symbol,
                pin=lambda syms: self._pin("condor", syms),
                trading_day_before=lambda d: previous_trading_day(
                    d, is_trading_day if cal is not None else None
                ),
                context=lambda: self.market_context(
                    self.cfg.condor.underlying, from_ns(runner.clock.now_ns())
                ),
            )
            if self.spread.positions:  # held overnight: bring their legs back into the feed
                self._pin("condor", self.condor.legs_in_use())
        from engine.cockpit.strategies import DebitSpreadStrategy, GammaBreakoutStrategy

        if self.debit_acct is not None:
            self.debit = DebitSpreadStrategy(
                cfg=self.cfg.debit,
                account=self.debit_acct,
                strikes_of=self._strike_grid,
                option_symbol=self.option_symbol,
                lot_size=lambda sym: self.master.get(sym).lot_size if sym in self.master else 1,
                pin=lambda syms: self._pin("debit", syms),
                context=lambda und: self.market_context(und, from_ns(runner.clock.now_ns())),
            )
        if self.gamma_acct is not None:
            self.gamma = GammaBreakoutStrategy(
                cfg=self.cfg.gamma,
                account=self.gamma_acct,
                expiry_today=self._expiry_today,
                atm_option=lambda und, kind: self.atm_option(
                    und, Kind.CALL if kind == "CE" else Kind.PUT
                ),
                chain_rows=lambda und: list(
                    ((self.analytics.chain.get(und) or {}) if self.analytics else {}).get("rows")
                    or []
                ),
                context=lambda und: self.market_context(und, from_ns(runner.clock.now_ns())),
            )
        self.event("system", f"{self.source} engine started ({runner.adapter.name})")

    def _pin(self, owner: str, symbols: list[str]) -> list[str]:
        if self.runner is None or not hasattr(self.runner, "pin"):
            return list(symbols)  # no live feed to add to (tests): nothing is refused
        return list(self.runner.pin(owner, symbols))

    def _strike_grid(self, und: str, kind: str) -> tuple[Any, list[float]] | None:
        """Nearest expiry with options in the live feed, and all its listed strikes."""
        assert self.runner is not None
        want = Kind.CALL if kind == "CE" else Kind.PUT
        exps: list[date] = sorted(
            {
                e
                for s in self.runner.subscribed
                if s in self.master
                and self.master.get(s).underlying == und
                and self.master.get(s).kind is want
                and (e := self.master.get(s).expiry) is not None
            }
        )
        if not exps:
            return None
        strikes = sorted(
            float(o.strike)
            for o in self.master.options(und, exps[0])
            if o.kind is want and o.strike is not None
        )
        return (exps[0], strikes) if strikes else None

    def _expiry_today(self, und: str, day: Any) -> bool:
        assert self.runner is not None
        return any(
            self.master.get(s).expiry == day
            for s in getattr(self.runner, "subscribed", ())
            if s in self.master
            and self.master.get(s).underlying == und
            and self.master.get(s).kind.is_option
        )

    def _plays_step(self, now: datetime) -> None:
        if not self.plays:
            return
        from engine.cockpit.plays_base import Market

        m = Market(self, now)
        for p in self.plays:
            try:
                for text in p.step(now, m):
                    if text:
                        self.event(p.key, str(text), now)
            except Exception as exc:  # one strategy's bug must not stop the others
                self.event("system", f"{p.key}: {type(exc).__name__}: {exc}", now)

    def warm_start(self, store: Any) -> None:
        """After a mid-session restart, rebuild today's index tapes from the recorder's quotes
        (index spot and front future since the open), so the 30-min opening range, VWAP, CVD,
        ATR and relative volume start at 09:15 rather than at the restart. Option-side tape
        items (ATM straddle, IVs, per-strike OI) still start from the restart."""
        if self.runner is None or store is None or not self.plays:
            return
        from types import SimpleNamespace

        from engine.cockpit.tape import IndexTape

        r = self.runner
        now = datetime.fromtimestamp(r.clock.now_ns() / 1e9, IST)
        open_dt = r.session.open_dt
        if now <= open_dt + timedelta(minutes=1):
            return
        for und, spot_sym in r.spot_symbol.items():
            futs = self.master.futures(und, now.date())[:1]
            fut_sym = futs[0].symbol if futs else None
            syms = [spot_sym] + ([fut_sym] if fut_sym else [])
            rows = store.quotes(
                now.date(),
                as_of=now,
                start=open_dt,
                end=now,
                symbols=syms,
                columns="ts, symbol, ltp, volume, oi, bid, ask",
            ).to_pylist()
            t = IndexTape(und)
            spot = 0.0
            for q in rows:
                px = q.get("ltp")
                if not px or px <= 0:
                    continue
                ts = q["ts"]
                if q["symbol"] == spot_sym:
                    spot = float(px)
                    t.on_sample(ts, spot=spot)
                elif spot:
                    fut = SimpleNamespace(
                        symbol=fut_sym,
                        ltp=float(px),
                        cum_volume=float(q.get("volume") or 0),
                        oi_now=int(q.get("oi") or 0),
                        bids=((float(q.get("bid") or 0), 0, 0),),
                        asks=((float(q.get("ask") or 0), 0, 0),),
                    )
                    t.on_sample(ts, spot=spot, fut=fut)
            if t.bars:
                self.tapes[und] = t
                self.event(
                    "system",
                    f"{und} tape rebuilt from the recording: {len(t.bars)} minute bars since "
                    f"{t.bars[0].start:%H:%M}",
                    now,
                )

    def _tape_samples(self, now: datetime) -> None:
        """Feed the minute tapes (index, front future, ATM straddle, IVs, skew, chain OI)."""
        if self.runner is None or not self.plays:
            return
        from engine.cockpit.tape import IndexTape, OptionTape

        a = self.analytics
        states = self.runner.scanner.states
        for und in getattr(self.runner, "spot_symbol", {}):
            spot = self.runner.spots().get(und, 0.0)
            if not spot:
                continue
            t = self.tapes.setdefault(und, IndexTape(und))
            ce = self.atm_option(und, Kind.CALL)
            pe = self.atm_option(und, Kind.PUT)
            straddle = cum = None
            ivs: dict[str, float] = {}
            if ce and pe and ce in states and pe in states:
                cs, ps = states[ce], states[pe]
                if cs.ltp and ps.ltp:
                    straddle = float(cs.ltp) + float(ps.ltp)
                    cum = float(cs.cum_volume or 0) + float(ps.cum_volume or 0)
                    if ce in self.master:
                        t.atm_strike = float(self.master.get(ce).strike or 0) or None
                if a is not None:
                    ivs["ce_atm"] = float((a.detail.get(ce) or {}).get("iv") or 0)
                    ivs["pe_atm"] = float((a.detail.get(pe) or {}).get("iv") or 0)
            if a is not None:
                ivs["atm"] = float(a.strip.get(und, {}).get("atm_iv_pct") or 0)
                rows = (a.chain.get(und) or {}).get("rows") or []
                c25 = min(
                    rows,
                    key=lambda r: abs(((r.get("ce") or {}).get("delta") or 9) - 0.25),
                    default=None,
                )
                p25 = min(
                    rows,
                    key=lambda r: abs(((r.get("pe") or {}).get("delta") or 9) + 0.25),
                    default=None,
                )
                civ = ((c25 or {}).get("ce") or {}).get("iv")
                piv = ((p25 or {}).get("pe") or {}).get("iv")
                if civ and piv:
                    ivs["skew25"] = float(piv) - float(civ)
                tables = a.chains.get(und) or {}
                if len(tables) >= 2:
                    nxt = tables[sorted(tables)[1]]
                    nrows = nxt.get("rows") or []
                    f = float(nxt.get("forward") or 0)
                    if nrows and f:
                        r0 = min(nrows, key=lambda r: abs(float(r["k"]) - f))
                        v = [
                            x
                            for x in (
                                (r0.get("ce") or {}).get("iv"),
                                (r0.get("pe") or {}).get("iv"),
                            )
                            if x
                        ]
                        if v:
                            ivs["next_atm"] = sum(v) / len(v)
            t.on_sample(now, spot=spot, fut=self._front_future(und), straddle=straddle,
                        straddle_cum_vol=cum, ivs=ivs)  # fmt: skip
            for sym in (ce, pe):
                if sym:
                    self.otape_watch.add(sym)
        if a is not None and getattr(a, "chain_rev", 0) != self._chain_rev_seen:
            self._chain_rev_seen = a.chain_rev
            for und, ch in a.chain.items():
                if ch and ch.get("rows"):
                    self.tapes.setdefault(und, IndexTape(und)).on_chain(now, ch["rows"])
        for sym in list(self.otape_watch):
            st = states.get(sym)
            if st is not None and st.ltp:
                self.otapes.setdefault(sym, OptionTape()).on_sample(
                    now, float(st.ltp), float(st.cum_volume or 0)
                )
            if a is not None and sym in a.detail and a.detail[sym].get("iv"):
                h = self._iv_hist.setdefault(sym, deque(maxlen=600))
                h.append((now.timestamp(), float(a.detail[sym]["iv"])))

    def stop_confirm(self, sym: str, bid: float, stop: float) -> bool:
        """breaking_retail.md: confirm a long-option stop with the model price (Black-76 on
        the forward and the option's 5-minute average IV). True when the model also says the
        stop is reached (within 4%), or when the model can not be computed (fail safe)."""
        from engine.analytics.expiry_clock import expiry_clock
        from engine.cockpit.gates import model_price

        a = self.analytics
        if a is None or sym not in self.master or self.runner is None:
            return True
        ins = self.master.get(sym)
        h = [v for t, v in self._iv_hist.get(sym, ()) if t >= self._now_ts() - 300]
        strip = a.strip.get(ins.underlying, {})
        f = strip.get("forward") or strip.get("spot")
        if not h or not f or ins.expiry is None or ins.strike is None:
            return True
        try:
            now = from_ns(self.runner.clock.now_ns())
            clk = expiry_clock(
                now, ins.expiry, book=a.book, calendar=a.calendar, exchange=ins.exchange
            )
            r, _ = a._rate(now)
            theo = model_price(float(f), float(ins.strike), clk.t_var, sum(h) / len(h) / 100,
                               is_call=ins.kind is Kind.CALL, t_cal=clk.t_cal, r=r)  # fmt: skip
        except Exception:
            return True
        if theo is None:
            return True
        ok = theo <= stop * 1.04
        if not ok:
            self.event("system", f"stop on {self.label(sym)} held: bid {bid:.2f} but model "
                                 f"{theo:.2f} (quote air-pocket?)")  # fmt: skip
        return ok

    def _now_ts(self) -> float:
        return from_ns(self.runner.clock.now_ns()).timestamp() if self.runner else time.time()

    def _zone_samples(self, now: datetime) -> None:
        if not self.cfg.zones.enabled or self.runner is None:
            return
        for und in getattr(self.runner, "spot_symbol", {}):
            st = self._front_future(und)
            if st is None or not st.ltp:
                continue
            b = st.bids[0] if st.bids else (0.0, 0, 0)
            a = st.asks[0] if st.asks else (0.0, 0, 0)
            self.zones.on_sample(
                und, now, ltp=st.ltp, cum_volume=float(st.cum_volume or 0),
                bid=b[0], ask=a[0], bid_qty=float(b[1]), ask_qty=float(a[1]),
            )  # fmt: skip

    def _front_future(self, und: str) -> Any:
        assert self.runner is not None
        best = None
        for s in self.runner.subscribed:
            if s not in self.master:
                continue
            o = self.master.get(s)
            if (
                o.underlying == und
                and o.kind is Kind.FUTURE
                and o.expiry is not None
                and (best is None or o.expiry < best[0])
            ):
                best = (o.expiry, s)
        return self.runner.scanner.states.get(best[1]) if best else None

    def option_symbol(self, und: str, expiry: Any, strike: float, kind: str) -> str | None:
        want = Kind.CALL if kind == "CE" else Kind.PUT
        for o in self.master.options(und, expiry):
            if o.kind is want and o.strike is not None and abs(float(o.strike) - strike) < 1e-6:
                return o.symbol
        return None

    @property
    def master(self) -> InstrumentMaster:
        assert self.runner is not None
        return self.runner.master

    def label(self, symbol: str) -> str:
        return instrument_label(self.master.get(symbol)) if symbol in self.master else symbol

    def event(self, kind: str, text: str, now: datetime | None = None) -> None:
        t = now or (from_ns(self.runner.clock.now_ns()) if self.runner else datetime.now(IST))
        if not text:
            return
        # The same line from the same source within 30 s is a repeat, not news: one is enough.
        last = self._event_seen.get((kind, text))
        if kind != "system" and last is not None and 0 <= (t - last).total_seconds() < 30:
            return
        self._event_seen[(kind, text)] = t
        if len(self._event_seen) > 2000:
            self._event_seen = {
                k: v for k, v in self._event_seen.items() if (t - v).total_seconds() < 30
            }
        self.events.appendleft(
            {"t": t.astimezone(IST).strftime("%H:%M:%S"), "kind": kind, "text": text}
        )
        self._urgent = True

    def books(self) -> dict[str, Book]:
        assert self.runner is not None
        return books_from(self.runner.scanner.states)

    # ------------------------------------------------------------------ engine hooks

    def on_episode(self, sig: ShadowSignal) -> None:
        """A confirmed order-flow episode: log it, add it to the signal study, and let the
        directional strategy (SPEC 5.2) use it as an entry confirmation."""
        view = self.signal_view(sig)
        if view is None:
            self.event("signal", f"{sig.grade.value} {sig.direction} {self.label(sig.symbol)} "
                                 f"score {sig.confidence_score:.0f}", sig.created_at)  # fmt: skip
            return
        und, bullish, vehicle = view
        why = self.reading_excluded(sig, und)
        if why:
            self.event(
                "signal",
                f"{sig.grade.value} {self.label(sig.symbol)} not used: {why}",
                sig.created_at,
            )
            return
        self.event(
            "signal",
            f"{sig.grade.value} {self.label(sig.symbol)}: {und} {'UP' if bullish else 'DOWN'} "
            f"(score {sig.confidence_score:.0f}, {sig.wave.value.replace('_', ' ').lower()})",
            sig.created_at,
        )
        books = self.books()
        if self.study is not None and self.runner is not None:
            spot = self.runner.spots().get(und, 0.0)
            vb = books.get(vehicle) if vehicle else None
            self.study.add(
                signal_id=sig.signal_id,
                now=sig.created_at,
                symbol=sig.symbol,
                label=self.label(sig.symbol),
                underlying=und,
                bullish=bullish,
                score=sig.confidence_score,
                wave=sig.wave.value,
                spot=spot,
                vehicle=vehicle or "",
                vehicle_label=self.label(vehicle) if vehicle else "",
                lot=self.master.get(vehicle).lot_size if vehicle and vehicle in self.master else 1,
                vehicle_book=vb,
                context=self.market_context(und, sig.created_at),
            )
        if self.setups is not None and self.runner is not None:
            spot = self.runner.spots().get(und, 0.0)
            st = self.setups.add_reading(
                underlying=und,
                bullish=bullish,
                now=sig.created_at,
                spot=spot,
                label=self.label(sig.symbol),
                score=sig.confidence_score,
                wave=sig.wave.value,
            )
            if st is not None and len(st.readings) == 1:
                self.event(
                    "setup",
                    f"{und} {st.direction} setup forming at {spot:,.2f}: waiting for the index "
                    f"to follow through {self.cfg.setups.confirm_move_pct:g}% and hold "
                    f"{self.cfg.setups.hold_s:.0f} s",
                    sig.created_at,
                )

    def reading_excluded(self, sig: ShadowSignal, und: str) -> str | None:
        """Why a confirmed reading does not count (None if it does): too late in the day (or
        in an expiring index's last stretch), or an option with no convexity left."""
        c = self.cfg
        hhmm = sig.created_at.astimezone(IST).strftime("%H:%M")
        if hhmm >= c.readings_until:
            return f"after {c.readings_until}"
        expiring = self.runner is not None and self._expiry_today(
            und, sig.created_at.astimezone(IST).date()
        )
        if expiring and hhmm >= c.readings_until_expiry_day:
            return f"{und} expires today: readings stop at {c.readings_until_expiry_day}"
        g = self.analytics.detail.get(sig.symbol) if self.analytics is not None else None
        if g and g.get("settling"):
            return "expiring now: the price is settlement-locked"
        if g:
            d = abs(float(g.get("delta") or 0.0))
            if not c.reading_min_abs_delta <= d <= c.reading_max_abs_delta:
                return (
                    f"|delta| {d:.2f} outside {c.reading_min_abs_delta}-{c.reading_max_abs_delta}"
                )
            tv, intr = float(g.get("time_value") or 0.0), float(g.get("intrinsic") or 0.0)
            if tv + intr > 0 and tv / (tv + intr) * 100 < c.reading_min_time_value_pct:
                return "almost no time value left (settlement-locked)"
        return None

    def _on_setup_confirmed(self, st: Any, now: datetime) -> None:
        """A confirmed setup: event, signal study, and the 5.2 directional entry."""
        self.event("setup", f"{st.underlying} {st.direction} setup CONFIRMED: {st.why}", now)
        vehicle = self.atm_option(st.underlying, Kind.CALL if st.bullish else Kind.PUT)
        books = self.books()
        if self.study is not None and self.runner is not None:
            self.study.add(
                signal_id=st.id,
                now=now,
                symbol=st.underlying,
                label=f"setup ({len(st.readings)} readings)",
                underlying=st.underlying,
                bullish=st.bullish,
                score=st.best_score,
                wave="SETUP_CONFIRMED",
                spot=self.runner.spots().get(st.underlying, 0.0),
                vehicle=vehicle or "",
                vehicle_label=self.label(vehicle) if vehicle else "",
                lot=self.master.get(vehicle).lot_size if vehicle and vehicle in self.master else 1,
                vehicle_book=books.get(vehicle) if vehicle else None,
                context=self.market_context(st.underlying, now),
            )
        self._directional_entry(st.id, st.best_score, st.underlying, st.bullish, vehicle, now)
        if self.debit is not None and self.runner is not None:
            text = self.debit.on_confirmed(
                und=st.underlying,
                bullish=st.bullish,
                score=st.best_score,
                ref=st.id,
                spot=self.runner.spots().get(st.underlying, 0.0),
                now=now,
            )
            if text:
                self.event("debit", text, now)
        for p in self.plays:
            if hasattr(p, "on_setup") and p.enabled:
                p.on_setup(st.underlying, st.bullish, st.id)

    def signal_view(self, sig: ShadowSignal) -> tuple[str, bool, str | None] | None:
        """(underlying, bullish?, ATM long option expressing it) for a signal on any contract."""
        if self.runner is None or sig.symbol not in self.master:
            return None
        ins = self.master.get(sig.symbol)
        if ins.kind is Kind.CALL:
            bullish = sig.direction == "LONG"
        elif ins.kind is Kind.PUT:
            bullish = sig.direction == "SHORT"
        else:
            bullish = sig.direction == "LONG"
        return (
            ins.underlying,
            bullish,
            self.atm_option(ins.underlying, Kind.CALL if bullish else Kind.PUT),
        )

    def atm_option(self, underlying: str, want: Kind) -> str | None:
        """Nearest-expiry option of ``want`` type closest to spot, among contracts with a book."""
        assert self.runner is not None
        ref = self.runner.spots().get(underlying, 0.0)
        if ref <= 0:
            return None
        cands: list[tuple[float, str]] = []
        for sym, st in self.runner.scanner.states.items():
            if sym not in self.master or not st.asks:
                continue
            o = self.master.get(sym)
            if o.underlying == underlying and o.kind is want and o.strike is not None:
                expiry_rank = o.expiry.toordinal() if o.expiry else 0
                cands.append((expiry_rank * 1e7 + abs(o.strike - ref), sym))
        return min(cands)[1] if cands else None

    def _directional_entry(
        self,
        ref_id: str,
        score: float,
        und: str,
        bullish: bool,
        vehicle: str | None,
        now: datetime,
    ) -> None:
        """SPEC 5.2: one directional idea per day per index, entered on a confirmed setup
        (order-flow readings plus the index following through); exits are the strategy's own
        (premium stop, target, trailing stop, square-off). Book changes do not close it."""
        d = self.cfg.directional
        if not d.enabled or und not in d.underlyings or score < d.min_score:
            return
        hhmm = now.astimezone(IST).strftime("%H:%M")
        if not d.first_entry <= hhmm <= d.last_entry:
            return
        state = self._directional_today.setdefault(und, {"bullish": None, "entries": 0})
        if state["entries"] >= d.max_entries_per_day:
            return
        if state["bullish"] is not None and state["bullish"] != bullish:
            return  # one idea a day: no flipping direction
        if any(
            self.master.get(s).underlying == und for s in self.bot.positions if s in self.master
        ):
            return  # already holding this idea
        if vehicle is None:
            self.event("bot", f"directional: no ATM option with a book for {und}", now)
            return
        book = self.books().get(vehicle)
        if book is None:
            return
        ok, text = self.bot.open_long_option(
            vehicle,
            book,
            now=now,
            lots=1,
            stop_pct=d.stop_pct,
            target_pct=d.target_pct,
            trail=d.trail,
            signal_id=ref_id,
            context=self.market_context(und, now),
            reason=f"5.2 {und} {'UP' if bullish else 'DOWN'} confirmed setup "
            f"(best reading {score:.0f})",
        )
        if ok:
            state["bullish"] = bullish
            state["entries"] += 1
        self.event("bot", text.replace(vehicle, self.label(vehicle)), now)

    def on_step(self, now_ns: int) -> None:
        if self.runner is None:
            return
        now = from_ns(now_ns)
        self._zone_samples(now)
        self._tape_samples(now)
        self._drain_commands(now)
        self._follow_track_a(now)
        if now_ns >= self._next_analytics and self.analytics is not None:
            self.analytics.update(now, self.runner.scanner.states)
            self._next_analytics = now_ns + int(self.cfg.analytics_interval_s * NS_PER_S)
        wall = time.monotonic()
        if wall >= self._next_publish or self._urgent:
            books = self.books()
            spots = self.runner.spots()
            self._check_feed(now_ns, now)
            if not self.feed_frozen:
                for sacct in self.spread_accounts():
                    for text in sacct.process_executions(books, now):
                        self.event(sacct.name, text, now)
                for acct in self.long_accounts():
                    for text in acct.check_exits(books, now):
                        self.event(acct.name, text, now)
                if self.debit is not None:
                    for text in self.debit.step(now, books):
                        self.event("debit", text.replace("\n", " "), now)
                if self.gamma is not None:
                    for und, spot in spots.items():
                        fut = self._front_future(und)
                        self.gamma.on_tick(
                            now, und, spot, fut.ltp if fut else 0.0, fut.cum_volume if fut else 0.0
                        )
                        text = self.gamma.check(now, und, books)
                        if text:
                            self.event("gamma", text, now)
                if self.setups is not None:
                    if now >= self.runner.session.close_dt:
                        for text in self.setups.close_all(now, "market closed"):
                            self.event("setup", text, now)
                    else:
                        for st in self.setups.update(now, spots):
                            self._on_setup_confirmed(st, now)
                self._condor_step(now, books, spots, wall)
                self._plays_step(now)
            if self.study is not None:
                self.study.update(now, spots, books)
            self.publish(now)
            self._next_publish = wall + self.cfg.publish_interval_ms / 1000.0
            self._urgent = False

    def _check_feed(self, now_ns: int, now: datetime) -> None:
        """Approved rule (execution.disconnect_kill_after_s): with no fresh quote in market
        hours, freeze new entries and pause exits until quotes return; never act on stale
        prices. Outside market hours the feed is quiet by design and nothing is frozen."""
        assert self.runner is not None
        last = max((st.last_recv_ns for st in self.runner.scanner.states.values()), default=0)
        self.feed_age_s = (now_ns - last) / NS_PER_S if last else None
        sess = self.runner.session
        in_hours = sess.open_dt <= now < sess.close_dt
        stale = in_hours and (self.feed_age_s is None or self.feed_age_s > self.cfg.feed_stale_s)
        if stale and not self.feed_frozen:
            self.event(
                "system",
                f"FEED STALE: no quote for {self.feed_age_s or 0:.0f} s; new paper entries frozen "
                "and exits paused until quotes return",
                now,
            )
        elif self.feed_frozen and not stale:
            self.event("system", "feed back: entries and exits resume", now)
        self.feed_frozen = stale

    def market_context(self, und: str, now: datetime) -> dict[str, Any]:
        """The market state at a decision, stored with the trade so Research can ask later
        which conditions the results came from (point in time: only what was known now)."""
        ctx: dict[str, Any] = {"underlying": und, "hour": now.astimezone(IST).strftime("%H:00")}
        if self.runner is None:
            return ctx
        try:
            r = self.runner
            sess = r.session
            ctx["min_from_open"] = round((now - sess.open_dt).total_seconds() / 60)
            strip = self.analytics.strip.get(und, {}) if self.analytics is not None else {}
            chain = self.analytics.chain.get(und, {}) if self.analytics is not None else {}
            spot = strip.get("spot") or r.spots().get(und)
            ctx.update(
                {
                    "spot": spot,
                    "atm_iv": strip.get("atm_iv_pct"),
                    "ivp": strip.get("iv_percentile"),
                    "vix": strip.get("vix"),
                    "fut_basis": strip.get("fut_basis"),
                    "pcr": (chain or {}).get("pcr_oi"),
                    "gex": ((chain or {}).get("gex") or {}).get("regime"),
                    "skew": ((chain or {}).get("smile") or {}).get("skew_pts"),
                }
            )
            if spot:
                for key, lvl in (
                    ("call_wall", (chain or {}).get("call_wall")),
                    ("put_wall", (chain or {}).get("put_wall")),
                    ("max_pain", (chain or {}).get("max_pain")),
                ):
                    if lvl:
                        ctx[f"{key}_dist_pct"] = round((lvl / spot - 1) * 100, 2)
                st = r.scanner.states.get(r.spot_symbol.get(und, ""))
                if st is not None:
                    if st.prev_close:
                        ctx["day_chg_pct"] = round((spot / st.prev_close - 1) * 100, 2)
                    if st.pdh and st.pdl:
                        ctx["vs_prev_day"] = (
                            "above PDH"
                            if spot > st.pdh
                            else "below PDL"
                            if spot < st.pdl
                            else "inside"
                        )
                hi, lo = (
                    r.or_hi.get(r.spot_symbol.get(und, "")),
                    r.or_lo.get(r.spot_symbol.get(und, "")),
                )
                if hi and lo and r.or_frozen:
                    ctx["vs_opening_range"] = (
                        "above" if spot > hi else "below" if spot < lo else "inside"
                    )
            d = getattr(self.analytics, "dispersion", None) if self.analytics is not None else None
            if d and und == getattr(self.analytics, "dispersion_index", ""):
                ctx["breadth_pct"] = d.get("weighted_breadth_pct")
                ctx["corr"] = d.get("realized_corr")
            t = self._trend(und, now)
            ctx["setups_up"], ctx["setups_down"] = t.get("up", 0), t.get("down", 0)
            ctx.update(self._regime(und, now))
            fut = self._front_future(und)
            if fut is not None and fut.ltp:
                zv = self.zones.zones(und, fut.ltp)
                ctx["zone"] = (
                    "floor below (sellers trapped)" if zv["floors"] and not zv["ceilings"]
                    else "ceiling above (buyers trapped)" if zv["ceilings"] and not zv["floors"]
                    else "both" if zv["floors"] else "none"
                )  # fmt: skip
            if self.calendar_events:
                from engine.data.events.calendar_file import blackout_status

                ev = blackout_status(self.calendar_events, now)
                ctx["event_window"] = bool(ev.in_macro_blackout)
        except Exception as exc:  # context is for research; never block a trade on it
            ctx["context_error"] = type(exc).__name__
        return ctx

    def _regime(self, und: str, now: datetime) -> dict[str, Any]:
        """Day-regime features (12 Nov review): expiry day, opening gap, how trending the day
        is so far (efficiency = |close - open| / (high - low)), range and VIX change."""
        assert self.runner is not None
        r = self.runner
        out: dict[str, Any] = {"expiry_day": self._expiry_today(und, now.astimezone(IST).date())}
        st = r.scanner.states.get(r.spot_symbol.get(und, ""))
        if st is not None and st.ltp and st.session_open:
            if st.prev_close:
                out["gap_pct"] = round((st.session_open / st.prev_close - 1) * 100, 2)
            rng = st.session_high - st.session_low
            if rng > 0:
                out["efficiency"] = round(abs(st.ltp - st.session_open) / rng, 2)
                out["range_pct"] = round(rng / st.session_open * 100, 2)
        for sym, vst in r.scanner.states.items():
            if "VIX" in sym.upper() and vst.ltp and vst.session_open:
                out["vix_chg"] = round(vst.ltp - vst.session_open, 2)
                break
        return out

    def _trend(self, und: str, now: datetime) -> dict[str, Any]:
        if self.setups is None:
            return {"up": 0, "down": 0, "forming": []}
        return dict(self.setups.trend(und, now, self.cfg.condor.veto_lookback_min))

    def _condor_step(
        self, now: datetime, books: dict[str, Book], spots: dict[str, float], wall: float
    ) -> None:
        cs = self.condor
        if cs is None:
            return
        und = self.cfg.condor.underlying
        trend = self._trend(und, now)
        for text in cs.check_exits(now, books, spots.get(und), trend):
            self.event("condor", text, now)
        if cs.plan is not None:
            text = cs.try_execute(now, books)
            if text:
                self.event("condor", text, now)
        if not self.cfg.condor.enabled or wall < self._next_condor_eval:
            return
        self._next_condor_eval = wall + 60.0
        strip = self.analytics.strip.get(und, {}) if self.analytics is not None else {}
        chain = self.analytics.chain.get(und) if self.analytics is not None else None
        scale: float | None = None
        names: list[str] = []
        if self.calendar_events:
            from engine.data.events.calendar_file import blackout_status

            ev = blackout_status(self.calendar_events, now)
            scale = float(ev.short_vega_scale_factor)
            names = list(ev.active_blackout_events)
        before = cs.decision
        cs.evaluate(
            now,
            strip=strip,
            chain=chain,
            trend=trend,
            event_scale=scale,
            event_names=names,
            events=self.calendar_events,
        )
        if cs.decision != before and (cs.plan is not None or "managing exits" not in cs.decision):
            self.event("condor", cs.decision, now)

    def spread_accounts(self) -> list[Any]:
        base = [a for a in (self.spread, self.debit_acct) if a is not None]
        return base + [p.account for p in self.plays if hasattr(p, "plan")]

    def long_accounts(self) -> list[PaperAccount]:
        base = [a for a in (self.bot, self.manual, self.score_acct, self.gamma_acct) if a]
        return base + [p.account for p in self.plays if hasattr(p, "managed")]

    def finish(self, now: datetime) -> None:
        books = self.books() if self.runner is not None else {}
        for acct in self.long_accounts():
            for text in acct.flatten(books, now, "session end"):
                self.event(acct.name, text, now)
        if self.debit_acct is not None:  # debit spreads are intraday
            for pos in list(self.debit_acct.positions):
                _, text = self.debit_acct.close(pos, books, now, "session end")
                self.event("debit", text, now)
        for p in self.plays:
            if hasattr(p, "record_close"):
                p.record_close(now)
            if hasattr(p, "trades") and not getattr(p, "overnight", False):
                for pos in list(p.account.positions):
                    _, text = p.account.close(pos, books, now, "session end")
                    self.event(p.key, text, now)
        if self.runner is not None:
            self._follow_track_a(now)
            if self.study is not None:
                self.study.finish(now, self.runner.spots(), books)
        self.event("system", f"{self.source} engine finished", now)
        self.publish(now)

    # ------------------------------------------------------------------ Track A -> Track B

    def _follow_track_a(self, now: datetime) -> None:
        assert self.runner is not None
        sc = self.runner.scanner
        for sig in list(sc.active_signals.values()):
            if sig.status is not ShadowSignalStatus.ACTIVE or sig.signal_id in self._seen_triggered:
                continue
            self._seen_triggered.add(sig.signal_id)
            self.event(
                "micro",
                f"micro ledger (legacy scalp exits): entered {sig.direction} "
                f"{self.label(sig.symbol)} (score {sig.confidence_score:.0f})",
                now,
            )
            if self.cfg.bot.enabled and sig.confidence_score >= self.cfg.bot.min_score:
                self._bot_enter(sig, now)
        new = sc.closed_outcomes[self._outcomes_seen :]
        self._outcomes_seen = len(sc.closed_outcomes)
        for o in new:
            self.event(
                "micro",
                f"micro ledger (legacy): closed {self.label(o.symbol)} {o.status.value}: "
                f"net ₹{o.net_pnl_inr:,.0f}",
                now,
            )
            vehicle = self._bot_links.pop(o.signal_id, None)
            acct = self.score_acct or self.bot
            if vehicle and vehicle in acct.positions:
                book = self.books().get(vehicle)
                if book is not None:
                    _, text = acct.close(
                        vehicle, book, now=now, reason=f"signal exit ({o.status.value})"
                    )
                    self.event(acct.name, text, now)

    def choose_vehicle(self, sig: ShadowSignal) -> str | None:
        """Long option the paper accounts may buy for a signal (V2: long options only).

        A long signal on an option is that option. Otherwise: bullish -> ATM call, bearish ->
        ATM put of the nearest watched expiry. A SHORT on a call is bearish, on a put bullish.
        """
        assert self.runner is not None
        if sig.symbol not in self.master:
            return None
        ins = self.master.get(sig.symbol)
        if ins.kind.is_option and sig.direction == "LONG":
            return sig.symbol
        if ins.kind.is_option:
            want = Kind.PUT if ins.kind is Kind.CALL else Kind.CALL
        else:
            want = Kind.CALL if sig.direction == "LONG" else Kind.PUT
        states = self.runner.scanner.states
        ref = self.runner.spots().get(ins.underlying) or (
            states[sig.symbol].ltp if sig.symbol in states else 0.0
        )
        cands: list[tuple[float, str]] = []
        for sym, st in states.items():
            if sym not in self.master or not st.asks:
                continue
            o = self.master.get(sym)
            if o.underlying == ins.underlying and o.kind is want and o.strike is not None:
                expiry_rank = o.expiry.toordinal() if o.expiry else 0
                cands.append((expiry_rank * 1e7 + abs(o.strike - ref), sym))
        return min(cands)[1] if cands and ref > 0 else None

    def _bot_enter(self, sig: ShadowSignal, now: datetime) -> None:
        vehicle = self.choose_vehicle(sig)
        if vehicle is None:
            self.event("bot", f"no option to buy for {self.label(sig.symbol)}", now)
            return
        book = self.books().get(vehicle)
        if book is None:
            self.event("bot", f"no book for {self.label(vehicle)}", now)
            return
        acct = self.score_acct or self.bot
        und = self.master.get(vehicle).underlying if vehicle in self.master else ""
        ok, text = acct.open_long_option(
            vehicle,
            book,
            now=now,
            lots=1,
            stop_pct=self.cfg.bot.option_stop_pct,
            signal_id=sig.signal_id,
            reason=f"{sig.direction} {self.label(sig.symbol)} score {sig.confidence_score:.0f}",
            context=self.market_context(und, now),
        )
        if ok:
            self._bot_links[sig.signal_id] = vehicle
        self.event(acct.name, text.replace(vehicle, self.label(vehicle)), now)

    # ------------------------------------------------------------------ commands

    def _drain_commands(self, now: datetime) -> None:
        if self.publisher is None:
            return
        for _ in range(50):
            try:
                cid, cmd = self.publisher.commands.get_nowait()
            except Exception:
                return
            if cmd.get("cmd") == "search":  # results go back as data, not as an event line
                try:
                    hits = self.search(str(cmd.get("q", "")))
                except Exception:
                    hits = []
                self.publisher.reply(
                    cid, {"id": cmd.get("id"), "ok": True, "kind": "search", "data": hits}
                )
                continue
            try:
                ok, text = self.handle(cmd, now)
            except Exception as exc:  # a bad command must not break the loop
                ok, text = False, f"error: {type(exc).__name__}"
            self.publisher.reply(cid, {"id": cmd.get("id"), "ok": ok, "text": text})
            self.event("command", text, now)

    def handle(self, cmd: dict[str, Any], now: datetime) -> tuple[bool, str]:
        assert self.runner is not None
        name = str(cmd.get("cmd", ""))
        books = self.books()
        named = {a.name: a for a in self.long_accounts()}
        acct = named.get(str(cmd.get("account") or "manual"), self.manual)
        if self.feed_frozen and name in ("buy", "take_signal", "close", "flatten", "condor_close"):
            return False, "feed stale: orders wait until fresh quotes return"
        if name == "buy":
            sym = str(cmd.get("symbol", ""))
            book = books.get(sym)
            if book is None:
                return False, f"no live book for {self.label(sym)}"
            stop = float(cmd.get("stop_pct") or self.cfg.manual_default_stop_pct)
            und = self.master.get(sym).underlying if sym in self.master else ""
            ok, text = self.manual.open_long_option(
                sym,
                book,
                now=now,
                lots=int(cmd.get("lots", 1)),
                stop_pct=stop,
                reason="manual",
                context=self.market_context(und, now),
            )
            return ok, text.replace(sym, self.label(sym))
        if name == "take_signal":
            sym = str(cmd.get("symbol", ""))
            sig = self.runner.scanner.active_signals.get(sym) or self.runner.last_eval.get(sym)
            if sig is None:
                return False, "no signal on that contract"
            vehicle = self.choose_vehicle(sig)
            if vehicle is None or vehicle not in books:
                return False, "no option with a live book to express it"
            stop = float(cmd.get("stop_pct") or self.cfg.manual_default_stop_pct)
            ok, text = self.manual.open_long_option(
                vehicle,
                books[vehicle],
                now=now,
                lots=1,
                stop_pct=stop,
                signal_id=sig.signal_id,
                reason=f"took {sig.direction} {self.label(sym)} score {sig.confidence_score:.0f}",
                context=self.market_context(
                    self.master.get(sym).underlying if sym in self.master else "", now
                ),
            )
            return ok, text.replace(vehicle, self.label(vehicle))
        if name == "close":
            sym = str(cmd.get("symbol", ""))
            book = books.get(sym)
            if book is None:
                return False, "no live book; cannot close yet"
            ok, text = acct.close(sym, book, now=now, reason=f"closed by hand ({acct.name})")
            return ok, text.replace(sym, self.label(sym))
        if name == "flatten":
            texts = acct.flatten(books, now, f"flatten ({acct.name})")
            return True, "; ".join(texts) or "nothing open"
        if name in ("pause", "resume", "speed", "stop"):
            if self.replay is None:
                return False, "replay controls only work in replay"
            if name == "pause":
                self.replay.pause()
            elif name == "resume":
                self.replay.resume()
            elif name == "speed":
                self.replay.set_speed(float(cmd.get("speed", 1.0)))
            elif self.on_stop is not None:
                self.on_stop()
            return True, f"replay {name}"
        if name == "watch":
            sym = str(cmd.get("symbol", ""))
            if sym not in self.master:
                return False, "unknown instrument"
            if sym in self.runner.subscribed:
                return True, f"{self.label(sym)} is already in the live feed"
            pins = [s for s in self.user_pins if s != sym] + [sym]
            pins = pins[-self.cfg.user_pins_max :]
            got = self._pin("page", pins)
            self.user_pins = got
            if sym not in got:
                return False, "no room in the 50-key feed for another contract"
            return True, f"added {self.label(sym)} to the live feed (30-level depth in a moment)"
        if name == "unwatch":
            sym = str(cmd.get("symbol", ""))
            self.user_pins = self._pin("page", [s for s in self.user_pins if s != sym])
            return True, f"removed {self.label(sym)} from the extra feed slots"
        if name == "condor_close":
            if self.condor is None or not self.spread.positions:
                return False, "no condor open"
            out = []
            for pos in list(self.spread.positions):
                ok, text = self.spread.close(pos, books, now, "closed by hand")
                out.append(text)
            self._pin("condor", self.condor.legs_in_use())
            return True, "; ".join(out)
        if name == "ping":
            return True, "pong"
        return False, f"unknown command {name!r}"

    # ------------------------------------------------------------------ greeks

    def _with_greeks(self, view: dict[str, Any], now: datetime) -> dict[str, Any]:
        """Add Black-76 Greeks (ours: synthetic forward, own IV) to each open option position."""
        books = self.books()
        for pos in view.get("positions", []):
            book = books.get(pos["symbol"])
            g = self.position_greeks(pos["symbol"], int(pos["qty"]), book, now) if book else None
            if g is not None:
                pos["greeks"] = g
        return view

    def position_greeks(
        self, symbol: str, qty: int, book: Book, now: datetime
    ) -> dict[str, float] | None:
        """Greeks for ``qty`` units: delta in underlying units, theta in rupees a day, vega in
        rupees per vol point, vanna in delta per vol point, charm in delta per day."""
        from engine.analytics.expiry_clock import expiry_clock
        from engine.analytics.greeks import compute_black76_greeks
        from engine.analytics.iv import implied_vol_black76

        a = self.analytics
        if a is None or symbol not in self.master or not book.bids or not book.asks:
            return None
        ins = self.master.get(symbol)
        if not ins.kind.is_option or ins.expiry is None or ins.strike is None:
            return None
        strip = a.strip.get(ins.underlying, {})
        f = strip.get("forward") or strip.get("spot")
        if not f:
            return None
        mid = 0.5 * (book.bids[0][0] + book.asks[0][0])
        clk = expiry_clock(now, ins.expiry, book=a.book, calendar=a.calendar, exchange=ins.exchange)
        r, _ = a._rate(now)
        is_call = ins.kind is Kind.CALL
        kw: dict[str, Any] = {"t_cal": clk.t_cal, "risk_free_rate": r}
        iv = implied_vol_black76(mid, float(f), float(ins.strike), clk.t_var, is_call=is_call, **kw)
        if iv is None:
            return None
        g = compute_black76_greeks(
            float(f), float(ins.strike), clk.t_var, iv, is_call=is_call, **kw
        )
        return {
            "iv": round(iv * 100, 2),
            "delta": round(g.delta * qty, 2),
            "gamma": round(g.gamma * qty, 4),
            "theta": round(g.theta_per_day * qty, 2),
            "vega": round(g.vega_per_1pct * qty, 2),
            "vanna": round(g.vanna * qty / 100.0, 3),
            "charm": round(g.charm_per_day * qty, 3),
        }

    # ------------------------------------------------------------------ snapshot

    def publish(self, now: datetime) -> None:
        if self.publisher is None:
            return
        self.publisher.publish(self.snapshot(now))

    def search(self, q: str, limit: int = 25) -> list[dict[str, Any]]:
        """Instruments whose label contains every word of ``q`` (live ones first)."""
        words = [w for w in q.upper().replace(",", " ").split() if w]
        if not words or self.runner is None:
            return []
        if self._search_index is None:
            idx = []
            for ins in self.master.all():
                if ins.kind.is_option or ins.kind in (Kind.FUTURE, Kind.INDEX, Kind.EQUITY):
                    exp = ins.expiry.toordinal() if ins.expiry else 0
                    idx.append((instrument_label(ins).upper(), ins.symbol, ins.underlying, exp))
            self._search_index = idx
        live = set(self.runner.subscribed)
        hits = [h for h in self._search_index if all(w in h[0] for w in words)]
        # Live first, then an exact underlying match (NIFTY before FINNIFTY), nearest expiry.
        hits.sort(key=lambda h: (h[1] not in live, h[2].upper() not in words, h[3], h[0]))
        return [
            {"symbol": sym, "label": self.label(sym), "live": sym in live}
            for _, sym, _, _ in hits[:limit]
        ]

    def portfolio(self, snap: dict[str, Any]) -> list[dict[str, Any]]:
        """One row per paper strategy for the overview: status, today's result, open risk."""
        accts = snap.get("accounts", {})
        cfg = self.cfg

        def long_row(key: str, title: str, ref: str, enabled: bool, rules: str) -> dict[str, Any]:
            a = accts.get(key) or {}
            return {
                "key": key, "title": title, "ref": ref, "enabled": enabled, "rules": rules,
                "day_net": a.get("day_net"), "realized": a.get("realized_net"),
                "open": len(a.get("positions", [])), "trades": a.get("trades_today", 0),
                "wins": a.get("wins", 0), "losses": a.get("losses", 0),
            }  # fmt: skip

        d = cfg.directional
        rows = [
            long_row("bot", "Directional on confirmed setups", "5.2", d.enabled,
                     f"naked ATM; stop −{d.stop_pct:g}%, target +{d.target_pct:g}%, trailing"),
            long_row("score", "Score-follow (original A+ bot)", "test", cfg.bot.enabled,
                     f"A+ signal (score ≥ {cfg.bot.min_score:g}) → ATM option; stop "
                     f"−{cfg.bot.option_stop_pct:g}%; exits when the signal's episode closes"),
            long_row("gamma", "Expiry-afternoon gamma breakout", "5.10", cfg.gamma.enabled,
                     "expiry days 13:45–15:15 after a quiet morning"),
            long_row("manual", "Manual", "—", True, "your own tickets"),
        ]  # fmt: skip
        for v in snap.get("plays", []):
            a = v.get("account") or {}
            closed = a.get("closed", [])
            rows.append(
                {
                    "key": "play:" + v["key"],
                    "title": v["title"],
                    "ref": v["ref"],
                    "enabled": v["enabled"],
                    "rules": v.get("decision", ""),
                    "day_net": a.get("day_net"),
                    "realized": a.get("realized_net"),
                    "open": len(a.get("positions", [])),
                    "trades": a.get("trades_today", 0),
                    "wins": sum(1 for t in closed if (t.get("net") or 0) > 0),
                    "losses": sum(1 for t in closed if (t.get("net") or 0) < 0),
                }
            )
        for key, title, ref, enabled, block in (
            ("condor", "Condor / credit spreads", "5.1", cfg.condor.enabled, snap.get("condor")),
            ("debit", "Debit spreads on confirmed setups", "5.2 variant", cfg.debit.enabled,
             snap.get("debit")),
        ):  # fmt: skip
            a = (block or {}).get("account") or {}
            rows.append(
                {
                    "key": key,
                    "title": title,
                    "ref": ref,
                    "enabled": enabled,
                    "rules": (block or {}).get("decision", ""),
                    "day_net": a.get("day_net"),
                    "realized": a.get("realized_net"),
                    "open": len(a.get("positions", [])),
                    "trades": a.get("trades_today", 0),
                    "wins": sum(1 for t in a.get("closed", []) if t.get("net", 0) > 0),
                    "losses": sum(1 for t in a.get("closed", []) if t.get("net", 0) < 0),
                }
            )
        return rows

    def _oi_read(self, ins: Any, st: Any, row: dict[str, Any]) -> dict[str, Any] | None:
        """Price change vs OI change for an option: buying, writing, covering or unwinding.
        OI change is against the previous session's NSE bhavcopy when known, else since the
        engine first saw the contract today."""
        prev = None
        if self.analytics is not None and ins.expiry is not None and ins.strike is not None:
            prev = (self.analytics.prev_oi.get(ins.underlying) or {}).get(
                (ins.expiry.isoformat(), float(ins.strike), ins.kind.value)
            )
        doi = (st.oi_now - prev) if prev is not None and st.oi_now else row.get("doi")
        chg = row.get("chg_pct")
        if not doi or chg is None or chg == 0:
            return None
        if chg > 0:
            label = "buying (long build-up)" if doi > 0 else "short covering"
        else:
            label = "writing (short build-up)" if doi > 0 else "long unwinding"
        put = ins.kind is Kind.PUT
        index_view = None
        if label.startswith("writing"):
            index_view = "UP" if put else "DOWN"  # writers sell puts when they expect no fall
        elif label == "short covering":
            index_view = "DOWN" if put else "UP"
        return {
            "label": label,
            "oi_chg": doi,
            "baseline": "previous session" if prev is not None else "since engine start",
            "index_view": index_view,
        }

    def _score_1m(self, sym: str, score: float | None, wall: float) -> float | None:
        """Average of the per-second scores over the last minute (what the page shows)."""
        h = self._score_hist.setdefault(sym, deque(maxlen=60))
        if score is not None and (not h or wall - h[-1][0] >= 1.0):
            h.append((wall, score))
        while h and wall - h[0][0] > 60.0:
            h.popleft()
        return round(sum(x[1] for x in h) / len(h), 1) if h else None

    def snapshot(self, now: datetime) -> dict[str, Any]:
        assert self.runner is not None
        r = self.runner
        sc = r.scanner
        rows = []
        books: dict[str, dict[str, list[list[float]]]] = {}
        wall = time.monotonic()
        pinned = set(r.pinned()) if hasattr(r, "pinned") else set()
        for sym in r.subscribed:
            st = sc.states.get(sym)
            if st is None:
                continue
            ins = self.master.get(sym) if sym in self.master else None
            bid = st.bids[0][0] if st.bids else None
            ask = st.asks[0][0] if st.asks else None
            row: dict[str, Any] = {
                "symbol": sym,
                "label": instrument_label(ins) if ins else sym,
                "kind": ins.kind.value if ins else "",
                "underlying": ins.underlying if ins else "",
                "ltp": st.ltp or None,
                "bid": bid,
                "ask": ask,
                "spread": round(ask - bid, 2) if bid and ask else None,
                "lot": st.lot_size,
                "is_option": bool(st.is_option),
            }
            if st.oi_now and sym not in self._oi_first:
                self._oi_first[sym] = (st.oi_now, now.astimezone(IST).strftime("%H:%M"))
            first_oi = self._oi_first.get(sym)
            row.update(
                {
                    "open": st.session_open or None,
                    "high": st.session_high or None,
                    "low": st.session_low or None,
                    "prev_close": st.prev_close or None,
                    "chg_pct": round((st.ltp / st.prev_close - 1) * 100, 2)
                    if st.ltp and st.prev_close
                    else None,
                    "pdh": st.pdh or None,
                    "pdl": st.pdl or None,
                    "volume": st.cum_volume or None,
                    "oi": st.oi_now or None,
                    "doi": st.oi_now - first_oi[0] if first_oi else None,
                    "oi_since": first_oi[1] if first_oi else None,
                    "pinned": sym in pinned,
                    "expiry": ins.expiry.isoformat() if ins and ins.expiry else None,
                    "strike": ins.strike if ins else None,
                }
            )
            if self.analytics is not None and sym in self.analytics.detail:
                row["greeks"] = self.analytics.detail[sym]
            if ins is not None and ins.kind.is_option:
                row["oi_read"] = self._oi_read(ins, st, row)
            dv = getattr(r, "delivery", {}).get(sym)
            if dv is not None:  # a delivery-spike stock from last night's watchlist
                row["delivery"] = dv.bias
            last = r.last_eval.get(sym)
            row["score_1m"] = self._score_1m(
                sym, last.confidence_score if last is not None else None, wall
            )
            if last is not None:
                row["sig"] = _sig_row(last)
                # What the reading says about the underlying (a SHORT on a put is bullish).
                up = (last.direction == "LONG") != (ins is not None and ins.kind is Kind.PUT)
                row["reads"] = "UP" if up else "DOWN"
            rows.append(row)
            if st.bids or st.asks:
                books[sym] = {"b": _levels(st.bids), "a": _levels(st.asks)}
        levels = {}
        for und, spot_sym in r.spot_symbol.items():
            st = sc.states.get(spot_sym)
            levels[und] = {
                "or_hi": r.or_hi.get(spot_sym),
                "or_lo": r.or_lo.get(spot_sym),
                "or_frozen": r.or_frozen,
                "pdh": st.pdh if st else None,
                "pdl": st.pdl if st else None,
                "pdc": st.prev_close if st else None,
                "call_wall": st.call_oi_wall if st else None,
                "put_wall": st.put_oi_wall if st else None,
            }
        lag = list(r.lag_ms)
        track_a_open = [
            {
                **_sig_row(s),
                "symbol": s.symbol,
                "label": self.label(s.symbol),
                "tradable": sc.is_tradable(s),
            }
            for s in sc.active_signals.values()
        ]
        cfg = self.cfg
        snap: dict[str, Any] = {
            "v": 1,
            "source": self.source,
            "mode": "PAPER",
            "now": now.isoformat(),
            "time": now.astimezone(IST).strftime("%H:%M:%S"),
            "day": r.session.day.isoformat(),
            "session": {
                "open": r.session.normal_open.strftime("%H:%M"),
                "close": r.session.normal_close.strftime("%H:%M"),
                "no_new_entries": sc.config.paper_no_new_entries_after.strftime("%H:%M"),
                "square_off": sc.config.paper_square_off_at.strftime("%H:%M"),
            },
            "feed": {
                "name": r.adapter.name,
                "status": r.last_feed_status,
                "lag_ms": round(statistics.median(lag)) if lag else None,
                "lag_note": "exchange time has 1-second resolution",
                "quotes": r.stats.quotes,
                "depths": r.stats.depths,
                "dropped": r.stats.dropped_events,
                "watchlist": len(r.subscribed),
                "episodes": r.stats.signals,
                "cockpit_errors": r.cockpit_errors,
                "age_s": round(self.feed_age_s, 1) if self.feed_age_s is not None else None,
                "frozen": self.feed_frozen,
                "watchdog_restarts": getattr(r, "watchdog_restarts", 0),
            },
            "strip": self.analytics.view() if self.analytics is not None else {},
            "levels": levels,
            "scanner": rows,
            "books": books,
            "track_a": {
                "summary": sc.ledger_summary(),
                "open": track_a_open,
                "closed": [_outcome_row(o, self.label(o.symbol)) for o in sc.closed_outcomes[-40:]][
                    ::-1
                ],
            },
            "accounts": {
                "bot": self._with_greeks(self.bot.view(self.books(), now), now),
                "manual": self._with_greeks(self.manual.view(self.books(), now), now),
                **{
                    a.name: self._with_greeks(a.view(self.books(), now), now)
                    for a in (self.score_acct, self.gamma_acct)
                    if a is not None
                },
            },
            "events": list(self.events)[:80],
            "config": {
                "bot_enabled": cfg.bot.enabled,
                "bot_min_score": cfg.bot.min_score,
                "bot_stop_pct": cfg.bot.option_stop_pct,
                "manual_stop_pct": cfg.manual_default_stop_pct,
                "cost_broker": sc.cost_broker,
            },
        }
        if self.study is not None:
            snap["study"] = self.study.view()
        if self.setups is not None:
            snap["setups"] = self.setups.view()
        if self.condor is not None and self.spread is not None:
            und = self.cfg.condor.underlying
            snap["condor"] = {
                **self.condor.view(),
                "account": self.spread.view(self.books(), now, r.spots().get(und)),
                "trend": self._trend(und, now),
            }
        snap["pins"] = {"page": list(self.user_pins), "max": self.cfg.user_pins_max}
        if self.debit is not None:
            snap["debit"] = {
                **self.debit.view(),
                "account": self.debit_acct.view(self.books(), now, None),
            }
        if self.gamma is not None:
            snap["gamma"] = self.gamma.view()
        prices = {}
        for und in getattr(r, "spot_symbol", {}):
            fut = self._front_future(und)
            if fut is not None and fut.ltp:
                prices[und] = fut.ltp
        snap["zones"] = self.zones.view(prices)
        play_views = []
        bks = self.books()
        for p in self.plays:
            v = p.view()
            acct = p.account
            if hasattr(p, "managed"):
                v["account"] = self._with_greeks(acct.view(bks, now), now)
            else:
                v["account"] = acct.view(bks, now, None)
            play_views.append(v)
        snap["plays"] = play_views
        snap["portfolio"] = self.portfolio(snap)
        if self.analytics is not None:
            snap["chains"] = self.analytics.chains_view()  # the gateway sends these on request
        d = cfg.directional
        snap["config"]["directional"] = {
            "enabled": d.enabled,
            "min_score": d.min_score,
            "window": f"{d.first_entry}-{d.last_entry}",
            "stop_pct": d.stop_pct,
            "target_pct": d.target_pct,
            "trail": [list(t) for t in d.trail],
            "max_entries": d.max_entries_per_day,
        }
        if self.calendar_events:
            from engine.data.events.calendar_file import blackout_status, upcoming

            ev_st = blackout_status(self.calendar_events, now)
            snap["events_cal"] = {
                "blackout": ev_st.in_macro_blackout,
                "active": list(ev_st.active_blackout_events),
                "earnings_weight_24h_pct": ev_st.earnings_weight_24h_pct,
                "short_vega_scale": ev_st.short_vega_scale_factor,
                "upcoming": upcoming(self.calendar_events, now, 4),
                "blocks_entries": self.cfg.block_entries_in_event_window,
            }
        if self.replay is not None:
            snap["replay"] = self.replay.progress()
        return snap
