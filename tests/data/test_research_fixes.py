"""13 Nov 2026 Research-tab review: the signal study survives restarts and has a 60-75 band,
the conditions table knows each trade's direction and shows unrecorded values, bulk deals are
netted per client, and swing breakouts are not taken far past their level."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from engine.cockpit.research import STUDY_BANDS, context_stats, net_deals, study_rows
from engine.strategies.swing_scanner import Setup, SwingConfig, scan_symbol
from tests.data.test_cockpit import OPEN, _book
from tests.data.test_cockpit_study import _study
from tests.unit.test_swing import CFG, bars_from, flat, with_pivots


def test_study_entry_survives_a_restart_and_skips_horizons_missed_while_down(
    repo_root: Path, tmp_path: Path
) -> None:
    t0 = OPEN.replace(hour=10)
    st = _study(repo_root, tmp_path)
    st.add(signal_id="S1", now=t0, symbol="X", label="X", underlying="NIFTY", bullish=True,
           score=70, wave="W", spot=25000.0, vehicle="OPT", vehicle_label="OPT", lot=65,
           vehicle_book=_book(100, 100.5))  # fmt: skip
    st.update(t0 + timedelta(minutes=15), {"NIFTY": 25020.0}, {"OPT": _book(104, 104.5)})
    # restart: a new study object resumes S1 with its 15-minute result
    st2 = _study(repo_root, tmp_path)
    assert [e.signal_id for e in st2.entries] == ["S1"] and "15m" in st2.entries[0].results
    # back 50 minutes later: 30m and 60m fell due while down -> skipped, not measured late
    st2.update(t0 + timedelta(minutes=65), {"NIFTY": 25100.0}, {"OPT": _book(110, 110.5)})
    res = st2.entries[0].results
    assert res["30m"] == {"skipped": "engine down at the horizon"} and "skipped" in res["60m"]
    st2.finish(OPEN.replace(hour=15, minute=20), {"NIFTY": 25050.0}, {"OPT": _book(102, 102.5)})
    rows = study_rows(tmp_path / "signal_study.jsonl")
    assert len(rows) == 1 and rows[0]["done"] and rows[0]["results"]["15m"]["hit"]
    assert STUDY_BANDS[0][:2] == (60.0, 75.0)


def test_conditions_table_direction_and_unrecorded_values(tmp_path: Path) -> None:
    t = "2026-11-13T10:00:00+05:30"
    rows = [
        {"type": "open", "t": t, "symbol": "NSE_FO|1", "reason": "rules.md NIFTY breakthrough 65 (DOWN)"},
        {"type": "close", "symbol": "NSE_FO|1", "opened_at": t, "net_inr": -100.0,
         "reason": "stop", "context": {"breadth_pct": -40, "hour": "10:00"}},
        {"type": "close", "symbol": "NSE_FO|2", "opened_at": t, "net_inr": 50.0, "reason": "x",
         "context": {"breadth_pct": 40, "option_type": "CE", "ivp": 60, "event_window": False}},
    ]  # fmt: skip
    (tmp_path / "paper_breakthrough.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8"
    )
    feats = {
        f["key"]: {r["bucket"]: r for r in f["rows"]} for f in context_stats(tmp_path)["features"]
    }
    # the put (DOWN, from its entry reason) with breadth falling is WITH breadth, not against
    assert feats["_breadth_align"]["with breadth"]["trades"] == 2
    assert feats["ivp"]["not recorded"]["trades"] == 1  # rows add up to the total
    assert feats["event_window"]["no"]["trades"] == 1 and "False" not in feats["event_window"]


def test_bulk_deals_net_per_client_drop_jobbers_and_dedupe() -> None:
    def deal(kind: str, sym: str, client: str, side: str, cr: float, qty: int, px: float) -> dict:
        return {"day": "2026-11-12", "kind": kind, "symbol": sym, "client": client,
                "side": side, "qty": qty, "price": px, "value_cr": cr}  # fmt: skip

    d = [
        deal("bulk", "KJ", "HFT", "BUY", 370.01, 5_000_000, 700.3),
        deal("bulk", "KJ", "HFT", "SELL", 370.2, 5_000_000, 700.7),
        deal("block", "GL", "A", "BUY", 201.2, 4_000_000, 503.0),
        deal("bulk", "GL", "A", "BUY", 201.2, 4_000_000, 503.0),  # same trade, both files
        deal("block", "GL", "B", "SELL", 201.2, 4_000_000, 503.0),
        deal("bulk", "GR", "D", "BUY", 111.27, 1_350_000, 824.2),
        deal("bulk", "GR", "D", "SELL", 87.12, 1_060_000, 821.9),
    ]
    out = net_deals(d)
    got = {(r["symbol"], r["client"]): r for r in out["rows"]}
    assert ("KJ", "HFT") not in got and out["jobbers"] == ["HFT"]
    assert got[("GL", "A")]["net_cr"] == 201.2 and got[("GL", "A")]["kind"] == "block+bulk"
    assert "transfer with B" in got[("GL", "A")]["note"]
    assert got[("GR", "D")]["net_cr"] == 24.15  # 12% churn but a large net: kept


def test_swing_breakout_too_far_past_the_level_is_not_taken() -> None:
    rows = with_pivots(flat(70), [20, 35, 50], high=110.0)
    near = [*rows, (108.0, 113.0, 107.5, 112.0, 2.0e6)]  # 1.8% past 110
    far = [*rows, (108.0, 122.0, 107.5, 120.0, 2.0e6)]  # 9% past 110
    c = scan_symbol("AAA", bars_from(near), CFG)
    assert c is not None and c.setup is Setup.BREAKOUT
    assert scan_symbol("AAA", bars_from(far), CFG) is None
    loose = SwingConfig(**{**CFG.__dict__, "max_extension_pct": 10.0})
    assert scan_symbol("AAA", bars_from(far), loose) is not None
