from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from engine.core.calendar import TradingCalendar
from engine.core.rules import RuleBook
from engine.sidecars.event_arb.models import (
    BuybackTerms,
    Event,
    EventType,
    Filing,
    OFSTerms,
    OpenOfferTerms,
    PriceSnapshot,
    RightsTerms,
    Tri,
)
from engine.sidecars.event_arb.report import AlertState, build_alerts, render_html
from engine.sidecars.event_arb.run import make_cost_fn, rule_inputs
from engine.sidecars.event_arb.screen import (
    EventArbConfig,
    Level,
    RuleInputs,
    Verdict,
    screen_all,
    screen_event,
)
from engine.sidecars.event_arb.sources import Announcement

AS_OF = date(2026, 11, 9)
RULES = RuleInputs(
    reservation_pct=15, small_value_limit_inr=200_000, settlement_days=1, stcg_rate_pct=20
)
CFG = EventArbConfig()


@pytest.fixture(scope="module")
def cal(repo_root: Path) -> TradingCalendar:
    return TradingCalendar.from_rulebook(RuleBook.load(repo_root / "config" / "rules"))


def make_buyback(**terms: object) -> Event:
    base = dict(
        price=425.0,
        shares=1_159_199,
        shares_outstanding=74_787_032,
        record_date=date(2026, 11, 20),
        promoter_participation=Tri.NO,
        small_holder_shares_shp=6_000_000,
        shp_as_of=date(2026, 9, 30),
    )
    base.update(terms)
    return Event(
        id="T-1",
        type=EventType.TENDER_BUYBACK,
        symbol="TEST",
        company="Test <Co> Ltd",
        announced_on=date(2026, 10, 30),
        compiled_on=AS_OF,
        filings=(Filing("https://example.org/filing"),),
        terms=BuybackTerms(**base),  # type: ignore[arg-type]
        price=PriceSnapshot(289.0, AS_OF, "test", price_at_announcement=296.75),
    )


def levels(s: object) -> list[str]:
    return [f.text for f in s.flags]  # type: ignore[attr-defined]


def test_buyback_screen_computes_last_buy_day_and_scenarios(cal: TradingCalendar) -> None:
    s = screen_event(make_buyback(), AS_OF, cal, CFG, RULES)
    assert s.last_buy_day == date(2026, 11, 19)
    assert len(s.results) == 6 and s.sizing is not None and s.sizing.shares > 0
    assert s.pessimistic is not None
    assert any("small buyback" in t for t in levels(s))
    assert s.verdict in {Verdict.AMBER, Verdict.RED}


def test_record_date_passed_is_red(cal: TradingCalendar) -> None:
    s = screen_event(make_buyback(), date(2026, 11, 20), cal, CFG, RULES)
    assert s.verdict is Verdict.RED and any("record date passed" in t for t in levels(s))


def test_last_buy_day_skips_holiday(cal: TradingCalendar) -> None:
    s = screen_event(make_buyback(record_date=date(2026, 11, 11)), AS_OF, cal, CFG, RULES)
    assert s.last_buy_day == date(2026, 11, 9)  # 10 Nov 2026 is a holiday


def test_open_market_buyback_is_red(cal: TradingCalendar) -> None:
    e = replace(make_buyback(), type=EventType.OPEN_MARKET_BUYBACK)
    assert screen_event(e, AS_OF, cal, CFG, RULES).verdict is Verdict.RED


def test_unknowns_are_flagged_not_guessed(cal: TradingCalendar) -> None:
    s = screen_event(
        make_buyback(
            promoter_participation=Tri.UNKNOWN,
            small_holder_shares_shp=None,
            shares_outstanding=None,
        ),
        AS_OF,
        cal,
        CFG,
        RULES,
    )
    texts = " | ".join(levels(s))
    assert "promoter participation UNKNOWN" in texts
    assert "acceptance UNKNOWN" in texts and not s.results


