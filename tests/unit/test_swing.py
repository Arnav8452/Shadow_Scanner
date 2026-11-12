"""Swing pillar (SPEC §5.6, §5.8): scanner setups, instrument choice, the overnight paper
account (fills, stops, guards, limits, costs) and the trend sleeve. Synthetic data only."""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from engine.core.calendar import TradingCalendar
from engine.core.config import load_settings
from engine.core.rules import RuleBook
from engine.data.eod.download import eod_path
from engine.execution.swing_paper import DayBar, Order, SwingFillConfig, SwingPaperAccount
from engine.strategies.swing_scanner import (
    Bar,
    Candidate,
    OptionQuote,
    Setup,
    SwingConfig,
    plan_trade,
    scan_symbol,
)
from engine.strategies.trend_sleeve import is_month_end, tsmom_signal

D0 = date(2026, 1, 5)
CFG = SwingConfig(min_turnover_cr=1.0)


def bars_from(rows: list[tuple[float, float, float, float, float]]) -> list[Bar]:
    return [Bar(D0 + timedelta(days=i), *r) for i, r in enumerate(rows)]


def flat(
    n: int, *, c: float = 104.0, v: float = 1e6
) -> list[tuple[float, float, float, float, float]]:
    return [(c, c + 2, c - 2, c, v) for _ in range(n)]


def with_pivots(
    rows: list, at: list[int], *, high: float | None = None, low: float | None = None
) -> list:
    rows = list(rows)
    for i in at:
        o, h, lo, c, v = rows[i]
        rows[i] = (o, high if high else h, low if low else lo, c, v)
    return rows


def test_breakout_on_volume_above_multi_touch_resistance() -> None:
    rows = with_pivots(flat(70), [20, 35, 50], high=110.0)
    rows.append((108.0, 113.0, 107.5, 112.0, 2.0e6))  # first close above 110 on 2x volume
    c = scan_symbol("AAA", bars_from(rows), CFG)
    assert c is not None and c.setup is Setup.BREAKOUT and c.direction == "LONG"
    assert c.level == pytest.approx(110.0) and c.touches == 3 and c.volume_mult == 2.0
    weak = [*rows[:-1], (108.0, 113.0, 107.5, 112.0, 1.2e6)]  # 1.2x volume: no breakout
    assert scan_symbol("AAA", bars_from(weak), CFG) is None
    coil = scan_symbol("AAA", bars_from(rows), SwingConfig(min_turnover_cr=1, coil_filter=True,
                                                            coil_max_range_pct=1.0))  # fmt: skip
    assert coil is None  # range before the break is wider than the coil limit


def test_breakdown_is_the_bearish_mirror() -> None:
    rows = with_pivots(flat(70), [20, 35, 50], low=98.0)
    rows.append((100.0, 100.5, 96.0, 96.5, 2.5e6))
    c = scan_symbol("BBB", bars_from(rows), CFG)
    assert c is not None and c.setup is Setup.BREAKDOWN and c.direction == "SHORT"
    assert c.level == pytest.approx(98.0)


def test_support_bounce_in_an_uptrend() -> None:
    rows = [(100 + i * 0.2, 101 + i * 0.2, 99 + i * 0.2, 100 + i * 0.2, 1e6) for i in range(70)]
    rows = with_pivots(rows, [45, 55, 62], low=108.0)
    prev_c = rows[-1][3]
    rows.append((109.0, 115.5, 108.1, 115.0, 1.1e6))  # tags 108, closes near the high, up
    c = scan_symbol("CCC", bars_from(rows), CFG)
    assert prev_c < 115.0
    assert c is not None and c.setup is Setup.SUPPORT_BOUNCE and c.level == pytest.approx(108.0)


def _cand(direction: str = "LONG", close: float = 500.0) -> Candidate:
    setup = Setup.BREAKOUT if direction == "LONG" else Setup.BREAKDOWN
    return Candidate("AAA", D0, setup, direction, 490.0, close, 2.0, 3, False, "test")


