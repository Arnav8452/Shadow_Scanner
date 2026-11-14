"""Candle backtest runner on SYNTHETIC data only: a generated index path and Black-76 option
prices. No real market data and nothing from private trade logs."""

from __future__ import annotations

import math
import urllib.error
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from engine.analytics.iv import black76_price
from engine.backtest.candles.data import (
    Bar,
    Contract,
    MemorySource,
    RequestBudget,
    UpstoxCandleSource,
    parse_candles,
)
from engine.backtest.candles.fills import Costs, Haircut, table_margin
from engine.backtest.candles.market import IvHistory, Market, Series
from engine.backtest.candles.runner import (
    daily_pnl,
    register_trial,
    render_report,
    run_candle_backtest,
)
from engine.backtest.candles.strategies import (
    CandleTrade,
    CondorVariant,
    Ctx,
    LegTrade,
    Log3Params,
    StrategyRefused,
    _condor_exit,
    _fill_leg,
    _long_exit,
    condor_variants,
    resolve,
)
from engine.cockpit.condor import CondorConfig
from engine.core.calendar import TradingCalendar
from engine.core.rules import IST, RuleBook

ROOT = Path(__file__).resolve().parents[2]
RISK = {
    "per_trade_max_loss_pct": {"directional": 2.5, "defined_risk_spread": 5.0},
    "daily_loss_limit_pct": 2.0,
    "max_trades_per_day": 5,
    "cooloff_after_loss_min": 15,
}


@pytest.fixture(scope="module")
def book() -> RuleBook:
    return RuleBook.load(ROOT / "config" / "rules")


def _weekdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _at(d: date, hm: str) -> datetime:
    return datetime.combine(d, time.fromisoformat(hm), tzinfo=IST)


class Synth:
    """A generated Nifty-like world: weekday sessions 09:15-15:29, Thursday expiries, option
    bars priced by Black-76 on the index with a day-dependent flat IV. A bar's open is the
    price at its start plus a small jitter, its close the price at its end, so a fill at the
    next open never equals the previous close."""

    underlying = "NIFTY"

    def __init__(self, days: list[date], spot: Any, iv: Any) -> None:
        self.days = days
        self.spot = spot  # (day index, minute 0..375) -> level
        self.iv = iv  # day index -> vol
        self._exp = [d for d in days if d.weekday() == 3]
        self.errors: list[str] = []

    def expiries(self) -> list[date]:
        return self._exp

    def contracts(self, expiry: date) -> dict[tuple[float, str], Contract]:
        out = {}
        for k in range(22500, 24551, 50):
            for kind in ("CE", "PE"):
                out[(float(k), kind)] = Contract(f"S|{expiry}|{k}|{kind}", expiry, k, kind, 75)
        return out

    def _bars(self, di: int, px: Any) -> list[Bar]:
        d = self.days[di]
        out = []
        for i in range(375):
            o, c = px(i), px(i + 1)
            out.append(
                Bar(
                    _at(d, "09:15") + timedelta(minutes=i),
                    round(o + 0.25, 2),
                    max(o, c) + 0.5,
                    min(o, c) - 0.5,
                    c,
                    100,
                    1000,
                )
            )
        return out

    def index_bars(self, day: date) -> list[Bar]:
        if day not in self.days:
            return []
        di = self.days.index(day)
        return self._bars(di, lambda i: self.spot(di, i))

    def option_bars(self, c: Contract) -> list[Bar]:
        out: list[Bar] = []
        for di, d in enumerate(self.days):
            if d > c.expiry or d < c.expiry - timedelta(days=14):
                continue
            later = sum(1 for x in self.days if d < x <= c.expiry)

            def px(i: int, di: int = di, later: int = later) -> float:
                t = max(375 - i + 375 * later, 15) / (375 * 252)
                p = black76_price(
                    self.spot(di, i), c.strike, t, self.iv(di), is_call=c.kind == "CE",
                    t_cal=t, risk_free_rate=0.0,
                )  # fmt: skip
                return max(0.05, round(p * 20) / 20)

            out += self._bars(di, px)
        return out

    def sessions(self, start: date, end: date) -> list[date]:
        return [d for d in self.days if start <= d <= end]


def _calm(di: int, i: int) -> float:
    return 23500 + 20 * math.sin(i / 30)


