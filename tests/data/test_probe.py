"""upstox-probe: reports per segment what a feed actually delivers (fake feed, no network)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from engine.data.adapters.base import Depth, FeedStatus, Quote, Sink, StatusKind
from engine.data.recorder.probe import run_probe
from engine.data.reference.instrument_master import Instrument, InstrumentMaster, Kind
from tests.data.conftest import DAY, WEEKLY, ins


def master_with_keys() -> InstrumentMaster:
    items = [
        ins("NSE_INDEX|Nifty 50", Kind.INDEX),
        ins("NSE_INDEX|India VIX", Kind.INDEX, underlying="INDIAVIX"),
        ins("NSE_FO|61471", Kind.FUTURE, expiry=date(2026, 11, 23)),
        ins("NSE_FO|1", Kind.CALL, expiry=WEEKLY, strike=25000.0),
        ins("NSE_FO|2", Kind.PUT, expiry=WEEKLY, strike=25000.0),
        ins("NSE_FO|3", Kind.CALL, expiry=WEEKLY, strike=25050.0),
    ]
    eq = Instrument(
        symbol="NSE_EQ|INE002A01018",
        broker="upstox",
        exchange="NSE",
        segment="cm",
        token="",
        exchange_token="2885",
        underlying="RELIANCE",
        kind=Kind.EQUITY,
        expiry=None,
        expiry_ts=None,
        strike=None,
        lot_size=1,
        tick_size=0.1,
        isin="",
        description="RELIANCE",
    )
    return InstrumentMaster([*items, eq], as_of=DAY, source="test")


class FakeFeed:
    """Answers each subscription with a snapshot, but drops F&O when ``fo_active`` is False."""

    name = "fake"
    max_symbols = 100
    supports_depth = True
    depth_counts_separately = False

    def __init__(self, *, fo_active: bool, levels: int = 5) -> None:
        self.fo_active = fo_active
        self.levels = levels
        self.sink: Sink | None = None
        self.subscribed: list[str] = []

    def start(self, sink: Sink) -> None:
        self.sink = sink
        sink(FeedStatus(StatusKind.CONNECTED, 0, "fake"))

    def subscribe(self, symbols: Sequence[str], *, depth: bool) -> None:
        assert self.sink is not None
        self.subscribed += list(symbols)
        for s in symbols:
            if s.startswith("NSE_FO") and not self.fo_active:
                continue
            ltp = 25010.0 if "Nifty" in s else 100.0
            self.sink(Quote(s, 1, ltp=ltp, oi=12345 if s.startswith("NSE_FO") else None))
            if "INDEX" not in s:
                lv = tuple((ltp - i, 65, 1) for i in range(self.levels))
                self.sink(Depth(s, 1, lv, lv))

    def unsubscribe(self, symbols: Sequence[str], *, depth: bool) -> None: ...

    def restart(self) -> None: ...

    def stop(self) -> None: ...


def probe(feed: FakeFeed) -> Any:
    t = [0.0]

    def sleep(s: float) -> None:
        t[0] += s

    return run_probe(
        feed,
        master_with_keys(),
        day=DAY,
        spots={"NIFTY": "NSE_INDEX|Nifty 50"},
        vix_symbol="NSE_INDEX|India VIX",
        seconds=2.0,
        sleep=sleep,
        now=lambda: t[0],
    )


def test_probe_reports_all_segments_and_atm_options() -> None:
    feed = FakeFeed(fo_active=True, levels=30)
    rep = probe(feed)
    assert rep.segment_summary() == {"NSE_INDEX": True, "NSE_FO": True, "NSE_EQ": True}
    # ATM (25000) call and put are added once the spot price is known; 25050 is not
    assert "NSE_FO|1" in feed.subscribed and "NSE_FO|2" in feed.subscribed
    assert "NSE_FO|3" not in feed.subscribed
    assert rep.lines["NSE_FO|1"].oi == 12345 and rep.lines["NSE_FO|1"].max_depth_levels == 30
    assert "OK" in rep.render() and "connected" in rep.render()


def test_probe_flags_missing_fo_segment() -> None:
    rep = probe(FakeFeed(fo_active=False))
    seg = rep.segment_summary()
    assert seg["NSE_INDEX"] and seg["NSE_EQ"] and not seg["NSE_FO"]
    assert "NSE_FO NOTHING RECEIVED" in rep.render()
