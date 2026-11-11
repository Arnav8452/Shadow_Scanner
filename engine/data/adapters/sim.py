"""Synthetic feed for dry runs and tests — never real market data.

Prices follow a seeded random walk; options are priced with the Bachelier formula so the
chain looks plausible. Output is tagged ``source='sim'`` and the runner writes it under
``data/sim/`` so it can never mix with real recordings.
"""

from __future__ import annotations

import math
import random
import threading
from collections.abc import Callable, Mapping, Sequence

from engine.core.clock import NS_PER_S, Clock
from engine.data.adapters.base import Depth, FeedStatus, Quote, Sink, StatusKind
from engine.data.reference.instrument_master import InstrumentMaster, Kind


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bachelier(forward: float, strike: float, sigma_pts: float, call: bool) -> float:
    if sigma_pts <= 0:
        intrinsic = forward - strike if call else strike - forward
        return max(intrinsic, 0.0)
    d = (forward - strike) / sigma_pts
    c = (forward - strike) * _norm_cdf(d) + sigma_pts * _norm_pdf(d)
    return c if call else c - (forward - strike)


class SimFeed:
    name = "sim"
    supports_depth = True
    depth_counts_separately = False

    def __init__(
        self,
        master: InstrumentMaster,
        clock: Clock,
        *,
        spots: Mapping[str, float],
        max_symbols: int = 5000,
        iv: float = 0.13,
        seed: int = 7,
        interval_s: float = 1.0,
        threaded: bool = False,
        depth_levels: int = 5,
    ) -> None:
        self.master = master
        self.clock = clock
        self.max_symbols = max_symbols
        self.iv = iv
        self.rng = random.Random(seed)
        self.spots = dict(spots)  # underlying -> level
        self.interval_s = interval_s
        self.threaded = threaded
        self.depth_levels = max(5, depth_levels)
        self.symbols: set[str] = set()
        self.depth: set[str] = set()
        self.sink: Sink | None = None
        self.paused_until_ns = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.connects = 0

    # ------------------------------------------------------------------ adapter API

    def start(self, sink: Sink) -> None:
        self.sink = sink
        self.connects += 1
        sink(FeedStatus(StatusKind.CONNECTED, self.clock.now_ns(), "sim"))
        if self.threaded and self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="simfeed", daemon=True)
            self._thread.start()

    def subscribe(self, symbols: Sequence[str], *, depth: bool) -> None:
        with self._lock:
            new = set(symbols) | self.symbols
            if len(new) > self.max_symbols:
                if self.sink:
                    self.sink(
                        FeedStatus(StatusKind.LIMIT, self.clock.now_ns(), f"cap {self.max_symbols}")
                    )
                return
            self.symbols = new
            if depth:
                self.depth |= set(symbols)

    def unsubscribe(self, symbols: Sequence[str], *, depth: bool) -> None:
        with self._lock:
            self.symbols -= set(symbols)
            self.depth -= set(symbols)

    def restart(self) -> None:
        if self.sink is not None:
            self.sink(FeedStatus(StatusKind.DISCONNECTED, self.clock.now_ns(), "sim restart"))
            self.paused_until_ns = 0
            self.connects += 1
            self.sink(FeedStatus(StatusKind.CONNECTED, self.clock.now_ns(), "sim"))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ simulation

    def pause(self, seconds: float) -> None:
        """Simulate a silent feed (for gap tests)."""
        self.paused_until_ns = self.clock.now_ns() + int(seconds * NS_PER_S)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.pump()

    def pump(self, emit: Callable[[str], bool] | None = None) -> int:
        """Emit one update for every subscribed symbol (or those ``emit`` accepts)."""
        now = self.clock.now_ns()
        if self.sink is None or now < self.paused_until_ns:
            return 0
        for u in list(self.spots):
            self.spots[u] *= math.exp(self.rng.gauss(0.0, 0.00015))
        with self._lock:
            syms = sorted(self.symbols)
            depth = set(self.depth)
        n = 0
        for sym in syms:
            if emit is not None and not emit(sym):
                continue
            q, d = self._quote(sym, now, sym in depth)
            if q is None:
                continue
            self.sink(q)
            n += 1
            if d is not None:
                self.sink(d)
                n += 1
        return n

    def _quote(self, sym: str, now: int, with_depth: bool) -> tuple[Quote | None, Depth | None]:
        ins = self.master.get(sym)
        spot = self.spots.get(ins.underlying)
        exch = now // NS_PER_S
        if ins.kind is Kind.INDEX:
            level = spot if spot is not None else 100.0 * (1 + self.rng.random())
            if "VIX" in ins.symbol.upper():  # NSE:INDIAVIX-INDEX or NSE_INDEX|India VIX
                level = self.iv * 100
            return Quote(sym, now, exch_ts=exch, ltp=round(level, 2), prev_close=level), None
        if spot is None:
            return None, None
        if ins.kind is Kind.FUTURE:
            px = spot * 1.002
        elif ins.kind.is_option and ins.strike is not None and ins.expiry_ts is not None:
            years = max((ins.expiry_ts.timestamp() - now / NS_PER_S) / (365 * 86400), 1e-4)
            sigma_pts = spot * self.iv * math.sqrt(years)
            px = max(bachelier(spot, ins.strike, sigma_pts, ins.kind is Kind.CALL), 0.05)
        else:
            px = spot
        tick = ins.tick_size or 0.05
        half = max(tick, round(px * 0.002 / tick) * tick)
        bid = max(round((px - half) / tick) * tick, 0.0)
        ask = round((px + half) / tick) * tick
        lot = ins.lot_size or 1
        q = Quote(
            sym,
            now,
            exch_ts=exch,
            ltt=exch,
            ltp=round(px, 2),
            ltq=lot,
            volume=lot * self.rng.randint(1, 10_000),
            oi=lot * self.rng.randint(1, 50_000),
            bid=round(bid, 2),
            bid_qty=lot * self.rng.randint(1, 50),
            ask=round(ask, 2),
            ask_qty=lot * self.rng.randint(1, 50),
        )
        d = None
        if with_depth:
            bids = tuple(
                (round(max(bid - i * tick, 0.0), 2), lot * self.rng.randint(1, 50), 1 + i)
                for i in range(self.depth_levels)
            )
            asks = tuple(
                (round(ask + i * tick, 2), lot * self.rng.randint(1, 50), 1 + i)
                for i in range(self.depth_levels)
            )
            d = Depth(sym, now, bids, asks)
        return q, d
