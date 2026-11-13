"""Results iron fly (H4): construction, P&L, haircut, concurrency. Synthetic quotes only."""

from __future__ import annotations

from datetime import date

import pytest

from engine.research.results_fly import base_haircut, build_fly, select_concurrent, stats

B, D, A, EXP = date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6), date(2026, 5, 26)


def _q(prices: dict[tuple[float, str], float], traded: bool = True) -> dict:
    return {k: {"close": v, "contracts": 10 if traded else 0} for k, v in prices.items()}


def _chain(atm_c: float, atm_p: float, w10: tuple[float, float], w20: tuple[float, float]) -> dict:
    return _q({
        (1000.0, "CE"): atm_c, (1000.0, "PE"): atm_p,
        (1010.0, "CE"): w10[0], (990.0, "PE"): w10[1],
        (1020.0, "CE"): w20[0], (980.0, "PE"): w20[1],
    })  # fmt: skip


def zero(px: float) -> float:
    return 0.0


def test_a_credit_above_the_wing_is_rejected_as_stale() -> None:
    """Closes giving more credit than the wing width would be a riskless profit: real quotes
    never allow it, stale closes can. Such a fly is not built."""
    q_in = _chain(30, 28, (24, 22), (18, 17))  # w10 credit 12 > 10; w20 credit 23 > 20
    q_out = _chain(12, 14, (6, 7), (2, 3))
    assert build_fly("ACME", D, B, A, EXP, 500, 1001.0, 1000.0, q_in, q_out, cap_inr=5000,
                     hc=zero) is None  # fmt: skip


def test_cap_picks_the_widest_fitting_wing_and_gross_is_exact() -> None:
    q_out = _chain(8, 8, (3, 3), (1, 1))
    q_in = _chain(20, 20, (16, 16), (12, 12))  # w10: credit 8, loss 2/unit; w20: credit 16, loss 4
    t = build_fly("ACME", D, B, A, EXP, 1000, 1000.0, 1000.0, q_in, q_out, cap_inr=3000, hc=zero)
    assert t is not None and t.wing == 10.0 and t.max_loss(zero) == pytest.approx(2000)
    t2 = build_fly("ACME", D, B, A, EXP, 1000, 1000.0, 1000.0, q_in, q_out, cap_inr=5000, hc=zero)
    assert t2 is not None and t2.wing == 20.0
    # gross at w=10: body 20+20 -> 8+8 = +24; wings 16+16 -> 3+3 = -26 ... per unit -2 x 1000
    assert t.gross(zero) == pytest.approx(((40 - 16) - (32 - 6)) * 1000)
    assert t.gross(lambda px: base_haircut(px)) < t.gross(zero)  # the haircut costs money


def test_untraded_leg_voids_the_event() -> None:
    q_in = _chain(20, 20, (16, 16), (12, 12))
    q_out = _q({k: v["close"] for k, v in _chain(8, 8, (3, 3), (1, 1)).items()}, traded=False)
    assert build_fly("ACME", D, B, A, EXP, 1000, 1000.0, 1000.0, q_in, q_out,
                     cap_inr=5000, hc=zero) is None  # fmt: skip


def test_concurrency_budget_and_stats() -> None:
    q_in = _chain(20, 20, (16, 16), (12, 12))
    q_out = _chain(8, 8, (3, 3), (1, 1))
    ts = [build_fly(f"S{i}", D, B, A, EXP, 1000, 1000.0, 1000.0, q_in, q_out, cap_inr=3000,
                    hc=zero) for i in range(5)]  # fmt: skip
    chosen = select_concurrent([t for t in ts if t], zero, budget=7000)  # 2000 each -> 3 fit
    assert len(chosen) == 3
    s = stats([100.0, -50.0, 200.0, -10.0])
    assert s["n"] == 4 and s["win_pct"] == 50 and s["profit_factor"] == pytest.approx(5.0)
    assert base_haircut(5.0) == 0.20 and base_haircut(100.0) == pytest.approx(1.5)


def test_a_stale_exit_above_the_wing_is_capped() -> None:
    q_in = _chain(20, 20, (16, 16), (12, 12))
    q_out = _chain(150, 0.3, (118, 0.2), (100, 0.1))  # calls 150 vs 118: spread 32 > wing 10
    t = build_fly("ACME", D, B, A, EXP, 100, 1000.0, 1100.0, q_in, q_out, cap_inr=5000, hc=zero)
    assert t is not None and t.exit_cost_close() > t.wing
    # the loss is capped at (wing - credit) x lot
    assert t.gross(zero) == pytest.approx(-(t.wing - t.credit(zero)) * t.lot)
