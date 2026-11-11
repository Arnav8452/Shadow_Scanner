"""Event records for the event-arbitrage tracker (Addendum A).

Unpublished facts are ``None`` and print as ``UNKNOWN`` — never guessed. Every event carries
the filing URL(s) it was compiled from and the date it was compiled (``compiled_on``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class EventType(StrEnum):
    TENDER_BUYBACK = "tender_buyback"
    OPEN_MARKET_BUYBACK = "open_market_buyback"
    OPEN_OFFER = "open_offer"
    RIGHTS = "rights"
    OFS = "ofs"
    DEMERGER = "demerger"


class Tri(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


def show(value: object, fmt: str = "{}") -> str:
    return "UNKNOWN" if value is None else fmt.format(value)


@dataclass(frozen=True)
class Filing:
    url: str
    title: str = ""
    published_on: date | None = None


@dataclass(frozen=True)
class PriceSnapshot:
    """Market data the screen needs. Entered manually until L11 (M3) supplies it."""

    price: float
    as_of: date
    source: str
    price_at_announcement: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    avg_daily_value_inr: float | None = None  # 20-day average traded value


@dataclass(frozen=True)
class BuybackTerms:
    price: float  # P_bb
    shares: int  # N_bb
    shares_outstanding: int | None = None  # N_tot
    published_size_pct: float | None = None  # "x% of paid-up equity" as stated in the filing
    record_date: date | None = None
    promoter_participation: Tri = Tri.UNKNOWN
    # Shareholding pattern: "resident individuals holding nominal share capital up to ₹2 lakh".
    small_holder_shares_shp: int | None = None
    shp_as_of: date | None = None
    # Entitlement ratio for the reserved category once the letter of offer publishes it
    # (accepted shares per share held on the record date).
    published_small_entitlement: float | None = None
    past_small_acceptance: tuple[float, ...] = ()
    tender_open: date | None = None
    tender_close: date | None = None
    unaccepted_return_date: date | None = None


@dataclass(frozen=True)
class OpenOfferTerms:
    price: float
    offer_shares: int
    public_shares: int | None = None
    tender_open: date | None = None
    tender_close: date | None = None
    expected_payment_date: date | None = None
    pending_approvals: str = ""


@dataclass(frozen=True)
class RightsTerms:
    issue_price: float
    rights_shares: int  # new shares per `held_shares`
    held_shares: int
    record_date: date | None = None
    re_trading_start: date | None = None
    re_trading_end: date | None = None
    issue_open: date | None = None
    issue_close: date | None = None
    re_price: float | None = None
    partly_paid: bool = False


@dataclass(frozen=True)
class OFSTerms:
    floor_price: float
    non_retail_date: date | None = None
    retail_date: date | None = None
    retail_reservation_pct: float | None = None
    indicative_price: float | None = None


@dataclass(frozen=True)
class DemergerTerms:
    ratio: str = ""
    record_date: date | None = None
    expected_listing: date | None = None


Terms = BuybackTerms | OpenOfferTerms | RightsTerms | OFSTerms | DemergerTerms

TERMS_FOR: dict[EventType, type] = {
    EventType.TENDER_BUYBACK: BuybackTerms,
    EventType.OPEN_MARKET_BUYBACK: BuybackTerms,
    EventType.OPEN_OFFER: OpenOfferTerms,
    EventType.RIGHTS: RightsTerms,
    EventType.OFS: OFSTerms,
    EventType.DEMERGER: DemergerTerms,
}


@dataclass(frozen=True)
class Event:
    id: str
    type: EventType
    symbol: str
    company: str
    announced_on: date
    compiled_on: date
    filings: tuple[Filing, ...]
    terms: Terms
    price: PriceSnapshot | None = None
    unverified_fields: tuple[str, ...] = ()  # facts copied from secondary/unchecked sources
    notes: str = ""
    field_sources: dict[str, str] = field(default_factory=dict)

    @property
    def filing_url(self) -> str:
        return self.filings[0].url if self.filings else ""
