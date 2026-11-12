"""Paper ledger of the shadow scanner: square-off, naked-short filter, book walk, 2x slippage,
and brokerage from the configured plan."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from engine.core.config import load_settings
from engine.core.rules import IST
from engine.data.adapters.base import Depth, Quote
from engine.data.eod.bhavcopy import DeliveryBias
from engine.shadow.models import ShadowSignal, ShadowSignalStatus
from engine.shadow.scanner import InstrumentMicrostructureState, ShadowOrderBookScanner

SYM = "NSE:NIFTY50-FUT"
DAY_10_30 = datetime(2026, 11, 12, 10, 30, tzinfo=IST)


def _ns(t: datetime) -> int:
    return int(t.timestamp() * 1_000_000_000)


def _book(top_bid: float, top_ask: float, qty: int = 5200) -> tuple[tuple, tuple]:
    bids = tuple((top_bid - i * 0.5, qty, 14) for i in range(30))
    asks = tuple((top_ask + i * 0.5, qty, 4) for i in range(30))
    return bids, asks


def _scanner_with_signal(repo_root: Path) -> tuple[ShadowOrderBookScanner, ShadowSignal]:
    """The stop-hunt reclaim scenario from the scanner test, on 12 Nov 2026."""
    settings = load_settings(root=repo_root, version="v1_shadow", environ={})
    # signal_confirm_s=0: these tests build a signal from a single book update.
    cfg = replace(settings.shadow_scanner, signal_confirm_s=0.0)
    scanner = ShadowOrderBookScanner(cfg, capital_inr=200_000.0)
    scanner.register_instrument(
        InstrumentMicrostructureState(
            symbol=SYM,
            tick_size=0.05,
            lot_size=65,
            pdh=24250.0,
            pdl=24000.0,
            prev_close=24080.0,
            orh_15m=24150.0,
            orl_15m=24010.0,
            delivery_bias=DeliveryBias.LONG_ACCUMULATION,
        )
    )
    now_ns = _ns(DAY_10_30)
    for dt_s, ltp, ltq, vol, oi in ((-180, 23980.0, 650, 100_000, 12_000_000),
                                     (0, 24008.0, 3900, 120_000, 11_800_000)):  # fmt: skip
        scanner.on_quote(
            Quote(
                SYM,
                now_ns + dt_s * 1_000_000_000,
                ltp=ltp,
                ltq=ltq,
                volume=vol,
                oi=oi,
                open=24040.0,
                high=24050.0,
                low=23965.0,
            )
        )
    bids = tuple((24008.0 - i * 0.5, 5200 if i < 5 else 700, 14) for i in range(30))
    asks = tuple((24008.5 + i * 0.5, 850 if i < 5 else 220, 4) for i in range(30))
    sig = scanner.on_depth(Depth(SYM, now_ns, bids, asks), now=DAY_10_30)
    assert sig is not None and SYM in scanner.active_signals
    return scanner, scanner.active_signals[SYM]


def _trigger(scanner: ShadowOrderBookScanner, sig: ShadowSignal, at: datetime) -> None:
    bids, asks = _book(sig.entry_price, sig.entry_price + 0.5)
    scanner.on_quote(Quote(SYM, _ns(at), ltp=sig.entry_price + 1.0))
    scanner.on_depth(Depth(SYM, _ns(at), bids, asks), now=at)
    assert scanner.active_signals[SYM].status is ShadowSignalStatus.ACTIVE


def test_open_trade_is_squared_off_at_cutoff(repo_root: Path) -> None:
    scanner, sig = _scanner_with_signal(repo_root)
    _trigger(scanner, sig, DAY_10_30 + timedelta(seconds=1))
    late = DAY_10_30.replace(hour=15, minute=21)
    bids, asks = _book(sig.entry_price + 3.0, sig.entry_price + 3.5)
    scanner.on_quote(Quote(SYM, _ns(late), ltp=sig.entry_price + 3.2))  # no stop or target hit
    scanner.on_depth(Depth(SYM, _ns(late), bids, asks), now=late)
    assert SYM not in scanner.active_signals
    out = scanner.closed_outcomes[-1]
    assert out.status is ShadowSignalStatus.SQUARED_OFF_EOD
    assert out.exit_price == bids[0][0]
    # Upstox Plus futures brokerage is ₹30 an order (config), so two orders cost at least ₹60.
    assert scanner.cost_broker == "upstox_plus"
    assert out.charges_inr >= 60.0


def test_session_end_closes_active_and_drops_pending(repo_root: Path) -> None:
    scanner, sig = _scanner_with_signal(repo_root)
    _trigger(scanner, sig, DAY_10_30 + timedelta(seconds=1))
    pending = replace(sig, symbol="NSE:OTHER-FUT", status=ShadowSignalStatus.PENDING_TRIGGER)
    scanner.active_signals["NSE:OTHER-FUT"] = pending
    closed = scanner.close_open_signals(DAY_10_30 + timedelta(minutes=5), "Session end")
    assert closed == 1
    assert scanner.active_signals == {}
    assert scanner.closed_outcomes[-1].status is ShadowSignalStatus.SQUARED_OFF_EOD
    assert scanner.not_paper_traded["pending_cancelled_at_cutoff"] == 1


def test_option_shorts_are_tracked_but_tagged_and_late_entries_skipped(repo_root: Path) -> None:
    scanner, sig = _scanner_with_signal(repo_root)
    # Track A measures the signal, so option shorts are tracked (not censored) but tagged.
    opt_short = replace(sig, symbol="NSE:NIFTY26N1725000CE", direction="SHORT")
    assert scanner._paper_skip_reason(opt_short, DAY_10_30) is None
    assert not scanner.is_tradable(opt_short)
    assert scanner.is_tradable(replace(sig, direction="SHORT"))  # futures short
    assert scanner.is_tradable(replace(sig, symbol="NSE:NIFTY26N1725000PE"))  # long option
    # Broker keys such as Upstox "NSE_FO|54749" carry no type: the master's kind decides.
    scanner.register_instrument(InstrumentMicrostructureState("NSE_FO|54749", is_option=True))
    assert not scanner.is_tradable(replace(sig, symbol="NSE_FO|54749", direction="SHORT"))
    late = DAY_10_30.replace(hour=15, minute=1)
    assert scanner._paper_skip_reason(sig, late) == "after_no_new_entries_time"


def test_option_charges_use_master_kind_for_broker_keys(repo_root: Path) -> None:
    scanner, _ = _scanner_with_signal(repo_root)
    scanner.register_instrument(InstrumentMicrostructureState("NSE_FO|1", is_option=True))
    scanner.register_instrument(InstrumentMicrostructureState("NSE_FO|2", is_option=False))
    on = DAY_10_30.date()
    opt = scanner._round_trip_charges(
        "NSE_FO|1", entry_turnover=6500.0, exit_turnover=7000.0, is_long=True, on=on
    )
    fut = scanner._round_trip_charges(
        "NSE_FO|2", entry_turnover=6500.0, exit_turnover=7000.0, is_long=True, on=on
    )
    assert opt != fut  # options: flat brokerage and premium STT; futures: turnover-based


def test_stock_charges_are_intraday_cash_equity(repo_root: Path) -> None:
    from engine.backtest.costs import equity_intraday_charges
    from engine.core.rules import RuleBook

    scanner, _ = _scanner_with_signal(repo_root)
    key = "NSE_EQ|INE094A01015"  # Upstox key: the type comes from the master, not the text
    scanner.register_instrument(InstrumentMicrostructureState(key, is_option=False, is_equity=True))
    assert scanner.cost_segment(key) == "equity_intraday"
    assert scanner.cost_segment("NSE_FO|2") == "futures"  # unknown: futures as before
    on = DAY_10_30.date()
    got = scanner._round_trip_charges(
        key, entry_turnover=100_000.0, exit_turnover=101_000.0, is_long=True, on=on
    )
    book = RuleBook.load(repo_root / "config" / "rules")
    want = sum(
        equity_intraday_charges(
            book, on, side=s, value_inr=v, broker=scanner.cost_broker, allow_unverified=True
        ).total
        for s, v in (("buy", 100_000.0), ("sell", 101_000.0))
    )
    assert got == pytest.approx(want)
    short = scanner._round_trip_charges(
        key, entry_turnover=101_000.0, exit_turnover=100_000.0, is_long=False, on=on
    )
    assert short == pytest.approx(want)  # a short sells at entry: STT on the sell leg either way


def test_untriggered_signal_is_cancelled_after_cutoff(repo_root: Path) -> None:
    scanner, sig = _scanner_with_signal(repo_root)
    late = DAY_10_30.replace(hour=15, minute=5)
    bids, asks = _book(sig.entry_price - 5.0, sig.entry_price - 4.5)
    scanner.on_quote(Quote(SYM, _ns(late), ltp=sig.entry_price - 4.8))
    scanner.on_depth(Depth(SYM, _ns(late), bids, asks), now=late)
    assert SYM not in scanner.active_signals
    assert scanner.closed_outcomes == []
    assert scanner.not_paper_traded["pending_cancelled_at_cutoff"] == 1


def test_fills_walk_the_book_and_report_2x_slippage() -> None:
    st = InstrumentMicrostructureState(symbol="X", tick_size=0.05, lot_size=65)
    st.bids = ((99.0, 50, 1), (98.0, 50, 1))
    st.asks = ((100.0, 40, 1), (101.0, 40, 1))
    avg, slip, thin = ShadowOrderBookScanner._walk_book(st, buy=True, qty=65, fallback=0.0)
    assert avg == pytest.approx((40 * 100.0 + 25 * 101.0) / 65)
    assert slip == pytest.approx((avg - 99.5) * 65)
    assert not thin
    avg, _, thin = ShadowOrderBookScanner._walk_book(st, buy=False, qty=130, fallback=0.0)
    assert thin  # only 100 units visible on the bid
    assert avg == pytest.approx((50 * 99.0 + 80 * 98.0) / 130)
    st.bids, st.asks = (), ()
    assert ShadowOrderBookScanner._walk_book(st, buy=True, qty=65, fallback=97.0) == (
        97.0,
        0.0,
        True,
    )


def test_outcome_carries_slippage_and_2x_net(repo_root: Path) -> None:
    scanner, sig = _scanner_with_signal(repo_root)
    _trigger(scanner, sig, DAY_10_30 + timedelta(seconds=1))
    scanner.close_open_signals(DAY_10_30 + timedelta(minutes=1))
    out = scanner.closed_outcomes[-1]
    assert out.slippage_inr > 0
    assert out.net_pnl_2x_slippage_inr == pytest.approx(out.net_pnl_inr - out.slippage_inr)
    summary = scanner.ledger_summary()
    assert summary["net_pnl_2x_slippage_inr"] == pytest.approx(out.net_pnl_2x_slippage_inr)
    assert summary["cost_broker"] == "upstox_plus"


def test_tradeable_grade_must_hold_before_it_counts(repo_root: Path) -> None:
    """A score that crosses 75 on one update is flicker: nothing is tracked until it has held
    (same direction) for signal_confirm_s."""
    settings = load_settings(root=repo_root, version="v1_shadow", environ={})
    cfg = replace(settings.shadow_scanner, signal_confirm_s=5.0)
    scanner = ShadowOrderBookScanner(cfg, capital_inr=200_000.0)
    scanner.register_instrument(
        InstrumentMicrostructureState(
            symbol=SYM, tick_size=0.05, lot_size=65, pdh=24250.0, pdl=24000.0,
            prev_close=24080.0, orh_15m=24150.0, orl_15m=24010.0,
            delivery_bias=DeliveryBias.LONG_ACCUMULATION,
        )
    )  # fmt: skip
    now_ns = _ns(DAY_10_30)
    for dt_s, ltp, ltq, vol, oi in ((-180, 23980.0, 650, 100_000, 12_000_000),
                                     (0, 24008.0, 3900, 120_000, 11_800_000)):  # fmt: skip
        scanner.on_quote(Quote(SYM, now_ns + dt_s * 1_000_000_000, ltp=ltp, ltq=ltq, volume=vol,
                               oi=oi, open=24040.0, high=24050.0, low=23965.0))  # fmt: skip
    bids = tuple((24008.0 - i * 0.5, 5200 if i < 5 else 700, 14) for i in range(30))
    asks = tuple((24008.5 + i * 0.5, 850 if i < 5 else 220, 4) for i in range(30))
    first = scanner.on_depth(Depth(SYM, now_ns, bids, asks), now=DAY_10_30)
    assert first is not None and first.confidence_score >= 75 and not first.confirmed
    assert SYM not in scanner.active_signals
    later = DAY_10_30 + timedelta(seconds=6)
    again = scanner.on_depth(Depth(SYM, _ns(later), bids, asks), now=later)
    assert again is not None and again.confirmed
    assert SYM in scanner.active_signals