def test_low_cushion_is_red(cal: TradingCalendar) -> None:
    # tiny reserve vs huge small-holder base → A ≈ 0.3% → cushion < 1%
    s = screen_event(make_buyback(small_holder_shares_shp=60_000_000), AS_OF, cal, CFG, RULES)
    assert s.verdict is Verdict.RED
    assert any(f.level is Level.RED and "cushion" in f.text for f in s.flags)


def test_future_announcement_excluded(cal: TradingCalendar) -> None:
    e = replace(make_buyback(), announced_on=date(2026, 11, 15))
    assert screen_all([e], AS_OF, cal, CFG, RULES) == []


def test_other_event_types(cal: TradingCalendar) -> None:
    f = (Filing("https://example.org/x"),)
    px = PriceSnapshot(100.0, AS_OF, "t")
    oo = Event(
        "O",
        EventType.OPEN_OFFER,
        "OO",
        "OO Ltd",
        AS_OF,
        AS_OF,
        f,
        OpenOfferTerms(
            price=120, offer_shares=260, public_shares=1000, expected_payment_date=date(2027, 1, 8)
        ),
        px,
    )
    rt = Event(
        "R",
        EventType.RIGHTS,
        "RT",
        "RT Ltd",
        AS_OF,
        AS_OF,
        f,
        RightsTerms(issue_price=95, rights_shares=1, held_shares=5, re_price=1.0),
        px,
    )
    of = Event("F", EventType.OFS, "OF", "OF Ltd", AS_OF, AS_OF, f, OFSTerms(floor_price=92), px)
    out = {s.event.id: s for s in screen_all([oo, rt, of], AS_OF, cal, CFG, RULES)}
    assert out["O"].other["premium_pct"] == pytest.approx(20)
    assert len(out["O"].other["open_offer"]) == 3
    assert any("issue price" in t for t in levels(out["R"]))  # only 5% above issue
    assert out["F"].other["floor_discount_pct"] == pytest.approx(8)


def test_html_report_escapes_and_shows_assumptions(cal: TradingCalendar) -> None:
    s = screen_all([make_buyback()], AS_OF, cal, CFG, RULES)
    cand = [
        Announcement(
            "X <b>",
            "https://example.org/a",
            "Buyback <script>",
            "Updates",
            "2026-11-09T10:00:00+05:30",
            "tender_buyback",
        )
    ]
    html = render_html(s, cand, AS_OF, CFG, RULES)
    assert "Not investment advice" in html and "<script>" not in html and "Test &lt;Co&gt;" in html
    assert "assumes:" in html and "breakeven exit" in html


def test_alerts(cal: TradingCalendar, tmp_path: Path) -> None:
    e = make_buyback(record_date=date(2026, 11, 12), tender_open=date(2026, 11, 11))
    s = screen_all([e], AS_OF, cal, CFG, RULES)
    st = AlertState()
    cand = [
        Announcement(
            "C",
            "https://example.org/c",
            "Open offer",
            "Open Offer",
            "2026-11-09T10:00:00+05:30",
            "open_offer",
        )
    ]
    alerts = build_alerts(s, cand, AS_OF, cal, st)
    joined = "\n".join(alerts)
    assert "New" in joined and "last buy day is the next trading day (2026-11-11)" in joined
    assert "tender window opens next trading day" in joined and "Candidate open_offer" in joined
    st.save(tmp_path / "s.json")
    again = build_alerts(s, cand, AS_OF, cal, AlertState.load(tmp_path / "s.json"))
    assert not any(a.startswith(("New", "Candidate")) for a in again)


def test_rule_inputs_and_costs_from_repo(repo_root: Path) -> None:
    book = RuleBook.load(repo_root / "config" / "rules")
    ri = rule_inputs(book, AS_OF)
    assert ri.reservation_pct == 15 and ri.small_value_limit_inr == 200_000
    assert ri.settlement_days == 1 and ri.stcg_rate_pct == 20
    assert any("tax_rules" in u for u in ri.unverified)
    fn = make_cost_fn(book, AS_OF, "zerodha")
    assert fn("buy", 100_000, False).total > 100  # STT alone is ₹100
