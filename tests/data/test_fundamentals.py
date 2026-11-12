"""Equity pillar: NSE XBRL parsers (saved, trimmed real filings), point-in-time Parquet store,
results dates -> event calendar, the evening delivery watchlist and its live wiring, and the
cockpit Research panel. No network: every download goes through a fake fetcher."""

from __future__ import annotations

import io
import json
import urllib.error
import zipfile
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from engine.cockpit.research import equity
from engine.core.calendar import TradingCalendar
from engine.core.config import load_settings
from engine.core.rules import IST, RuleBook
from engine.data.constituents.table import load_constituent_table
from engine.data.eod.bhavcopy import DeliveryBias, parse_cash_bhavcopy_csv
from engine.data.eod.delivery_watch import (
    build_delivery_watchlist,
    load_delivery_watchlist,
    watchlist_path,
)
from engine.data.eod.download import eod_path
from engine.data.eod.evening import EveningConfig, evening_trade_date, run_evening
from engine.data.events.calendar_file import load_calendar
from engine.data.events.earnings import AUTO_TAG, earnings_rows, merge_calendar
from engine.data.fundamentals.nse import (
    BoardMeeting,
    parse_board_meetings,
    parse_results_list,
    parse_shareholding_list,
    pick_filings,
    results_list_url,
    shareholding_list_url,
)
from engine.data.fundamentals.store import (
    equity_summary,
    refresh_symbol,
    results_as_of,
    shareholding_as_of,
    upsert,
)
from engine.data.fundamentals.xbrl import (
    XbrlError,
    parse_results_xbrl,
    parse_shareholding_xbrl,
)
from engine.data.reference.instrument_master import InstrumentMaster, Kind
from engine.shadow.live import (
    DeliveryWatchConfig,
    ShadowLiveConfig,
    ShadowLiveRunner,
    WatchSpec,
    delivery_group,
    load_shadow_live_config,
    resolve_equities,
    select_watchlist,
)
from tests.data.conftest import DAY, build_master, ins

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "nse"
KNOWN = datetime(2026, 10, 30, 21, 32, 55, tzinfo=IST)


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


# --------------------------------------------------------------------------- XBRL parsers


def test_results_indas_reliance() -> None:
    r = parse_results_xbrl(
        fx("if_indas_reliance_2026q2_consolidated.xml"), known_at=KNOWN, filing_id="207709"
    )
    assert (r.symbol, r.isin, r.schema) == ("RELIANCE", "INE002A01018", "IndAs")
    assert (r.period_start, r.period_end, r.period_months) == (
        date(2026, 7, 1),
        date(2026, 9, 30),
        3,
    )  # the quarter, not the April-September half year in the same file
    assert r.consolidated and r.audited is False
    assert r.revenue_inr == 3_328_610_000_000
    assert r.pat_inr == 295_500_000_000 and r.pat_owners_inr == 255_480_000_000
    assert r.eps_basic == 18.88 and r.eps_diluted == 18.88
    # EBITDA = PBT before exceptional + finance costs + D&A - other income
    assert r.ebitda_inr == 386_130_000_000 + 84_810_000_000 + 153_150_000_000 - 57_930_000_000
    assert r.board_meeting_date == date(2026, 10, 30) and r.known_at == KNOWN


def test_results_banking_nbfc_and_insurer_schemas() -> None:
    bank = parse_results_xbrl(fx("if_banking_hdfcbank_2026q2_consolidated.xml"), known_at=KNOWN)
    assert bank.schema == "Banking" and bank.ebitda_inr is None
    assert bank.revenue_inr == 951_316_300_000  # interest earned
    assert bank.operating_profit_inr == 328_715_400_000  # before provisions
    assert bank.pat_inr == 207_000_300_000 and bank.eps_basic == 13.43
    assert bank.eps_diluted == 13.41
    nbfc = parse_results_xbrl(fx("if_nbfc_bajfinance_2026q2_consolidated.xml"), known_at=KNOWN)
    assert nbfc.schema == "NBFC" and nbfc.ebitda_inr is None
    assert nbfc.revenue_inr == 245_463_700_000
    assert (nbfc.pat_inr, nbfc.pat_owners_inr) == (63_665_700_000, 62_662_000_000)
    assert (nbfc.eps_basic, nbfc.eps_diluted) == (10.07, 10.05)
    li = parse_results_xbrl(fx("if_li_hdfclife_2026q2_consolidated.xml"), known_at=KNOWN)
    assert li.schema == "LI" and li.revenue_inr == 218_557_900_000  # net premium
    assert (li.pbt_inr, li.pat_inr, li.eps_basic) == (5_405_000_000, 5_216_000_000, 2.41)


def test_results_parser_guards() -> None:
    with pytest.raises(ValueError, match="timezone"):
        parse_results_xbrl(
            fx("if_indas_reliance_2026q2_consolidated.xml"), known_at=datetime(2026, 10, 30)
        )  # noqa: DTZ001, E501
    with pytest.raises(XbrlError, match="DTD"):
        parse_results_xbrl(
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>', known_at=KNOWN
        )
    with pytest.raises(XbrlError, match="not XML|not an XBRL"):
        parse_results_xbrl(b"<html>blocked</html>", known_at=KNOWN)