def _trend(di: int, i: int) -> float:
    # flat opening range, then a steady push up from 10:00 (0.012% a minute)
    base = 23500 + 3 * math.sin(i)
    return base if i < 45 else base * (1 + 0.00012 * (i - 45))


def _ctx(book: RuleBook, src: Any, mult: float = 1.0, charges_mult: float = 1.0, **kw: Any) -> Ctx:
    cal = TradingCalendar.from_rulebook(book)
    return Ctx(
        market=Market(src, book, cal),
        haircut=Haircut().scaled(mult),
        costs=Costs(book, date(2026, 11, 13), charges_mult=charges_mult),
        calendar=cal,
        capital_inr=200_000,
        risk=RISK,
        run_date=date(2026, 11, 13),
        **kw,
    )


# ---------------------------------------------------------------------------- fills, rules


def test_haircut_default_extreme_and_table() -> None:
    d = Haircut()  # max(₹0.10, 0.5%): 2-3x the recorded median half-spread
    assert d.half(10.0) == pytest.approx(0.10) and d.half(200.0) == pytest.approx(1.0)
    assert d.describe() == "max(₹0.1, 0.5% of price)"
    t = Haircut(table=((0.0, 0.5), (50.0, 0.2)))  # measured half-spreads by price bucket
    assert t.half(20.0) == pytest.approx(0.1) and t.half(100.0) == pytest.approx(0.2)
    assert t.half(1.0) == pytest.approx(0.025)  # floor: half a tick
    h = Haircut(1.5, 1.5)  # the first brief's extreme value
    assert h.half(10.0) == 1.5  # the ₹1.50 floor
    assert h.half(200.0) == pytest.approx(3.0)  # 1.5% of price
    assert h.buy(200.0) == 203.0 and h.sell(200.0) == 197.0
    assert h.sell(1.0) == 0.05  # never below one tick
    assert h.scaled(2.0).half(200.0) == pytest.approx(6.0)


@pytest.mark.parametrize(
    "name",
    ["score", "breakthrough", "spoof_fade", "zone_rider", "oi_ignition", "oi_credit"],
)
def test_order_book_strategies_are_refused(name: str) -> None:
    with pytest.raises(StrategyRefused, match="refused on candles"):
        resolve(name)


def test_not_built_and_aliases() -> None:
    with pytest.raises(StrategyRefused, match="not built"):
        resolve("gamma")
    assert resolve("log3_mom").key == "log3"
    assert resolve("CONDOR").key == "condor"


def test_costs_and_margin_come_from_rules(book: RuleBook) -> None:
    c = Costs(book, date(2026, 11, 13))
    sell = c.order("SELL", 100.0, 65, date(2025, 3, 3))
    buy = c.order("BUY", 100.0, 65, date(2025, 3, 3))
    assert sell > buy > 30  # ₹30 Upstox Plus brokerage + GST; STT only on the sale
    assert table_margin(book, "IRON_CONDOR", 1, date(2026, 11, 13)) == 85000.0
    old = Costs(book, date(2026, 11, 13), costs_on="trade_date")
    old.order("BUY", 100.0, 75, date(2025, 3, 3))
    assert old.fallbacks == 1  # the Plus row starts 11 Nov 2026 -> fallback broker


# ---------------------------------------------------------------------- point in time


def test_series_lookups_are_point_in_time() -> None:
    d = date(2025, 1, 6)
    bars = [
        Bar(_at(d, "09:15") + timedelta(minutes=i), i, i + 1, i - 1, i + 0.5) for i in (0, 1, 5)
    ]
    s = Series(bars)
    assert s.last(_at(d, "09:16")) is bars[1]
    assert s.last(_at(d, "09:19"), max_age_min=2) is None
    assert s.next_after(_at(d, "09:16")) is bars[2]
    assert s.next_after(_at(d, "09:16"), within_min=3) is None


def test_fill_is_next_open_plus_haircut_never_the_close(book: RuleBook) -> None:
    d = date(2025, 1, 6)
    c = Contract("X", date(2025, 1, 9), 23500, "CE", 75)
    bars = [
        Bar(_at(d, "10:00"), 100, 101, 99, 100.0),
        Bar(_at(d, "10:01"), 104, 106, 103, 105.0),
    ]
    src = MemorySource({d: bars}, {c.expiry: {(23500.0, "CE"): c}}, {"X": bars})
    ctx = _ctx(book, src)
    lg = _fill_leg(ctx, c, "BUY", 75, _at(d, "10:00"), 3)
    assert lg is not None
    assert lg.entry_ref == 104 and lg.entry_px == pytest.approx(104 + ctx.haircut.half(104))
    assert lg.entry_px != 100.0 + ctx.haircut.half(100.0)  # not the decision bar's close


