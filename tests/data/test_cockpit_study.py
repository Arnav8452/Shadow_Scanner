"""Re-scope (12 Nov 2026): order flow as a confirmation input, not a scalper.

Covers the signal study (horizon measurement), the SPEC 5.2 directional entry rules on the Bot
account, and the paper account's premium target and trailing stop.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from engine.cockpit.research import study_stats
from engine.cockpit.study import SignalStudy, summarize
from engine.core.rules import RuleBook
from engine.data.reference.instrument_master import Kind
from tests.data.conftest import DAY, WEEKLY, opt_symbol
from tests.data.test_cockpit import CE, OPEN, _account, _book, _core, _signal

# ---------------------------------------------------------------- target and trailing stop


def test_target_on_premium_closes_the_position(repo_root: Path, tmp_path: Path) -> None:
    acct = _account(repo_root, tmp_path, "bot")
    t = OPEN.replace(hour=10)
    ok, text = acct.open_long_option(
        CE, _book(99.9, 100.0), now=t, lots=1, stop_pct=10, target_pct=30
    )
    assert ok, text
    pos = acct.positions[CE]
    assert pos.stop_price == 90.0 and pos.target_price == 130.0
    assert not acct.check_exits({CE: _book(129.0, 129.1)}, t + timedelta(minutes=5))
    out = acct.check_exits({CE: _book(130.0, 130.1)}, t + timedelta(minutes=6))
    assert out and acct.closed[-1].reason == "target on premium"


def test_trailing_stop_locks_gains_and_never_lowers(repo_root: Path, tmp_path: Path) -> None:
    acct = _account(repo_root, tmp_path, "bot")
    t = OPEN.replace(hour=10)
    trail = ((15.0, 7.0), (25.0, 12.0))
    assert acct.open_long_option(
        CE, _book(99.9, 100.0), now=t, lots=1, stop_pct=10, target_pct=30, trail=trail
    )[0]
    pos = acct.positions[CE]
    acct.check_exits({CE: _book(116.0, 116.1)}, t + timedelta(minutes=1))
    assert pos.stop_price == 107.0  # +15% reached: lock +7%
    acct.check_exits({CE: _book(126.0, 126.1)}, t + timedelta(minutes=2))
    assert pos.stop_price == 112.0  # +25% reached: lock +12%
    acct.check_exits({CE: _book(115.0, 115.1)}, t + timedelta(minutes=3))
    assert CE in acct.positions and pos.stop_price == 112.0  # pullback does not lower it
    out = acct.check_exits({CE: _book(111.9, 112.0)}, t + timedelta(minutes=4))
    assert out and acct.closed[-1].reason == "trailing stop"
    assert acct.closed[-1].gross_inr > 0


def test_target_and_trail_survive_a_restart(repo_root: Path, tmp_path: Path) -> None:
    acct = _account(repo_root, tmp_path, "bot")
    t = OPEN.replace(hour=10)
    acct.open_long_option(
        CE, _book(99.9, 100.0), now=t, lots=1, stop_pct=10, target_pct=30, trail=((15.0, 7.0),)
    )
    again = _account(repo_root, tmp_path, "bot")
    again.load_journal(DAY)
    pos = again.positions[CE]
    assert pos.target_price == 130.0 and pos.trail == ((15.0, 7.0),)


# ---------------------------------------------------------------- directional (SPEC 5.2)


def _directional_core(repo_root: Path, tmp_path: Path, spot: list[float]):  # type: ignore[no-untyped-def]
    from engine.cockpit.setups import SetupConfig, SetupTracker

    core = _core(repo_root, spot=spot[0])
    core.bot = _account(repo_root, tmp_path, "bot")
    core.manual = _account(repo_root, tmp_path, "manual")
    core.setups = SetupTracker(SetupConfig(), journal=None)
    core.runner.spots = lambda: {"NIFTY": spot[0]}  # type: ignore[union-attr]
    return core


def _follow_through(core, spot: list[float], t, up: bool = True):  # type: ignore[no-untyped-def]
    """Move the index 0.06% the setup's way and hold it past the 60-s confirmation."""
    spot[0] = round(spot[0] * (1.0006 if up else 0.9994), 2)
    core.setups.update(t + timedelta(seconds=20), core.runner.spots())
    done = core.setups.update(t + timedelta(seconds=85), core.runner.spots())
    for st in done:
        core._on_setup_confirmed(st, t + timedelta(seconds=85))
    return done


