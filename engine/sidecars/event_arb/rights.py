"""Rights issue / rights entitlement (RE) calculator (Addendum A.4). Information only.

Retail cannot short the stock overnight, so an RE trading below fair value is a *discounted
long position* (buy RE + apply via ASBA, new shares list weeks later), not a hedged arbitrage.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.sidecars.event_arb.models import RightsTerms


def re_fair_value(share_price: float, issue_price: float) -> float:
    """Intrinsic value of one RE (right to one fully-paid share at the issue price)."""
    return max(0.0, share_price - issue_price)


def theoretical_ex_rights_price(cum_price: float, terms: RightsTerms) -> float:
    held, new = terms.held_shares, terms.rights_shares
    return (held * cum_price + new * terms.issue_price) / (held + new)


def issue_price_cushion(share_price: float, issue_price: float) -> float:
    """How far the share can fall before the RE is worthless."""
    return (share_price - issue_price) / share_price


@dataclass(frozen=True)
class REGap:
    fair_value: float
    re_price: float
    gap_inr: float  # fair − RE price (positive = RE cheap)
    gap_pct_of_share: float  # gap as % of share price, comparable with cost %
    cheap_after_costs: bool


def re_gap(share_price: float, terms: RightsTerms, round_trip_cost_pct: float) -> REGap | None:
    if terms.re_price is None:
        return None
    fair = re_fair_value(share_price, terms.issue_price)
    gap = fair - terms.re_price
    gap_pct = gap / share_price * 100
    return REGap(
        fair, terms.re_price, gap, gap_pct, cheap_after_costs=gap_pct > round_trip_cost_pct
    )
