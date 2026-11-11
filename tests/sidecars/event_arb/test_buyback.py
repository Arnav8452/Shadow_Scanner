"""Addendum A.3 formulas, including the Medi Assist golden table (9 Nov 2026 data)."""

import math

import pytest

from engine.backtest.costs import ChargeBreakdown
from engine.sidecars.event_arb import buyback as bb
from engine.sidecars.event_arb.models import BuybackTerms

P_BB, P_BUY, N_BB = 425.0, 289.0, 1_159_199


def test_golden_sizing_and_reserve() -> None:
    sizing = bb.size_position(
        P_BUY, capital_inr=100_000, max_holding_value_inr=150_000, small_value_limit_inr=200_000
    )
    assert sizing.shares == 346 and sizing.binding == "capital"
    assert bb.reserved_shares(N_BB, 15) == pytest.approx(173_879.85)  # "R ≈ 1,73,880"
    n_tot = N_BB / 0.0155
    assert n_tot / 1e7 == pytest.approx(7.48, abs=0.005)  # "≈ 7.48 crore"
    assert bb.size_fraction(N_BB, round(n_tot)) == pytest.approx(0.0155, abs=1e-6)


@pytest.mark.parametrize(
    (
        "a",
        "accepted_exact",
        "accepted_whole",
        "table_accepted",
        "gain",
        "table_gain",
        "p_exit",
        "cush",
    ),
    [
        (0.05, 17.3, 17, 17, 2352.8, 2300, 281.84, 0.0248),
        (0.10, 34.6, 34, 35, 4705.6, 4700, 273.89, 0.0523),
        (0.50, 173.0, 173, 173, 23528.0, 23500, 153.00, 0.4706),
    ],
)
def test_golden_table(
    a: float,
    accepted_exact: float,
    accepted_whole: int,
    table_accepted: int,
    gain: float,
    table_gain: float,
    p_exit: float,
    cush: float,
) -> None:
    r = bb.evaluate(p_buy=P_BUY, p_bb=P_BB, shares=346, scenario=bb.Scenario("x", a, "golden"))
    assert r.accepted_exact == pytest.approx(accepted_exact)
    assert r.accepted_whole == accepted_whole
    assert abs(r.accepted_exact - table_accepted) < 0.5  # table rounds q·A ("~35")
    assert r.gain_on_accepted == pytest.approx(gain)
    assert abs(r.gain_on_accepted - table_gain) <= 60  # table shows "~₹2,300" etc.
    assert r.breakeven_exit == pytest.approx(p_exit, abs=0.01)
    assert r.cushion == pytest.approx(cush, abs=1e-4)


def test_breakeven_identity() -> None:
    """Selling unaccepted shares at P_exit* makes total P&L exactly zero (before costs)."""
    for a in (0.01, 0.2, 0.7):
        pe = bb.breakeven_exit_price(P_BUY, P_BB, a)
        assert pe is not None
        assert a * (P_BB - P_BUY) + (1 - a) * (pe - P_BUY) == pytest.approx(0, abs=1e-9)
    assert bb.breakeven_exit_price(P_BUY, P_BB, 1.0) is None
    with pytest.raises(ValueError):
        bb.breakeven_exit_price(P_BUY, P_BB, 1.2)


def test_reservation_whichever_higher() -> None:
    # Small holders own 20% of equity → proportional entitlement (20% of N_bb) beats 15%.
    r = bb.reserved_shares(1_000, 15, small_holder_shares=2_000_000, shares_outstanding=10_000_000)
    assert r == pytest.approx(200)
    r = bb.reserved_shares(1_000, 15, small_holder_shares=500_000, shares_outstanding=10_000_000)
    assert r == pytest.approx(150)


def test_acceptance_formula() -> None:
    assert bb.entitlement(150, 1_000) == pytest.approx(0.15)
    assert bb.acceptance(0.15, 1.0) == pytest.approx(0.15)
    assert bb.acceptance(0.15, 0.4) == pytest.approx(0.375)
    assert bb.acceptance(0.6, 0.4) == 1.0
    with pytest.raises(ValueError):
        bb.acceptance(0.1, 0)