def _opt(premium: float, lot: int = 500, kind: str = "CE") -> OptionQuote:
    return OptionQuote("AAA", D0 + timedelta(days=20), 500.0, kind, premium, lot, 1000)


def test_instrument_choice_follows_iv_rank_events_ban_and_budget() -> None:
    kw = {"cfg": SwingConfig(), "capital": 200_000.0, "has_fno": True, "banned": False,
          "results_date": None}  # fmt: skip
    p = plan_trade(_cand(), iv_rank=30.0, option=_opt(8.0), **kw)  # type: ignore[arg-type]
    assert p.vehicle == "OPTION" and p.qty == 500 and p.risk_inr == 4_000.0  # premium = risk
    assert p.stop == pytest.approx(6.0)  # 25% premium stop
    rich = plan_trade(_cand(), iv_rank=30.0, option=_opt(12.0), **kw)  # type: ignore[arg-type]
    assert rich.vehicle == "CASH" and "budget" in rich.reason  # ₹6,000 > 2.5% of ₹2L
    high = plan_trade(_cand(), iv_rank=80.0, option=_opt(8.0), **kw)  # type: ignore[arg-type]
    assert high.vehicle == "CASH" and "IV rank 80" in high.reason
    assert high.qty == 100 and high.stop == 470.0  # min(5000/30=166, 25% value cap 100)
    earn = plan_trade(_cand(), iv_rank=30.0, option=_opt(8.0),
                      **{**kw, "results_date": D0 + timedelta(days=5)})  # type: ignore[arg-type]  # fmt: skip
    assert earn.vehicle == "CASH" and "results" in earn.reason
    ban = plan_trade(_cand(), iv_rank=30.0, option=_opt(8.0), **{**kw, "banned": True})  # type: ignore[arg-type]
    assert ban.vehicle == "CASH" and "ban" in ban.reason
    bear = plan_trade(_cand("SHORT"), iv_rank=80.0, option=_opt(8.0, kind="PE"), **kw)  # type: ignore[arg-type]
    assert bear.vehicle == "SKIP" and "no overnight cash shorts" in bear.reason
    put = plan_trade(_cand("SHORT"), iv_rank=20.0, option=_opt(8.0, kind="PE"), **kw)  # type: ignore[arg-type]
    assert put.vehicle == "OPTION" and put.option is not None and put.option.option_type == "PE"


# --------------------------------------------------------------------------- paper account


@pytest.fixture
def acct(repo_root: Path, tmp_path: Path) -> SwingPaperAccount:
    settings = load_settings(root=repo_root, environ={})
    book = RuleBook.load(repo_root / "config" / "rules")
    return SwingPaperAccount(
        tmp_path / "swing_paper_state.json", limits=settings.risk, book=book,
        calendar=TradingCalendar.from_rulebook(book), broker="upstox_plus",
        fills=SwingFillConfig(cash_half_spread_pct=0.0, option_half_spread_pct=0.0),
    )  # fmt: skip


# Upstox Plus rule rows start 11 Nov 2026; 24 Nov is a holiday (monthly expiry 23 Nov).
T1, T2, T3, T4 = date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17)


def _cash(qty: int = 100, px: float = 500.0, line: float = 490.0) -> Order:
    return Order(id="", strategy="swing", symbol="AAA", instrument="CASH", key="AAA", qty=qty,
                 est_price=px, stop_pct=6.0, line=line, setup_close=px)  # fmt: skip


