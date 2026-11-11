"""Quick check of what a broker feed actually delivers to *this* account (``tasks.py upstox-probe``).

Upstox sends a snapshot of current prices right after a subscription, even outside market
hours, so this works at any time of day. It subscribes one instrument of each kind (index,
VIX, futures, ATM call/put, one stock), waits a few seconds and reports per segment whether
prices, depth and open interest arrived. Typical uses: is the token accepted, is the F&O
segment active for this account, does Upstox Plus give 30 levels.
"""

from __future__ import annotations

import queue
import time as _time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from engine.data.adapters.base import Depth, FeedAdapter, FeedEvent, FeedStatus, Quote
from engine.data.reference.instrument_master import InstrumentMaster, Kind, classify_expiries


@dataclass
class ProbeLine:
    symbol: str
    label: str
    quotes: int = 0
    ltp: float | None = None
    max_depth_levels: int = 0
    oi: int | None = None


@dataclass
class ProbeReport:
    lines: dict[str, ProbeLine] = field(default_factory=dict)
    statuses: list[str] = field(default_factory=list)

    def segment_summary(self) -> dict[str, bool]:
        """Segment prefix (NSE_FO, NSE_INDEX, ...) -> did any of its instruments deliver data."""
        out: dict[str, bool] = {}
        for ln in self.lines.values():
            seg = ln.symbol.split("|", 1)[0] if "|" in ln.symbol else ln.symbol.split(":", 1)[0]
            out[seg] = out.get(seg, False) or ln.quotes > 0
        return out

    def render(self) -> str:
        rows = [f"{'instrument':<34} {'quotes':>6} {'ltp':>10} {'depth':>5} {'OI':>12}"]
        for ln in self.lines.values():
            ltp = f"{ln.ltp:.2f}" if ln.ltp is not None else "-"
            oi = f"{ln.oi:,}" if ln.oi is not None else "-"
            rows.append(
                f"{ln.label[:34]:<34} {ln.quotes:>6} {ltp:>10} {ln.max_depth_levels:>5} {oi:>12}"
            )
        rows.append(
            "segments: "
            + ", ".join(
                f"{seg} {'OK' if ok else 'NOTHING RECEIVED'}"
                for seg, ok in sorted(self.segment_summary().items())
            )
        )
        if self.statuses:
            rows.append("feed status: " + " | ".join(self.statuses[:8]))
        return "\n".join(rows)


def _equity_key(master: InstrumentMaster, trading_symbol: str) -> str | None:
    for ins in master.all():
        if ins.kind is Kind.EQUITY and ins.description == trading_symbol:
            return ins.symbol
    return None


def run_probe(
    adapter: FeedAdapter,
    master: InstrumentMaster,
    *,
    day: date,
    spots: Mapping[str, str],  # underlying -> spot key
    vix_symbol: str | None,
    equity: str = "RELIANCE",
    seconds: float = 20.0,
    sleep: Callable[[float], None] = _time.sleep,
    now: Callable[[], float] = _time.monotonic,
) -> ProbeReport:
    report = ProbeReport()
    q: queue.Queue[FeedEvent] = queue.Queue()

    def want(symbols: Sequence[str], label_of: Mapping[str, str]) -> None:
        new = [s for s in symbols if s and s not in report.lines]
        for s in new:
            report.lines[s] = ProbeLine(s, label_of.get(s, s))
        if new:
            adapter.subscribe(new, depth=True)

    def drain() -> None:
        while True:
            try:
                ev = q.get_nowait()
            except queue.Empty:
                return
            if isinstance(ev, FeedStatus):
                report.statuses.append(f"{ev.kind.value}: {ev.detail}"[:160])
                continue
            ln = report.lines.get(ev.symbol)
            if ln is None:
                continue
            if isinstance(ev, Quote):
                ln.quotes += 1
                ln.ltp = ev.ltp if ev.ltp is not None else ln.ltp
                ln.oi = ev.oi if ev.oi is not None else ln.oi
            elif isinstance(ev, Depth):
                ln.max_depth_levels = max(ln.max_depth_levels, len(ev.bids), len(ev.asks))

    core: list[str] = []
    labels: dict[str, str] = {}
    for name, key in spots.items():
        core.append(key)
        labels[key] = f"{name} spot ({key})"
        futs = master.futures(name, day)
        if futs:
            core.append(futs[0].symbol)
            labels[futs[0].symbol] = f"{name} future {futs[0].expiry}"
    if vix_symbol:
        core.append(vix_symbol)
        labels[vix_symbol] = "India VIX"
    eq = _equity_key(master, equity)
    if eq:
        core.append(eq)
        labels[eq] = f"{equity} (cash)"
    adapter.start(q.put)
    want([s for s in core if s in master], labels)

    deadline = now() + seconds
    options_added: set[str] = set()
    while now() < deadline:
        sleep(0.25)
        drain()
        for name, key in spots.items():  # ATM call/put once the spot price is known
            spot_line = report.lines.get(key)
            if name in options_added or spot_line is None or not spot_line.ltp:
                continue
            roles = classify_expiries(master.option_expiries(name, day), ("current_weekly",))
            exp = roles.get("current_weekly")
            if exp is None:
                continue
            strikes = master.strikes(name, exp)
            if not strikes:
                continue
            atm = min(strikes, key=lambda k: abs(k - spot_line.ltp))  # type: ignore[operator]
            opts = [i for i in master.options(name, exp) if i.strike == atm]
            want(
                [i.symbol for i in opts],
                {i.symbol: f"{name} {exp} {atm:g} {i.kind.value}" for i in opts},
            )
            options_added.add(name)
    drain()
    adapter.stop()
    drain()
    return report
