"""Tender-offer buyback model (Addendum A.3). Information only — tenders are placed manually.

Notation: P_bb buyback price, N_bb shares bought back, N_tot shares outstanding, P_buy our
entry price, H_small shares held by small shareholders, R reserved shares, E entitlement
(accepted per share held if every small holder tenders), p participation, A acceptance.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from engine.backtest.costs import ZERO, ChargeBreakdown
from engine.sidecars.event_arb.models import BuybackTerms

DEFAULT_PARTICIPATIONS: tuple[float, ...] = (0.4, 0.6, 1.0)
DEFAULT_DROPS: tuple[float, ...] = (0.05, 0.10, 0.15)

# (side, value_inr, include_dp) -> charges
CostFn = Callable[[Literal["buy", "sell"], float, bool], ChargeBreakdown]


def no_costs(_side: Literal["buy", "sell"], _value: float, _dp: bool) -> ChargeBreakdown:
    return ZERO


# ---------------------------------------------------------------- formulas (A.3 steps 1–7)


def size_fraction(shares: int, shares_outstanding: int) -> float:
    """Step 1: N_bb / N_tot."""
    if shares_outstanding <= 0:
        raise ValueError("shares_outstanding must be > 0")
    return shares / shares_outstanding


def reserved_shares(
    shares: int,
    reservation_pct: float,
    *,
    small_holder_shares: float | None = None,
    shares_outstanding: int | None = None,
) -> float:
    """Step 2: R. SEBI Buy-back Regs 2018, Reg 6 proviso: 15% of N_bb **or** the small
    holders' proportional entitlement, whichever is higher (the addendum's ``0.15 × N_bb``
    is the floor). The proportional leg needs H_small and N_tot."""
    r = reservation_pct / 100.0 * shares
    if small_holder_shares is not None and shares_outstanding:
        r = max(r, shares * small_holder_shares / shares_outstanding)
    return r


def entitlement(reserved: float, small_holder_shares: float) -> float:
    """Step 4: E = R / H_small."""
    if small_holder_shares <= 0:
        raise ValueError("small_holder_shares must be > 0")
    return reserved / small_holder_shares


def acceptance(entitlement_ratio: float, participation: float) -> float:
    """Step 5: A = min(1, E / p). If only a fraction p of small holders tender, the reserved
    pool is shared among fewer shares."""
    if not 0 < participation <= 1:
        raise ValueError("participation must be in (0, 1]")
    return min(1.0, entitlement_ratio / participation)


def breakeven_exit_price(p_buy: float, p_bb: float, a: float) -> float | None:
    """Step 7: P_exit* = P_buy − A·(P_bb − P_buy)/(1 − A). ``None`` when A = 1 (nothing left)."""
    if not 0 <= a <= 1:
        raise ValueError("acceptance must be in [0, 1]")
    if a >= 1:
        return None
    return p_buy - a * (p_bb - p_buy) / (1 - a)


def cushion(p_buy: float, p_exit: float | None) -> float | None:
    """(P_buy − P_exit*) / P_buy: how far the price may fall after the record date before the
    trade loses money (before costs/tax)."""
    return None if p_exit is None else (p_buy - p_exit) / p_buy


# ---------------------------------------------------------------- scenarios


@dataclass(frozen=True)
class Scenario:
    label: str
    acceptance: float
    assumption: str  # shown next to every number


def acceptance_scenarios(
    terms: BuybackTerms,
    reservation_pct: float,
    *,
    participations: Sequence[float] = DEFAULT_PARTICIPATIONS,
    optimistic_holdings_factor: float = 0.5,
    explicit: Iterable[float] = (),
) -> list[Scenario]:
    """Acceptance scenarios from (in priority order) the published entitlement, shareholding
    pattern data, the company's past acceptance ratios, and any explicit values."""
    out: list[Scenario] = []
    if terms.published_small_entitlement is not None:
        e = terms.published_small_entitlement
        for p in participations:
            out.append(
                Scenario(
                    f"published E, p={p:.0%}",
                    acceptance(e, p),
                    f"letter-of-offer entitlement {e:.4f}; {p:.0%} of small holders tender",
                )
            )
    elif terms.small_holder_shares_shp is not None and terms.shares_outstanding:
        h = terms.small_holder_shares_shp
        for case, hh in (("pessimistic", h), ("optimistic", h * optimistic_holdings_factor)):
            r = reserved_shares(
                terms.shares,
                reservation_pct,
                small_holder_shares=hh,
                shares_outstanding=terms.shares_outstanding,
            )
            e = entitlement(r, hh)
            basis = (
                "SHP ≤₹2L nominal"
                if case == "pessimistic"
                else f"{optimistic_holdings_factor:.0%} of SHP"
            )
            shp_date = terms.shp_as_of or "UNKNOWN"
            for p in participations:
                note = (
                    f"H_small={hh:,.0f} ({basis}, SHP as of {shp_date}); E={e:.4f}; "
                    f"{p:.0%} of small holders tender"
                )
                out.append(Scenario(f"{case}, p={p:.0%}", acceptance(e, p), note))
    for i, a in enumerate(terms.past_small_acceptance, start=1):
        out.append(
            Scenario(f"history #{i}", a, "company's past small-shareholder acceptance ratio")
        )
    for a in explicit:
        out.append(Scenario(f"assumed A={a:.0%}", a, "explicit assumption (no data)"))
    return out


