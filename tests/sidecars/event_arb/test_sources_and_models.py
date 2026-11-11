from datetime import date
from pathlib import Path

import pytest

from engine.sidecars.event_arb import ofs, open_offer, rights
from engine.sidecars.event_arb.models import BuybackTerms, EventType, OFSTerms, RightsTerms, Tri
from engine.sidecars.event_arb.sources import (
    CandidateStore,
    EventFileError,
    classify,
    event_from_mapping,
    load_events,
    parse_nse_rss,
)

RSS = """<rss version="2.0"><channel>
<item><title>Jyothy Labs Limited</title><link>https://example.org/1.pdf</link>
<description>Jyothy Labs Limited has informed the Exchange regarding 'Addendum to Public Announcement - Buyback of Equity Shares of the Company'. |SUBJECT: Updates</description>
<pubDate>09-Nov-2026 16:49:59</pubDate></item>
<item><title>Tata Motors Limited</title><link>https://example.org/2.pdf</link>
<description>press release titled Voluntary Totalitarian Tender Offer for all the common shares of Iveco Group N.V. |SUBJECT: Press Release</description>
<pubDate>09-Nov-2026 21:30:31</pubDate></item>
<item><title>Castrol India Limited</title><link>https://example.org/3.zip</link>
<description>UBS Securities India Private Limited has Submitted to the Exchange a copy of detailed public statement |SUBJECT: Updates</description>
<pubDate>09-Nov-2026 12:39:42</pubDate></item>
<item><title>Some Co</title><link>https://example.org/4.pdf</link>
<description>Disclosure of shareholding |SUBJECT: Disclosure under SEBI Takeover Regulations</description>
<pubDate>09-Nov-2026 11:00:00</pubDate></item>
</channel></rss>"""


def test_parse_nse_rss_filters_and_classifies() -> None:
    items = parse_nse_rss(RSS)
    kinds = {a.company: a.kind for a in items}
    assert kinds == {"Jyothy Labs Limited": "tender_buyback", "Castrol India Limited": "open_offer"}
    jy = next(a for a in items if a.company.startswith("Jyothy"))
    assert "route not stated" in jy.note and jy.published.startswith("2026-11-09T16:49:59+05:30")


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Buyback of shares through tender offer route", EventType.TENDER_BUYBACK),
        ("Buy-back through open market route via stock exchanges", EventType.OPEN_MARKET_BUYBACK),
        ("Letter of offer for rights issue", EventType.RIGHTS),
        ("Notice of Offer for Sale by promoter", EventType.OFS),
        ("Scheme of demerger approved", EventType.DEMERGER),
        ("Quarterly results", None),
    ],
)
def test_classify(text: str, kind: EventType | None) -> None:
    assert classify(text)[0] == kind


def test_candidate_store_dedupes_and_is_point_in_time(tmp_path: Path) -> None:
    store = CandidateStore(tmp_path / "c.jsonl")
    items = parse_nse_rss(RSS)
    assert len(store.add(items)) == 2 and store.add(items) == []
    assert len(store.recent(date(2026, 11, 9))) == 2
    assert store.recent(date(2026, 11, 8)) == []  # nothing from the future
    assert store.recent(date(2027, 2, 1), days=60) == []


def test_repo_events_file_loads(repo_root: Path) -> None:
    events = load_events(repo_root / "config" / "event_arb" / "events.yaml")
    med = next(e for e in events if e.symbol == "MEDIASSIST")
    assert isinstance(med.terms, BuybackTerms)
    assert med.terms.shares == 1_159_199 and med.terms.price == 425
    assert med.terms.shares_outstanding is None  # UNKNOWN stays unknown
    assert med.terms.promoter_participation is Tri.UNKNOWN
    assert med.terms.record_date == date(2026, 11, 20)


def test_event_validation() -> None:
    good = {
        "id": "x",
        "type": "ofs",
        "symbol": "X",
        "company": "X",
        "announced_on": "2026-11-01",
        "compiled_on": "2026-11-09",
        "filings": [{"url": "https://example.org"}],
        "terms": {"floor_price": "1,234.5", "retail_date": "UNKNOWN"},
    }
    e = event_from_mapping(good)
    assert isinstance(e.terms, OFSTerms) and e.terms.floor_price == 1234.5
    assert e.terms.retail_date is None
    with pytest.raises(EventFileError, match="filing"):
        event_from_mapping({**good, "filings": []})
    with pytest.raises(EventFileError, match="unknown field"):
        event_from_mapping({**good, "terms": {"floor_price": 1, "flor": 2}})
    with pytest.raises(EventFileError, match="type"):
        event_from_mapping({**good, "type": "ipo"})


def test_open_offer_math() -> None:
    assert open_offer.acceptance(260, 1000, 1.0) == pytest.approx(0.26)
    assert open_offer.acceptance(260, 1000, 0.2) == 1.0
    assert open_offer.annualised(0.02, 365) == pytest.approx(0.02)
    assert open_offer.annualised(0.02, 0) is None


def test_rights_math() -> None:
    t = RightsTerms(issue_price=80, rights_shares=1, held_shares=4, re_price=12)
    assert rights.re_fair_value(100, 80) == 20 and rights.re_fair_value(70, 80) == 0
    assert rights.theoretical_ex_rights_price(100, t) == pytest.approx(96)
    gap = rights.re_gap(100, t, round_trip_cost_pct=0.5)
    assert gap is not None and gap.gap_inr == pytest.approx(8) and gap.cheap_after_costs
    assert rights.issue_price_cushion(100, 80) == pytest.approx(0.2)


def test_ofs_math() -> None:
    assert ofs.floor_discount(OFSTerms(floor_price=90), 100) == pytest.approx(0.10)