def test_cash_swing_fill_stop_and_costs(acct: SwingPaperAccount) -> None:
    ok, msg = acct.place_entry(_cash(), day=T1, risk_budget_pct=2.5)
    assert ok, msg
    acct.process_day(T2, {"AAA": DayBar(505, 512, 500, 510)}, {"AAA": 510})
    pos = acct.state.positions[0]
    assert pos.entry_price == 505 and pos.stop == pytest.approx(474.7)
    assert pos.entry_charges > 50  # delivery: 0.1% STT etc. on ₹50,500
    out = acct.process_day(T3, {"AAA": DayBar(480, 482, 470, 471)}, {"AAA": 471})
    assert any("stop" in x for x in out) and not acct.state.positions
    c = acct.state.closed[0]
    assert c.exit_price == pytest.approx(474.7) and c.net < c.gross < 0
    assert acct.state.cash == pytest.approx(200_000 + c.net, abs=0.05)


def test_line_and_time_stops_queue_exits_for_the_next_open(acct: SwingPaperAccount) -> None:
    acct.place_entry(_cash(), day=T1, risk_budget_pct=2.5)
    acct.process_day(T2, {"AAA": DayBar(505, 508, 500, 489)}, {"AAA": 489})  # closed below 490
    assert "below the line" in acct.state.positions[0].exit_reason
    acct.process_day(T3, {"AAA": DayBar(488, 495, 486, 494)}, {"AAA": 494})
    assert not acct.state.positions and acct.state.closed[0].exit_price == 488
    acct.place_entry(_cash(line=0), day=T3, risk_budget_pct=2.5)
    acct.process_day(T4, {"AAA": DayBar(495, 499, 492, 498)}, {"AAA": 498})
    acct.process_day(date(2026, 11, 18), {"AAA": DayBar(498, 499, 494, 497)}, {"AAA": 497})
    assert acct.state.positions[0].exit_reason == ""  # 1 session held
    acct.process_day(date(2026, 11, 19), {"AAA": DayBar(497, 499, 495, 499)}, {"AAA": 499})
    assert "time stop" in acct.state.positions[0].exit_reason  # 2 sessions, no close > 500


def test_same_day_round_trip_is_costed_intraday(acct: SwingPaperAccount) -> None:
    acct.place_entry(_cash(), day=T1, risk_budget_pct=2.5)
    acct.process_day(T2, {"AAA": DayBar(500, 501, 460, 465)}, {"AAA": 465})  # stopped same day
    c = acct.state.closed[0]
    assert c.entry_day == c.exit_day == T2.isoformat()
    from engine.backtest.costs import equity_delivery_charges, equity_intraday_charges

    def cost(fn, side: str, v: float) -> float:  # type: ignore[no-untyped-def]
        return float(fn(acct.book, T2, side=side, value_inr=v, broker="upstox_plus").total)

    intraday = cost(equity_intraday_charges, "buy", 50_000) + cost(
        equity_intraday_charges, "sell", 47_000
    )
    delivery = cost(equity_delivery_charges, "buy", 50_000) + cost(
        equity_delivery_charges, "sell", 47_000
    )
    assert c.charges == pytest.approx(intraday, abs=0.05) and intraday < delivery / 2


def test_option_expiry_guard_e2(acct: SwingPaperAccount) -> None:
    key = "AAA 2026-11-23 500 CE"  # expiry Mon 23 Nov: E-1 = Fri 20, E-2 = Thu 19
    o = Order(id="", strategy="swing", symbol="AAA", instrument="OPTION", key=key, qty=500,
              est_price=8.0, stop_pct=25.0, line=490.0, setup_close=500.0,
              expiry="2026-11-23")  # fmt: skip
    assert acct.place_entry(o, day=T3, risk_budget_pct=2.5)[0]
    up = {"AAA": DayBar(501, 506, 500, 505)}
    acct.process_day(T4, {key: DayBar(8, 9, 7.5, 8.8), **up}, {"AAA": 505})
    assert acct.state.positions[0].exit_reason == ""  # 18, 19, 20, 23 left
    acct.process_day(date(2026, 11, 18), {key: DayBar(8.8, 10, 8.5, 9.5), **up}, {"AAA": 506})
    assert "E-2" in acct.state.positions[0].exit_reason  # sells at the 19 Nov (E-2) open
    acct.process_day(date(2026, 11, 19), {key: DayBar(9.6, 10, 9, 9.2), **up}, {"AAA": 506})
    assert not acct.state.positions and acct.state.closed[0].exit_day == "2026-11-19"


