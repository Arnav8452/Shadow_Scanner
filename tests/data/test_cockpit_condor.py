"""Setups (readings -> confirmed ideas) and SPEC 5.1 condor / credit spreads on paper."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from engine.cockpit.condor import (
    CondorConfig,
    CondorStrategy,
    Leg,
    SpreadAccount,
    cost_to_close,
    previous_trading_day,
)
from engine.cockpit.paper import Book
from engine.cockpit.setups import SetupConfig, SetupTracker
from engine.core.config import load_settings
from engine.core.rules import IST, RuleBook
from engine.data.reference.instrument_master import Kind
from tests.data.conftest import DAY, WEEKLY, opt_symbol

OPEN = datetime(2026, 11, 11, 9, 15, tzinfo=IST)
T = OPEN.replace(hour=11)

# ---------------------------------------------------------------- setups


def _tracker(tmp_path: Path | None = None) -> SetupTracker:
    return SetupTracker(SetupConfig(), journal=tmp_path / "setups.jsonl" if tmp_path else None)


def _read(tr: SetupTracker, at: datetime, up: bool = True, spot: float = 25000.0) -> Any:
    return tr.add_reading(
        underlying="NIFTY", bullish=up, now=at, spot=spot, label="x", score=82, wave="W"
    )


def test_setup_confirms_only_after_follow_through_and_hold(tmp_path: Path) -> None:
    tr = _tracker(tmp_path)
    s = _read(tr, T)
    assert s.state == "FORMING"
    assert not tr.update(T + timedelta(seconds=30), {"NIFTY": 25005.0})  # +0.02%: not yet
    assert not tr.update(T + timedelta(seconds=40), {"NIFTY": 25015.0})  # past the line...
    assert not tr.update(T + timedelta(seconds=80), {"NIFTY": 25016.0})  # ...held 40 s
    done = tr.update(T + timedelta(seconds=101), {"NIFTY": 25014.0})  # held 61 s
    assert done == [s] and s.state == "CONFIRMED" and s.confirm_spot == 25014.0
    assert tr.trend("NIFTY", T + timedelta(minutes=2), 30) == {"up": 1, "down": 0, "forming": []}
    # Giving back the whole move ends it; it still counts in the look-back window.
    tr.update(T + timedelta(minutes=5), {"NIFTY": 24999.0})
    assert s.state == "ENDED" and "gave back" in s.why
    assert tr.trend("NIFTY", T + timedelta(minutes=10), 30)["up"] == 1
    assert tr.trend("NIFTY", T + timedelta(minutes=40), 30)["up"] == 0
    # A restart the same day reloads finished setups.
    again = SetupTracker(SetupConfig(), journal=tmp_path / "setups.jsonl", day=T)
    assert [r["state"] for r in again.history] == ["ENDED"]


def test_setup_dip_below_the_line_resets_the_hold() -> None:
    tr = _tracker()
    s = _read(tr, T)
    tr.update(T + timedelta(seconds=10), {"NIFTY": 25015.0})
    tr.update(T + timedelta(seconds=50), {"NIFTY": 25005.0})  # back inside: timer resets
    tr.update(T + timedelta(seconds=60), {"NIFTY": 25015.0})
    assert not tr.update(T + timedelta(seconds=100), {"NIFTY": 25015.0})  # only 40 s
    assert tr.update(T + timedelta(seconds=121), {"NIFTY": 25015.0}) == [s]


def test_setup_fails_on_opposite_reading_adverse_move_or_timeout() -> None:
    tr = _tracker()
    up = _read(tr, T)
    down = _read(tr, T + timedelta(seconds=30), up=False)
    assert up.state == "FAILED" and "opposite" in up.why and down.state == "FORMING"
    tr.update(T + timedelta(minutes=1), {"NIFTY": 25025.0})  # +0.10% against a DOWN setup
    assert down.state == "FAILED" and "against" in down.why
    late = _read(tr, T + timedelta(minutes=2))
    tr.update(T + timedelta(minutes=13), {"NIFTY": 25001.0})
    assert late.state == "FAILED" and "no follow-through" in late.why
    assert tr.trend("NIFTY", T + timedelta(minutes=14), 30) == {"up": 0, "down": 0, "forming": []}


# ---------------------------------------------------------------- spread account


def _acct(repo_root: Path, tmp_path: Path) -> SpreadAccount:
    settings = load_settings(root=repo_root, environ={})
    return SpreadAccount(
        "condor",
        book=RuleBook.load(repo_root / "config" / "rules"),
        limits=settings.risk,
        broker_name="upstox_plus",
        session_open=OPEN,
        journal=tmp_path / "paper_condor.jsonl",
    )


def _bk(bid: float, ask: float, qty: int = 1300) -> Book:
    return Book(
        tuple((round(bid - i * 0.05, 2), qty, 5) for i in range(10) if bid - i * 0.05 > 0),
        tuple((round(ask + i * 0.05, 2), qty, 5) for i in range(10)),
    )


WP, SP = opt_symbol(WEEKLY, 24600, Kind.PUT), opt_symbol(WEEKLY, 24800, Kind.PUT)
SC, WC = opt_symbol(WEEKLY, 25200, Kind.CALL), opt_symbol(WEEKLY, 25400, Kind.CALL)


def _legs() -> list[Leg]:
    return [
        Leg(WP, "PE", 24600, "BUY", 65),
        Leg(SP, "PE", 24800, "SELL", 65),
        Leg(SC, "CE", 25200, "SELL", 65),
        Leg(WC, "CE", 25400, "BUY", 65),
    ]


def _entry_books() -> dict[str, Book]:
    return {WP: _bk(4.9, 5.0), WC: _bk(4.9, 5.0), SP: _bk(30.0, 30.1), SC: _bk(30.0, 30.1)}


def _open(acct: SpreadAccount, books: dict[str, Book] | None = None) -> tuple[bool, str]:
    return acct.open_spread(
        structure="IRON_CONDOR", underlying="NIFTY", expiry=WEEKLY, legs=_legs(), width_pts=200,
        books=books or _entry_books(), now=T, spot=25000.0, reason="test",
    )  # fmt: skip


def test_condor_buys_wings_before_selling_shorts(repo_root: Path, tmp_path: Path) -> None:
    acct = _acct(repo_root, tmp_path)
    ok, text = _open(acct)
    assert ok, text
    sides = [(o.symbol, o.side) for o in acct.broker.order_history if o.filled_qty]
    assert [s for _, s in sides] == ["BUY", "BUY", "SELL", "SELL"]
    pos = acct.positions[0]
    assert pos.credit_pts == pytest.approx(50.0)  # 2 x 30 sold - 2 x 5 bought
    assert pos.max_loss_inr == pytest.approx((200 - 50) * 65)
    assert pos.short_put == 24800 and pos.short_call == 25200


def test_condor_unwinds_wings_when_a_short_cannot_fill(repo_root: Path, tmp_path: Path) -> None:
    acct = _acct(repo_root, tmp_path)
    books = _entry_books()
    books[SC] = _bk(28.0, 30.0)  # spread too wide: the broker refuses to sell into it
    ok, text = _open(acct, books)
    assert not ok and "wings unwound" in text and not acct.positions
    filled = [o for o in acct.broker.order_history if o.filled_qty]
    # wings bought, short put sold, then unwound: short put bought back before wings sold
    assert [(o.symbol, o.side) for o in filled][-3:] == [(SP, "BUY"), (WP, "SELL"), (WC, "SELL")]


def test_condor_refused_when_worst_case_exceeds_the_limit(repo_root: Path, tmp_path: Path) -> None:
    acct = _acct(repo_root, tmp_path)
    books = _entry_books()
    books[SP], books[SC] = _bk(10.0, 10.1), _bk(10.0, 10.1)  # credit 10: loss 190 x 65
    ok, text = _open(acct, books)
    assert not ok and "per_trade_risk_exceeded" in text


def test_condor_close_buys_shorts_first_and_books_net(repo_root: Path, tmp_path: Path) -> None:
    acct = _acct(repo_root, tmp_path)
    assert _open(acct)[0]
    pos = acct.positions[0]
    books = {WP: _bk(2.0, 2.1), WC: _bk(2.0, 2.1), SP: _bk(11.9, 12.0), SC: _bk(11.9, 12.0)}
    assert cost_to_close(pos, books) == pytest.approx(2 * 12.0 - 2 * 2.0)
    n0 = len(acct.broker.order_history)
    ok, text = acct.close(pos, books, T + timedelta(hours=1), "take profit")
    assert ok, text
    exits = [(o.symbol, o.side) for o in acct.broker.order_history[n0:] if o.filled_qty]
    assert [s for _, s in exits] == ["BUY", "BUY", "SELL", "SELL"]
    t = acct.closed[-1]
    assert t.debit_pts == pytest.approx(20.0) and t.gross_inr == pytest.approx(30.0 * 65)
    assert 0 < t.net_inr < t.gross_inr


def test_condor_is_held_overnight_through_the_journal(repo_root: Path, tmp_path: Path) -> None:
    acct = _acct(repo_root, tmp_path)
    assert _open(acct)[0]
    nxt = _acct(repo_root, tmp_path)
    nxt.load_journal(date(2026, 11, 12))
    assert len(nxt.positions) == 1 and nxt.trades_today == 0
    short = next(lg for lg in nxt.positions[0].legs if lg.side == "SELL")
    assert short.entry_px == pytest.approx(30.0)
    same = _acct(repo_root, tmp_path)
    same.load_journal(DAY)
    assert same.trades_today == 1


# ---------------------------------------------------------------- strategy


def _chain() -> dict[str, Any]:
    """50-point grid around 25000: |delta| = 0.5 - d/1200 and OTM premium 80 - d/8 at distance d,
    so the 0.16-delta shorts sit 400 points out at 30."""
    rows = []
    for k in range(24300, 25751, 50):
        d = abs(k - 25000)
        delta = max(0.01, 0.5 - d / 1200)
        otm = max(0.5, 80 - d / 8)
        itm = d + 80
        ce_px, pe_px = (otm, itm) if k >= 25000 else (itm, otm)
        rows.append(
            {
                "k": float(k),
                "ce": {"bid": ce_px, "ask": ce_px + 0.05, "delta": round(delta if k >= 25000 else 1 - delta, 3)},
                "pe": {"bid": pe_px, "ask": pe_px + 0.05, "delta": round(-delta if k <= 25000 else delta - 1, 3)},
            }
        )  # fmt: skip
    return {"expiry": WEEKLY.isoformat(), "rows": rows, "gex": {"regime": "POSITIVE_GAMMA_PIN"}}


STRIP = {"spot": 25000.0, "forward": 25000.0, "iv_percentile": 60.0, "expiry": WEEKLY.isoformat()}


def _strategy(repo_root: Path, tmp_path: Path, **cfg: Any) -> tuple[CondorStrategy, list[list[str]]]:
    pins: list[list[str]] = []
    st = CondorStrategy(
        cfg=CondorConfig(**cfg),
        account=_acct(repo_root, tmp_path),
        lot_size=lambda s: 65,
        option_symbol=lambda u, e, k, kind: opt_symbol(e, int(k), Kind.CALL if kind == "CE" else Kind.PUT),
        pin=pins.append,
        trading_day_before=lambda d: previous_trading_day(d, None),
    )  # fmt: skip
    return st, pins


def _eval(st: CondorStrategy, trend: dict[str, Any] | None = None, **kw: Any) -> None:
    st.evaluate(
        kw.pop("now", T), strip=kw.pop("strip", STRIP), chain=kw.pop("chain", _chain()),
        trend=trend or {"up": 0, "down": 0, "forming": []},
        event_scale=kw.pop("event_scale", 1.0), event_names=["RBI policy"],
        events=kw.pop("events", ()),
    )  # fmt: skip


def test_condor_plan_on_a_quiet_tape(repo_root: Path, tmp_path: Path) -> None:
    st, pins = _strategy(repo_root, tmp_path)
    _eval(st)
    assert st.plan is not None, st.decision
    assert st.plan.structure == "IRON_CONDOR"
    legs = {(lg.side, lg.kind, lg.strike) for lg in st.plan.legs}
    # 0.18 delta is the nearest to 0.16 on this grid; wings 200 points further out
    assert legs == {("SELL", "PE", 24600), ("BUY", "PE", 24400),
                    ("SELL", "CE", 25400), ("BUY", "CE", 25600)}  # fmt: skip
    assert pins and len(pins[-1]) == 4
    books = {lg.symbol: _bk(30.0, 30.1) if lg.side == "SELL" else _bk(4.9, 5.0)
             for lg in st.plan.legs}  # fmt: skip
    text = st.try_execute(T + timedelta(seconds=5), books)
    assert text and text.startswith("opened iron condor") and st.account.positions


def test_order_flow_veto_picks_the_structure(repo_root: Path, tmp_path: Path) -> None:
    st, _ = _strategy(repo_root, tmp_path)
    _eval(st, {"up": 1, "down": 0, "forming": []})
    assert st.plan is not None and st.plan.structure == "BULL_PUT"
    # One side alone: a 200-wide spread's worst case is above the ₹10,000 limit, so 150.
    assert {(lg.side, lg.kind, lg.strike) for lg in st.plan.legs} == {
        ("SELL", "PE", 24600), ("BUY", "PE", 24450)
    }
    _eval(st, {"up": 0, "down": 2, "forming": []})
    assert st.plan is not None and st.plan.structure == "BEAR_CALL"
    _eval(st, {"up": 1, "down": 1, "forming": []})
    assert st.plan is None and "two-way" in st.decision
    _eval(st, {"up": 0, "down": 0, "forming": ["UP"]})
    assert st.plan is None and "forming" in st.decision
    st2, _ = _strategy(repo_root, tmp_path, one_sided_on_trend=False)
    _eval(st2, {"up": 1, "down": 0, "forming": []})
    assert st2.plan is None and "trending" in st2.decision


def test_condor_filters_block_entry(repo_root: Path, tmp_path: Path) -> None:
    st, _ = _strategy(repo_root, tmp_path)
    _eval(st, strip={**STRIP, "iv_percentile": 40.0})
    assert st.plan is None and "iv percentile" in st.decision
    _eval(st, strip={k: v for k, v in STRIP.items() if k != "iv_percentile"})
    assert st.plan is None and "no IV history" in st.decision
    _eval(st, event_scale=0.0)
    assert st.plan is None and "RBI policy" in st.decision
    from engine.data.events.event_calendar import EventCategory, ScheduledEvent

    def ev(title: str, cat: EventCategory, at: datetime) -> ScheduledEvent:
        return ScheduledEvent(title, cat, at, at + timedelta(hours=1))

    rbi = ev("RBI MPC", EventCategory.RBI_MPC, T + timedelta(days=2))
    cpi = ev("CPI", EventCategory.INDIA_MACRO, T + timedelta(hours=5))
    _eval(st, events=[cpi])
    assert st.plan is not None  # CPI is not one of SPEC 5.1's hold blockers
    _eval(st, events=[cpi, rbi])
    assert st.plan is None and "RBI MPC" in st.decision
    _eval(st, events=[ev("RBI MPC", EventCategory.RBI_MPC, T + timedelta(days=9))])
    assert st.plan is not None  # after the planned exit (Monday 16 Nov 15:00)
    _eval(st, now=OPEN.replace(hour=9, minute=45))
    assert st.plan is None and "entry window" in st.decision
    _eval(st, now=datetime(2026, 11, 16, 11, 0, tzinfo=IST))  # 1 day to expiry
    assert st.plan is None and "expiry" in st.decision


def test_condor_exit_rules(repo_root: Path, tmp_path: Path) -> None:
    st, _ = _strategy(repo_root, tmp_path)
    assert _open(st.account)[0]
    pos = st.account.positions[0]  # credit 50; shorts 24800 PE / 25200 CE
    quiet = {"up": 0, "down": 0, "forming": []}

    def books(short: float, wing: float) -> dict[str, Book]:
        return {SP: _bk(short - 0.1, short), SC: _bk(short - 0.1, short),
                WP: _bk(wing, wing + 0.1), WC: _bk(wing, wing + 0.1)}  # fmt: skip

    later = T + timedelta(hours=1)
    assert st.exit_reason(pos, later, books(25.0, 3.0), 25000.0, quiet) is None
    assert "take profit" in str(st.exit_reason(pos, later, books(14.0, 2.0), 25000.0, quiet))
    assert "stop" in str(st.exit_reason(pos, later, books(60.0, 5.0), 25000.0, quiet))
    assert "touched" in str(st.exit_reason(pos, later, books(25.0, 3.0), 25201.0, quiet))
    up = {"up": 1, "down": 0, "forming": []}
    assert "order flow" in str(st.exit_reason(pos, later, books(25.0, 3.0), 25100.0, up))
    assert st.exit_reason(pos, later, books(25.0, 3.0), 24900.0, up) is None  # far from it
    eve = datetime(2026, 11, 16, 15, 0, tzinfo=IST)  # Monday before the Tuesday expiry
    assert "time exit" in str(st.exit_reason(pos, eve, books(25.0, 3.0), 25000.0, quiet))
    out = st.check_exits(later, books(14.0, 2.0), 25000.0, quiet)
    assert out and out[0].startswith("closed iron condor") and not st.account.positions


# ---------------------------------------------------------------- 13 Nov 2026 owner rules


def test_condor_side_stop_catches_one_tested_side(repo_root: Path, tmp_path: Path) -> None:
    """Credit 50 = 25 per side. The put side now costs 57 to close (>= 2.2 x 25) while the call
    side has decayed to 1: the combined 58 is far under the 2x-credit stop (100), but the
    tested side alone triggers the exit."""
    st, _ = _strategy(repo_root, tmp_path)
    assert _open(st.account)[0]
    pos = st.account.positions[0]
    books = {WP: _bk(3.0, 3.05), SP: _bk(59.95, 60.0), SC: _bk(1.95, 2.0), WC: _bk(1.0, 1.05)}
    why = st.exit_reason(pos, T, books, None, {})
    assert why is not None and why.startswith("put side stop"), why


def test_condor_books_before_a_weekend_near_its_target(repo_root: Path, tmp_path: Path) -> None:
    from datetime import datetime as _dt

    st, _ = _strategy(repo_root, tmp_path)
    assert _open(st.account)[0]
    pos = st.account.positions[0]
    # cost to close 28 of a 50 credit: 22 captured = 88% of the 25-pt take-profit
    books = {WP: _bk(0.05, 0.1), SP: _bk(13.95, 14.0), SC: _bk(13.95, 14.0), WC: _bk(0.05, 0.1)}
    fri = _dt(2026, 11, 13, 15, 12, tzinfo=T.tzinfo)
    thu = _dt(2026, 11, 12, 15, 12, tzinfo=T.tzinfo)
    why = st.exit_reason(pos, fri, books, None, {})
    assert why is not None and "market closure" in why, why
    assert st.exit_reason(pos, fri.replace(hour=15, minute=5), books, None, {}) is None
    assert st.exit_reason(pos, thu, books, None, {}) is None  # Friday is a trading day


def test_condor_books_the_untested_side_once_and_keeps_the_rest(repo_root: Path, tmp_path: Path) -> None:
    """SPEC 5.1: the call side has decayed > 85% while the put side costs more than its
    credit to close -> book the call side only; the take-profit/stop now count what it cost."""
    st, _ = _strategy(repo_root, tmp_path)
    assert _open(st.account)[0]
    pos = st.account.positions[0]
    assert pos.context["wing_cost_pct"] == pytest.approx(10 / 60 * 100, abs=0.1)
    books = {WP: _bk(8.0, 8.05), SP: _bk(34.0, 34.05), SC: _bk(2.0, 2.05), WC: _bk(0.05, 0.1)}
    assert st.untested_side(pos, T, books) == ("CE", st.untested_side(pos, T, books)[1])
    out = st.check_exits(T, books, 24950.0, {})
    assert any("booked the CE side" in x for x in out), out
    assert [lg.open for lg in pos.legs if lg.kind == "CE"] == [False, False] and st.account.positions
    assert st.untested_side(pos, T, books) is None  # one adjustment only
    from engine.cockpit.condor import booked_debit

    assert booked_debit(pos) == pytest.approx(2.05 - 0.05)


def test_condor_waits_out_the_first_candle_on_a_gap(repo_root: Path, tmp_path: Path) -> None:
    st, _ = _strategy(repo_root, tmp_path)
    assert _open(st.account)[0]
    pos = st.account.positions[0]
    gap = {WP: _bk(40.0, 40.05), SP: _bk(120.0, 120.05), SC: _bk(1.0, 1.05), WC: _bk(0.05, 0.1)}
    early = T.replace(hour=9, minute=20)
    assert st.exit_reason(pos, early, gap, 24700.0, {}) is None  # judged after 09:30
    assert st.exit_reason(pos, T.replace(hour=9, minute=31), gap, 24700.0, {}) is not None