def test_directional_enters_only_on_a_confirmed_setup(repo_root: Path, tmp_path: Path) -> None:
    spot = [25000.0]
    core = _directional_core(repo_root, tmp_path, spot)
    call = opt_symbol(WEEKLY, 25000, Kind.CALL)
    t = OPEN.replace(hour=10)
    core.on_episode(_signal("NSE:NIFTY26NOVFUT", "LONG", t, score=85))
    assert not core.bot.positions  # a reading alone never trades
    assert core.setups.setups[0].state == "FORMING"
    assert _follow_through(core, spot, t)
    assert call in core.bot.positions, core.bot.refusals
    pos = core.bot.positions[call]
    assert pos.target_price is not None and pos.trail  # strategy exits, not the book's
    assert pos.stop_price == pytest.approx(pos.avg_price * 0.9, abs=0.01)
    assert "confirmed setup" in pos.reason
    assert pos.context["underlying"] == "NIFTY" and pos.context["hour"] == "10:00"
    core.bot.close(call, _book(110.0, 110.5), now=t + timedelta(minutes=20), reason="test")
    assert core.bot.closed[-1].context["underlying"] == "NIFTY"
    again = _account(repo_root, tmp_path, "bot")
    again.load_journal(DAY)
    assert again.closed[-1].context["hour"] == "10:00"
    # One idea a day: closed above, another confirmation the same way may re-enter once more.
    assert core.bot.trades_today == 1


def test_directional_never_flips_and_respects_window_and_score(
    repo_root: Path, tmp_path: Path
) -> None:
    core = _directional_core(repo_root, tmp_path, [25000.0])
    put = opt_symbol(WEEKLY, 25000, Kind.PUT)
    call = opt_symbol(WEEKLY, 25000, Kind.CALL)
    core._directional_entry("a", 90.0, "NIFTY", True, call, OPEN.replace(minute=25))  # 09:25
    core._directional_entry("b", 78.0, "NIFTY", True, call, OPEN.replace(hour=10))  # score
    assert not core.bot.positions
    t = OPEN.replace(hour=10, minute=30)
    core._directional_entry("c", 85.0, "NIFTY", False, put, t)
    assert put in core.bot.positions
    core._directional_entry("c2", 90.0, "NIFTY", False, put, t + timedelta(minutes=2))
    assert core.bot.trades_today == 1  # holding the idea: no stacking
    core.bot.close(put, _book(100.0, 100.5), now=t + timedelta(minutes=10), reason="test")
    core._directional_entry("d", 95.0, "NIFTY", True, call, t + timedelta(minutes=40))
    assert call not in core.bot.positions  # one idea a day: no flip
    core._directional_entry("e", 95.0, "NIFTY", False, put, OPEN.replace(hour=14, minute=5))
    assert not core.bot.positions  # after 14:00


# ---------------------------------------------------------------- signal study


def _study(repo_root: Path, tmp_path: Path) -> SignalStudy:
    return SignalStudy(
        book=RuleBook.load(repo_root / "config" / "rules"),
        broker_name="upstox_plus",
        close_at=OPEN.replace(hour=15, minute=20),
        journal=tmp_path / "signal_study.jsonl",
    )