def test_risk_gate_refusals(acct: SwingPaperAccount) -> None:
    big = _cash(qty=300)  # 300 x 500 x 6% = ₹9,000 risk > 2.5% (₹5,000)
    ok, why = acct.place_entry(big, day=T1, risk_budget_pct=2.5)
    assert not ok and "per trade" in why
    assert acct.place_entry(_cash(), day=T1, risk_budget_pct=2.5)[0]
    ok, why = acct.place_entry(_cash(), day=T1, risk_budget_pct=2.5)
    assert not ok and "no stacking" in why
    huge = Order(id="", strategy="swing", symbol="BBB", instrument="CASH", key="BBB", qty=110,
                 est_price=500.0, stop_pct=6.0)  # fmt: skip
    ok, why = acct.place_entry(huge, day=T1, risk_budget_pct=2.5)
    assert not ok and "capital in use" in why  # ₹50k + ₹55k > 50% of ₹2L


def test_kill_switch_on_daily_loss(acct: SwingPaperAccount) -> None:
    o = Order(id="", strategy="trend", symbol="ETF", instrument="CASH", key="ETF", qty=180,
              est_price=500.0, stop_pct=0.0)  # fmt: skip
    acct.limits = acct.limits.__class__(**{**acct.limits.__dict__, "max_open_risk_pct": 100.0,
                                           "per_trade_max_loss_pct": {"directional": 100.0}})  # fmt: skip
    assert acct.place_entry(o, day=T1, risk_budget_pct=100.0)[0]
    acct.process_day(T2, {"ETF": DayBar(500, 501, 499, 500)}, {"ETF": 500})
    out = acct.process_day(T3, {"ETF": DayBar(480, 481, 470, 475)}, {"ETF": 475})  # -₹4,500
    assert any("KILL SWITCH" in x for x in out) and acct.state.kill_until == T4.isoformat()
    ok, why = acct.place_entry(_cash(), day=T3, risk_budget_pct=2.5)
    assert not ok and "kill switch" in why


def test_state_survives_restart(acct: SwingPaperAccount, repo_root: Path) -> None:
    acct.place_entry(_cash(), day=T1, risk_budget_pct=2.5)
    acct.process_day(T2, {"AAA": DayBar(505, 512, 500, 510)}, {"AAA": 510})
    acct.save()
    again = SwingPaperAccount(acct.path, limits=acct.limits, book=acct.book, calendar=acct.cal,
                              broker="upstox_plus")  # fmt: skip
    assert again.state.positions[0].entry_price == 505 and again.state.last_day == T2.isoformat()
    assert again.process_day(T2, {}, {}) == [f"{T2} already processed"]
    assert again.journal.exists() and '"type": "fill"' in again.journal.read_text()


# --------------------------------------------------------------------------- trend sleeve


def _index_file(raw: Path, day: date, close: float) -> None:
    p = eod_path(raw, "indices", day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value\n"
        f"Nifty 50,{day:%d-%m-%Y},1,1,1,{close}\n",
        encoding="utf-8",
    )


def test_trend_signal_12m_vs_tbill(repo_root: Path, tmp_path: Path) -> None:
    cal = TradingCalendar.from_rulebook(RuleBook.load(repo_root / "config" / "rules"))
    me = date(2026, 10, 30)
    assert is_month_end(cal, me) and not is_month_end(cal, date(2026, 10, 29))
    fetched: list[date] = []
    raw = tmp_path / "raw"
    _index_file(raw, me, 25_000.0)
    sig = tsmom_signal(
        raw,
        me,
        cal=cal,
        rf_pct=6.0,
        fetch=lambda d: (fetched.append(d), _index_file(raw, d, 23_000.0))[0],
    )
    assert fetched == [date(2025, 10, 31)] and sig is not None
    assert sig.ret_12m_pct == pytest.approx(8.7, abs=0.01) and sig.long  # 8.7% > 6%
    flat_sig = tsmom_signal(raw, me, cal=cal, rf_pct=9.0)
    assert flat_sig is not None and not flat_sig.long


