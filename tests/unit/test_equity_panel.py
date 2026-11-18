"""Equity data foundation: delivery (MTO), ban-list archive, symbol changes, daily panel."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from engine.data.eod.bhavcopy import parse_cash_bhavcopy_csv
from engine.data.eod.download import eod_path, eod_url
from engine.data.equity.corporate_actions import (
    CorpAction,
    parse_corporate_actions,
    parse_subject,
    rights_factor,
)
from engine.data.equity.panel import build_panel, price_index
from engine.data.equity.sources import (
    SymbolChange,
    canonical_symbol,
    fo_stock_symbols,
    parse_mto,
    parse_secban,
    parse_symbol_changes,
)

MTO = (
    "Security Wise Delivery Position - Compulsory Rolling Settlement\n"
    "10,MTO,03012018,714273995,0001637\n"
    "Trade Date <03-JAN-2018>,Settlement Type <N>,Settlement No <2018003>\n"
    "Record Type,Sr No,Name of Security,Quantity Traded,Deliverable Quantity(gross across "
    "client level),% of Deliverable Quantity to Traded Quantity\n"
    "20,1,ACME,EQ,20000,9000,45.00\n"
    "20,2,ACME,N1,50,50,100.00\n"
    "20,3,BETA,BE,1000,1000,100.00\n"
)


def test_mto_delivery_file_parses() -> None:
    rows = parse_mto(MTO)
    assert [(r.symbol, r.series) for r in rows] == [("ACME", "EQ"), ("ACME", "N1"), ("BETA", "BE")]
    acme = rows[0]
    assert acme.trade_date == date(2018, 1, 3)
    assert (acme.traded_qty, acme.deliverable_qty, acme.deliverable_pct) == (20000, 9000, 45.0)


def test_mto_and_ban_archive_urls() -> None:
    d = date(2018, 1, 3)
    assert eod_url("mto", d) == (
        "https://nsearchives.nseindia.com/archives/equities/mto/MTO_03012018.DAT"
    )
    assert eod_url("secban", d) == (
        "https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_03012018.csv"
    )
    assert eod_path(Path("raw"), "mto", d) == Path("raw/eod/mto/2018/2018-01-03.csv")


def test_secban_archive_parses() -> None:
    assert parse_secban("Securities in Ban For Trade Date 01-JAN-2015: NIL\n") == (
        date(2015, 1, 1),
        [],
    )
    assert parse_secban("Securities in Ban For Trade Date 03-JAN-2018: \n1,ACME\n2,BETA\n") == (
        date(2018, 1, 3),
        ["ACME", "BETA"],
    )


def test_symbol_changes_chain_by_date_and_allow_reuse() -> None:
    text = (
        "NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\n"
        "Acme Ltd,ACME,ACMENEW,05-JAN-2018\n"
        "Acme Ltd,ACMENEW,ACMEX,01-MAR-2020\n"
        " Bad row without date,FOO,BAR,\n"
    )
    changes = parse_symbol_changes(text)
    assert changes == [
        SymbolChange("ACME", "ACMENEW", date(2018, 1, 5)),
        SymbolChange("ACMENEW", "ACMEX", date(2020, 3, 1)),
    ]
    assert canonical_symbol("ACME", date(2018, 1, 3), changes) == "ACMEX"
    assert canonical_symbol("ACMENEW", date(2019, 1, 1), changes) == "ACMEX"
    assert canonical_symbol("ACMEX", date(2021, 1, 1), changes) == "ACMEX"
    # "ACME" traded again after the change is a different company that reused the symbol.
    assert canonical_symbol("ACME", date(2022, 1, 1), changes) == "ACME"


def test_fo_stock_symbols_reads_both_layouts() -> None:
    old = (
        "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,"
        "CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,\n"
        "FUTIDX,NIFTY,25-Jan-2018,0,XX,1,1,1,1,1,1,1,1,0,03-JAN-2018,\n"
        "FUTSTK,ACME,25-Jan-2018,0,XX,1,1,1,1,1,1,1,1,0,03-JAN-2018,\n"
        "OPTSTK,BETA,25-Jan-2018,10,CE,1,1,1,1,1,1,1,1,0,03-JAN-2018,\n"
    )
    assert fo_stock_symbols(old) == {"ACME"}
    new = "TradDt,FinInstrmTp,TckrSymb\n2026-11-05,STF,ACME\n2026-11-05,IDF,NIFTY\n"
    assert fo_stock_symbols(new) == {"ACME"}


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.csv", text)
    return buf.getvalue()


def _put(raw: Path, dataset: str, day: date, text: str) -> None:
    p = eod_path(raw, dataset, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_zip(text) if p.name.endswith(".zip") else text.encode())


OLD_CM_HEAD = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,"
    "TOTALTRADES,ISIN,\n"
)
NEW_CM_HEAD = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
)


def test_corporate_action_subjects_parse() -> None:
    assert parse_subject("Bonus 1:1") == [("bonus", pytest.approx(0.5))]
    assert parse_subject("Bonus 2:5") == [("bonus", pytest.approx(5 / 7))]
    split = "Face Valus Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"
    assert parse_subject(split) == [("split", pytest.approx(0.2))]
    assert parse_subject("Consolidation Of Shares From Rs 1/- Per Share To Rs 10/- Per Share") == [
        ("split", pytest.approx(10.0))
    ]
    combo = "Annual General Meeting/ Dividend - Rs 29.50/- Per Share And Bonus 1:1"
    assert parse_subject(combo) == [("dividend", 29.5), ("bonus", pytest.approx(0.5))]
    assert parse_subject(
        "Interim Dividend - Rs 5 Per Share And Special Dividend - Rs 10/- Per Sh"
    ) == [("dividend", 15.0)]
    assert parse_subject("Rights 14:19 @ Premium Rs 5/- Per Share") == [("rights", (14, 19, 5.0))]
    assert parse_subject("Demerger") == [("demerger", None)]
    assert parse_subject("Annual General Meeting") == []


def test_rights_factor_uses_last_close() -> None:
    # 1 new share for every 4 held at FV 10 + premium 40 = 50, cum price 100:
    # TERP = (4*100 + 1*50) / 5 = 90, factor 0.9.
    assert rights_factor(1, 4, premium=40.0, face_value=10.0, cum_price=100.0) == pytest.approx(0.9)
    assert rights_factor(1, 4, premium=40.0, face_value=10.0, cum_price=0.0) is None


def test_corporate_actions_json_parses() -> None:
    text = (
        '[{"symbol":"ACME","series":"EQ","isin":"INE000A01011","faceVal":"10",'
        '"exDate":"04-Jan-2018","subject":"Bonus 1:1"},'
        '{"symbol":"ACME","series":"EQ","isin":"INE000A01011","faceVal":"10",'
        '"exDate":"-","subject":"Bonus 1:1"}]'
    )
    acts = parse_corporate_actions(text)
    assert acts == [CorpAction("ACME", "EQ", "INE000A01011", date(2018, 1, 4), "Bonus 1:1", 10.0)]


def test_panel_joins_delivery_fo_ban_and_adjusts_corporate_actions(tmp_path: Path) -> None:
    d1, d2, d3 = date(2018, 1, 3), date(2018, 1, 4), date(2024, 1, 2)
    _put(tmp_path, "cm", d1, OLD_CM_HEAD
         + "ACME,EQ,99,101,98,100,100,98,20000,2000000,03-JAN-2018,500,INE000A01011,\n"
         + "ACMEBOND,N3,1,1,1,1,1,1,1,1,03-JAN-2018,1,INE000A07011,\n")  # fmt: skip
    # 1:1 bonus ex 2018-01-04 plus a ₹1 dividend. NSE's PREVCLOSE stays unadjusted (100), as
    # in its real files; the adjustment comes from the corporate-actions list.
    _put(tmp_path, "cm", d2, OLD_CM_HEAD
         + "ACME,EQ,50,53,49,52,52,100,40000,2080000,04-JAN-2018,800,INE000A01011,\n")  # fmt: skip
    # Renamed to ACMENEW (effective 2018-01-05); UDiFF layout by 2024.
    _put(tmp_path, "cm", d3, NEW_CM_HEAD
         + "2024-01-02,2024-01-02,CM,NSE,STK,1,INE000A01011,ACMENEW,EQ,,,,,ACME LTD,52,54,51,53,"
         "53,52,,53,,,30000,1590000,600,F1,1,,,,,\n")  # fmt: skip
    _put(tmp_path, "mto", d1, MTO)
    _put(tmp_path, "fo", d1, "INSTRUMENT,SYMBOL,EXPIRY_DT\nFUTSTK,ACME,25-Jan-2018\n")
    _put(tmp_path, "fo", d2, "INSTRUMENT,SYMBOL,EXPIRY_DT\nFUTSTK,OTHER,25-Jan-2018\n")
    _put(tmp_path, "secban", d2, "Securities in Ban For Trade Date 04-JAN-2018: \n1,ACME\n")
    changes = [SymbolChange("ACME", "ACMENEW", date(2018, 1, 5))]
    acts = [
        CorpAction("ACME", "EQ", "INE000A01011", d2, "Dividend - Rs 1/- Per Share And Bonus 1:1",
                   10.0),
    ]  # fmt: skip

    rows = list(build_panel(tmp_path, [d1, d2, date(2018, 1, 5), d3], changes, acts))
    assert [(r["symbol"], r["canonical"], r["trade_date"]) for r in rows] == [
        ("ACME", "ACMENEW", d1),
        ("ACME", "ACMENEW", d2),
        ("ACMENEW", "ACMENEW", d3),
    ]
    a, b, c = rows
    assert a["delivery_qty"] == 9000 and a["delivery_pct"] == 45.0
    assert b["delivery_qty"] is None  # no MTO file that day: unknown, not zero
    assert (a["in_fo"], b["in_fo"], c["in_fo"]) == (True, False, None)  # no fo file on d3
    assert (a["fo_banned"], b["fo_banned"], c["fo_banned"]) == (None, True, None)
    # First row: no earlier close of ours, so the return falls back to NSE's previous close.
    assert a["ret"] == pytest.approx(100 / 98 - 1) and a["ca_factor"] == 1.0
    assert a["ca_event"] is False and a["dividend"] == 0.0
    assert b["ca_factor"] == pytest.approx(0.5) and b["ca_event"] is True
    assert b["ret"] == pytest.approx(52 / 50 - 1)  # not -48%
    assert b["ret_total"] == pytest.approx((52 + 1) / 50 - 1) and b["dividend"] == 1.0
    assert b["gap_flag"] is False
    assert c["ca_factor"] == 1.0 and c["ret"] == pytest.approx(53 / 52 - 1)
    assert c["isin"] == "INE000A01011" and c["series"] == "EQ"
    assert price_index([r["ret"] for r in rows]) == pytest.approx([1.0, 1.04, 1.04 * 53 / 52])


def test_unexplained_gap_is_flagged(tmp_path: Path) -> None:
    d1, d2 = date(2018, 1, 3), date(2018, 1, 4)
    _put(tmp_path, "cm", d1, OLD_CM_HEAD + "ACME,EQ,1,1,1,100,1,100,1,1,03-JAN-2018,1,X,\n")
    _put(tmp_path, "cm", d2, OLD_CM_HEAD + "ACME,EQ,1,1,1,45,1,100,1,1,04-JAN-2018,1,X,\n")
    rows = list(build_panel(tmp_path, [d1, d2], [], []))
    assert rows[1]["ret"] == pytest.approx(-0.55) and rows[1]["gap_flag"] is True


def test_mto_misspelled_header_and_unreadable_side_files(tmp_path: Path) -> None:
    odd = MTO.replace("Trade Date <03-JAN-2018>", "rade Date <03-JAN-2018>")
    assert parse_mto(odd)[0].trade_date == date(2018, 1, 3)  # date from the "10,MTO" record
    d = date(2018, 1, 3)
    _put(tmp_path, "cm", d, OLD_CM_HEAD + "ACME,EQ,1,1,1,100,1,98,10,1000,03-JAN-2018,1,X,\n")
    _put(tmp_path, "mto", d, "garbage without any date\n")
    _put(tmp_path, "secban", d, "garbage\n")
    rows = list(build_panel(tmp_path, [d], [], []))
    assert rows[0]["delivery_qty"] is None and rows[0]["fo_banned"] is None


def test_ban_file_with_full_month_name() -> None:
    assert parse_secban("Securities in Ban For Trade Date 01-JUNE-2017:\n1,ACME\n") == (
        date(2017, 6, 1),
        ["ACME"],
    )


def test_cm_bhavcopy_two_digit_year_date_parses() -> None:
    rows = parse_cash_bhavcopy_csv(
        OLD_CM_HEAD + "ACME,EQ,10,12,9,11,11,10,1000,11000,13-Jul-20,50,INE000A01011\n"
    )
    assert len(rows) == 1
    assert rows[0].trade_date == date(2020, 7, 13)
    assert rows[0].symbol == "ACME"


# ------------------------------------------------- missed actions (fix of 18 Nov 2026)


def test_split_subject_without_from_parses() -> None:
    assert parse_subject("Face Value Split Rs 10 To Rs 1") == [("split", pytest.approx(0.1))]
    assert parse_subject("Face Value Split From Rs 10 To Rs 1") == [("split", pytest.approx(0.1))]


def _two_days(raw: Path, row1: str, row2: str) -> tuple[date, date]:
    d1, d2 = date(2018, 1, 3), date(2018, 1, 4)
    _put(raw, "cm", d1, OLD_CM_HEAD + row1)
    _put(raw, "cm", d2, OLD_CM_HEAD + row2)
    return d1, d2


def test_action_listed_under_the_current_symbol_and_new_isin_is_applied(tmp_path: Path) -> None:
    # NSE's list names Cadila's 2015 split under ZYDUSLIFE with the post-split ISIN.
    d1, d2 = _two_days(
        tmp_path,
        "OLDSYM,EQ,2080,2090,2070,2081.2,1,2070,1,1,03-JAN-2018,1,INE000A01019,\n",
        "OLDSYM,EQ,418.5,430,415,425.65,1,2081.2,1,1,04-JAN-2018,1,INE000A01019,\n",
    )
    changes = [SymbolChange("OLDSYM", "NEWSYM", date(2022, 1, 1))]
    acts = [
        CorpAction("NEWSYM", "EQ", "INE000A01027", d2,
                   "Face Value Split (Sub-Division) - From Rs 5/- Per Share To Re 1/- Per Share", 1.0),
    ]  # fmt: skip
    rows = list(build_panel(tmp_path, [d1, d2], changes, acts))
    assert rows[1]["ca_factor"] == pytest.approx(0.2)
    assert rows[1]["ret"] == pytest.approx(425.65 / (2081.2 * 0.2) - 1)
    assert rows[1]["gap_flag"] is False


def test_demerger_is_adjusted_from_the_ex_date_open(tmp_path: Path) -> None:
    # Century Textiles' ABREL demerger, ex 11 Oct 2019: special pre-open discovered 390.
    d1, d2 = _two_days(
        tmp_path,
        "CTX,EQ,880,885,875,882.2,1,880,1,1,03-JAN-2018,1,INE000B01011,\n",
        "CTX,EQ,390,400,385,393.85,1,882.2,1,1,04-JAN-2018,1,INE000B01011,\n",
    )
    acts = [CorpAction("CTX", "EQ", "INE000B01011", d2, "Demerger", 10.0)]
    b = list(build_panel(tmp_path, [d1, d2], [], acts))[1]
    assert b["ca_factor"] == pytest.approx(390 / 882.2)
    assert b["ret"] == pytest.approx(393.85 / 390 - 1)
    assert b["ca_estimated"] is True and b["ca_unknown"] is False and b["gap_flag"] is False


def test_scheme_of_arrangement_uses_the_open_only_on_a_real_gap(tmp_path: Path) -> None:
    d1, d2 = _two_days(
        tmp_path,
        "SCH,EQ,100,101,99,100,1,100,1,1,03-JAN-2018,1,INE000C01011,\n",
        "SCH,EQ,99,101,98,100.5,1,100,1,1,04-JAN-2018,1,INE000C01011,\n",
    )
    acts = [CorpAction("SCH", "EQ", "INE000C01011", d2, "Scheme Of Arrangement", 10.0)]
    b = list(build_panel(tmp_path, [d1, d2], [], acts))[1]
    assert b["ca_factor"] == 1.0 and b["ca_unknown"] is True and b["ca_estimated"] is False
    raw2 = tmp_path / "two"
    d1, d2 = _two_days(
        raw2,
        "SCH,EQ,100,101,99,100,1,100,1,1,03-JAN-2018,1,INE000C01011,\n",
        "SCH,EQ,70,72,69,71,1,100,1,1,04-JAN-2018,1,INE000C01011,\n",
    )
    b = list(build_panel(raw2, [d1, d2], [], acts))[1]
    assert b["ca_factor"] == pytest.approx(0.70) and b["ca_estimated"] is True


def test_unlisted_etf_unit_split_is_inferred_only_for_fund_units(tmp_path: Path) -> None:
    # NIFTYBEES 1:10 on 19 Dec 2019 is missing from NSE's equities list.
    d1, d2 = _two_days(
        tmp_path,
        "ETF,EQ,1290,1295,1288,1292.54,1,1290,1,1,03-JAN-2018,1,INF732E01011,\n"
        "STK,EQ,1290,1295,1288,1292.54,1,1290,1,1,03-JAN-2018,1,INE000D01011,\n"
        "ODD,EQ,3800,3810,3790,3810,1,3800,1,1,03-JAN-2018,1,INF754K01EL1,\n",
        "ETF,EQ,129.2,131,128,130.2,1,129.25,1,1,04-JAN-2018,1,INF732E01011,\n"
        "STK,EQ,129.2,131,128,130.2,1,1292.54,1,1,04-JAN-2018,1,INE000D01011,\n"
        "ODD,EQ,1832,2300,1800,2238.74,1,3810,1,1,04-JAN-2018,1,INF754K01EL1,\n",
    )
    rows = {
        r["symbol"]: r for r in build_panel(tmp_path, [d1, d2], [], []) if r["trade_date"] == d2
    }
    etf, stk, odd = rows["ETF"], rows["STK"], rows["ODD"]
    assert etf["ca_factor"] == pytest.approx(0.1) and etf["ca_estimated"] is True
    assert etf["ret"] == pytest.approx(130.2 / 129.254 - 1) and etf["gap_flag"] is False
    assert "inferred" in etf["ca_subject"]
    assert stk["ca_factor"] == 1.0 and stk["gap_flag"] is True  # a company: never inferred
    assert odd["ca_factor"] == 1.0 and odd["gap_flag"] is True  # not a clean ratio


def test_abbreviated_and_variant_subjects_parse() -> None:
    assert parse_subject("Fv Splt Frm Rs 10 To Re 1") == [("split", pytest.approx(0.1))]
    assert parse_subject("Fv Splt Frm Rs 10 To Rs 2") == [("split", pytest.approx(0.2))]
    assert parse_subject("Bonus- 1:2") == [("bonus", pytest.approx(2 / 3))]
    assert parse_subject("Bonus Ncrps 1:116") == []  # preference shares, not equity
    assert parse_subject("Rights 1:2@Prem Rs 40") == [("rights", (1, 2, 40.0))]
    assert parse_subject("Rights 7:10 @ Prm Rs 102/-") == [("rights", (7, 10, 102.0))]
    assert parse_subject("Rights Issue 4:17@ Premium Rs 390/-") == [("rights", (4, 17, 390.0))]
    assert parse_subject("Rights : 24:10 At Par") == [("rights", (24, 10, 0.0))]
    assert parse_subject("Rights 87:38 @ Premium Of Rs 2.50 Per Share") == [
        ("rights", (87, 38, 2.5))
    ]
    assert parse_subject("Rights 2:7") == []  # no issue price: no factor


def test_demerger_without_pre_open_discovery_falls_back_to_the_close(tmp_path: Path) -> None:
    # Adani Enterprises, 3 Jun 2015: opened 573.3 on a 637 base, closed 109.75.
    d1, d2 = _two_days(
        tmp_path,
        "ADE,EQ,638,640,630,637,1,633,1,1,03-JAN-2018,1,INE000E01011,\n",
        "ADE,EQ,573.3,580,105,109.75,1,637,1,1,04-JAN-2018,1,INE000E01011,\n",
    )
    acts = [CorpAction("ADE", "EQ", "INE000E01011", d2, "Scheme Of Arrangement", 1.0)]
    b = list(build_panel(tmp_path, [d1, d2], [], acts))[1]
    assert b["ca_factor"] == pytest.approx(109.75 / 637)
    assert b["ret"] == pytest.approx(0.0, abs=1e-12) and b["gap_flag"] is False
    assert b["ca_estimated"] is True and "close" in b["ca_subject"]