def test_shareholding_pattern_reliance() -> None:
    known = datetime(2026, 10, 21, 16, 25, 15, tzinfo=IST)
    s = parse_shareholding_xbrl(fx("shp_reliance_2026-09-30.xml"), known_at=known)
    assert (s.symbol, s.as_on) == ("RELIANCE", date(2026, 9, 30))
    assert s.promoter_pct == pytest.approx(50.44)
    assert s.public_pct == pytest.approx(49.56)
    assert s.fii_pct == pytest.approx(16.67) and s.fpi_cat1_pct == pytest.approx(15.98)
    assert s.dii_pct == pytest.approx(21.72) and s.mf_pct == pytest.approx(10.31)
    assert s.insurance_pct == pytest.approx(9.47) and s.retail_pct == pytest.approx(7.24)
    assert s.total_shares == 13_532_538_722 and s.shareholders == 5_072_457


# --------------------------------------------------------------------------- NSE lists


def test_results_list_and_pick_consolidated() -> None:
    refs = parse_results_list(fx("integrated_filing_reliance.json").decode())
    assert len(refs) == 6 and all(r.xbrl_url.startswith("https://nsearchives") for r in refs)
    first = refs[0]
    assert first.known_at == KNOWN and first.consolidated is True
    assert first.period_end == date(2026, 9, 30) and first.filing_id == "207709"
    picked = pick_filings(refs, 2)
    assert [(p.period_end, p.consolidated) for p in picked] == [
        (date(2026, 9, 30), True),
        (date(2026, 6, 30), True),
    ]


def test_lists_skip_links_off_the_nse_archive() -> None:
    row = json.loads(fx("integrated_filing_reliance.json"))
    row["data"][0]["xbrl"] = "https://example.com/evil.xml"
    assert len(parse_results_list(json.dumps(row))) == 5


def test_shareholding_list() -> None:
    refs = parse_shareholding_list(fx("shp_master_reliance.json").decode(), "RELIANCE")
    assert [r.period_end for r in refs] == [date(2026, 9, 30), date(2026, 6, 30), date(2026, 3, 31)]
    assert refs[0].known_at == datetime(2026, 10, 21, 16, 25, 15, tzinfo=IST)
    assert refs[0].consolidated is None and refs[0].revision == "Original"


# --------------------------------------------------------------------------- store (point in time)


def _rel() -> object:
    return parse_results_xbrl(fx("if_indas_reliance_2026q2_consolidated.xml"), known_at=KNOWN)


def test_store_is_point_in_time_and_computes_growth(tmp_path: Path) -> None:
    cur = parse_results_xbrl(
        fx("if_indas_reliance_2026q2_consolidated.xml"), known_at=KNOWN, filing_id="q2"
    )
    prev = replace(
        cur,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        revenue_inr=cur.revenue_inr / 1.1,  # type: ignore[operator]
        known_at=datetime(2026, 7, 17, 19, 50, tzinfo=IST),
        filing_id="q1",
    )
    yoy = replace(
        cur,
        period_start=date(2025, 7, 1),
        period_end=date(2025, 9, 30),
        revenue_inr=cur.revenue_inr / 1.25,  # type: ignore[operator]
        pat_inr=cur.pat_inr / 2,  # type: ignore[operator]
        known_at=datetime(2025, 10, 17, 19, 36, tzinfo=IST),
        filing_id="q2-ly",
    )
    standalone = replace(cur, consolidated=False, revenue_inr=1.0, filing_id="q2-sa")
    upsert(tmp_path / "results.parquet", [cur, prev, yoy, standalone])
    before = datetime(2026, 10, 30, 21, 0, tzinfo=IST)  # 33 minutes before the Q2 filing
    rows = results_as_of(tmp_path, "RELIANCE", before)
    assert rows[0]["period_end"] == date(2026, 6, 30)
    s = equity_summary(tmp_path, "RELIANCE", datetime(2026, 11, 12, tzinfo=IST))
    r = s["results"]
    assert r["period_end"] == "2026-09-30" and r["consolidated"] is True  # not standalone
    assert r["revenue_yoy_pct"] == pytest.approx(25.0)
    assert r["revenue_qoq_pct"] == pytest.approx(10.0)
    assert r["pat_yoy_pct"] == pytest.approx(100.0)
    assert r["revenue_cr"] == pytest.approx(332_861.0)
    # a revision supersedes only from its own known_at
    revised = replace(cur, revenue_inr=1e12, known_at=KNOWN + timedelta(days=3), filing_id="q2r")
    upsert(tmp_path / "results.parquet", [revised])
    assert results_as_of(tmp_path, "RELIANCE", KNOWN + timedelta(days=1))[0]["revenue_inr"] == (
        cur.revenue_inr
    )
    assert results_as_of(tmp_path, "RELIANCE", KNOWN + timedelta(days=4))[0]["revenue_inr"] == 1e12


