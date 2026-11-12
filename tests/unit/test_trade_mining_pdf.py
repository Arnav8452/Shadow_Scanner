"""Word-position parser for reference Trade History PDFs (M1). Synthetic pages only: no real log
content, names or account IDs belong in fixtures."""

from __future__ import annotations

from datetime import date

from engine.trade_mining.diagnostics import diagnostics_markdown, group_positions, underlying_of
from engine.trade_mining.pdf_report import (
    Word,
    _day_net,
    parse_amount,
    parse_trade_history_pages,
)
from engine.trade_mining.validate import validate_parsed_report

COLS = {
    "TIME": 42, "ACCOUNT": 90, "INST": 139, "TYPE": 231, "QTY": 265, "MARGIN": 295,
    "MODALITY": 341, "REQUESTED": 394, "ENTRY": 450, "EXIT": 490, "MFE": 530,
    "GROSS": 578, "CHARGES": 630, "NET": 677, "FLAGS": 728,
}  # fmt: skip


def header(top: float) -> list[Word]:
    first = ["TIME", "MARGIN", "MODALITY", "REQUESTED", "ENTRY", "EXIT", "MFE", "GROSS"]
    second = ["ACCOUNT", "INST", "TYPE", "QTY", "CHARGES", "NET", "FLAGS"]
    ws = [Word(n, COLS[n], top) for n in first] + [Word("/", 546, top)]
    ws += [Word(n, COLS[n], top + 4) for n in second] + [Word("P&L", 694, top + 4)]
    ws += [Word("(IN/OUT)", 42, top + 8), Word("PRICE", 450, top + 8), Word("P&L", 578, top + 8)]
    return ws


def day_header(top: float, day: str, *nets: str) -> list[Word]:
    ws = [Word(day, 33, top), Word("True", 600, top + 1), Word("Equity:", 614, top + 1)]
    ws += [Word("₹150,000.00", 640, top), Word("Daily", 690, top + 1), Word("Net:", 705, top + 1)]
    x = 720.0
    for n in nets:
        ws.append(Word(n, x, top))
        x += 30
    return ws


def trade(
    top: float,
    *,
    t_in: str,
    t_out: str,
    inst: str,
    side: str,
    qty: str,
    entry: str,
    exit_: str,
    gross: str,
    charges: list[str],
    net: str,
    inst_on_exit_line: bool = False,
    flags: list[str] | None = None,
) -> list[Word]:
    y_mid = top + 9
    y_inst = top + 17 if inst_on_exit_line else y_mid  # some rows print the contract by the exit
    ws = [
        Word("+430.0", COLS["MFE"], top - 6),
        Word(t_in, 42, top),
        Word("7290", 90, top if not inst_on_exit_line else top + 1),
        Word("MARKET", COLS["MODALITY"], top),
        Word("MKT", COLS["REQUESTED"], top),
        Word(inst, COLS["INST"], y_inst),
        Word(side, COLS["TYPE"], y_inst),
        Word(qty, COLS["QTY"], y_inst),
        Word("6.4%", COLS["MARGIN"], y_inst),
        Word(entry, COLS["ENTRY"], y_mid),
        Word(exit_, COLS["EXIT"], y_mid),
        Word("/", COLS["MFE"], y_mid),
        Word(gross, COLS["GROSS"], y_mid),
        Word(net, COLS["NET"], y_mid),
        Word("SL_M", COLS["MODALITY"], top + 14),
        Word(t_out, 42, top + 18),
        Word("LIVE", 93, top + 18),
        Word("251.00", COLS["REQUESTED"], top + 18),
        Word("-150.0", COLS["MFE"], top + 22),
    ]
    for k, c in enumerate(charges):  # a wrapped charge prints "-" then "₹1,150.00" below
        ws.append(Word(c, COLS["CHARGES"], y_mid + 9 * k))
    for k, f in enumerate(flags or ["-"]):
        ws.append(Word(f, COLS["FLAGS"], top - 2 + 7 * k))
    return ws


