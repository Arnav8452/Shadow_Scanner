"""13 Nov 2026 fixes: live-feed-first delta picks, stops fitted to the per-trade limit, no
attempts past the daily trade cap, refusal de-dupe per contract."""

from __future__ import annotations

from pathlib import Path

from engine.cockpit.plays_long import PutPanicPlay
from engine.data.reference.instrument_master import Kind
from tests.data.conftest import WEEKLY, opt_symbol
from tests.data.test_cockpit import _account
from tests.data.test_plays import FakeMarket, bk, mkt  # noqa: F401  (fixture)

CE25000 = opt_symbol(WEEKLY, 25000, Kind.CALL)
CE24950 = opt_symbol(WEEKLY, 24950, Kind.CALL)


def _rows() -> list[dict[str, object]]:
    return [
        {"k": 24950.0, "ce": {"sym": CE24950, "delta": 0.66}, "pe": {}},
        {"k": 25000.0, "ce": {"sym": CE25000, "delta": 0.61}, "pe": {}},
    ]


def test_by_delta_prefers_a_live_contract_and_asks_for_the_chain_pick(mkt: FakeMarket) -> None:  # noqa: F811
    mkt.rows = _rows()
    mkt._books = {CE24950: bk(300, 301)}
    mkt.greeks = {CE24950: {"delta": 0.58, "gamma": 0.003, "iv": 15.0}}
    pick = mkt.by_delta("NIFTY", "CE", 0.60)
    assert pick is not None and pick[0] == CE24950  # live, within 0.05 of 0.60
    assert not mkt.__dict__.get("want_feed")
    mkt.greeks = {CE24950: {"delta": 0.70, "gamma": 0.003, "iv": 15.0}}  # live but off band
    pick = mkt.by_delta("NIFTY", "CE", 0.60)
    assert pick is not None and pick[0] == CE25000  # the chain's pick ...
    assert mkt.want_feed == [CE25000]  # ... and the play is asked to pin it


def _play(repo_root: Path, tmp_path: Path, name: str = "pp") -> PutPanicPlay:
    return PutPanicPlay("put_panic", "Put panic", "S1", _account(repo_root, tmp_path, name))


def test_stop_is_tightened_to_fit_the_per_trade_limit(
    repo_root: Path,
    tmp_path: Path,
    mkt: FakeMarket,  # noqa: F811
) -> None:
    p = _play(repo_root, tmp_path)
    mkt._books[CE25000] = bk(299.5, 300.0)
    # 35% of Rs 300 x 65 = Rs 6,825 > Rs 5,000: fitted to about 25%, not refused.
    out = p.enter(mkt, "NIFTY", CE25000, lots=1, reason="t", bullish=True, stop_pct=35)
    assert out.startswith("bought"), (out, p.decision)
    pos = p.account.positions[CE25000]
    assert (pos.avg_price - pos.stop_price) * pos.qty <= 5000
    assert "to fit the limit" in pos.reason
    # Rs 1,000 premium: a fitting stop would be ~7.5%, under 10%: skipped, nothing journaled.
    p2 = _play(repo_root, tmp_path / "b")
    mkt._books[CE25000] = bk(999, 1000)
    assert p2.enter(mkt, "NIFTY", CE25000, lots=1, reason="t", bullish=True, stop_pct=35) == ""
    assert "too dear" in " ".join(p2.notes) and not p2.account.refusals


def test_no_attempts_once_the_daily_trade_cap_is_reached(
    repo_root: Path,
    tmp_path: Path,
    mkt: FakeMarket,  # noqa: F811
) -> None:
    p = _play(repo_root, tmp_path)
    p.account.trades_today = p.account.limits.max_trades_per_day
    mkt._books[CE25000] = bk(99.5, 100.0)
    assert p.enter(mkt, "NIFTY", CE25000, lots=1, reason="t", bullish=True) == ""
    assert not p.account.refusals and "daily trade cap" in " ".join(p.notes)