def test_holding_changes(tmp_path: Path) -> None:
    known = datetime(2026, 10, 21, 16, 25, tzinfo=IST)
    cur = parse_shareholding_xbrl(fx("shp_reliance_2026-09-30.xml"), known_at=known, filing_id="a")
    prev = replace(
        cur,
        as_on=date(2026, 6, 30),
        promoter_pct=50.48,
        fii_pct=17.0,
        known_at=datetime(2026, 7, 16, 19, 24, tzinfo=IST),
        filing_id="b",
    )
    upsert(tmp_path / "shareholding.parquet", [cur, prev])
    assert len(shareholding_as_of(tmp_path, "RELIANCE", known - timedelta(minutes=1))) == 1
    h = equity_summary(tmp_path, "RELIANCE", known)["holding"]
    assert h["promoter_pct_chg"] == pytest.approx(-0.04)
    assert h["fii_pct_chg"] == pytest.approx(-0.33)
    assert h["prev_as_on"] == "2026-06-30"


class FakeNse:
    """Serves saved fixtures by URL and counts requests."""

    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        for key, body in self.routes.items():
            if key in url:
                return body
        raise urllib.error.HTTPError(url, 404, "not found", None, None)  # type: ignore[arg-type]


def test_refresh_symbol_downloads_once_and_writes_parquet(tmp_path: Path) -> None:
    fake = FakeNse(
        {
            results_list_url("RELIANCE"): fx("integrated_filing_reliance.json"),
            shareholding_list_url("RELIANCE"): fx("shp_master_reliance.json"),
            "INTEGRATED_FILING_INDAS": fx("if_indas_reliance_2026q2_consolidated.xml"),
            "SHP_": fx("shp_reliance_2026-09-30.xml"),
        }
    )
    now = datetime(2026, 11, 12, 20, 0, tzinfo=IST)
    raw, pq_dir = tmp_path / "raw", tmp_path / "parquet"
    rep = refresh_symbol(
        "RELIANCE", raw_dir=raw, parquet_dir=pq_dir, now=now, n_periods=1, fetcher=fake,
        sleep=lambda s: None,
    )  # fmt: skip
    assert (rep.results_new, rep.shareholding_new, rep.errors) == (1, 1, [])
    assert len(fake.calls) == 4
    meta = json.loads(
        next((raw / "fundamentals" / "xbrl" / "results").rglob("*.meta.json")).read_text()
    )
    assert meta["known_at"] == KNOWN.isoformat() and meta["filing_id"] == "207709"
    s = equity_summary(pq_dir, "RELIANCE", now)
    assert s["results"]["period_end"] == "2026-09-30" and s["holding"]["promoter_pct"] == 50.44
    rep2 = refresh_symbol(
        "RELIANCE", raw_dir=raw, parquet_dir=pq_dir, now=now, n_periods=1, fetcher=fake,
        sleep=lambda s: None,
    )  # fmt: skip
    assert (rep2.results_new, rep2.shareholding_new) == (0, 0) and len(fake.calls) == 6


def test_refresh_reports_a_blocked_endpoint(tmp_path: Path) -> None:
    fake = FakeNse({"nseindia.com/api": b"<html>Access Denied</html>"})
    rep = refresh_symbol(
        "RELIANCE", raw_dir=tmp_path, parquet_dir=tmp_path, now=KNOWN, fetcher=fake,
        sleep=lambda s: None,
    )  # fmt: skip
    assert len(rep.errors) == 2 and all("not JSON" in e for e in rep.errors)


# --------------------------------------------------------------------------- results dates


def _meetings() -> list[BoardMeeting]:
    return parse_board_meetings(fx("board_meetings_2026-11-10_to_30.json").decode())


def test_board_meetings_to_calendar_rows(repo_root: Path) -> None:
    ms = _meetings()
    # BAJFINANCE's 10 Nov meeting was not about results; GVPTECH is not an index member
    assert {m.symbol for m in ms if m.is_results} == {"EICHERMOT", "GRASIM", "TMPV", "GVPTECH"}
    cons = load_constituent_table(repo_root / "config" / "reference" / "nifty50_constituents.csv")
    rows = earnings_rows(ms, cons, index="NIFTY", on=date(2026, 11, 12))
    assert [(r["symbol"], r["date"]) for r in rows] == [
        ("EICHERMOT", "2026-11-13"),
        ("GRASIM", "2026-11-13"),
        ("TMPV", "2026-11-13"),
    ]
    assert rows[0]["category"] == "CONSTITUENT_EARNINGS" and AUTO_TAG in rows[0]["notes"]
    cons_nw = [replace(c, weight_pct=None) for c in cons]  # weights not loaded
    nw = earnings_rows(ms, cons_nw, index="NIFTY", on=date(2026, 11, 12))
    assert nw[0]["index_weight_pct"] == "" and "not loaded" in nw[0]["notes"]
    assert "06 Nov 2026 12:18" in rows[0]["notes"]  # when NSE disseminated the intimation
    # a constituent with a weight carries it, flagged by its status (independent of the file)
    ms2 = [replace(ms[1], symbol="HDFCBANK")]
    for status, verified, flag in (
        ("UNVERIFIED", False, "UNVERIFIED"),
        ("derived", False, "derived from free-float"),
        ("", True, None),
    ):
        cons2 = [
            replace(c, weight_pct=11.8, weight_status=status, weight_verified=verified)
            if c.symbol == "HDFCBANK"
            else c
            for c in cons
        ]
        hdfc = earnings_rows(ms2, cons2, index="NIFTY", on=date(2026, 11, 12))[0]
        assert hdfc["index_weight_pct"] == "11.8"
        assert (flag in hdfc["notes"]) if flag else ("UNVERIFIED" not in hdfc["notes"])