def test_study_measures_each_horizon_and_the_close(repo_root: Path, tmp_path: Path) -> None:
    st = _study(repo_root, tmp_path)
    t0 = OPEN.replace(hour=10)
    e = st.add(
        signal_id="S1", now=t0, symbol=CE, label="ce", underlying="NIFTY", bullish=True,
        score=85, wave="STOP_HUNT_RECLAIM", spot=25000.0, vehicle=CE, vehicle_label="ce",
        lot=65, vehicle_book=_book(99.9, 100.0),
    )  # fmt: skip
    assert e is not None and e.opt_ask0 == 100.0
    st.update(t0 + timedelta(minutes=10), {"NIFTY": 25010.0}, {CE: _book(103.0, 103.1)})
    assert not e.results
    st.update(t0 + timedelta(minutes=15), {"NIFTY": 25020.0}, {CE: _book(105.0, 105.1)})
    r15 = e.results["15m"]
    assert r15["move_pts"] == 20.0 and r15["hit"] is True
    assert r15["opt_ret_pct"] == 5.0
    assert r15["opt_net_inr"] < 5.0 * 65  # round-trip charges deducted
    st.update(t0 + timedelta(minutes=30), {"NIFTY": 24990.0}, {CE: _book(97.0, 97.1)})
    assert e.results["30m"]["hit"] is False and e.results["30m"]["opt_net_inr"] < 0
    st.update(t0 + timedelta(minutes=60), {"NIFTY": 25030.0}, {CE: _book(108.0, 108.1)})
    assert not e.done
    st.update(OPEN.replace(hour=15, minute=20), {"NIFTY": 25050.0}, {CE: _book(110.0, 110.1)})
    assert e.done and e.results["close"]["move_pts"] == 50.0
    from engine.cockpit.research import study_rows

    lines = (tmp_path / "signal_study.jsonl").read_text().splitlines()
    assert len(lines) > 1  # every update is journaled (a restart resumes it)
    rows = study_rows(tmp_path / "signal_study.jsonl")
    assert len(rows) == 1 and rows[0]["direction"] == "UP" and rows[0]["done"]
    s = {x["horizon"]: x for x in summarize(rows, ["15m", "30m", "60m", "close"])}
    assert s["15m"]["hit_pct"] == 100.0 and s["30m"]["hit_pct"] == 0.0
    # A restart the same day reloads finished rows into the view.
    assert len(_study(repo_root, tmp_path).view()["rows"]) == 1


def test_study_bearish_signal_and_late_horizons(repo_root: Path, tmp_path: Path) -> None:
    st = _study(repo_root, tmp_path)
    t0 = OPEN.replace(hour=14, minute=45)  # +60 min falls after the 15:20 square-off
    e = st.add(
        signal_id="S2", now=t0, symbol=CE, label="x", underlying="NIFTY", bullish=False,
        score=80, wave="W", spot=25000.0, vehicle="", vehicle_label="", lot=65,
        vehicle_book=None,
    )  # fmt: skip
    assert e is not None
    st.update(t0 + timedelta(minutes=15), {"NIFTY": 24980.0}, {})
    assert e.results["15m"]["move_pts"] == 20.0  # index fell: a DOWN signal was right
    assert "opt_net_inr" not in e.results["15m"]  # no option book recorded
    st.update(OPEN.replace(hour=15, minute=20), {"NIFTY": 24990.0}, {})
    assert e.done and e.results["60m"] == {"skipped": "after square-off"}
    # Signals at or after the square-off are not studied.
    assert st.add(
        signal_id="S3", now=OPEN.replace(hour=15, minute=25), symbol=CE, label="x",
        underlying="NIFTY", bullish=True, score=80, wave="W", spot=25000.0, vehicle="",
        vehicle_label="", lot=65, vehicle_book=None,
    ) is None  # fmt: skip


def test_study_stats_by_band_with_t_stat(tmp_path: Path) -> None:
    def row(score: float, move: float, net: float) -> dict[str, object]:
        res = {"move_pct": move, "move_pts": move * 250, "hit": move > 0, "opt_net_inr": net}
        return {
            "t0": "2026-11-12T10:00:00+05:30", "time": "10:00:00", "score": score,
            "wave": "W", "underlying": "NIFTY", "direction": "UP",
            "results": {"15m": res, "30m": res, "60m": {"skipped": "x"}, "close": res},
        }  # fmt: skip

    rows = [row(85, 0.10, 200), row(86, 0.12, 250), row(88, -0.02, -150), row(76, -0.05, -300)]
    path = tmp_path / "signal_study.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nbad\n", encoding="utf-8")
    d = study_stats(path)
    bands = {b["key"]: b for b in d["bands"]}
    c = bands["80–90"]["15m"]
    assert c["n"] == 3 and c["hit_pct"] == pytest.approx(66.7) and c["t"] is not None
    assert c["opt_net"] == 300.0
    assert bands["75–80"]["15m"]["n"] == 1 and bands["75–80"]["15m"]["t"] is None
    assert bands["80–90"]["60m"]["n"] == 0
    assert d["days"] == 1 and d["total"][0]["close"]["n"] == 4


