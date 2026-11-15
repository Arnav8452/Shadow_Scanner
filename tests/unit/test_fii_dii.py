"""NSE provisional FII/DII cash flows: parsing and nightly filing under their own date."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from engine.core.rules import IST
from engine.data.eod.flows import (
    CURRENT_EOD_URLS,
    fetch_current_eod,
    latest_file,
    parse_fii_dii,
)

BODY = (
    '[{"buyValue":"11745.95","category":"DII","date":"13-Nov-2026","netValue":"-2253.58",'
    '"sellValue":"13999.53"},{"buyValue":"18,339.57","category":"FII/FPI","date":"13-Nov-2026",'
    '"netValue":"5736.55","sellValue":"12603.02"}]'
)


def test_parse_fii_dii() -> None:
    rows = parse_fii_dii(BODY)
    assert [(r.category, r.trade_date) for r in rows] == [
        ("DII", date(2026, 11, 13)),
        ("FII/FPI", date(2026, 11, 13)),
    ]
    assert rows[1].buy_cr == 18339.57 and rows[0].net_cr == -2253.58
    assert parse_fii_dii("not json") == [] and parse_fii_dii('[{"category":"DII"}]') == []


def test_fii_dii_saved_nightly_under_its_date(tmp_path: Path) -> None:
    assert "fii_dii" in CURRENT_EOD_URLS
    now = datetime(2026, 11, 15, 20, 0, tzinfo=IST)
    res = fetch_current_eod("fii_dii", raw_dir=tmp_path, now=now, fetcher=lambda u: BODY.encode())
    assert res.status == "ok" and res.day == date(2026, 11, 13)
    assert res.path == tmp_path / "eod" / "fii_dii" / "2026" / "2026-11-13.json"
    assert latest_file(tmp_path, "fii_dii") == res.path
    bad = fetch_current_eod("fii_dii", raw_dir=tmp_path, now=now, fetcher=lambda u: b"<html>")
    assert bad.status == "missing"