def _calendar_copy(repo_root: Path, dst: Path) -> Path:
    """The repo calendar without rows a previous evening run added (tests start clean)."""
    src = (repo_root / "config" / "events" / "macro_calendar.csv").read_text(encoding="utf-8")
    kept = [line for line in src.splitlines() if AUTO_TAG not in line]
    dst.write_text("\n".join(kept) + "\n", "utf-8")
    return dst


def test_merge_calendar_is_idempotent_and_respects_manual_rows(
    repo_root: Path, tmp_path: Path
) -> None:
    cal = _calendar_copy(repo_root, tmp_path / "macro_calendar.csv")
    raw_before = cal.read_text(encoding="utf-8")
    before = load_calendar(cal)
    cons = load_constituent_table(repo_root / "config" / "reference" / "nifty50_constituents.csv")
    today = date(2026, 11, 12)
    rows = earnings_rows(_meetings(), cons, index="NIFTY", on=today)
    assert merge_calendar(cal, rows, today=today) == 3
    after = load_calendar(cal)  # still loads (format and status rules hold)
    assert len(after) == len(before) + 3
    kept = [ln for ln in cal.read_text(encoding="utf-8").splitlines() if AUTO_TAG not in ln]
    assert kept == raw_before.splitlines()  # hand-written lines untouched, byte for byte
    assert merge_calendar(cal, rows, today=today) == 3 and len(load_calendar(cal)) == len(after)
    # postponed: NSE intimates a new date later -> the future auto row moves
    moved = [
        replace(m, meeting_date=date(2026, 11, 20), known_at=m.known_at + timedelta(days=2))
        for m in _meetings()
        if m.symbol == "EICHERMOT"
    ]
    rows2 = earnings_rows([*_meetings(), *moved], cons, index="NIFTY", on=today)
    merge_calendar(cal, rows2, today=today)
    eicher = [c for c in load_calendar(cal) if c.event.symbol == "EICHERMOT"]
    assert [c.event.start_ts.date() for c in eicher] == [date(2026, 11, 20)]
    # past auto rows are kept on the next day's refresh
    merge_calendar(cal, [], today=date(2026, 11, 14))
    assert {
        c.event.symbol for c in load_calendar(cal) if c.event.category == "CONSTITUENT_EARNINGS"
    } >= {"GRASIM", "TMPV"}  # noqa: E501


def test_manual_row_wins_over_auto(tmp_path: Path, repo_root: Path) -> None:
    cal = tmp_path / "c.csv"
    cal.write_text(
        "date,time_ist,category,title,symbol,index_weight_pct,duration_min,pre_blackout_min,"
        "post_blackout_min,source_url,verified_on,status,notes\n"
        "2026-11-13,15:30,CONSTITUENT_EARNINGS,Grasim results,GRASIM,1.0,30,15,30,"
        "https://example.org/x,2026-11-12,primary,entered by hand\n",
        encoding="utf-8",
    )
    cons = load_constituent_table(repo_root / "config" / "reference" / "nifty50_constituents.csv")
    rows = earnings_rows(_meetings(), cons, index="NIFTY", on=date(2026, 11, 12))
    assert merge_calendar(cal, rows, today=date(2026, 11, 12)) == 2
    grasim = [c for c in load_calendar(cal) if c.event.symbol == "GRASIM"]
    assert len(grasim) == 1 and grasim[0].notes == "entered by hand"


# --------------------------------------------------------------------------- delivery watchlist

T = date(2026, 11, 11)
HEADER = (
    "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
    "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER"
)
FNO = ("ACCUM", "CHURN", "BANNED", "FLAT", "THIN")  # THIN: spike, but its options barely trade


def _deliv_line(sym: str, d: date, prev: float, close: float, qty: int, dq: int) -> str:
    hi, lo = max(prev, close) * 1.01, min(prev, close) * 0.99
    return (
        f"{sym}, EQ, {d:%d-%b-%Y}, {prev:.2f}, {prev:.2f}, {hi:.2f}, {lo:.2f}, {close:.2f}, "
        f"{close:.2f}, {close:.2f}, {qty}, {qty * close / 1e5:.2f}, 100, {dq}, {dq / qty * 100:.2f}"
    )


