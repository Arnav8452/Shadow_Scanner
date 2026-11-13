"""SPEC 5.2 log3_entry_features: synthetic sessions only (no private log rows)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from engine.research.log3_entry_features import analyze, features_at, group_ideas, render

D = date(2026, 1, 5)


def _session(path: list[float]) -> list[list[object]]:
    t0 = datetime(2026, 1, 5, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    out: list[list[object]] = []
    prev = path[0]
    for i, px in enumerate(path):
        ts = (t0 + timedelta(minutes=i)).isoformat()
        out.append([ts, prev, max(prev, px) + 1, min(prev, px) - 1, px, 0, 0])
        prev = px
    return out


def test_group_ideas_merges_scale_ins_same_direction() -> None:
    rows = [
        {"trade_date": "2026-01-05", "time_in": "10:05:10", "instrument": "X100CE",
         "net_pnl_inr": "100"},
        {"trade_date": "2026-01-05", "time_in": "10:09:40", "instrument": "X100CE",
         "net_pnl_inr": "-30"},
        {"trade_date": "2026-01-05", "time_in": "10:25:00", "instrument": "X100PE",
         "net_pnl_inr": "5"},
        {"trade_date": "2026-01-05", "time_in": "11:00:00", "instrument": "X100CE",
         "net_pnl_inr": "1"},
    ]  # fmt: skip
    ideas = group_ideas(rows)
    assert [(i.t, i.bullish, i.fills) for i in ideas] == [
        (time(10, 5, 10), True, 2),
        (time(10, 25), False, 1),
        (time(11, 0), True, 1),
    ]
    assert ideas[0].net_inr == 70


def test_features_signed_by_direction_and_no_lookahead() -> None:
    # Flat to 10:00, then a steady climb; a huge spike AT 10:30 must not be seen at 10:30.
    path = [1000.0] * 45 + [1000.0 + i for i in range(1, 31)] + [5000.0] + [1030.0] * 30
    cs = _session(path)
    bull = features_at(cs, time(10, 30, 15), True)
    bear = features_at(cs, time(10, 30, 15), False)
    assert bull is not None and bear is not None
    assert bull["trend_pct"] > 2.9 and bear["trend_pct"] < -2.9
    assert bull["mom15_pct"] > 1.4 and bull["new_extreme_5m"] == 1.0
    assert bull["vs_or_pct"] > 0
    assert bull["from_extreme_pct"] < 0.2  # at the high, the 5000 spike is in the future
    assert bull["minutes_after_open"] == 75
    assert features_at(cs, time(9, 20), True) is None  # too early: no 15-min window


def test_analyze_finds_the_planted_momentum_rule() -> None:
    # Entries always come after a 15-minute push; otherwise the index chops.
    days = {}
    rows = []
    for k in range(4):
        d = date(2026, 1, 5 + k)
        path = [1000.0 + (2 if i % 2 else -2) for i in range(300)]
        for i in range(60, 76):  # push 10:15-10:30
            path[i] = path[59] + (i - 59) * 2
        for i in range(76, 300):
            path[i] = path[75] + (2 if i % 2 else -2)
        cs = _session(path)
        days[d] = [[c[0].replace("2026-01-05", d.isoformat()), *c[1:]] for c in cs]  # type: ignore[union-attr]
        rows.append({"trade_date": d.isoformat(), "time_in": "10:31:20", "instrument": "XCE",
                     "net_pnl_inr": "10"})  # fmt: skip
    res = analyze(group_ideas(rows), lambda d: days[d])
    assert res["measured"] == 4 and res["control"] > 100
    best = res["rules"][0][1]
    assert best.startswith(("mom15_pct >=", "trend_pct >=", "vs_vwap_pct >=", "vs_or_pct >="))
    text = render(res)
    assert "HYPOTHESIS" in text and "mom15_pct" in text
