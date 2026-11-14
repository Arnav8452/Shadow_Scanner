"""Session rules either side of 7 Dec 2026 (NSE/FAOP/77356, NSE/CMTR/77355, SEBI 15 Oct 2026).

From 7 Dec 2026: F&O closes 15:45; an expiring index contract stops at 15:30 on its expiry day
(stock derivatives trade to 15:45); expiry settlement is the underlying's cash VWAP 15:00-15:30;
the daily close-price window is 15:15-15:45; the cash closing auction runs 15:30-15:40.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from engine.analytics.expiry_clock import expiry_clock
from engine.core.calendar import TradingCalendar
from engine.core.clock import expiry_close_fn, market_session
from engine.core.rules import IST, RuleBook
from engine.data.reference.instrument_master import parse_upstox_master
from engine.shadow.scanner import ShadowOrderBookScanner

FRI = date(2026, 12, 4)  # last day of the 3 Aug 2026 regime
MON = date(2026, 12, 7)  # first day of the revised CAS / derivatives timings


@pytest.fixture(scope="module")
def book(repo_root: Path) -> RuleBook:
    return RuleBook.load(repo_root / "config" / "rules")


def test_nse_fo_session_4_dec_vs_7_dec(book: RuleBook) -> None:
    old = market_session(book, "NSE", "fo", FRI)
    new = market_session(book, "NSE", "fo", MON)
    assert old.verified and new.verified
    assert (old.normal_open, old.normal_close) == (time(9, 15), time(15, 40))
    assert (new.normal_open, new.normal_close) == (time(9, 15), time(15, 45))
    assert old.pre_open_end == time(9, 8) and new.pre_open_end == time(9, 10)
    assert old.close_vwap_start == time(15, 10) and new.close_vwap_start == time(15, 15)
    # expiring index contracts: 15:40 like everything else before, 15:30 from 7 Dec
    assert old.expiry_close(index=True) == time(15, 40)
    assert new.expiry_close(index=True) == time(15, 30)
    assert new.expiry_close(index=False) == time(15, 45)  # stock derivatives, incl. expiry day
    # settlement: CAS closing price before (no VWAP window), cash VWAP 15:00-15:30 after
    assert old.settle_vwap_start is None and old.settle_vwap_end is None
    assert (new.settle_vwap_start, new.settle_vwap_end) == (time(15, 0), time(15, 30))


def test_older_regimes_unchanged(book: RuleBook) -> None:
    pre = market_session(book, "NSE", "fo", date(2026, 7, 31))
    assert pre.normal_close == time(15, 30) and pre.expiry_close(index=True) == time(15, 30)
    aug = market_session(book, "NSE", "fo", date(2026, 8, 3))
    assert aug.normal_close == time(15, 40) and aug.expiry_close(index=True) == time(15, 40)


def test_nse_cash_closing_auction_4_dec_vs_7_dec(book: RuleBook) -> None:
    t = book["market_sessions"]

    def at(day: date, session: str) -> str:
        return t.lookup(day, exchange="NSE", segment="cm", session=session).value

    assert [at(FRI, s) for s in ("cas_start", "cas_entry_end", "cas_match_end")] == [
        "15:15",
        "15:30",
        "15:35",
    ]
    assert [at(MON, s) for s in ("cas_start", "cas_entry_end", "cas_match_end")] == [
        "15:30",
        "15:40",
        "15:45",
    ]
    assert at(FRI, "normal_close") == at(MON, "normal_close") == "15:30"
    assert at(MON, "post_close_start") == "15:50" and at(MON, "post_close_end") == "16:00"


def test_bse_fo_from_7_dec(book: RuleBook) -> None:
    old = market_session(book, "BSE", "fo", FRI, allow_unverified=True)
    new = market_session(book, "BSE", "fo", MON, allow_unverified=True)
    assert old.normal_close == time(15, 40) and old.expiry_close(index=True) == time(15, 40)
    assert new.normal_close == time(15, 45) and new.expiry_close(index=True) == time(15, 30)


def test_expiry_clock_on_expiry_day(book: RuleBook) -> None:
    cal = TradingCalendar.from_rulebook(book)
    at_1430 = datetime.combine(FRI, time(14, 30), IST)
    old = expiry_clock(at_1430, FRI, book=book, calendar=cal)
    assert old.expiry_close.time() == time(15, 40) and old.trading_minutes == 70

    at_1430 = datetime.combine(MON, time(14, 30), IST)
    idx = expiry_clock(at_1430, MON, book=book, calendar=cal)
    assert idx.expiry_close.time() == time(15, 30) and idx.trading_minutes == 60
    stk = expiry_clock(at_1430, MON, book=book, calendar=cal, index=False)
    assert stk.expiry_close.time() == time(15, 45) and stk.trading_minutes == 75
    assert idx.t_var < stk.t_var


def test_expiry_clock_across_the_change(book: RuleBook) -> None:
    cal = TradingCalendar.from_rulebook(book)
    now = datetime.combine(FRI, time(15, 0), IST)
    clk = expiry_clock(now, date(2026, 12, 8), book=book, calendar=cal)
    # Fri 15:00-15:40 (40) + Mon 09:15-15:45 (390) + Tue expiry 09:15-15:30 (375)
    assert clk.trading_minutes == 40 + 390 + 375
    assert clk.expiry_close == datetime(2026, 12, 8, 15, 30, tzinfo=IST)


def test_expiry_close_fn_per_contract(book: RuleBook) -> None:
    close_for = expiry_close_fn(book)
    assert close_for("NSE", FRI, True) == time(15, 40)
    assert close_for("NSE", date(2026, 12, 8), True) == time(15, 30)
    assert close_for("NSE", date(2026, 12, 29), False) == time(15, 45)
    assert close_for("BSE", date(2026, 12, 10), True) == time(15, 30)


def _ms(d: date) -> int:
    """Upstox master expiry: epoch ms at 23:59:59 IST of the expiry date."""
    return int(datetime.combine(d, time(23, 59, 59), IST).astimezone(UTC).timestamp() * 1000)


def test_upstox_master_expiry_times_follow_each_expiry(book: RuleBook) -> None:
    def opt(key: str, und: str, und_key: str, expiry: date) -> dict[str, object]:
        return {
            "segment": "NSE_FO",
            "instrument_type": "CE",
            "instrument_key": key,
            "exchange": "NSE",
            "expiry": _ms(expiry),
            "strike_price": 100.0,
            "lot_size": 65,
            "tick_size": 5.0,
            "underlying_symbol": und,
            "underlying_key": und_key,
            "trading_symbol": key,
        }

    rows = [
        opt("NSE_FO|1", "NIFTY", "NSE_INDEX|Nifty 50", date(2026, 12, 1)),
        opt("NSE_FO|2", "NIFTY", "NSE_INDEX|Nifty 50", date(2026, 12, 8)),
        opt("NSE_FO|3", "RELIANCE", "NSE_EQ|INE002A01018", date(2026, 12, 29)),
    ]
    items = {i.symbol: i for i in parse_upstox_master(rows, close_time=expiry_close_fn(book))}
    times = {k: items[k].expiry_ts.time() for k in ("NSE_FO|1", "NSE_FO|2", "NSE_FO|3")}  # type: ignore[union-attr]
    assert times == {"NSE_FO|1": time(15, 40), "NSE_FO|2": time(15, 30), "NSE_FO|3": time(15, 45)}


def test_scanner_cas_window_reads_the_rules(book: RuleBook) -> None:
    sc = ShadowOrderBookScanner(book=book)
    assert sc._cas_window(date(2026, 7, 31)) is None  # no closing auction before 3 Aug 2026
    assert sc._cas_window(FRI) == (time(15, 0), time(15, 35))
    assert sc._cas_window(MON) == (time(15, 15), time(15, 45))