def delivery_csv(d: date) -> str:
    lines = [HEADER]
    for sym in (*FNO, "NOFNO"):
        prev, close, qty, dq = 100.0, 100.0, 1_000_000, 300_000
        if d == T and sym in ("ACCUM", "BANNED", "NOFNO", "THIN"):
            close, qty, dq = 102.0, 1_500_000, 900_000  # 3x delivery, 60% vs 30%, +2%
        if d == T and sym == "CHURN":
            close, qty, dq = 107.0, 3_000_000, 300_000  # +7% on 10% delivery
        lines.append(_deliv_line(sym, d, prev, close, qty, dq))
    return "\n".join(lines) + "\n"


def fo_zip(d: date) -> bytes:
    head = "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,StrkPric,OptnTp,OpnPric,HghPric,LwPric,ClsPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,UndrlygPric,NewBrdLotQty"  # noqa: E501
    rows = [
        f"{d},{d},FO,NSE,STF,1,,{s},,2026-11-24,,,100,101,99,100,100,10,0,5,100,500" for s in FNO
    ]
    # Stock options, expiry 13 days out, strikes 90..110 around an underlying of 100. Near-ATM
    # (+-2 strikes, calls + puts = 10 rows): 600 contracts each, OI 100,000 shares = 200 contracts.
    # An expiry 2 days out is busier but too close (physical settlement) and must be ignored.
    for s_ in FNO:
        vol = 10 if s_ == "THIN" else 600
        for k in range(90, 115, 5):
            for t in ("CE", "PE"):
                rows.append(
                    f"{d},{d},FO,NSE,STO,2,,{s_},,2026-11-24,{k},{t},1,1,1,1,1,100000,0,{vol},100,500"
                )  # noqa: E501
                rows.append(
                    f"{d},{d},FO,NSE,STO,3,,{s_},,2026-11-13,{k},{t},1,1,1,1,1,900000,0,9000,100,500"
                )  # noqa: E501
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fo.csv", "\n".join([head, *rows]) + "\n")
    return buf.getvalue()


def _sessions(n: int) -> list[date]:
    out, d = [], T
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _write_raw(raw: Path, days: list[date], *, ban_for: date | None = date(2026, 11, 12)) -> None:
    for d in days:
        p = eod_path(raw, "delivery", d)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(delivery_csv(d), encoding="utf-8")
    fo = eod_path(raw, "fo", T)
    fo.parent.mkdir(parents=True, exist_ok=True)
    fo.write_bytes(fo_zip(T))
    if ban_for:
        ban = raw / "eod" / "fo_ban" / f"{ban_for.year}" / f"{ban_for.isoformat()}.csv"
        ban.parent.mkdir(parents=True, exist_ok=True)
        ban.write_text(f"Securities in Ban For Trade Date {ban_for:%d-%b-%Y}:\n1,BANNED\n", "utf-8")


def test_real_delivery_file_format_parses() -> None:
    rows = parse_cash_bhavcopy_csv((FIX / "sec_bhavdata_full_11112026_sample.csv").read_text())
    rel = next(r for r in rows if r.symbol == "RELIANCE")
    assert rel.trade_date == T and rel.delivery_qty == 9_143_699 and rel.delivery_pct == 52.09
    assert rel.known_at == datetime(2026, 11, 11, 19, 30, tzinfo=IST)


def test_build_watchlist_fno_only_ban_excluded_bias_order(tmp_path: Path) -> None:
    raw, out = tmp_path / "raw", tmp_path / "wl"
    _write_raw(raw, _sessions(12))
    p = build_delivery_watchlist(raw, out, T, now=datetime(2026, 11, 11, 20, tzinfo=IST))
    assert [i["symbol"] for i in p["items"]] == ["ACCUM", "CHURN"]  # NOFNO has no F&O
    assert [i["bias"] for i in p["items"]] == ["LONG_ACCUMULATION", "RETAIL_CHURN_FADE"]
    assert p["excluded_for_ban"] == ["BANNED"] and p["ban_trade_date"] == "2026-11-12"
    assert p["excluded_thin_options"] == ["THIN"]  # 100 contracts < 1000 (HYPOTHESIS)
    assert p["items"][0]["option_expiry"] == "2026-11-24"  # the 2-day expiry is skipped
    assert (
        p["items"][0]["option_contracts"] == 6000 and p["items"][0]["option_oi_contracts"] == 2000
    )
    assert p["thresholds_hypothesis"]["option_liquidity"]["min_contracts"] == 1000
    acc = p["items"][0]
    assert acc["delivery_qty_multiple"] == pytest.approx(3.0)
    assert acc["prev_close"] == 102.0 and acc["prev_high"] > acc["prev_low"] > 0
    assert p["known_at"] == "2026-11-11T19:30:00+05:30"
    assert watchlist_path(out, T).exists()
    one = build_delivery_watchlist(
        raw, out, T, now=datetime(2026, 11, 11, 20, tzinfo=IST), max_symbols=1,
        biases=[DeliveryBias.RETAIL_CHURN_FADE],
    )  # fmt: skip
    assert [i["symbol"] for i in one["items"]] == ["CHURN"]


