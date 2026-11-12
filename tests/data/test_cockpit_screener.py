"""EOD F&O screener and the context attribution in Research."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from engine.analytics.iv import black76_price
from engine.cockpit.research import context_stats
from engine.cockpit.screener import ScreenerCache, _quadrant, screen, summarize_day
from engine.core.calendar import TradingCalendar
from engine.core.rules import IST, RuleBook
from engine.data.eod.bhavcopy import FnOBhavcopyRow


def _rows(day: date, *, fut: float, fut_oi: int, vol: float, contracts: int) -> list[FnOBhavcopyRow]:
    known = datetime.combine(day, datetime.min.time(), IST).replace(hour=19, minute=30)
    exp, fexp = day + timedelta(days=13), day + timedelta(days=20)
    out = [
        FnOBhavcopyRow(day, "ACME", "STF", fexp, 0.0, "XX", fut, fut, fut, fut, fut, contracts,
                       fut_oi, 0, known, 1000.0, 500),
    ]  # fmt: skip
    t = 13 / 365
    for k in (950.0, 1000.0, 1050.0):
        for typ in ("CE", "PE"):
            px = black76_price(1000.0, k, t, vol, is_call=typ == "CE", t_cal=t, risk_free_rate=0.06)
            out.append(FnOBhavcopyRow(day, "ACME", "STO", exp, k, typ, px, px, px, px, px, 10,
                                      2000 if typ == "PE" else 1000, 0, known, 1000.0, 500))  # fmt: skip
    return out


def test_summary_has_iv_pcr_and_max_pain(repo_root: Path) -> None:
    book = RuleBook.load(repo_root / "config" / "rules")
    d = date(2026, 11, 10)
    s = summarize_day(_rows(d, fut=1002.0, fut_oi=10_000, vol=0.25, contracts=100), d, book=book,
                      calendar=TradingCalendar.from_rulebook(book))["ACME"]  # fmt: skip
    assert s["kind"] == "stock" and s["pcr"] == 2.0 and s["max_pain"] == 1000.0
    assert s["atm_iv"] == pytest.approx(25.0, abs=1.5)
    assert s["contracts"] == 160 and s["fut_oi"] == 10_000


def test_quadrants() -> None:
    assert _quadrant(1.0, 2.0) == "long build-up"
    assert _quadrant(-1.0, 2.0) == "short build-up"
    assert _quadrant(1.0, -2.0) == "short covering"
    assert _quadrant(-1.0, -2.0) == "long unwinding"
    assert _quadrant(None, 1.0) is None


class _Cache(ScreenerCache):
    """Summaries from memory instead of bhavcopy files."""

    def __init__(self, sums: dict[date, dict[str, dict[str, Any]]]) -> None:
        self.sums = sums

    def available_days(self, until: date, back_days: int = 400) -> list[date]:
        return sorted(d for d in self.sums if d <= until)

    def summary(self, day: date) -> dict[str, dict[str, Any]] | None:
        return self.sums.get(day)

    def cached_days(self) -> list[date]:
        return sorted(self.sums)


def test_screen_flags_and_point_in_time() -> None:
    base = date(2026, 6, 1)
    sums: dict[date, dict[str, dict[str, Any]]] = {}
    for i in range(80):
        d = base + timedelta(days=i)
        sums[d] = {"ACME": {"kind": "stock", "spot": 1000.0, "contracts": 100,
                            "fut": {"2026-12-30": 1000.0}, "fut_oi": 10_000,
                            "atm_iv": 20.0 + (i % 10) * 0.1}}  # fmt: skip
    last = base + timedelta(days=80)
    sums[last] = {"ACME": {"kind": "stock", "spot": 1010.0, "contracts": 300,
                           "fut": {"2026-12-30": 1010.0}, "fut_oi": 11_000, "atm_iv": 30.0,
                           "pcr": 0.8, "max_pain": 980.0}}  # fmt: skip
    # Before 19:30 on `last`, its file is not public yet: the screen shows the day before.
    early = screen(_Cache(sums), now=datetime.combine(last, datetime.min.time(), IST).replace(hour=18))
    assert early["day"] == (last - timedelta(days=1)).isoformat()
    out = screen(_Cache(sums), now=datetime.combine(last, datetime.min.time(), IST).replace(hour=20),
                 ban=["ACME"])  # fmt: skip
    row = out["rows"][0]
    assert out["day"] == last.isoformat() and row["ivp"] == 100 and row["ivp_days"] == 80
    assert row["fut_chg_pct"] == 1.0 and row["fut_oi_chg_pct"] == 10.0
    assert row["quadrant"] == "long build-up" and row["vol_x"] == 3.0
    assert row["max_pain_dist_pct"] == pytest.approx(-2.97, abs=0.01)
    assert any("rich premium" in f for f in row["flags"])
    assert any("unusual activity" in f for f in row["flags"]) and row["ban"]


def test_context_stats_split_by_entry_conditions(tmp_path: Path) -> None:
    def close(net: float, ivp: float, gex: str) -> dict[str, Any]:
        return {"type": "close", "symbol": "NIFTY25000CE", "net_inr": net,
                "context": {"ivp": ivp, "gex": gex, "hour": "10:00", "breadth_pct": 40}}  # fmt: skip

    (tmp_path / "paper_bot.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [close(500, 80, "POSITIVE_GAMMA_PIN"),
                                          close(-300, 20, "NEGATIVE_GAMMA_TREND"),
                                          {"type": "open"}]),  # fmt: skip
        encoding="utf-8",
    )
    study = [{"direction": "UP", "wave": "W", "context": {"ivp": 80, "breadth_pct": -40},
              "results": {"30m": {"move_pct": m, "hit": m > 0}}} for m in (0.1, 0.2, 0.15)]  # fmt: skip
    (tmp_path / "signal_study.jsonl").write_text(
        "\n".join(json.dumps(r) for r in study), encoding="utf-8"
    )
    d = context_stats(tmp_path)
    assert d["trades"] == 2 and d["signals"] == 3
    feats = {f["key"]: {r["bucket"]: r for r in f["rows"]} for f in d["features"]}
    assert feats["ivp"]["75+"]["trades"] == 1 and feats["ivp"]["75+"]["trade_net"] == 500
    assert feats["ivp"]["75+"]["signals"] == 3 and feats["ivp"]["75+"]["signal_hit_pct"] == 100
    assert feats["ivp"]["75+"]["t"] is not None and feats["ivp"]["<25"]["trade_win_pct"] == 0
    assert feats["_breadth_align"]["against breadth"]["signals"] == 3  # UP signals, breadth down
    assert feats["_breadth_align"]["with breadth"]["trades"] == 2
