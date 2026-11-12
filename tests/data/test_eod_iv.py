"""EOD downloads, the session-aware expiry clock, Black-76 golden values, ATM IV recovery."""

from __future__ import annotations

import io
import math
import urllib.error
import zipfile
from datetime import date, datetime, time
from pathlib import Path

import pytest

from engine.analytics.expiry_clock import expiry_clock
from engine.analytics.iv import black76_price, implied_vol_black76
from engine.core.calendar import TradingCalendar
from engine.core.rules import IST, RuleBook
from engine.data.eod.bhavcopy import FnOBhavcopyRow
from engine.data.eod.download import eod_path, fetch_eod, read_eod_text
from engine.data.eod.iv_history import atm_iv_for_day, percentile_rank, vix_close


@pytest.fixture(scope="module")
def book(repo_root: Path) -> RuleBook:
    return RuleBook.load(repo_root / "config" / "rules")


# ------------------------------------------------------------------ Black-76 golden values


def _ref_black76(f: float, k: float, t: float, r: float, s: float, call: bool) -> float:
    """Independent reference: N(x) via erfc, written separately from engine.analytics.iv."""

    def n(x: float) -> float:
        return 0.5 * math.erfc(-x / math.sqrt(2.0))

    d1 = (math.log(f / k) + 0.5 * s * s * t) / (s * math.sqrt(t))
    d2 = d1 - s * math.sqrt(t)
    df = math.exp(-r * t)
    return df * (f * n(d1) - k * n(d2)) if call else df * (k * n(-d2) - f * n(-d1))


def test_black76_haug_textbook_value() -> None:
    # Haug, The Complete Guide to Option Pricing Formulas: F=19, K=19, T=0.75, r=10%, σ=28%
    # gives call = put = 1.7011.
    c = black76_price(19.0, 19.0, 0.75, 0.28, is_call=True, t_cal=0.75, risk_free_rate=0.10)
    p = black76_price(19.0, 19.0, 0.75, 0.28, is_call=False, t_cal=0.75, risk_free_rate=0.10)
    assert c == pytest.approx(1.7011, abs=1e-4) and p == pytest.approx(1.7011, abs=1e-4)


@pytest.mark.parametrize("k", [23500.0, 24500.0, 25000.0, 25500.0, 26500.0])
@pytest.mark.parametrize("sigma", [0.09, 0.15, 0.35])
def test_iv_matches_independent_reference_within_0_1_vol_pt(k: float, sigma: float) -> None:
    """SPEC M4 acceptance: IV within 0.1 vol pt of an independent Black-76 implementation."""
    f, t, r = 25000.0, 6 / 252, 0.0534
    for call in (True, False):
        px = _ref_black76(f, k, t, r, sigma, call)
        if px < 0.05:  # below one tick there is no meaningful IV
            continue
        iv = implied_vol_black76(px, f, k, t, is_call=call, t_cal=t, risk_free_rate=r)
        assert iv is not None and abs(iv - sigma) < 0.001


# ------------------------------------------------------------------ expiry clock


def test_expiry_clock_uses_sessions_and_holidays(book: RuleBook) -> None:
    cal = TradingCalendar.from_rulebook(book)
    # Mon 9 Nov 2026 close -> Tue 17 Nov close: 11,12,13,16,17 (10 Nov is a holiday) x 385 min
    clk = expiry_clock(
        datetime(2026, 11, 9, 15, 40, tzinfo=IST), date(2026, 11, 17), book=book, calendar=cal
    )
    assert clk.trading_minutes == 5 * 385 and not clk.approx
    assert clk.expiry_close == datetime(2026, 11, 17, 15, 40, tzinfo=IST)
    assert clk.t_var == pytest.approx(5 / 252)
    # intraday: 12:40 on expiry day leaves 3 hours
    mid = expiry_clock(
        datetime(2026, 11, 17, 12, 40, tzinfo=IST), date(2026, 11, 17), book=book, calendar=cal
    )
    assert mid.trading_minutes == 180
    # before 3 Aug 2026 the close was 15:30 (375-minute sessions)
    old = expiry_clock(
        datetime(2026, 7, 28, 9, 15, tzinfo=IST), date(2026, 7, 28), book=book, calendar=cal
    )
    assert old.trading_minutes == 375 and old.expiry_close.time() == time(15, 30)
    # 2025 is covered (5 Nov 2025 is Guru Nanak Jayanti holiday, so 3 Nov close -> 6 Nov close = 2 sessions x 375 min)
    c25 = expiry_clock(
        datetime(2025, 11, 3, 15, 30, tzinfo=IST), date(2025, 11, 6), book=book, calendar=cal
    )
    assert c25.trading_minutes == 2 * 375 and not c25.approx
    # a year with no holiday list falls back to weekdays and says so
    assert expiry_clock(
        datetime(2024, 11, 4, 15, 30, tzinfo=IST), date(2024, 11, 5), book=book, calendar=cal
    ).approx