# ---------------------------------------------------------------- sizing


@dataclass(frozen=True)
class Sizing:
    shares: int
    binding: str
    caps: dict[str, int] = field(default_factory=dict)


def size_position(
    p_buy: float,
    *,
    capital_inr: float,
    max_holding_value_inr: float,
    small_value_limit_inr: float,
    existing_shares: int = 0,
    risk_budget_inr: float | None = None,
    worst_loss_per_share: float | None = None,
) -> Sizing:
    """Step 6, plus a risk-budget cap (SPEC §6) that the addendum omits.

    ``max_holding_value_inr`` is the buffer below the ₹2L small-shareholder test (default
    ₹1.5L) so a pre-record-date rally does not push the holding over the limit. Shares already
    held in any demat account under the same PAN sequence count toward the test (Regs Schedule).
    """
    if p_buy <= 0:
        raise ValueError("p_buy must be > 0")
    value_cap = min(max_holding_value_inr, small_value_limit_inr)
    caps = {
        "capital": math.floor(capital_inr / p_buy),
        "small-shareholder buffer": math.floor(value_cap / p_buy) - existing_shares,
    }
    if (
        risk_budget_inr is not None
        and worst_loss_per_share is not None
        and worst_loss_per_share > 0
    ):
        caps["risk budget"] = math.floor(risk_budget_inr / worst_loss_per_share)
    binding = min(caps, key=lambda k: caps[k])
    return Sizing(shares=max(0, caps[binding]), binding=binding, caps=caps)


def worst_loss_per_share(p_buy: float, p_bb: float, a: float, drop: float) -> float:
    """Loss per share held if acceptance is ``a`` and unaccepted shares are sold ``drop`` below
    P_buy (before costs). Negative = still a gain."""
    return (1 - a) * p_buy * drop - a * (p_bb - p_buy)


# ---------------------------------------------------------------- evaluation


@dataclass(frozen=True)
class ScenarioResult:
    scenario: Scenario
    shares: int
    accepted_exact: float  # q·A (formula level)
    accepted_whole: int  # floor(q·A) — what a registrar can actually accept
    gain_on_accepted: float  # q·A·(P_bb − P_buy), formula level
    breakeven_exit: float | None
    cushion: float | None
    net_pnl_by_drop: dict[float, float]  # whole shares, after costs (before tax)
    tax_by_drop: dict[float, float | None]
    charges_by_drop: dict[float, ChargeBreakdown]


def evaluate(
    *,
    p_buy: float,
    p_bb: float,
    shares: int,
    scenario: Scenario,
    drops: Sequence[float] = DEFAULT_DROPS,
    cost_fn: CostFn = no_costs,
    stcg_rate_pct: float | None = None,
) -> ScenarioResult:
    a = scenario.acceptance
    accepted_exact = shares * a
    accepted = math.floor(accepted_exact + 1e-9)
    unaccepted = shares - accepted
    p_exit = breakeven_exit_price(p_buy, p_bb, a)

    pnl: dict[float, float] = {}
    tax: dict[float, float | None] = {}
    charges: dict[float, ChargeBreakdown] = {}
    for d in drops:
        exit_px = p_buy * (1 - d)
        c = cost_fn("buy", shares * p_buy, False)
        if accepted:
            c = c + cost_fn("sell", accepted * p_bb, True)  # tender settles as an exchange sale
        if unaccepted:
            c = c + cost_fn("sell", unaccepted * exit_px, True)  # later sale, separate DP debit
        gross = accepted * (p_bb - p_buy) + unaccepted * (exit_px - p_buy)
        pnl[d] = gross - c.total
        charges[d] = c
        # Capital-gains regime (from 1 Apr 2026, UNVERIFIED): net STCG on this trade only.
        tax[d] = None if stcg_rate_pct is None else max(0.0, gross) * stcg_rate_pct / 100.0
    return ScenarioResult(
        scenario=scenario,
        shares=shares,
        accepted_exact=accepted_exact,
        accepted_whole=accepted,
        gain_on_accepted=accepted_exact * (p_bb - p_buy),
        breakeven_exit=p_exit,
        cushion=cushion(p_buy, p_exit),
        net_pnl_by_drop=pnl,
        tax_by_drop=tax,
        charges_by_drop=charges,
    )


# ---------------------------------------------------------------- pre-record-date exit


@dataclass(frozen=True)
class TenderVsSell:
    tender_value_per_share: float
    sell_now_per_share: float
    prefer_sell: bool


def tender_vs_sell_now(
    p_now: float, p_bb: float, a: float, post_record_drop: float
) -> TenderVsSell:
    """Compare holding to tender (A at P_bb, rest sold ``post_record_drop`` below today's price)
    with selling every share now. Before costs; both paths pay similar sell-side charges."""
    tender = a * p_bb + (1 - a) * p_now * (1 - post_record_drop)
    return TenderVsSell(tender, p_now, prefer_sell=p_now >= tender)
