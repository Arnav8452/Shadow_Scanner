"""Avoid-filter tags on paper entries (owner, 18 Nov 2026): call-heavy option volume and the
10 sessions after an ex-rights date. Recorded on every entry and close; they never block a
trade until the tagged results are judged (after 30 closed tagged trades)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from engine.core.rules import IST
from engine.data.eod.bhavcopy import FnOBhavcopyRow
from engine.data.equity.corporate_actions import CorpAction
from engine.strategies.avoid_tags import (
    AvoidTagConfig,
    entry_tags,
    pcv_ranks,
    rights_ex_dates,
    tag_report,
)

D = date(2026, 11, 18)


class WeekdayCal:
    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5

    def previous_trading_day(self, d: date) -> date:
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d

    def next_trading_day(self, d: date) -> date:
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d


CAL = WeekdayCal()


def _opt(sym: str, kind: str, contracts: int, day: date = D, inst: str = "STO") -> FnOBhavcopyRow:
    return FnOBhavcopyRow(
        trade_date=day, symbol=sym, instrument=inst, expiry=date(2026, 11, 24), strike=100.0,
        option_type=kind, open=1, high=1, low=1, close=1, settle_price=1, contracts=contracts,
        oi=0, change_in_oi=0, known_at=datetime(2026, 11, 18, 19, 30, tzinfo=IST),
    )  # fmt: skip


def test_pcv_rank_puts_call_heavy_names_at_the_bottom() -> None:
    rows = [
        _opt("CALLY", "CE", 900), _opt("CALLY", "PE", 100),  # PCV 0.10
        _opt("MID", "CE", 500), _opt("MID", "PE", 500),  # 0.50
        _opt("PUTTY", "CE", 100), _opt("PUTTY", "PE", 900),  # 0.90
        _opt("THIN", "CE", 100), _opt("THIN", "PE", 50),  # below the minimum: no rank
        _opt("NIFTY", "CE", 9000, inst="IDO"), _opt("NIFTY", "PE", 1, inst="IDO"),  # index
        _opt("CALLY", "CE", 9000, day=D - timedelta(days=30)),  # outside the window
    ]  # fmt: skip
    r = pcv_ranks(rows, min_contracts=500, days={D})
    assert set(r) == {"CALLY", "MID", "PUTTY"}
    assert r["CALLY"] == (pytest.approx(0.10), 0.0)
    assert r["MID"][1] == pytest.approx(0.5) and r["PUTTY"][1] == pytest.approx(1.0)


def test_ex_rights_window_runs_from_one_session_before_to_ten_after() -> None:
    acts = [
        CorpAction("RTS", "EQ", "INE1", date(2026, 11, 10), "Rights 1:5 @ Premium Rs 40", 10.0),
        CorpAction("DIV", "EQ", "INE2", date(2026, 11, 10), "Dividend - Rs 5 Per Share", 10.0),
    ]
    rights = rights_ex_dates(acts)
    assert rights == {"RTS": [date(2026, 11, 10)]}
    cfg = AvoidTagConfig()
    t = entry_tags("RTS", entry_day=date(2026, 11, 18), pcv={}, rights=rights, cal=CAL, cfg=cfg)
    assert t["ex_rights_date"] == "2026-11-10" and t["ex_rights_offset"] == 6
    assert t["in_ex_rights_window"] is True
    before = entry_tags("RTS", entry_day=date(2026, 11, 9), pcv={}, rights=rights, cal=CAL,
                        cfg=cfg)  # fmt: skip
    assert before["in_ex_rights_window"] is True and before["ex_rights_offset"] == -1
    late = entry_tags("RTS", entry_day=date(2026, 11, 25), pcv={}, rights=rights, cal=CAL,
                      cfg=cfg)  # fmt: skip
    assert late["in_ex_rights_window"] is False  # 11 sessions after the ex-date
    t2 = entry_tags("CALLY", entry_day=D, pcv={"CALLY": (0.1, 0.0)}, rights=rights, cal=CAL,
                    cfg=cfg)  # fmt: skip
    assert t2["call_heavy"] is True and t2["pcv_rank"] == 0.0
    t3 = entry_tags("NOOPT", entry_day=D, pcv={}, rights=rights, cal=CAL, cfg=cfg)
    assert t3["call_heavy"] is None and t3["pcv"] is None  # no option volume: unknown


def test_tag_report_splits_closed_trades_and_says_when_to_judge(tmp_path: Path) -> None:
    rows = []
    for i in range(4):
        rows.append({"type": "close", "net": 100.0 if i % 2 else -50.0,
                     "tags": {"call_heavy": i % 2 == 0, "in_ex_rights_window": False}})  # fmt: skip
    rows.append({"type": "close", "net": 10.0})  # an old close without tags: not counted
    (tmp_path / "paper_swing.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    rep = tag_report(tmp_path, review_after=30)
    sw = rep["accounts"]["swing"]
    assert sw["tagged"] == 4 and rep["ready"] is False
    ch = sw["call_heavy"]
    assert ch["yes"]["n"] == 2 and ch["yes"]["avg_net"] == -50.0
    assert ch["no"]["n"] == 2 and ch["no"]["avg_net"] == 100.0