# ------------------------------------------------------------------ ATM IV from a chain


def _chain(
    day: date, expiry: date, spot: float, sigma: float, book: RuleBook
) -> list[FnOBhavcopyRow]:
    cal = TradingCalendar.from_rulebook(book)
    close = datetime(day.year, day.month, day.day, 15, 40, tzinfo=IST)
    clk = expiry_clock(close, expiry, book=book, calendar=cal)
    r = 0.0534
    fwd = spot * math.exp(r * clk.t_cal)
    rows = []
    for k in range(24500, 25550, 50):
        for typ in ("CE", "PE"):
            df = math.exp(-r * clk.t_cal)
            sd = sigma * math.sqrt(clk.t_var)
            d1 = (math.log(fwd / k) + 0.5 * sd * sd) / sd
            d2 = d1 - sd
            nn = lambda x: 0.5 * math.erfc(-x / math.sqrt(2))  # noqa: E731
            px = (
                df * (fwd * nn(d1) - k * nn(d2))
                if typ == "CE"
                else df * (k * nn(-d2) - fwd * nn(-d1))
            )
            rows.append(
                FnOBhavcopyRow(
                    day,
                    "NIFTY",
                    "IDO",
                    expiry,
                    float(k),
                    typ,
                    px,
                    px,
                    px,
                    round(px, 2),
                    round(px, 2),
                    1000,
                    1000,
                    0,
                    close,
                    underlying_price=spot,
                    lot_size=65,
                )
            )
    return rows


def test_atm_iv_recovers_known_vol(book: RuleBook) -> None:
    cal = TradingCalendar.from_rulebook(book)
    rows = _chain(date(2026, 11, 9), date(2026, 11, 17), 25044.65, 0.1765, book)
    iv = atm_iv_for_day(rows, underlying="NIFTY", book=book, calendar=cal, r=0.0534)
    assert iv is not None and abs(iv.atm_iv - 0.1765) < 0.001
    assert iv.atm_strike == 25050.0 and iv.n_parity_strikes == 5 and iv.tier == "C_eod_close"
    assert iv.known_at.time() == time(19, 30)  # usable only from the next session
    # an expiry fewer than 2 trading days away is skipped
    near = _chain(date(2026, 11, 16), date(2026, 11, 17), 25044.65, 0.18, book)
    assert atm_iv_for_day(near, underlying="NIFTY", book=book, calendar=cal, r=0.0534) is None


def test_percentile_and_vix_parsing() -> None:
    assert percentile_rank([10, 12, 14, 16, 18], 15) == 60.0
    csv = "Index Name,Index Date,Open,High,Low,Closing Index Value\nNifty 50,09-11-2026,1,1,1,25044.65\nIndia VIX,09-11-2026,18.4,18.4,16.7,17.42\n"
    assert vix_close(csv) == 17.42


# ------------------------------------------------------------------ downloads


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("BhavCopy.csv", text)
    return buf.getvalue()


def test_fetch_eod_ok_missing_html_and_cache(tmp_path: Path) -> None:
    now = datetime(2026, 11, 11, 20, 0, tzinfo=IST)
    day = date(2026, 11, 9)
    calls: list[str] = []

    def ok(url: str) -> bytes:
        calls.append(url)
        return _zip("TradDt,TckrSymb\n2026-11-09,NIFTY\n")

    res = fetch_eod("fo", day, raw_dir=tmp_path, now=now, fetcher=ok)
    assert res.status == "ok" and res.path == eod_path(tmp_path, "fo", day)
    assert calls == [
        "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20261109_F_0000.csv.zip"
    ]
    assert "NIFTY" in read_eod_text(res.path)
    assert fetch_eod("fo", day, raw_dir=tmp_path, now=now, fetcher=ok).status == "cached"

    def not_found(url: str) -> bytes:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    assert (
        fetch_eod(
            "indices", date(2026, 11, 10), raw_dir=tmp_path, now=now, fetcher=not_found
        ).status
        == "missing"
    )
    html = fetch_eod(
        "participant_oi", day, raw_dir=tmp_path, now=now, fetcher=lambda u: b"<!DOCTYPE html>"
    )
    assert html.status == "missing" and not eod_path(tmp_path, "participant_oi", day).exists()
    with pytest.raises(ValueError):
        fetch_eod("nonsense", day, raw_dir=tmp_path, now=now, fetcher=ok)
