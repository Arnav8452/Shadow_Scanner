"""Offer for sale (OFS) screener (Addendum A.4). Information only."""

from __future__ import annotations

from engine.sidecars.event_arb.models import OFSTerms


def floor_discount(terms: OFSTerms, last_close: float) -> float:
    """(last close − floor) / last close. Positive = floor below market."""
    if last_close <= 0:
        raise ValueError("last_close must be > 0")
    return (last_close - terms.floor_price) / last_close
