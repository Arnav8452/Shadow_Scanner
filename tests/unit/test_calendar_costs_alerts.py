import logging
from datetime import date
from pathlib import Path

import pytest

from engine.backtest.costs import equity_delivery_charges
from engine.core.calendar import CalendarCoverageError, TradingCalendar
from engine.core.env import Secret
from engine.core.rules import RuleBook, RuleNotFoundError, UnverifiedRuleError
from ops.alerts.telegram import TelegramNotifier, format_digest


@pytest.fixture(scope="module")
def book(repo_root: Path) -> RuleBook:
    return RuleBook.load(repo_root / "config" / "rules")


# ---------------------------------------------------------------- calendar


def test_calendar_2026(book: RuleBook) -> None:
    cal = TradingCalendar.from_rulebook(book)
    assert not cal.is_trading_day(date(2026, 11, 10))  # Diwali Balipratipada
    assert not cal.is_trading_day(date(2026, 11, 14))  # Saturday
    assert cal.is_trading_day(date(2026, 11, 11))
    assert cal.previous_trading_day(date(2026, 11, 11)) == date(2026, 11, 9)
    assert cal.next_trading_day(date(2026, 11, 9)) == date(2026, 11, 11)
    assert cal.last_cum_date(date(2026, 11, 20)) == date(2026, 11, 19)
    assert cal.add_trading_days(date(2026, 11, 20), 3) == date(2026, 11, 26)  # skips 24 Nov
    # 8th (Sunday Muhurat session), 9th, 11th
    assert cal.trading_days_between(date(2026, 11, 6), date(2026, 11, 11)) == 3


def test_calendar_uncovered_year_raises(book: RuleBook) -> None:
    cal = TradingCalendar.from_rulebook(book)
    with pytest.raises(CalendarCoverageError, match="2027"):
        cal.is_trading_day(date(2027, 1, 4))


def test_special_session() -> None:
    cal = TradingCalendar([date(2026, 1, 26)], special_sessions=[date(2026, 11, 8)])
    assert cal.is_trading_day(date(2026, 11, 8))  # Sunday Muhurat session


# ---------------------------------------------------------------- equity delivery costs


def test_equity_delivery_buy_and_sell(book: RuleBook) -> None:
    on = date(2026, 11, 9)
    buy = equity_delivery_charges(book, on, side="buy", value_inr=100_000)
    assert buy.brokerage == 0 and buy.stt == pytest.approx(100)
    assert buy.stamp == pytest.approx(15) and buy.exchange == pytest.approx(3.07)
    assert buy.sebi == pytest.approx(0.10) and buy.gst == pytest.approx(0.18 * 3.17)
    assert buy.dp == 0 and buy.total == pytest.approx(100 + 15 + 3.07 + 0.10 + 0.5706)
    sell = equity_delivery_charges(book, on, side="sell", value_inr=100_000)
    assert sell.stamp == 0 and sell.dp == pytest.approx(15.34)
    assert sell.total == pytest.approx(100 + 3.07 + 0.10 + 0.18 * 3.17 + 15.34)
    assert (
        equity_delivery_charges(book, on, side="sell", value_inr=100_000, include_dp=False).dp == 0
    )
    with pytest.raises(ValueError):
        equity_delivery_charges(book, on, side="buy", value_inr=-1)


def test_costs_refuse_unverified_broker(book: RuleBook) -> None:
    with pytest.raises(RuleNotFoundError):
        equity_delivery_charges(book, date(2026, 11, 9), side="buy", value_inr=1, broker="fyers")


def test_lookup_all_refuses_partial_unverified(book: RuleBook) -> None:
    with pytest.raises(UnverifiedRuleError):
        book["exchange_charges"].lookup_all(date(2026, 11, 9), exchange="NSE", segment="options")
    rows = book["exchange_charges"].lookup_all(
        date(2026, 11, 9), allow_unverified=True, exchange="NSE", segment="options"
    )
    assert {r.keys["component"] for r in rows} == {"transaction", "ipft"}


# ---------------------------------------------------------------- telegram


def test_telegram_dry_run_without_token(caplog: pytest.LogCaptureFixture) -> None:
    n = TelegramNotifier.from_env({})
    assert n.dry_run
    with caplog.at_level(logging.INFO):
        assert n.send("hello") is False
    assert "dry-run" in caplog.text


def test_telegram_send_and_no_token_leak(caplog: pytest.LogCaptureFixture) -> None:
    sent: list[tuple[str, bytes]] = []

    def ok(url: str, body: bytes, timeout: float) -> int:
        sent.append((url, body))
        return 200

    n = TelegramNotifier(Secret("T", "123456:SECRET-abc"), "42", poster=ok)
    assert n.send("x" * 9000)
    assert len(sent) == 3 and b"chat_id=42" in sent[0][1]

    def boom(url: str, body: bytes, timeout: float) -> int:
        raise OSError(f"cannot reach {url}")

    n = TelegramNotifier(Secret("T", "123456:SECRET-abc"), "42", poster=boom)
    with caplog.at_level(logging.ERROR):
        assert n.send("x") is False
    assert "SECRET-abc" not in caplog.text and "failed" in caplog.text


def test_format_digest() -> None:
    assert format_digest("T", ["a", "b"]) == "T\n• a\n• b"
    assert format_digest("T", []) == "T"