def test_refusals_are_deduplicated_per_contract_not_just_the_last(
    repo_root: Path,
    tmp_path: Path,
    mkt: FakeMarket,  # noqa: F811
) -> None:
    acct = _account(repo_root, tmp_path)
    for i in range(30):  # two contracts alternating every second
        sym = CE25000 if i % 2 else CE24950
        acct._refuse(sym, mkt.now.replace(second=i), "in_loss_cooloff (9.1m < 15m)", "")
    assert len(acct.refusals) == 2


def test_married_future_history_is_rebuilt_after_a_restart(repo_root: Path, tmp_path: Path) -> None:
    from datetime import timedelta

    from engine.cockpit.plays_spread import MarriedFuturePlay
    from tests.data.test_plays import D, _spread

    p = MarriedFuturePlay("married", "Married", "F2", _spread(repo_root, tmp_path, "mf"))
    # Quotes every 20 s since 09:15: future 25,100 -> basis vs 25,000 spot, OI rising.
    samples = [
        (D + timedelta(seconds=20 * i), 25100.0, 25000.0, 1_000_000 + 100 * i) for i in range(150)
    ]
    n = p.seed_history("NIFTY", samples, WEEKLY + timedelta(days=6))
    assert n == 50  # one per minute
    assert len(p.fut_oi["NIFTY"]) == len(p.basis["NIFTY"]) == 50
    assert p.basis["NIFTY"][0] > 0
    # A longer live history is never replaced by a shorter rebuild.
    assert p.seed_history("NIFTY", samples[:30], None) == 10 and len(p.fut_oi["NIFTY"]) == 50


def test_term_spread_backfill_recovers_the_iv_from_synchronous_candles(repo_root: Path) -> None:
    from datetime import date, datetime, time, timedelta

    from engine.analytics.expiry_clock import expiry_clock
    from engine.analytics.iv import black76_price
    from engine.cockpit.term_backfill import atm_iv, monthly_expiries, pick_expiries
    from engine.core.rules import IST, RuleBook

    book = RuleBook.load(repo_root / "config" / "rules")
    exps = [date(2026, 11, 3), date(2026, 11, 9), date(2026, 11, 17), date(2026, 11, 23),
            date(2026, 12, 1), date(2026, 12, 29)]  # fmt: skip
    assert monthly_expiries(exps) == [date(2026, 11, 23), date(2026, 12, 29)]
    assert pick_expiries(date(2026, 11, 4), exps) == (date(2026, 11, 9), date(2026, 11, 23))
    assert pick_expiries(date(2026, 11, 12), exps) == (date(2026, 11, 17), date(2026, 12, 29))

    day, exp, k, f, r = date(2026, 11, 12), date(2026, 11, 17), 25000.0, 25040.0, 0.065
    calls, puts = [], []
    for i in range(60):  # 14:30-15:29, priced at 15% IV
        t = datetime.combine(day, time(14, 30), tzinfo=IST) + timedelta(minutes=i)
        clk = expiry_clock(t + timedelta(seconds=59), exp, book=book, calendar=None)
        kw = {"t_cal": clk.t_cal, "risk_free_rate": r}
        c = black76_price(f, k, clk.t_var, 0.15, is_call=True, **kw)
        p = black76_price(f, k, clk.t_var, 0.15, is_call=False, **kw)
        calls.append([t.isoformat(), c, c, c, c, 100, 0])
        puts.append([t.isoformat(), p, p, p, p, 0 if i % 2 else 100, 0])  # put trades every 2nd
    iv = atm_iv(day, exp, {k: (calls, puts)}, book=book, calendar=None, r=r)
    assert iv is not None and abs(iv - 15.0) < 0.05
    assert atm_iv(day, exp, {k: (calls[:4], puts[:4])}, book=book, calendar=None, r=r) is None