def test_watchlist_needs_history_and_a_current_ban_list(tmp_path: Path) -> None:
    raw, out = tmp_path / "raw", tmp_path / "wl"
    _write_raw(raw, _sessions(3), ban_for=T)  # stale ban list (applies to T itself)
    p = build_delivery_watchlist(raw, out, T, now=datetime(2026, 11, 11, 20, tzinfo=IST))
    assert p["items"] == [] and "only 3 delivery file" in p["note"]
    assert p["ban_list_saved"] is False


def test_live_reads_watchlist_only_from_the_next_session(tmp_path: Path) -> None:
    raw, out = tmp_path / "raw", tmp_path / "wl"
    _write_raw(raw, _sessions(12))
    build_delivery_watchlist(raw, out, T, now=datetime(2026, 11, 11, 20, tzinfo=IST))
    assert load_delivery_watchlist(out, T) == (None, [])  # never on the day it describes
    day, items = load_delivery_watchlist(out, date(2026, 11, 12))
    assert day == T and [i.symbol for i in items] == ["ACCUM", "CHURN"]
    assert load_delivery_watchlist(out, date(2026, 11, 20), max_age_days=5) == (T, [])


# --------------------------------------------------------------------------- live wiring


def _master_with_equities() -> InstrumentMaster:
    eq = [
        ins("NSE:ACCUM-EQ", Kind.EQUITY, underlying="ACCUM", lot=1),
        ins("NSE:CHURN-EQ", Kind.EQUITY, underlying="CHURN", lot=1),
    ]
    return InstrumentMaster([*build_master().all(), *eq], as_of=DAY, source="fyers")


def test_repo_config_delivery_group_fits_full_d30(repo_root: Path) -> None:
    cfg = load_shadow_live_config(load_settings(root=repo_root, environ={}))
    worst = (
        2 + 1 + sum(w.futures_months + 2 * (2 * w.option_strikes_each_side + 1) for w in cfg.watch)
    )
    assert cfg.delivery_watch.enabled and worst + cfg.delivery_watch.max_symbols <= 50
    dw = cfg.delivery_watch
    assert dw.bias_in_score is False  # watch-only until the event study supports it
    assert dw.spike.min_delivery_qty_mult == 2.0 and dw.option_liquidity.min_contracts == 1000


def test_delivery_group_resolves_and_feeds_scanner_state(tmp_path: Path, repo_root: Path) -> None:
    from engine.core.clock import ManualClock, MarketSession
    from engine.data.adapters.sim import SimFeed
    from engine.data.recorder.run import load_recorder_settings
    from engine.data.schema import DEPTH30_SCHEMA
    from engine.data.store import DatasetWriter
    from engine.shadow.scanner import ShadowOrderBookScanner

    raw, out = tmp_path / "raw", tmp_path / "wl"
    _write_raw(raw, _sessions(12))
    build_delivery_watchlist(raw, out, T, now=datetime(2026, 11, 11, 20, tzinfo=IST))
    master = _master_with_equities()
    assert resolve_equities(master, ["ACCUM", "NOPE"]) == {"ACCUM": "NSE:ACCUM-EQ"}
    dcfg = DeliveryWatchConfig(enabled=True, dir=out, max_symbols=5)
    day = date(2026, 11, 12)
    group, note = delivery_group(dcfg, master, day)
    assert set(group) == {"NSE:ACCUM-EQ", "NSE:CHURN-EQ"} and "2 stock(s)" in note
    assert delivery_group(replace(dcfg, enabled=False), master, day) == ({}, "delivery watch off")
    only_long = delivery_group(replace(dcfg, biases=("LONG_ACCUMULATION",)), master, day)[0]
    assert set(only_long) == {"NSE:ACCUM-EQ"}

    settings = load_settings(root=repo_root, environ={})
    base = load_recorder_settings(settings)
    nifty = replace(base.universe.underlyings[0], spot_symbol="NSE:NIFTY50-INDEX")
    rs = replace(
        base, universe=replace(base.universe, underlyings=(nifty,), vix_symbol="NSE:INDIAVIX-INDEX")
    )  # noqa: E501
    cfg = ShadowLiveConfig(
        watch=(WatchSpec("NIFTY", futures_months=1, option_strikes_each_side=3),),
        max_symbols=12,
    )
    now = datetime(2026, 11, 12, 9, 20, tzinfo=IST)
    wl = select_watchlist(master, cfg, rs, now=now, spots={"NIFTY": 25010.0}, extra=group)
    assert (
        wl[:5]
        == [
            "NSE:NIFTY50-INDEX",
            "NSE:INDIAVIX-INDEX",
            "NSE:NIFTY26NOVFUT",
            "NSE:ACCUM-EQ",
            "NSE:CHURN-EQ",
        ]
        and len(wl) == 12
    )  # stocks take slots ahead of the farthest options

    from datetime import time as _t

    session = MarketSession("NSE", "fo", day, _t(9, 15), _t(15, 40), _t(9, 0), _t(9, 8), True)
    clock = ManualClock(now)
    runner = ShadowLiveRunner(
        cfg,
        rs,
        scanner=ShadowOrderBookScanner(settings.shadow_scanner),
        master=master,
        adapter=SimFeed(master, clock, spots={}),
        clock=clock,
        session=session,
        depth_writer=DatasetWriter(tmp_path, "depth30", DEPTH30_SCHEMA),
        store=None,
        report_path=tmp_path / "d.html",
        ledger_file=tmp_path / "l.jsonl",
        feed_label="SYNTHETIC SIM (not market data)",
        delivery=group,
    )
    runner.start()
    st = runner.scanner.states["NSE:ACCUM-EQ"]
    # Owner: spikes pick what to watch, not what to trade -> no bias in the score by default.
    assert st.delivery_bias is DeliveryBias.NEUTRAL
    runner.cfg = replace(cfg, delivery_watch=DeliveryWatchConfig(bias_in_score=True))
    on = runner._state_for(master.get("NSE:ACCUM-EQ"))
    assert on.delivery_bias is DeliveryBias.LONG_ACCUMULATION
    assert runner._state_for(master.get("NSE:CHURN-EQ")).delivery_bias is (
        DeliveryBias.RETAIL_CHURN_FADE
    )
    assert st.prev_close == 102.0 and st.pdh > st.pdl > 0
    assert st.is_equity and not st.is_option  # costed as intraday cash equity
    assert runner.scanner.cost_segment("NSE:ACCUM-EQ") == "equity_intraday"
    assert runner.scanner.states["NSE:NIFTY26NOVFUT"].delivery_bias is DeliveryBias.NEUTRAL