def test_open_entries_name_their_vehicles_and_a_missing_quote_is_said(
    repo_root: Path, tmp_path: Path
) -> None:
    """Open entries list the options they still need priced, newest first (the cockpit pins
    them in the live feed); a horizon whose vehicle has no quote says so in its result."""
    st = _study(repo_root, tmp_path)
    t0 = OPEN.replace(hour=10)
    other = "NSE_FO|OTHER"
    for i, veh in enumerate((CE, other, CE)):
        st.add(
            signal_id=f"V{i}", now=t0 + timedelta(seconds=i), symbol=CE, label="ce",
            underlying="NIFTY", bullish=True, score=85, wave="STOP_HUNT_RECLAIM",
            spot=25000.0, vehicle=veh, vehicle_label="v", lot=65,
            vehicle_book=_book(99.9, 100.0),
        )  # fmt: skip
    assert st.open_vehicles() == [CE, other]
    st.update(t0 + timedelta(minutes=15, seconds=5), {"NIFTY": 25020.0}, {})
    r = st.entries[0].results["15m"]
    assert "opt_net_inr" not in r and r["opt_missing"] == "no quote for the vehicle"
    st.update(OPEN.replace(hour=15, minute=20), {"NIFTY": 25000.0}, {CE: _book(1.0, 1.1)})
    assert st.open_vehicles() == []


def test_signals_group_into_episodes_one_trade_each() -> None:
    """A burst of the same reading on many strikes is ONE bet: an episode = signals of the
    same underlying and direction within 15 minutes of the episode's first signal; the
    episode is measured on its first signal (what one trader would actually take)."""
    from engine.cockpit.study import episode_firsts

    def row(t: str, und: str = "NIFTY", d: str = "DOWN", net: float = 100.0) -> dict:
        return {"signal_id": t + und + d, "t0": f"2026-11-18T{t}+05:30", "underlying": und,
                "direction": d, "results": {"30m": {"move_pts": 5.0, "move_pct": 0.02,
                "hit": True, "opt_net_inr": net}}}  # fmt: skip

    rows = [
        row("09:17:05", net=1000.0),
        row("09:17:07"),
        row("09:25:00"),  # one burst
        row("09:33:00", net=-200.0),  # 16 min after the burst began: a new episode
        row("09:17:30", d="UP", net=-50.0),  # other direction: its own episode
        row("09:17:40", und="SENSEX", net=7.0),  # other index: its own episode
    ]
    firsts = episode_firsts(rows)
    assert [(r["t0"][11:19], r["underlying"], r["direction"], r["episode_signals"])
            for r in firsts] == [
        ("09:17:05", "NIFTY", "DOWN", 3), ("09:17:30", "NIFTY", "UP", 1),
        ("09:17:40", "SENSEX", "DOWN", 1), ("09:33:00", "NIFTY", "DOWN", 1),
    ]  # fmt: skip
    assert sum(r["results"]["30m"]["opt_net_inr"] for r in firsts) == 1000.0 - 50 + 7 - 200


def test_study_view_and_stats_report_episodes(repo_root: Path, tmp_path: Path) -> None:
    from engine.cockpit.research import study_stats

    st = _study(repo_root, tmp_path)
    t0 = OPEN.replace(hour=10)
    for i in range(3):  # one burst of three
        st.add(
            signal_id=f"B{i}", now=t0 + timedelta(seconds=i), symbol=CE, label="ce",
            underlying="NIFTY", bullish=True, score=85, wave="STOP_HUNT_RECLAIM",
            spot=25000.0, vehicle=CE, vehicle_label="ce", lot=65,
            vehicle_book=_book(99.9, 100.0),
        )  # fmt: skip
    st.update(OPEN.replace(hour=15, minute=20), {"NIFTY": 25050.0}, {CE: _book(110.0, 110.1)})
    v = st.view()
    assert v["signals"] == 3 and v["episodes"] == 1
    assert [s["n"] for s in v["episode_summary"] if s["horizon"] == "close"] == [1]
    d = study_stats(tmp_path / "signal_study.jsonl")
    assert d["total_episodes"][0]["close"]["n"] == 1 and d["total"][0]["close"]["n"] == 3