def test_stop_wins_when_one_bar_touches_stop_and_target(book: RuleBook) -> None:
    d = date(2025, 1, 6)
    bars = [
        Bar(_at(d, "10:01"), 100, 101, 99, 100),
        Bar(_at(d, "10:02"), 100, 200, 50, 120),  # both levels inside one bar
    ]
    ctx = _ctx(book, MemorySource({}, {}, {}))
    leg = LegTrade("23500CE", "CE", 23500, "BUY", 75, bars[0].start, 100.0, 101.5)
    at, _ref, px, why = _long_exit(ctx, Series(bars), leg, 91.35, 131.95, "15:20")
    assert why.startswith("stop") and px == ctx.haircut.sell(91.35) and at == bars[1].start
    ctx.trigger_on_haircut = True  # the level is then a bid proxy and fills as it is
    _, _, px2, why2 = _long_exit(ctx, Series(bars), leg, 91.35, 131.95, "15:20")
    assert why2.startswith("stop") and px2 == 91.35


def test_gap_through_stop_fills_at_the_open(book: RuleBook) -> None:
    d = date(2025, 1, 6)
    bars = [Bar(_at(d, "10:01"), 100, 101, 99, 100), Bar(_at(d, "10:02"), 70, 72, 69, 71)]
    ctx = _ctx(book, MemorySource({}, {}, {}))
    leg = LegTrade("23500CE", "CE", 23500, "BUY", 75, bars[0].start, 100.0, 101.5)
    _, _ref, px, why = _long_exit(ctx, Series(bars), leg, 91.35, 131.95, "15:20")
    assert "opened through" in why and px == ctx.haircut.sell(70)


def test_iv_percentile_uses_only_earlier_sessions() -> None:
    h = IvHistory(min_history=2)
    h.add(date(2025, 1, 1), 0.10)
    h.add(date(2025, 1, 2), 0.20)
    h.add(date(2025, 1, 3), 0.90)  # same day: must not count
    assert h.percentile(date(2025, 1, 3), 0.15) == (50.0, 2)
    assert h.percentile(date(2025, 1, 2), 0.15) == (None, 1)


def test_daily_pnl_marks_open_structures() -> None:
    d1, d2, d3 = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)
    leg = LegTrade("x", "CE", 1, "SELL", 1, _at(d1, "10:00"), 10, 10, _at(d3, "11:00"), 4, 4)
    tr = CandleTrade("condor", "IRON_CONDOR", d3, _at(d1, "10:00"), "", [leg], _at(d3, "11:00"))
    tr.marks = {d1: -2.0, d2: 3.0}
    assert daily_pnl([tr], [d1, d2, d3]) == [-2.0, 5.0, 3.0]  # sums to the net, 6


def test_trial_registry_counts_distinct_parameter_sets(tmp_path: Path) -> None:
    p = tmp_path / "trials.jsonl"
    now = datetime(2026, 11, 13, tzinfo=IST)
    _, n1 = register_trial(p, "log3", Log3Params(), now)
    _, n2 = register_trial(p, "log3", Log3Params(), now)
    _, n3 = register_trial(p, "log3", Log3Params(mom_min_pct=0.08), now)
    _, n4 = register_trial(p, "condor", CondorConfig(), now)
    assert (n1, n2, n3, n4) == (1, 1, 2, 1)


# ------------------------------------------------------------------- simulators (synthetic)


def test_log3_enters_on_the_push_and_decides_point_in_time(book: RuleBook) -> None:
    days = _weekdays(date(2025, 1, 6), date(2025, 1, 10))
    src = Synth(days, _trend, lambda di: 0.13)
    spec = resolve("log3")
    out = spec.run(_ctx(book, src), [days[1]], Log3Params())
    assert out.trades, out.skips
    tr = out.trades[0]
    assert tr.structure == "LONG_CALL"
    assert hhmm(tr.entry_at) >= "10:01"
    assert tr.charges > 0 and tr.haircut_inr > 0
    # Changing every index bar after the decision must not change the entry.
    later = tr.entry_at - timedelta(minutes=1)

    def spoiled(di: int, i: int) -> float:
        return _trend(di, i) if _at(days[di], "09:15") + timedelta(minutes=i) <= later else 20000.0

    src2 = Synth(days, spoiled, lambda di: 0.13)
    src2.option_bars = src.option_bars  # type: ignore[method-assign]
    out2 = spec.run(_ctx(book, src2), [days[1]], Log3Params())
    assert out2.trades and out2.trades[0].entry_at == tr.entry_at