# --------------------------------------------------------------------------- two nights


HEADER = (
    "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
    "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER"
)


def test_two_nights_offline(repo_root: Path, tmp_path: Path) -> None:
    from engine.strategies.swing_night import TrendConfig, run_swing_night

    settings = load_settings(root=repo_root, environ={})
    book = RuleBook.load(repo_root / "config" / "rules")
    cal = TradingCalendar.from_rulebook(book)
    raw = tmp_path / "raw"
    days: list[date] = []
    d = date(2026, 11, 11)
    while len(days) < 75:
        if cal.is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    t, t1 = days[-1], cal.next_trading_day(days[-1])
    rows = with_pivots(flat(74, v=2e6), [20, 35, 50], high=110.0)
    rows.append((108.0, 113.0, 107.5, 112.0, 4e6))
    for i, dd in enumerate([*days, t1]):
        o, h, lo, c, v = rows[i] if i < len(rows) else (112.5, 114, 111, 113.5, 2e6)
        p = eod_path(raw, "delivery", dd)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{HEADER}\nAAA, EQ, {dd:%d-%b-%Y}, {o}, {o}, {h}, {lo}, {c}, {c}, {c}, "
                     f"{int(v)}, {v * c / 1e5:.2f}, 10, {int(v / 2)}, 50.00\n", "utf-8")  # fmt: skip
        fo = eod_path(raw, "fo", dd)
        fo.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("f.csv", "TradDt,FinInstrmTp,TckrSymb,XpryDt,StrkPric,OptnTp,OpnPric,"
                        "HghPric,LwPric,ClsPric,OpnIntrst,TtlTradgVol,UndrlygPric,NewBrdLotQty\n"
                        f"{dd},STF,AAA,2026-11-24,,,1,1,1,1,10,5,{c},500\n")  # fmt: skip
        fo.write_bytes(buf.getvalue())
    kw = {"raw_dir": raw, "parquet_dir": tmp_path / "pq", "state_dir": tmp_path / "state",
          "book": book, "cal": cal, "limits": settings.risk, "broker": "upstox_plus",
          "cfg": SwingConfig(min_turnover_cr=1.0), "trend": TrendConfig(enabled=False),
          "fills": SwingFillConfig()}  # fmt: skip
    from datetime import datetime as _dt

    from engine.core.rules import IST

    r1 = run_swing_night(day=t, now=_dt(2026, 11, 11, 20, tzinfo=IST), **kw)  # type: ignore[arg-type]
    assert r1.candidates == 1 and r1.queued == 1, r1.lines  # cash: no IV history, no options
    plan = __import__("json").loads((tmp_path / "state" / "swing" / f"{t}.json").read_text())
    assert plan["plans"][0]["vehicle"] == "CASH" and "IV rank unknown" in plan["plans"][0]["reason"]
    r2 = run_swing_night(day=t1, now=_dt(2026, 11, 12, 20, tzinfo=IST), **kw)  # type: ignore[arg-type]
    assert any("bought AAA" in x for x in r2.lines), r2.lines

    # the cockpit's swing panel reads what the nights wrote
    from engine.cockpit import research

    view = research.swing(tmp_path)
    assert view["days"] == [t.isoformat(), t1.isoformat()] and view["day"] == t1.isoformat()
    assert [p["key"] for p in view["account"]["positions"]] == ["AAA"]
    first = research.swing(tmp_path, day=t.isoformat())
    assert first["plans"][0]["queued"] is True and first["account"]["pending"]
