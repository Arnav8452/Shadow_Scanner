"""Open offer (SEBI SAST Regs 2011) model (Addendum A.4). Information only."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from engine.sidecars.event_arb.buyback import Scenario, breakeven_exit_price, cushion
from engine.sidecars.event_arb.models import OpenOfferTerms


def acceptance(offer_shares: int, public_shares: int, participation: float) -> float:
    """Shares tendered = p × public float; acceptance = min(1, offer / tendered)."""
    if not 0 < participation <= 1:
        raise ValueError("participation must be in (0, 1]")
    if public_shares <= 0:
        raise ValueError("public_shares must be > 0")
    return min(1.0, offer_shares / (participation * public_shares))


def scenarios(terms: OpenOfferTerms, participations: Sequence[float]) -> list[Scenario]:
    if not terms.public_shares:
        return []
    return [
        Scenario(
            f"p={p:.0%}",
            acceptance(terms.offer_shares, terms.public_shares, p),
            f"{p:.0%} of public shareholders ({terms.public_shares:,}) tender",
        )
        for p in participations
    ]


def annualised(simple_return: float, days: int) -> float | None:
    if days <= 0:
        return None
    return float((1 + simple_return) ** (365 / days) - 1)


@dataclass(frozen=True)
class OpenOfferResult:
    scenario: Scenario
    gain_per_share_held: float  # A·(offer − P_buy), before costs
    breakeven_exit: float | None
    cushion: float | None
    days_to_payment: int | None
    annualised_if_flat: float | None  # unaccepted sold at P_buy


def evaluate(
    p_buy: float, terms: OpenOfferTerms, scenario: Scenario, as_of: date
) -> OpenOfferResult:
    a = scenario.acceptance
    gain = a * (terms.price - p_buy)
    days = (terms.expected_payment_date - as_of).days if terms.expected_payment_date else None
    p_exit = breakeven_exit_price(p_buy, terms.price, a)
    return OpenOfferResult(
        scenario=scenario,
        gain_per_share_held=gain,
        breakeven_exit=p_exit,
        cushion=cushion(p_buy, p_exit),
        days_to_payment=days,
        annualised_if_flat=annualised(gain / p_buy, days) if days else None,
    )
