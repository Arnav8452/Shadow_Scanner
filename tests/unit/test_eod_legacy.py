"""Old-layout NSE bhavcopies (before 1 Jan 2024): URLs, download validation and parsing."""

from __future__ import annotations

import io
import urllib.error
import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest

from engine.core.rules import IST
from engine.data.eod.bhavcopy import parse_cash_bhavcopy_csv, parse_fno_bhavcopy_csv
from engine.data.eod.download import (
    UDIFF_FIRST_DAY,
    eod_path,
    eod_url,
    fetch_eod,
    is_legacy,
    read_eod_text,
)

LEGACY_FO = (
    "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,"
    "CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,\n"
    "FUTIDX,NIFTY,25-Jan-2018,0,XX,10400,10450,10380,10441.5,10441.5,1200,9000.5,250000,-5000,"
    "03-JAN-2018,\n"
    "OPTIDX,NIFTY,25-Jan-2018,10500,CE,90,101.2,80,95.35,95.35,3000,150.2,420000,12000,"
    "03-JAN-2018,\n"
    "OPTSTK,ACME,25-Jan-2018,300,PE,0,0,0,4.1,4.25,0,0,6000,0,03-JAN-2018,\n"
)
LEGACY_CM = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,"
    "TOTALTRADES,ISIN,\n"
    "ACME,EQ,100,104,99,103.5,103.6,100.5,20000,2050000.5,03-JAN-2018,850,INE000A01011,\n"
    "ACMEBOND,N3,990,990,990,990,990,980,50,49500,03-JAN-2018,1,INE000A07011,\n"
)


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def test_url_switches_to_old_layout_before_udiff_start() -> None:
    assert date(2024, 1, 1) == UDIFF_FIRST_DAY
    old = date(2018, 1, 3)
    assert eod_url("fo", old) == (
        "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/2018/JAN/"
        "fo03JAN2018bhav.csv.zip"
    )
    assert eod_url("cm", old) == (
        "https://nsearchives.nseindia.com/content/historical/EQUITIES/2018/JAN/"
        "cm03JAN2018bhav.csv.zip"
    )
    assert is_legacy("fo", old) and is_legacy("cm", date(2023, 12, 29))
    new = date(2024, 1, 1)
    assert not is_legacy("fo", new)
    assert eod_url("fo", new).endswith("BhavCopy_NSE_FO_0_0_0_20240101_F_0000.csv.zip")
    # Other datasets keep their single URL whatever the date.
    assert not is_legacy("indices", old)
    assert eod_url("indices", old).endswith("ind_close_all_03012018.csv")
    # Both layouts land at the same path, so readers find either.
    assert eod_path(Path("raw"), "fo", old) == Path("raw/eod/fo/2018/2018-01-03.csv.zip")


def test_fetch_validates_old_layout_and_rejects_html(tmp_path: Path) -> None:
    now = datetime(2026, 11, 14, 20, 0, tzinfo=IST)
    day = date(2018, 1, 3)
    seen: list[str] = []

    def good(url: str) -> bytes:
        seen.append(url)
        return _zip("fo03JAN2018bhav.csv", LEGACY_FO)

    res = fetch_eod("fo", day, raw_dir=tmp_path, now=now, fetcher=good)
    assert res.status == "ok" and res.path is not None
    assert "historical/DERIVATIVES" in seen[0]
    assert read_eod_text(res.path).startswith("INSTRUMENT,SYMBOL")
    meta = res.path.with_name(res.path.name + ".meta.json").read_text("utf-8")
    assert "historical/DERIVATIVES" in meta

    res = fetch_eod("cm", day, raw_dir=tmp_path, now=now, fetcher=lambda u: _zip("x", LEGACY_CM))
    assert res.status == "ok"

    html = fetch_eod(
        "fo",
        date(2018, 1, 4),
        raw_dir=tmp_path,
        now=now,
        fetcher=lambda u: _zip("x", "<html>Access denied</html>"),
    )
    assert html.status == "missing" and not eod_path(tmp_path, "fo", date(2018, 1, 4)).exists()

    # A UDiFF file served for an old date is not the old layout: rejected.
    udiff = fetch_eod(
        "fo",
        date(2018, 1, 5),
        raw_dir=tmp_path,
        now=now,
        fetcher=lambda u: _zip("x", "TradDt,BizDt,TckrSymb\n"),
    )
    assert udiff.status == "missing"

    def holiday(url: str) -> bytes:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    assert fetch_eod(
        "fo", date(2018, 1, 26), raw_dir=tmp_path, now=now, fetcher=holiday
    ).status == ("missing")


def test_old_fo_layout_parses() -> None:
    rows = parse_fno_bhavcopy_csv(LEGACY_FO)
    assert [r.instrument for r in rows] == ["FUTIDX", "OPTIDX", "OPTSTK"]
    opt = rows[1]
    assert opt.trade_date == date(2018, 1, 3)
    assert opt.expiry == date(2018, 1, 25)
    assert (opt.symbol, opt.strike, opt.option_type) == ("NIFTY", 10500.0, "CE")
    assert (opt.open, opt.high, opt.low, opt.close, opt.settle_price) == (
        90.0,
        101.2,
        80.0,
        95.35,
        95.35,
    )
    assert (opt.contracts, opt.oi, opt.change_in_oi) == (3000, 420000, 12000)
    # The old layout has no underlying price or lot size: readers must use other sources.
    assert opt.underlying_price == 0.0 and opt.lot_size == 0
    assert rows[0].option_type == "XX" and rows[0].change_in_oi == -5000
    assert rows[2].settle_price == 4.25 and rows[2].close == 4.1
    assert opt.known_at == datetime(2018, 1, 3, 19, 30, tzinfo=IST)


def test_old_cm_layout_parses_equity_series_only() -> None:
    rows = parse_cash_bhavcopy_csv(LEGACY_CM)
    assert [r.symbol for r in rows] == ["ACME"]
    r = rows[0]
    assert r.trade_date == date(2018, 1, 3) and r.series == "EQ"
    assert (r.open, r.high, r.low, r.close, r.prev_close) == (100.0, 104.0, 99.0, 103.5, 100.5)
    assert r.traded_qty == 20000 and r.turnover_inr == 2050000.5
    assert r.delivery_qty == 0  # delivery comes from its own file


def test_cut_off_download_is_a_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client
    import urllib.request

    from engine.core import net

    class Resp:
        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            raise http.client.IncompleteRead(b"x" * 10, 90)

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp())
    with pytest.raises(ConnectionError):
        net.http_get_bytes("https://example.invalid/file.zip")


def test_backfill_retries_a_failed_day(tmp_path: Path) -> None:
    from engine.data.eod.download import backfill

    calls: list[str] = []
    waits: list[float] = []

    def flaky(url: str) -> bytes:
        calls.append(url)
        if len(calls) == 1:
            raise ConnectionError("cut off")
        return _zip("fo03JAN2018bhav.csv", LEGACY_FO)

    out = backfill(
        ["fo"],
        date(2018, 1, 3),
        date(2018, 1, 3),
        raw_dir=tmp_path,
        now=lambda: datetime(2026, 11, 15, 10, 0, tzinfo=IST),
        fetcher=flaky,
        sleep=waits.append,
    )
    assert [r.status for r in out] == ["ok"] and len(calls) == 2
    assert waits[0] >= 5.0  # backed off before the retry