def two_page_report() -> list[list[Word]]:
    page1 = [Word("Trade", 33, 39), Word("History:", 84, 39), Word("Someone", 156, 39)]
    page1 += [Word("Week", 33, 98), Word("2027-W09", 72, 98)]
    page1 += day_header(137, "2027-03-03", "₹1,108.42", "₹1,045.00", "(Legal)")
    page1 += header(165)
    page1 += trade(
        202, t_in="13:05:11", t_out="13:14:52", inst="SENSEX2730381000PE", side="BUY",
        qty="60", entry="210.00", exit_="230.00", gross="₹1,200.00", charges=["-₹91.58"],
        net="₹1,108.42", flags=["LIMIT", "BREACH", "(+₹140)"],
    )  # fmt: skip
    page1 += day_header(300, "2027-03-02", "₹-1,652.00")
    page1 += header(330)
    page1 += trade(
        370, t_in="10:41:36", t_out="10:47:19", inst="NIFTY27MARFUT", side="SELL",
        qty="195", entry="24,410.00", exit_="24,415.80", gross="₹-1,131.00",
        charges=["-", "₹1,150.00"], net="₹-2,281.00",
    )  # fmt: skip
    # The day continues on the next page without a repeated table header.
    page2 = trade(
        40, t_in="11:15:09", t_out="11:26:44", inst="SENSEX2730281500CE", side="BUY",
        qty="20", entry="402.15", exit_="434.75", gross="₹652.00", charges=["-₹23.00"],
        net="₹629.00", inst_on_exit_line=True,
    )  # fmt: skip
    return [page1, page2]


def test_parse_amount_forms() -> None:
    assert parse_amount("₹-3,140.00") == -3140.0
    assert parse_amount("-₹47.18") == -47.18
    assert parse_amount("+5,215.0") == 5215.0
    assert parse_amount("/") is None


def test_day_net_single_legal_and_multi_account() -> None:
    legal = [Word(t, x, 0) for t, x in [("Daily", 1), ("Net:", 2), ("₹7,412.30", 3),
                                         ("₹7,190.05", 4), ("(Legal)", 5)]]  # fmt: skip
    assert _day_net(legal) == (7412.30, 1)
    multi = [Word(t, x, 0) for t, x in [
        ("Daily", 1), ("Net:", 2), ("AB1234567890:", 3), ("₹5,320", 4), ("|", 5),
        ("AB0987654321:", 6), ("₹-1,908", 7), ("Payout:", 8), ("AB1234567890:", 9), ("₹-57.00", 10),
    ]]  # fmt: skip
    assert _day_net(multi) == (3412.0, 2)


def test_pdf_pages_parse_and_reconcile() -> None:
    res = parse_trade_history_pages(two_page_report())
    rep = res.report
    assert res.issues == ()
    assert len(rep.rows) == 3
    assert rep.stated_daily_nets == {date(2027, 3, 3): 1108.42, date(2027, 3, 2): -1652.0}
    by_inst = {r.instrument: r for r in rep.rows}
    fut = by_inst["NIFTY27MARFUT"]
    assert fut.trade_date == date(2027, 3, 2)
    assert fut.side == "SELL" and fut.qty == 195
    assert fut.stated_charges_inr == -1150.0  # "-" and "₹1,150.00" on two lines
    assert fut.charges_inr == 1150.0
    cont = by_inst["SENSEX2730281500CE"]
    assert cont.trade_date == date(2027, 3, 2)  # page 2 continues the previous day
    assert (cont.time_in, cont.time_out) == ("11:15:09", "11:26:44")
    first = by_inst["SENSEX2730381000PE"]
    assert first.breach_inr == 140.0
    assert first.is_live and first.exit_order_type == "SL_M" and first.requested_out == "251.00"
    assert first.mfe_inr == 430.0 and first.mae_inr == -150.0
    assert first.account_anon.startswith("ACCT-") and "7290" not in first.account_anon
    val = validate_parsed_report(rep)
    assert val.passed, (val.row_mismatches, val.daily_net_mismatches)


def test_daily_net_mismatch_is_reported() -> None:
    pages = two_page_report()
    pages[1] = [w for w in pages[1] if w.text != "11:26:44"]  # break the last trade block
    res = parse_trade_history_pages(pages)
    assert res.issues
    assert not validate_parsed_report(res.report).passed


def test_empty_report_never_passes() -> None:
    res = parse_trade_history_pages([[Word("nothing", 33, 10)]])
    assert not validate_parsed_report(res.report).passed


def test_diagnostics_markdown_has_no_identifiers() -> None:
    rows = parse_trade_history_pages(two_page_report()).report.rows
    md = diagnostics_markdown(rows, label="logX")
    assert "7290" not in md and "Someone" not in md
    assert "profit factor" in md
    assert underlying_of("SENSEX2730381000PE") == "SENSEX"
    assert underlying_of("M&M26OCTFUT") == "M&M"
    assert len(group_positions(rows)) == 3
