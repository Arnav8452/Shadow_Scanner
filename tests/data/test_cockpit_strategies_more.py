"""Debit spreads on confirmed setups, the SPEC 5.10 gamma breakout, and score-follow on its
own account."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from engine.cockpit.condor import SpreadAccount
from engine.cockpit.paper import Book
from engine.cockpit.strategies import (
    DebitConfig,
    DebitSpreadStrategy,
    GammaBreakoutStrategy,
    GammaConfig,
)
from engine.core.config import load_settings
from engine.core.rules import IST, RuleBook
from engine.data.reference.instrument_master import Kind
from tests.data.conftest import WEEKLY, opt_symbol
from tests.data.test_cockpit import _account

OPEN = datetime(2026, 11, 11, 9, 15, tzinfo=IST)
STRIKES = [float(k) for k in range(24800, 25250, 50)]


def _bk(bid: float, ask: float, qty: int = 1300) -> Book:
    return Book(
        tuple((round(bid - i * 0.05, 2), qty, 5) for i in range(10)),
        tuple((round(ask + i * 0.05, 2), qty, 5) for i in range(10)),
    )


def _debit(repo_root: Path, tmp_path: Path) -> tuple[DebitSpreadStrategy, list[list[str]]]:
    settings = load_settings(root=repo_root, environ={})
    acct = SpreadAccount(
        "debit", book=RuleBook.load(repo_root / "config" / "rules"), limits=settings.risk,
        broker_name="upstox_plus", session_open=OPEN, journal=tmp_path / "paper_debit.jsonl",
    )  # fmt: skip
    pins: list[list[str]] = []
    st = DebitSpreadStrategy(
        cfg=DebitConfig(), account=acct, strikes_of=lambda und, kind: (WEEKLY, STRIKES),
        option_symbol=lambda u, e, k, kind: opt_symbol(e, int(k), Kind.CALL if kind == "CE" else Kind.PUT),
        lot_size=lambda s: 65, pin=pins.append, context=lambda und: {"underlying": und},
    )  # fmt: skip
    return st, pins


ATM_C, FAR_C = opt_symbol(WEEKLY, 25000, Kind.CALL), opt_symbol(WEEKLY, 25100, Kind.CALL)


def test_debit_spread_enters_on_a_confirmed_setup_and_exits_on_its_rules(
    repo_root: Path, tmp_path: Path
) -> None:
    st, pins = _debit(repo_root, tmp_path)
    t = OPEN.replace(hour=10, minute=30)
    st.on_confirmed(und="NIFTY", bullish=True, score=78, ref="s0", spot=25010.0, now=t)
    assert st.plan is None  # below the score threshold
    st.on_confirmed(und="NIFTY", bullish=True, score=85, ref="s1", spot=25010.0, now=t)
    assert st.plan is not None and pins[-1] == [ATM_C, FAR_C]  # buy ATM, sell 2 strikes out
    out = st.step(t + timedelta(seconds=2), {ATM_C: _bk(100.0, 100.5), FAR_C: _bk(60.0, 60.5)})
    assert out and out[0].startswith("opened bull call")
    pos = st.account.positions[0]
    assert pos.credit_pts < 0 and pos.max_loss_inr == round(-pos.credit_pts * 65, 2)  # debit
    assert pos.context["underlying"] == "NIFTY"
    # Not flipping: a DOWN setup the same day is ignored.
    st.on_confirmed(und="NIFTY", bullish=False, score=95, ref="s2", spot=25010.0, now=t)
    assert st.plan is None
    debit = -pos.credit_pts  # ~40.5
    mid = {ATM_C: _bk(110.0, 110.5), FAR_C: _bk(65.0, 65.5)}  # worth ~44.5: hold
    assert st.exit_reason(pos, t + timedelta(minutes=5), mid) is None
    rich = {ATM_C: _bk(150.0, 150.5), FAR_C: _bk(70.0, 70.5)}  # worth ~79.5 >= target
    assert "target" in str(st.exit_reason(pos, t + timedelta(minutes=9), rich))
    poor = {ATM_C: _bk(70.0, 70.5), FAR_C: _bk(50.0, 50.5)}  # worth ~19.5 <= half the debit
    assert debit > 0 and "stop" in str(st.exit_reason(pos, t + timedelta(minutes=9), poor))
    late = OPEN.replace(hour=15, minute=20)
    assert st.exit_reason(pos, late, mid) == "square-off"
    out = st.step(late, mid)
    assert any(x.startswith("closed bull call") for x in out) and not st.account.positions


def test_debit_spread_bearish_uses_puts_below(repo_root: Path, tmp_path: Path) -> None:
    st, pins = _debit(repo_root, tmp_path)
    st.on_confirmed(und="NIFTY", bullish=False, score=85, ref="s1", spot=25010.0,
                    now=OPEN.replace(hour=11, minute=0))  # fmt: skip
    assert pins[-1] == [opt_symbol(WEEKLY, 25000, Kind.PUT), opt_symbol(WEEKLY, 24900, Kind.PUT)]
    st.plan.made_at -= timedelta(minutes=5)  # type: ignore[union-attr]
    out = st.step(OPEN.replace(hour=11, minute=2), {})
    assert out == ["debit spread plan dropped: leg books did not arrive in time"]


# ---------------------------------------------------------------- gamma breakout


def _gamma(repo_root: Path, tmp_path: Path, oi_now: int) -> tuple[GammaBreakoutStrategy, Any]:
    acct = _account(repo_root, tmp_path, "gamma")
    rows = {"oi": 10_000}
    st = GammaBreakoutStrategy(
        cfg=GammaConfig(), account=acct, expiry_today=lambda und, d: True,
        atm_option=lambda und, kind: opt_symbol(WEEKLY, 25000, Kind.CALL if kind == "CE" else Kind.PUT),
        chain_rows=lambda und: [{"k": 25000.0, "ce": {"oi": rows["oi"]}, "pe": {"oi": 9_000}}],
    )  # fmt: skip
    return st, rows


def _feed_day(st: GammaBreakoutStrategy, rows: dict[str, int], oi_after: int) -> datetime:
    """A quiet morning around 25,000, then a breakout bar at 13:50 on heavy volume."""
    t = OPEN
    vol = 0.0
    while t < OPEN.replace(hour=13, minute=50):
        if t > OPEN.replace(hour=13, minute=30):
            rows["oi"] = oi_after  # call writers cover after the 13:30 snapshot
        vol += 100
        spot = 25000.0 + (10 if t.minute % 2 else -10)
        st.on_tick(t, "NIFTY", spot, spot + 20, vol)
        t += timedelta(minutes=1)
    for i in range(5):  # 13:50-13:54: breakout, 10x volume per minute
        vol += 1000
        st.on_tick(t, "NIFTY", 25060.0 + i * 5, 25080.0, vol)
        t += timedelta(minutes=1)
    st.on_tick(t, "NIFTY", 25085.0, 25105.0, vol + 100)  # 13:55: the 13:50 bar is complete
    return t


def test_gamma_breakout_enters_once_after_compression_volume_and_covering(
    repo_root: Path, tmp_path: Path
) -> None:
    st, rows = _gamma(repo_root, tmp_path, 8_000)
    t = _feed_day(st, rows, oi_after=8_000)
    book = {opt_symbol(WEEKLY, 25000, Kind.CALL): _bk(90.0, 90.5)}
    text = st.check(t, "NIFTY", book)
    assert text and text.startswith("bought"), (text, st.days["NIFTY"].why)
    pos = st.account.positions[opt_symbol(WEEKLY, 25000, Kind.CALL)]
    assert pos.stop_price == round(pos.avg_price * 0.7, 2) and pos.trail == ((80.0, 40.0),)
    assert st.check(t + timedelta(minutes=5), "NIFTY", book) is None  # one attempt per expiry


def test_gamma_breakout_needs_short_covering(repo_root: Path, tmp_path: Path) -> None:
    st, rows = _gamma(repo_root, tmp_path, 12_000)
    t = _feed_day(st, rows, oi_after=12_000)  # call OI rose: writers adding, not covering
    assert st.check(t, "NIFTY", {}) is None
    assert "no short covering" in st.days["NIFTY"].why


def test_gamma_breakout_skips_wide_mornings_and_non_expiry_days(
    repo_root: Path, tmp_path: Path
) -> None:
    st, rows = _gamma(repo_root, tmp_path, 8_000)
    st.on_tick(OPEN.replace(hour=9, minute=20), "NIFTY", 24800.0, 24820.0, 100)  # wide morning
    t = _feed_day(st, rows, oi_after=8_000)
    assert st.check(t, "NIFTY", {}) is None and "no compression" in st.days["NIFTY"].why
    st2, rows2 = _gamma(repo_root, tmp_path / "b", 8_000)
    st2.expiry_today = lambda und, d: False
    t2 = _feed_day(st2, rows2, oi_after=8_000)
    assert st2.check(t2, "NIFTY", {}) is None and st2.days["NIFTY"].why == "not an expiry day"


def test_score_follow_trades_on_its_own_account(repo_root: Path, tmp_path: Path) -> None:
    from engine.shadow.models import ShadowLedgerOutcome, ShadowSignalStatus
    from tests.data.test_cockpit import _core, _signal

    core = _core(repo_root)
    core.bot = _account(repo_root, tmp_path, "bot")
    core.manual = _account(repo_root, tmp_path, "manual")
    core.score_acct = _account(repo_root, tmp_path, "score")
    t = OPEN.replace(hour=10, minute=30)
    sig = _signal("NSE:NIFTY26NOVFUT", "LONG", t)
    core._bot_enter(sig, t)
    call = opt_symbol(WEEKLY, 25000, Kind.CALL)
    assert call in core.score_acct.positions and not core.bot.positions  # 5.2 stays separate
    assert core.score_acct.positions[call].context["underlying"] == "NIFTY"
    core.runner.scanner.closed_outcomes.append(  # type: ignore[union-attr]
        ShadowLedgerOutcome(
            sig.signal_id, sig.symbol, "LONG", sig.wave, 85.0, ShadowSignalStatus.HIT_T1,
            25010.0, 25040.0, 24990.0, 25040.0, 25060.0, 65, 1950.0, 300.0, 1650.0, 1.2,
            t, t + timedelta(minutes=4), "Hit Target 1",
        )
    )  # fmt: skip
    core._follow_track_a(t + timedelta(minutes=4))
    assert call not in core.score_acct.positions
    assert core.score_acct.closed[-1].reason == "signal exit (HIT_T1)"