# --------------------------------------------------------------------------- evening job


def _calendar(repo_root: Path) -> TradingCalendar:
    return TradingCalendar.from_rulebook(RuleBook.load(repo_root / "config" / "rules"))


def test_evening_trade_date(repo_root: Path) -> None:
    cal = _calendar(repo_root)
    assert evening_trade_date(datetime(2026, 11, 11, 19, 45, tzinfo=IST), cal) == T
    assert evening_trade_date(datetime(2026, 11, 12, 6, 0, tzinfo=IST), cal) == T
    assert evening_trade_date(datetime(2026, 11, 14, 10, 0, tzinfo=IST), cal) == date(2026, 11, 13)


def test_evening_job_end_to_end_offline(tmp_path: Path, repo_root: Path) -> None:
    def fetch(url: str) -> bytes:
        if "sec_bhavdata_full_" in url:
            d = datetime.strptime(url.rsplit("_", 1)[1][:8], "%d%m%Y").date()  # noqa: DTZ007
            return delivery_csv(d).encode()
        if "BhavCopy_NSE_FO" in url:
            return fo_zip(T)
        if "fo_secban" in url:
            return b"Securities in Ban For Trade Date 12-NOV-2026:\n1,BANNED\n"
        if url.endswith(("bulk.csv", "block.csv")):
            return b"Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"  # noqa: E501
        if "corporate-board-meetings" in url:
            return fx("board_meetings_2026-11-10_to_30.json")
        if "integrated-filing-results" in url or "share-holdings-master" in url:
            return b'{"data": []}'  # the synthetic stocks have no filings
        raise urllib.error.HTTPError(url, 404, "nf", None, None)  # type: ignore[arg-type]

    cal_file = _calendar_copy(repo_root, tmp_path / "macro_calendar.csv")
    cfg = EveningConfig(
        constituents_file=repo_root / "config" / "reference" / "nifty50_constituents.csv",
        calendar_file=cal_file,
        watchlist_dir=tmp_path / "wl",
        heavyweights_top_n=0,
        pace_s=0,
    )
    rep = run_evening(
        cfg,
        raw_dir=tmp_path / "raw",
        parquet_dir=tmp_path / "pq",
        now=datetime(2026, 11, 11, 20, 0, tzinfo=IST),
        calendar=_calendar(repo_root),
        fetcher=fetch,
        sleep=lambda s: None,
        steps=("eod", "watchlist", "calendar", "fundamentals"),  # weights: test_index_weights
    )
    assert rep.trade_date == T and rep.watchlist == ["ACCUM", "CHURN"], rep.lines
    assert rep.earnings_rows == 3 and rep.errors == 0, rep.lines
    assert rep.fundamentals == ["ACCUM", "CHURN"]  # filings refreshed for the watchlist
    assert eod_path(tmp_path / "raw", "delivery", date(2026, 10, 13)).exists()  # lookback filled