def hhmm(at: datetime) -> str:
    return at.astimezone(IST).strftime("%H:%M")


def test_condor_hedge_first_and_out_before_expiry(book: RuleBook) -> None:
    days = _weekdays(date(2025, 1, 1), date(2025, 1, 17))
    src = Synth(days, _calm, lambda di: 0.12 + 0.003 * di)  # IV rising: percentile 100
    ctx = _ctx(book, src, iv_history_from=days[0], iv_min_history=3)
    rng = [d for d in days if d >= date(2025, 1, 8)]
    out = resolve("condor").run(ctx, rng, CondorConfig())
    assert out.trades, out.skips
    tr = out.trades[0]
    assert [lg.side for lg in tr.legs] == ["BUY", "SELL", "BUY", "SELL"]  # wing, then short
    for kind in ("PE", "CE"):
        w, s = [lg for lg in tr.legs if lg.kind == kind]
        assert w.entry_at <= s.entry_at
        assert abs(w.strike - s.strike) == tr.width
    assert tr.exit_at is not None and tr.exit_at.date() < tr.expiry
    assert tr.margin_inr == 85000.0
    assert (tr.width - tr.credit_pts) * 75 <= 10_000  # per-trade limit (5% of ₹2L)


def test_run_and_report_say_candle_approximation(book: RuleBook, tmp_path: Path) -> None:
    days = _weekdays(date(2025, 1, 6), date(2025, 1, 10))
    src = Synth(days, _trend, lambda di: 0.13)
    res = run_candle_backtest(
        resolve("log3"),
        Log3Params(),
        lambda sc: _ctx(
            book, src, 2.0 if sc == "stress" else 1.0, charges_mult=2.0 if sc == "stress" else 1.0
        ),
        days,
        trials_path=tmp_path / "t.jsonl",
        mc_paths=50,
    )
    assert res.s1.trades >= 1 and res.n_trials == 1
    assert res.s2.haircut_paid > res.s1.haircut_paid
    text = render_report(res, "x.csv")
    assert "CANDLE APPROXIMATION" in text and "2x max(₹0.1, 0.5% of price), charges x2" in text
    assert "Deflated Sharpe" in text and "Walk-forward" in text


# ------------------------------------------------------------------------- Upstox source


TOKEN = "SYNTHETIC-TOKEN-NOT-REAL"


class FakeHttp:
    def __init__(self, fail_first: int = 0) -> None:
        self.urls: list[str] = []
        self.fail = fail_first

    def __call__(self, url: str, token: str) -> dict[str, Any]:
        assert token == TOKEN
        self.urls.append(url)
        if self.fail:
            self.fail -= 1
            raise urllib.error.HTTPError(url, 429, "slow down", {}, None)  # type: ignore[arg-type]
        if "expiries" in url:
            return {"data": ["2025-01-09"]}
        if "option/contract" in url:
            return {"data": [{"instrument_key": "NSE_FO|1", "strike_price": 23500.0,
                              "instrument_type": "CE", "lot_size": 75}]}  # fmt: skip
        return {
            "data": {
                "candles": [
                    ["2025-01-08T09:16:00+05:30", 2, 3, 1, 2.5, 10, 7],
                    ["2025-01-08T09:15:00+05:30", 1, 2, 1, 2, 5, 6],
                ]
            }
        }


def _budget() -> RequestBudget:
    return RequestBudget(clock=lambda: 0.0, sleep=lambda s: None, limits=(), min_gap_s=0)


