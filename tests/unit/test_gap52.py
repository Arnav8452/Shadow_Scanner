"""Gap to a 52-week high (engine/strategies/gap52.py): NSE's 52-week file, the signal rules,
and the trailing stop and hold limit it adds to the swing paper account."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine.core.calendar import TradingCalendar
from engine.core.config import load_settings
from engine.core.rules import RuleBook
from engine.data.eod.bhavcopy import CashBhavcopyRow
from engine.data.eod.week52 import Week52, effective_date, parse_week52
from engine.execution.swing_paper import DayBar, Order, SwingFillConfig, SwingPaperAccount
from engine.strategies.gap52 import Gap52Config, find_signals, gap52_config

W52_TEXT = (
    '"Disclaimer - The Data provided in the adjusted 52 week high and adjusted 52 week low '
    'columns  are adjusted for corporate actions (bonus, splits & rights)."\n'
    '"Effective for 20-Nov-2026"\n'
    '"SYMBOL","SERIES","Adjusted_52_Week_High","52_Week_High_Date","Adjusted_52_Week_Low",'
    '"52_Week_Low_DT"\n'
    '"1STCUS$","EQ","-","-","-","-"\n'
    '"AAA","EQ","    102.00","19-NOV-2026","    70.50","30-MAR-2026"\n'
    '"BBB","EQ","    100.00","05-JAN-2026","    60.00","13-OCT-2026"\n'
    '"BBB","BE","    999.00","05-JAN-2026","    60.00","13-OCT-2026"\n'
)


def test_week52_file_parses_and_names_its_session() -> None:
    assert effective_date(W52_TEXT) == date(2026, 11, 20)
    w = parse_week52(W52_TEXT)
    assert set(w) == {"AAA", "BBB"}  # "-" rows and other series left out
    assert w["AAA"].high == 102.0 and w["AAA"].high_date == date(2026, 11, 19)
    assert w["BBB"].high == 100.0 and w["BBB"].low_date == date(2026, 10, 13)


D, PREV = date(2026, 11, 20), date(2026, 11, 19)
IST = timezone(timedelta(hours=5, minutes=30))


def _row(sym: str, open_: float, prev_close: float) -> CashBhavcopyRow:
    return CashBhavcopyRow(
        trade_date=D, symbol=sym, series="EQ", open=open_, high=open_ * 1.01, low=open_ * 0.99,
        close=open_, prev_close=prev_close, traded_qty=1000, turnover_inr=1e7, delivery_qty=500,
        delivery_pct=50.0, known_at=datetime(2026, 11, 20, 19, 30, tzinfo=IST),
    )  # fmt: skip


def _w(sym: str, high: float, on: date) -> Week52:
    return Week52(sym, "EQ", high, on, high * 0.6, date(2026, 3, 1))


def test_signal_rules_as_written() -> None:
    cfg = Gap52Config()
    old = date(2026, 6, 1)
    today = {
        "OK": _row("OK", 102.0, 100.0),  # +2.0%, at the high, high set months ago
        "OK2": _row("OK2", 50.8, 50.0),  # +1.6%
        "SMALL": _row("SMALL", 101.0, 100.0),  # +1.0%: not "around 2%"
        "BIG": _row("BIG", 104.0, 100.0),  # +4.0%
        "BELOW": _row("BELOW", 102.0, 100.0),  # open below its 52-week high
        "YDAY": _row("YDAY", 102.0, 100.0),  # made its 52-week high yesterday
        "NOTFO": _row("NOTFO", 102.0, 100.0),  # not an F&O stock
    }
    w52 = {
        "OK": _w("OK", 101.5, old), "OK2": _w("OK2", 50.5, old), "SMALL": _w("SMALL", 100.5, old),
        "BIG": _w("BIG", 103.0, old), "BELOW": _w("BELOW", 102.5, old),
        "YDAY": _w("YDAY", 101.0, PREV), "NOTFO": _w("NOTFO", 101.0, old),
    }  # fmt: skip
    fno = set(today) - {"NOTFO"}
    sig = find_signals(today, w52, fno, prev_day=PREV, prev_turnover={"OK2": 9e9, "OK": 1e6},
                       cfg=cfg)  # fmt: skip
    assert [s.symbol for s in sig] == ["OK2", "OK"]  # most liquid first
    assert sig[1].gap_pct == pytest.approx(2.0) and sig[1].high52 == 101.5


def test_config_reads_settings_block(repo_root: Path) -> None:
    raw = load_settings(root=repo_root, environ={}).raw.get("gap52_paper")
    cfg = gap52_config(raw)
    assert cfg.start_date == date(2026, 11, 23) and cfg.trail_pct > 0 and cfg.max_hold_sessions > 0


# --------------------------------------------------------------------------- paper account


@pytest.fixture
def acct(repo_root: Path, tmp_path: Path) -> SwingPaperAccount:
    settings = load_settings(root=repo_root, environ={})
    book = RuleBook.load(repo_root / "config" / "rules")
    return SwingPaperAccount(
        tmp_path / "gap52_paper_state.json", limits=settings.risk, book=book,
        calendar=TradingCalendar.from_rulebook(book), broker="upstox_plus",
        fills=SwingFillConfig(cash_half_spread_pct=0.0, option_half_spread_pct=0.0),
        journal_name="paper_gap52.jsonl",
    )  # fmt: skip


def _order(max_hold: int = 5) -> Order:
    return Order(id="", strategy="gap52", symbol="AAA", instrument="CASH", key="AAA", qty=200,
                 est_price=100.0, stop_pct=2.0, trail_pct=2.0, max_hold=max_hold)  # fmt: skip


T1, T2, T3, T4 = date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17)


def test_trailing_stop_follows_the_high_then_fills(acct: SwingPaperAccount) -> None:
    assert acct.journal.name == "paper_gap52.jsonl"
    acct.place_entry(_order(), day=T1, risk_budget_pct=2.5)
    acct.process_day(T1, {"AAA": DayBar(100, 104, 99, 103.5)}, {"AAA": 103.5})
    pos = acct.state.positions[0]
    assert pos.entry_price == 100 and pos.stop == pytest.approx(101.92)  # 104 less 2%
    out = acct.process_day(T2, {"AAA": DayBar(103, 103.5, 101, 102)}, {"AAA": 102})
    assert any("stop" in x for x in out) and not acct.state.positions
    assert acct.state.closed[0].exit_price == pytest.approx(101.92)


def test_close_below_the_trail_means_the_trailing_order_sold(acct: SwingPaperAccount) -> None:
    acct.place_entry(_order(), day=T1, risk_budget_pct=2.5)
    acct.process_day(T1, {"AAA": DayBar(100, 106, 99.5, 103)}, {"AAA": 103})
    c = acct.state.closed[0]
    assert c.reason == "trailing stop" and c.exit_price == pytest.approx(103.88)


def test_initial_stop_is_checked_before_the_trail(acct: SwingPaperAccount) -> None:
    acct.place_entry(_order(), day=T1, risk_budget_pct=2.5)
    acct.process_day(T1, {"AAA": DayBar(100, 105, 97, 99)}, {"AAA": 99})  # low first: stopped
    c = acct.state.closed[0]
    assert c.reason == "stop" and c.exit_price == pytest.approx(98.0)


def test_order_hold_limit_queues_a_sale(acct: SwingPaperAccount) -> None:
    acct.place_entry(_order(max_hold=2), day=T1, risk_budget_pct=2.5)
    flat = DayBar(100, 100.5, 99.5, 100)
    acct.process_day(T1, {"AAA": flat}, {"AAA": 100})
    acct.process_day(T2, {"AAA": flat}, {"AAA": 100})
    assert acct.state.positions[0].exit_reason == ""
    acct.process_day(T3, {"AAA": flat}, {"AAA": 100})
    assert acct.state.positions[0].exit_reason == "maximum hold 2 sessions"
    acct.process_day(T4, {"AAA": DayBar(100.2, 100.5, 99.9, 100)}, {"AAA": 100})
    assert acct.state.closed[0].exit_price == pytest.approx(100.2)