# --------------------------------------------------------------------------- cockpit


def test_research_equity_panel(tmp_path: Path) -> None:
    data = tmp_path / "data"
    assert "No delivery watchlist" in equity(data)["empty"]
    wl = data / "state" / "delivery_watchlist" / "2026-11-11.json"
    wl.parent.mkdir(parents=True)
    wl.write_text(
        json.dumps(
            {
                "trade_date": "2026-11-11",
                "sessions_used": 21,
                "ban_trade_date": "2026-11-12",
                "excluded_for_ban": ["BANNED"],
                "items": [
                    {"symbol": "RELIANCE", "bias": "LONG_ACCUMULATION", "delivery_qty_multiple": 2.4,
                     "delivery_pct": 52.1, "close_return_pct": 1.3},
                    {"symbol": "NOFILINGS", "bias": "RETAIL_CHURN_FADE"},
                ],
            }
        ),
        encoding="utf-8",
    )  # fmt: skip
    pq_dir = data / "parquet" / "fundamentals"
    upsert(
        pq_dir / "results.parquet",
        [
            parse_results_xbrl(
                fx("if_indas_reliance_2026q2_consolidated.xml"), known_at=KNOWN, filing_id="x"
            )
        ],
    )  # noqa: E501
    out = equity(data, now=datetime(2026, 11, 12, 9, 0, tzinfo=IST))
    assert out["trade_date"] == "2026-11-11" and out["excluded_for_ban"] == ["BANNED"]
    rel, other = out["rows"]
    assert rel["symbol"] == "RELIANCE" and rel["results"]["period_end"] == "2026-09-30"
    assert rel["results"]["ebitda_margin_pct"] == pytest.approx(17.0, abs=0.1)
    assert rel["holding"] is None and other["results"] is None
    # nothing filed after "now" leaks in
    early = equity(data, now=datetime(2026, 10, 1, tzinfo=IST))
    assert early["rows"][0]["results"] is None


def test_upstox_stock_keys_can_fall_back_to_full_mode() -> None:
    from engine.data.adapters.upstox import MODE_FULL, MODE_FULL_D30, UpstoxFeed

    d30 = UpstoxFeed(mode=MODE_FULL_D30)
    assert d30.mode_for("NSE_EQ|INE094A01015") == MODE_FULL_D30
    assert d30.mode_for("NSE_INDEX|Nifty 50") == MODE_FULL  # indices: no book in full_d30
    mixed = UpstoxFeed(mode=MODE_FULL_D30, full_mode_prefixes=("NSE_EQ|",))
    assert mixed.mode_for("NSE_EQ|INE094A01015") == MODE_FULL
    assert mixed.mode_for("NSE_FO|54749") == MODE_FULL_D30


def test_results_general_insurer_icici_lombard() -> None:
    gi = parse_results_xbrl(fx("if_gi_icicigi_2026q2_standalone.xml"), known_at=KNOWN)
    assert (gi.symbol, gi.schema, gi.consolidated, gi.audited) == ("ICICIGI", "GI", False, True)
    assert gi.revenue_inr == 66_518_700_000  # net premium earned
    assert gi.pbt_inr == 12_474_100_000 and gi.pat_inr == 9_464_000_000
    assert gi.eps_basic == 18.95 and gi.ebitda_inr is None
    assert gi.period_end == date(2026, 9, 30) and gi.period_months == 3


def test_unknown_schema_shows_not_parsed(tmp_path: Path) -> None:
    from engine.data.fundamentals.store import PARSED_SCHEMAS

    assert {"IndAs", "Banking", "NBFC", "LI", "GI"} <= PARSED_SCHEMAS  # each has a fixture test
    cur = parse_results_xbrl(
        fx("if_indas_reliance_2026q2_consolidated.xml"), known_at=KNOWN, filing_id="u"
    )
    upsert(tmp_path / "results.parquet", [replace(cur, schema="unknown")])
    r = equity_summary(tmp_path, "RELIANCE", datetime(2026, 11, 12, tzinfo=IST))["results"]
    assert r["not_parsed"] is True and r["revenue_cr"] is None and r["pat_cr"] is None


def test_holiday_file_with_previous_days_rows_is_not_a_session(tmp_path: Path) -> None:
    from engine.data.eod.delivery_watch import delivery_history

    raw = tmp_path / "raw"
    _write_raw(raw, _sessions(10))  # 11 Nov back to 29 Oct, weekdays
    holiday = date(2026, 11, 10)  # NSE served 9 Nov's file under 10 Nov's name
    eod_path(raw, "delivery", holiday).write_text(delivery_csv(date(2026, 11, 9)), "utf-8")
    hist = delivery_history(raw, T, sessions=7)
    got = {r.trade_date for rows in hist.values() for r in rows}
    assert len(got) == 7 and min(got) == date(2026, 11, 2)  # looked one file further back
    assert all(r.trade_date != holiday for rows in hist.values() for r in rows)