def test_upstox_source_caches_and_uses_range_urls(tmp_path: Path) -> None:
    http = FakeHttp()
    live = {date(2026, 11, 17): {(26000.0, "CE"): ("NSE_FO|9", 65)}}
    kw: dict[str, Any] = {"today": date(2026, 11, 13), "live": live, "budget": _budget()}
    src = UpstoxCandleSource(TOKEN, http, tmp_path, **kw)
    assert src.expiries() == [date(2025, 1, 9), date(2026, 11, 17)]
    c = src.contracts(date(2025, 1, 9))[(23500.0, "CE")]
    assert c.lot == 75
    bars = src.option_bars(c)
    assert [b.close for b in bars] == [2.0, 2.5] and bars[0].oi == 6  # oldest first
    assert (
        "/expired-instruments/historical-candle/NSE_FO%7C1/1minute/2025-01-09/2024-12-26"
        in http.urls[-1]
    )
    lc = src.contracts(date(2026, 11, 17))[(26000.0, "CE")]
    src.option_bars(lc)
    assert "/v3/historical-candle/NSE_FO%7C9/minutes/1/2026-11-12/2026-11-03" in http.urls[-1]
    n = len(http.urls)
    again = UpstoxCandleSource(TOKEN, http, tmp_path, **kw)
    again.expiries()
    again.option_bars(again.contracts(date(2025, 1, 9))[(23500.0, "CE")])
    assert len(http.urls) == n  # everything came from the cache
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert TOKEN.encode() not in f.read_bytes()


def test_upstox_source_backs_off_on_429(tmp_path: Path) -> None:
    http = FakeHttp(fail_first=2)
    src = UpstoxCandleSource(TOKEN, http, tmp_path, today=date(2026, 11, 13), budget=_budget())
    assert src.expiries() == [date(2025, 1, 9)]
    assert len(http.urls) == 3