def test_scenarios_from_shp_pessimistic_below_optimistic() -> None:
    t = BuybackTerms(
        price=P_BB, shares=N_BB, shares_outstanding=74_787_032, small_holder_shares_shp=6_000_000
    )
    sc = bb.acceptance_scenarios(t, 15)
    assert len(sc) == 6
    pess = {s.label: s.acceptance for s in sc if s.label.startswith("pessimistic")}
    opt = {s.label: s.acceptance for s in sc if s.label.startswith("optimistic")}
    # pessimistic p=100%: E = 0.15·N_bb / H
    assert pess["pessimistic, p=100%"] == pytest.approx(0.15 * N_BB / 6_000_000)
    assert opt["optimistic, p=100%"] == pytest.approx(2 * pess["pessimistic, p=100%"])
    assert all("H_small" in s.assumption for s in sc)


def test_published_entitlement_takes_priority_and_history_added() -> None:
    t = BuybackTerms(
        price=P_BB,
        shares=N_BB,
        shares_outstanding=1,
        small_holder_shares_shp=1,
        published_small_entitlement=0.08,
        past_small_acceptance=(0.2,),
    )
    sc = bb.acceptance_scenarios(t, 15, explicit=[0.05])
    labels = [s.label for s in sc]
    assert labels[0].startswith("published") and "history #1" in labels and "assumed A=5%" in labels
    assert not any(lbl.startswith("pessimistic") for lbl in labels)


def test_no_data_gives_no_scenarios() -> None:
    assert bb.acceptance_scenarios(BuybackTerms(price=P_BB, shares=N_BB), 15) == []


def test_sizing_caps() -> None:
    s = bb.size_position(
        300,
        capital_inr=1_000_000,
        max_holding_value_inr=150_000,
        small_value_limit_inr=200_000,
        existing_shares=100,
    )
    assert s.shares == 400 and s.binding == "small-shareholder buffer"  # 500 − 100 held
    loss = bb.worst_loss_per_share(300, 425, 0.05, 0.15)
    s = bb.size_position(
        300,
        capital_inr=1_000_000,
        max_holding_value_inr=150_000,
        small_value_limit_inr=200_000,
        risk_budget_inr=3_000,
        worst_loss_per_share=loss,
    )
    assert s.binding == "risk budget" and s.shares == math.floor(3_000 / loss)


def test_worst_loss_can_be_gain() -> None:
    assert bb.worst_loss_per_share(289, 425, 0.5, 0.15) < 0


def test_evaluate_costs_and_tax() -> None:
    calls: list[tuple[str, float, bool]] = []

    def cost(side: str, value: float, dp: bool) -> ChargeBreakdown:
        calls.append((side, value, dp))
        return ChargeBreakdown(0, value * 0.001, 0, 0, 0, 0, 10.0 if dp else 0.0)

    r = bb.evaluate(
        p_buy=P_BUY,
        p_bb=P_BB,
        shares=346,
        scenario=bb.Scenario("x", 0.10, ""),
        drops=(0.10,),
        cost_fn=cost,
        stcg_rate_pct=20,
    )  # type: ignore[arg-type]
    gross = 34 * (P_BB - P_BUY) + 312 * (P_BUY * 0.9 - P_BUY)
    charges = (346 * P_BUY + 34 * P_BB + 312 * P_BUY * 0.9) * 0.001 + 20
    assert r.net_pnl_by_drop[0.10] == pytest.approx(gross - charges)
    assert r.tax_by_drop[0.10] == 0.0  # net loss → no tax
    assert [c[0] for c in calls] == ["buy", "sell", "sell"]


def test_tender_vs_sell_now() -> None:
    # Low acceptance: 0.05·425 + 0.95·(300·0.9) = 277.75 < 300 → selling beats tendering even
    # without a rally. Equivalent to cushion < assumed post-record drop: negative-EV trade.
    low = bb.tender_vs_sell_now(300, 425, 0.05, 0.10)
    assert low.tender_value_per_share == pytest.approx(0.05 * 425 + 0.95 * 300 * 0.9)
    assert low.prefer_sell
    high = bb.tender_vs_sell_now(300, 425, 0.50, 0.10)  # 212.5 + 135 = 347.5 > 300
    assert not high.prefer_sell
    # equivalence with the cushion test (measured from today's price)
    for a in (0.05, 0.2, 0.5):
        c = bb.cushion(300, bb.breakeven_exit_price(300, 425, a))
        assert c is not None
        assert bb.tender_vs_sell_now(300, 425, a, 0.10).prefer_sell == (c <= 0.10)
