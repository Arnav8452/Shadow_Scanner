"""Paper account for small-shareholder tender buybacks (research T_bb1 rules, paper only)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from engine.sidecars.event_arb.models import BuybackTerms, Event, EventType, Filing
from engine.sidecars.event_arb.paper import (
    BuybackPaperConfig,
    PaperState,
    Quote,
    cum_date,
    step,
)

REC = date(2026, 11, 20)  # Friday
CUM = date(2026, 11, 19)
RET = date(2026, 12, 4)


class WeekdayCal:
    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5

    def previous_trading_day(self, d: date) -> date:
        d -= timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d


CAL = WeekdayCal()


def _event(
    price: float = 425.0, acceptance: float | None = None, ret: date | None = RET, eid: str = "E1"
) -> Event:
    return Event(
        id=eid,
        type=EventType.TENDER_BUYBACK,
        symbol="ACME" if eid == "E1" else f"ACME{eid}",
        company="Acme",
        announced_on=date(2026, 10, 30),
        compiled_on=date(2026, 11, 9),
        filings=(Filing("https://example.com/f", "filing", date(2026, 10, 30)),),
        terms=BuybackTerms(
            price=price,
            shares=1000,
            record_date=REC,
            unaccepted_return_date=ret,
            actual_small_acceptance_pct=acceptance,
        ),
    )


def _charges(side: str, value: float, dp: bool) -> float:
    return value * 0.001 + (15.34 if dp else 0.0)


CFG = BuybackPaperConfig(start_date=date(2026, 11, 1))


def _q(close: float, turnover: float = 1e9) -> Quote:
    return Quote(close=close, turnover_inr=turnover)


def test_cum_date_is_the_last_session_strictly_before_the_record_date() -> None:
    assert cum_date(REC, CAL) == CUM
    assert cum_date(date(2026, 11, 23), CAL) == date(2026, 11, 20)  # Monday -> Friday


def test_enters_at_the_cum_date_close_with_half_spread_and_buy_charges() -> None:
    st = PaperState()
    _, journal = step(CUM, [_event()], {"ACME": _q(300.0)}, st, CFG, CAL, _charges)
    assert len(st.positions) == 1
    p = st.positions[0]
    px = 300.0 * (1 + 5 / 10_000)  # liquid: 5 bp
    assert p.entry_px == pytest.approx(px)
    assert p.qty == int(50_000 // px)
    assert p.buy_charges == pytest.approx(p.qty * px * 0.001)  # no DP on a buy
    assert journal[0]["type"] == "fill" and journal[0]["symbol"] == "ACME"
    assert "E1" in st.done
    # the next day does not enter again
    step(CUM + timedelta(days=1), [_event()], {"ACME": _q(280.0)}, st, CFG, CAL, _charges)
    assert len(st.positions) == 1


def test_illiquid_names_pay_a_wider_spread() -> None:
    st = PaperState()
    step(CUM, [_event()], {"ACME": _q(300.0, turnover=1e7)}, st, CFG, CAL, _charges)
    assert st.positions[0].entry_px == pytest.approx(300.0 * (1 + 15 / 10_000))


def test_skips_without_a_discount_when_slots_are_full_or_paused() -> None:
    st = PaperState()
    _, j = step(CUM, [_event(price=290.0)], {"ACME": _q(300.0)}, st, CFG, CAL, _charges)
    assert not st.positions and j[0]["type"] == "skip" and "discount" in j[0]["reason"]
    st = PaperState()
    evs = [_event(eid=e) for e in ("E1", "E2", "E3")]
    quotes = {e.symbol: _q(300.0) for e in evs}
    _, j = step(CUM, evs, quotes, st, CFG, CAL, _charges)
    assert len(st.positions) == 2  # two ₹50k slots
    assert any(r["type"] == "skip" and "slot" in r["reason"] for r in j)
    st = PaperState(paused="owner review")
    _, j = step(CUM, [_event()], {"ACME": _q(300.0)}, st, CFG, CAL, _charges)
    assert not st.positions and "paused" in j[0]["reason"]


def test_events_before_the_start_date_and_missed_entries_are_not_traded() -> None:
    st = PaperState()
    late = replace(CFG, start_date=date(2026, 11, 25))
    _, j = step(CUM, [_event()], {"ACME": _q(300.0)}, st, late, CAL, _charges)
    assert not st.positions and not j
    st = PaperState()
    _, j = step(CUM + timedelta(days=1), [_event()], {"ACME": _q(300.0)}, st, CFG, CAL, _charges)
    assert not st.positions and j[0]["type"] == "skip" and "missed" in j[0]["reason"]


def test_settles_accepted_shares_at_the_buyback_price_and_sells_the_rest() -> None:
    st = PaperState()
    step(CUM, [_event()], {"ACME": _q(300.0)}, st, CFG, CAL, _charges)
    p = st.positions[0]
    # return date passed but acceptance not yet published: keep holding, say so
    lines, j = step(RET, [_event()], {"ACME": _q(290.0)}, st, CFG, CAL, _charges)
    assert st.positions and any("acceptance" in x for x in lines) and not j
    ev = _event(acceptance=40.0)
    _, j = step(RET + timedelta(days=1), [ev], {"ACME": _q(280.0)}, st, CFG, CAL, _charges)
    assert not st.positions and j[0]["type"] == "close"
    c = j[0]
    q_acc = round(p.qty * 0.40)
    q_ret = p.qty - q_acc
    ret_px = 280.0 * (1 - 5 / 10_000)
    proceeds = q_acc * 425.0 + q_ret * ret_px
    sell_ch = _charges("sell", q_acc * 425.0, True) + _charges("sell", q_ret * ret_px, True)
    assert c["q_acc"] == q_acc and c["q_ret"] == q_ret
    assert c["net"] == pytest.approx(
        proceeds - p.qty * p.entry_px - p.buy_charges - sell_ch, abs=0.01
    )
    assert c["charges"] == pytest.approx(p.buy_charges + sell_ch, abs=0.01)
    assert (
        c["entry_day"] == CUM.isoformat() and c["exit_day"] == (RET + timedelta(days=1)).isoformat()
    )
    assert st.closed_events == 1 and st.cum_net == pytest.approx(c["net"], abs=0.01)


def test_kill_rules_pause_new_entries() -> None:
    st = PaperState()
    step(CUM, [_event()], {"ACME": _q(300.0)}, st, CFG, CAL, _charges)
    # 0% accepted and the stock halves: a single event loss above the cap pauses entries
    _, j = step(RET, [_event(acceptance=0.0)], {"ACME": _q(150.0)}, st, CFG, CAL, _charges)
    assert any(r["type"] == "kill" for r in j)
    assert st.paused and "₹" in st.paused
    st2 = PaperState(cum_net=-4_900.0)
    step(CUM, [_event()], {"ACME": _q(300.0)}, st2, CFG, CAL, _charges)
    _, j = step(RET, [_event(acceptance=0.0)], {"ACME": _q(290.0)}, st2, CFG, CAL, _charges)
    assert st2.paused and "cumulative" in st2.paused


def test_record_date_value_over_the_small_holder_cap_is_flagged() -> None:
    st = PaperState()
    step(CUM, [_event()], {"ACME": _q(300.0)}, st, CFG, CAL, _charges)
    lines, _ = step(REC, [_event()], {"ACME": _q(1500.0)}, st, CFG, CAL, _charges)
    assert any("₹2 lakh" in x for x in lines)


def test_state_round_trips_through_json(tmp_path: Path) -> None:
    st = PaperState()
    step(CUM, [_event()], {"ACME": _q(300.0)}, st, CFG, CAL, _charges)
    path = tmp_path / "s.json"
    st.save(path)
    again = PaperState.load(path)
    assert again.positions == st.positions and again.done == st.done
    assert json.loads(path.read_text(encoding="utf-8"))["positions"][0]["symbol"] == "ACME"
    assert PaperState.load(tmp_path / "missing.json") == PaperState()


def test_tender_is_charged_as_a_corporate_action_order_even_when_nothing_is_accepted() -> None:
    calls: list[tuple[str, float, bool]] = []

    def rec(side: str, value: float, dp: bool) -> float:
        calls.append((side, value, dp))
        return 23.6 if side == "tender" else value * 0.001

    st = PaperState()
    step(CUM, [_event()], {"ACME": _q(300.0)}, st, CFG, CAL, rec)
    calls.clear()
    _, j = step(RET, [_event(acceptance=0.0)], {"ACME": _q(290.0)}, st, CFG, CAL, rec)
    assert ("tender", 0.0, False) in calls  # the fee is per order, accepted or not
    assert any(c[0] == "sell" and c[2] for c in calls)  # the returned shares are sold
    assert j[0]["type"] == "close"


def test_entries_and_closes_carry_avoid_filter_tags() -> None:
    seen: list[tuple[str, date]] = []

    def tags(symbol: str, entry_day: date) -> dict:
        seen.append((symbol, entry_day))
        return {"call_heavy": None, "in_ex_rights_window": True}

    st = PaperState()
    _, j = step(CUM, [_event()], {"ACME": _q(300.0)}, st, CFG, CAL, _charges, tags)
    assert seen == [("ACME", CUM)] and j[0]["tags"]["in_ex_rights_window"] is True
    _, j = step(RET, [_event(acceptance=40.0)], {"ACME": _q(290.0)}, st, CFG, CAL, _charges)
    assert j[0]["type"] == "close" and j[0]["tags"]["in_ex_rights_window"] is True


def test_entries_record_the_buyback_size_as_an_acceptance_tag() -> None:
    ev = _event()
    ev = replace(ev, terms=replace(ev.terms, published_size_pct=2.0))
    st = PaperState()
    _, j = step(CUM, [ev], {"ACME": _q(300.0)}, st, CFG, CAL, _charges)
    assert j[0]["tags"]["bb_size_pct"] == 2.0 and j[0]["tags"]["bb_small_quota_pct"] == 0.3
    _, j = step(RET, [replace(ev, terms=replace(ev.terms, actual_small_acceptance_pct=50.0))],
                {"ACME": _q(290.0)}, st, CFG, CAL, _charges)  # fmt: skip
    assert j[0]["tags"]["bb_size_pct"] == 2.0  # carried to the close record