def test_request_budget_waits_inside_the_window() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    b = RequestBudget(limits=((2, 60.0),), min_gap_s=0, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        b.wait()
    assert slept and now[0] >= 60.0


def test_parse_candles_sorts_oldest_first() -> None:
    bars = parse_candles(
        [["2025-01-08T09:16:00+05:30", 1, 1, 1, 1], ["2025-01-08T09:15:00+05:30", 2, 2, 2, 2]]
    )
    assert bars[0].close == 2 and bars[0].volume == 0


# ------------------------------------------------------------ condor variants (13 Nov evening)


def test_stress_doubles_charges(book: RuleBook) -> None:
    one = Costs(book, date(2026, 11, 13)).order("SELL", 100.0, 65, date(2025, 3, 3))
    two = Costs(book, date(2026, 11, 13), charges_mult=2.0).order(
        "SELL", 100.0, 65, date(2025, 3, 3)
    )
    assert two == pytest.approx(2 * one)


def test_condor_variant_grid_is_eight_distinct_trials(tmp_path: Path) -> None:
    vs = condor_variants(CondorConfig())
    assert len(vs) == 10 and "live config" in vs[0].label
    assert vs[8].cfg.side_stop_mult == 0 and vs[9].skip_pre_closure
    assert {(v.structure, v.cfg.short_delta, v.cfg.stop_mult) for v in vs[:8]} == {
        (s_, d, m)
        for s_ in ("IRON_CONDOR", "TREND_SPREAD")
        for d in (0.16, 0.20)
        for m in (2.0, 1.5)
    }
    now = datetime(2026, 11, 13, tzinfo=IST)
    counts = [register_trial(tmp_path / "t.jsonl", "condor", v, now)[1] for v in vs]
    assert counts == list(range(1, 11))


def test_trend_spread_sells_one_side_with_economics(book: RuleBook) -> None:
    days = _weekdays(date(2025, 1, 1), date(2025, 1, 17))
    src = Synth(days, _calm, lambda di: 0.12 + 0.003 * di)
    ctx = _ctx(book, src, iv_history_from=days[0], iv_min_history=3)
    rng = [d for d in days if d >= date(2025, 1, 8)]
    out = resolve("condor").run(ctx, rng, CondorVariant(CondorConfig(), "TREND_SPREAD"))
    assert out.trades, out.skips
    tr = out.trades[0]
    assert tr.structure in ("BULL_PUT", "BEAR_CALL") and len(tr.legs) == 2
    assert tr.legs[0].side == "BUY"  # the wing first
    assert tr.margin_inr == 49500.0
    eco = tr.context["economics"]
    assert eco["net_tp"] is not None and 0 < eco["breakeven_win_pct"] < 100
    assert eco["tp_on_margin_pct"] == pytest.approx(eco["net_tp"] / 49500 * 100)


def _weekend_trade(ctx: Ctx, at: datetime) -> tuple[CandleTrade, dict[str, float]]:
    """An open iron condor whose cost to close is 62.5% of its credit (75% of the take-profit in
    hand) at ``at``, legs priced from the synthetic market."""
    m = ctx.market
    exp = date(2025, 1, 16)
    legs, px = [], {}
    for kind, short_k, wing_k in (("PE", 23000.0, 22900.0), ("CE", 24000.0, 24100.0)):
        cs, cw = m.contract(exp, short_k, kind), m.contract(exp, wing_k, kind)
        assert cs is not None and cw is not None
        ps, pw = m.close(cs, at, None), m.close(cw, at, None)
        assert ps and pw
        side_ctc = ps - pw  # exit rules read last trades
        legs.append(LegTrade(f"{wing_k:g}{kind}", kind, wing_k, "BUY", 75, at, 1.0, 1.0))
        legs.append(
            LegTrade(
                f"{short_k:g}{kind}",
                kind,
                short_k,
                "SELL",
                75,
                at,
                side_ctc / 0.75 + 1.0,
                side_ctc / 0.75 + 1.0,
            )
        )
        px[f"{short_k:g}{kind}"], px[f"{wing_k:g}{kind}"] = ps, pw
    credit = sum(lg.entry_px if lg.side == "SELL" else -lg.entry_px for lg in legs)
    tr = CandleTrade("condor", "IRON_CONDOR", exp, at, "", legs, credit_pts=credit, width=100)
    return tr, px


def test_weekend_books_when_little_is_left_vs_the_gap_risk(book: RuleBook) -> None:
    days = _weekdays(date(2025, 1, 6), date(2025, 1, 16))
    src = Synth(days, _calm, lambda di: 0.13)
    ctx = _ctx(book, src)
    fri = date(2025, 1, 10)  # Saturday is closed
    bar = next(b for b in src.index_bars(fri) if hhmm(b.start) == "15:14")
    tr, px = _weekend_trade(ctx, bar.start)
    cfg = CondorConfig()
    why = _condor_exit(ctx, cfg, tr, bar, px)
    assert why is not None and "gap risk" in why
    from dataclasses import replace

    assert _condor_exit(ctx, replace(cfg, weekend_reward_gap_ratio=0.0), tr, bar, px) is None
    wed = next(b for b in src.index_bars(date(2025, 1, 8)) if hhmm(b.start) == "15:14")
    tr2, px2 = _weekend_trade(ctx, wed.start)
    assert _condor_exit(ctx, cfg, tr2, wed, px2) is None  # Thursday trades: no closure


def test_overrides_make_a_new_trial(tmp_path: Path) -> None:
    from engine.backtest.candles.strategies import apply_overrides

    base = CondorVariant(CondorConfig())
    off = apply_overrides(base, "side_stop_mult=0,wing_widths=150/100")
    assert off.cfg.side_stop_mult == 0.0 and off.cfg.wing_widths == (150.0, 100.0)
    assert "side_stop_mult=0" in off.label
    assert apply_overrides(Log3Params(), "mom_min_pct=0.08").mom_min_pct == 0.08
    with pytest.raises(ValueError, match="unknown parameter"):
        apply_overrides(base, "no_such_field=1")
    now = datetime(2026, 11, 13, tzinfo=IST)
    p = tmp_path / "t.jsonl"
    assert register_trial(p, "condor", base, now)[1] == 1
    assert register_trial(p, "condor", off, now)[1] == 2


def test_side_stop_zero_means_off(book: RuleBook) -> None:
    from dataclasses import replace

    days = _weekdays(date(2025, 1, 6), date(2025, 1, 16))
    src = Synth(days, _calm, lambda di: 0.13)
    ctx = _ctx(book, src)
    wed = next(b for b in src.index_bars(date(2025, 1, 8)) if hhmm(b.start) == "11:00")
    tr, px = _weekend_trade(ctx, wed.start)
    for lg in tr.legs:  # the put side now costs 3x its credit to close; the whole far less
        if lg.kind == "PE" and lg.side == "SELL":
            lg.entry_px = lg.entry_ref = 1.0 + (px["23000PE"] - px["22900PE"]) / 3
    tr.credit_pts = sum(lg.entry_px if lg.side == "SELL" else -lg.entry_px for lg in tr.legs)
    cfg = CondorConfig(stop_mult=99.0, exit_on_short_touch=False)
    assert "put side stop" in (_condor_exit(ctx, cfg, tr, wed, px) or "")
    assert _condor_exit(ctx, replace(cfg, side_stop_mult=0.0), tr, wed, px) is None


# --------------------------------------------- model 2 (14 Nov review): credit at trade prices


def test_stops_compare_against_the_credit_at_trade_prices(book: RuleBook) -> None:
    """2025-02-04 shape: a put side worth 14.1 at trade prices but 11.1 after the haircut.
    A cost to close of 24.4 must not stop it (2x / 2.2x of 14.1 are 28.2 / 31.0)."""
    d = date(2025, 1, 8)  # a Wednesday: no closure rules
    at = _at(d, "11:00")
    legs = [
        LegTrade("22900PE", "PE", 22900, "BUY", 75, at, 5.0, 6.5),
        LegTrade("23000PE", "PE", 23000, "SELL", 75, at, 19.1, 17.6),
    ]
    tr = CandleTrade("condor", "BULL_PUT", date(2025, 1, 16), at, "", legs, credit_pts=11.1)
    ctx = _ctx(book, MemorySource({}, {}, {}))
    bar = Bar(_at(d, "11:05"), 23500, 23501, 23499, 23500)
    cfg = CondorConfig(exit_on_short_touch=False)
    assert _condor_exit(ctx, cfg, tr, bar, {"22900PE": 5.0, "23000PE": 29.4}) is None
    assert "stop" in (_condor_exit(ctx, cfg, tr, bar, {"22900PE": 5.0, "23000PE": 37.0}) or "")
    ctx.trigger_on_haircut = True  # the old model: the shrunk credit sets the levels
    assert "stop" in (_condor_exit(ctx, cfg, tr, bar, {"22900PE": 5.0, "23000PE": 29.4}) or "")


def test_no_entries_before_a_closure(book: RuleBook) -> None:
    days = _weekdays(date(2025, 1, 1), date(2025, 1, 17))
    src = Synth(days, _calm, lambda di: 0.12 + 0.003 * di)
    ctx = _ctx(book, src, iv_history_from=days[0], iv_min_history=3)
    rng = [d for d in days if d >= date(2025, 1, 8)]
    var = CondorVariant(CondorConfig(), skip_pre_closure=True)
    out = resolve("condor").run(ctx, rng, var)
    assert out.skips["last session before a market closure"] >= 1
    assert all(t.entry_at.weekday() != 4 for t in out.trades)


def test_measure_spreads_from_recorded_quotes(tmp_path: Path) -> None:
    import duckdb

    from engine.backtest.candles.spreads import haircut_table, measure_spreads, render_spreads

    day = tmp_path / "date=2026-11-12"
    day.mkdir()
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT TIMESTAMPTZ '2026-11-12 11:00:00+05:30' + to_seconds(i) AS ts, 'NIFTY' "
        "AS underlying, CASE WHEN i % 2 = 0 THEN 'CE' ELSE 'PE' END AS kind, 'snap' AS capture, "
        "DATE '2026-11-12' AS date, CASE WHEN i < 100 THEN DATE '2026-11-17' "
        "ELSE DATE '2026-12-29' END AS expiry, "
        "CASE WHEN i < 50 THEN 20.0 ELSE 150.0 END AS ltp, "
        "CASE WHEN i < 50 THEN 19.95 WHEN i < 100 THEN 149.8 ELSE 140.0 END AS bid, "
        "CASE WHEN i < 50 THEN 20.05 WHEN i < 100 THEN 150.2 ELSE 160.0 END AS ask "
        "FROM range(120) t(i)) "
        f"TO '{(day / 'q.parquet').as_posix()}' (FORMAT parquet)"
    )
    con.close()
    buckets, days = measure_spreads(tmp_path)  # the far expiry's wide quotes are left out
    assert days == ["2026-11-12"]
    by = {b.lo: b for b in buckets}
    assert by[10.0].n == 50 and by[10.0].median_inr == pytest.approx(0.05)
    assert by[100.0].p90_inr == pytest.approx(0.2)
    assert dict(haircut_table(buckets))[10.0] == pytest.approx(0.25)
    assert "Median ₹" in render_spreads(buckets, days)
