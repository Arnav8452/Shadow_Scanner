"""Screening flags and ranking (Addendum A.3 flags, A.4 models). Information only."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from engine.core.calendar import CalendarCoverageError, TradingCalendar
from engine.sidecars.event_arb import buyback, ofs, open_offer, rights
from engine.sidecars.event_arb.buyback import CostFn, Scenario, ScenarioResult, Sizing, no_costs
from engine.sidecars.event_arb.models import (
    BuybackTerms,
    Event,
    EventType,
    OFSTerms,
    OpenOfferTerms,
    RightsTerms,
    Tri,
)


class Level(StrEnum):
    RED = "RED"
    AMBER = "AMBER"
    INFO = "INFO"


class Verdict(StrEnum):
    RED = "RED"
    AMBER = "AMBER"
    GREEN = "GREEN"


@dataclass(frozen=True)
class Flag:
    level: Level
    text: str


@dataclass(frozen=True)
class EventArbConfig:
    """``event_arb`` section of settings.yaml (all values PROPOSED until the owner confirms)."""

    capital_inr: float = 100_000
    max_holding_value_inr: float = 150_000
    risk_budget_inr: float | None = 6_000
    worst_case_drop: float = 0.15
    participations: tuple[float, ...] = buyback.DEFAULT_PARTICIPATIONS
    optimistic_holdings_factor: float = 0.5
    drops: tuple[float, ...] = buyback.DEFAULT_DROPS
    assumed_acceptances: tuple[float, ...] = ()
    small_buyback_pct: float = 3.0
    runup_amber_pct: float = 8.0
    near_52w_high_pct: float = 5.0
    min_avg_daily_value_inr: float = 5_000_000
    cushion_amber_pct: float = 3.0
    cushion_red_pct: float = 1.0
    stale_price_trading_days: int = 3
    ofs_discount_alert_pct: float = 5.0
    re_gap_alert_pct: float = 5.0
    rights_cushion_amber_pct: float = 10.0
    round_trip_cost_pct: float = 0.25

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any], account_capital_inr: float) -> EventArbConfig:
        flags = dict(m.get("flags") or {})
        risk_pct = m.get("risk_budget_pct_of_account")
        return cls(
            capital_inr=float(m.get("capital_inr", cls.capital_inr)),
            max_holding_value_inr=float(m.get("max_holding_value_inr", cls.max_holding_value_inr)),
            risk_budget_inr=None
            if risk_pct is None
            else account_capital_inr * float(risk_pct) / 100,
            worst_case_drop=float(m.get("worst_case_drop_pct", 15)) / 100,
            participations=tuple(float(x) for x in m.get("participations", cls.participations)),
            optimistic_holdings_factor=float(m.get("optimistic_holdings_factor", 0.5)),
            drops=tuple(float(x) / 100 for x in m.get("drops_pct", [5, 10, 15])),
            assumed_acceptances=tuple(float(x) for x in m.get("assumed_acceptances") or ()),
            **{k: type(getattr(cls, k))(v) for k, v in flags.items()},
        )


@dataclass(frozen=True)
class RuleInputs:
    reservation_pct: float
    small_value_limit_inr: float
    settlement_days: int
    stcg_rate_pct: float | None  # None when buyback proceeds are not under capital gains
    unverified: tuple[str, ...] = ()


@dataclass
class Screened:
    event: Event
    as_of: date
    flags: list[Flag] = field(default_factory=list)
    last_buy_day: date | None = None
    sizing: Sizing | None = None
    results: list[ScenarioResult] = field(default_factory=list)
    other: dict[str, Any] = field(default_factory=dict)  # metrics for non-buyback events
    tender_vs_sell: buyback.TenderVsSell | None = None
    pessimistic: Scenario | None = None

    @property
    def verdict(self) -> Verdict:
        levels = {f.level for f in self.flags}
        if Level.RED in levels:
            return Verdict.RED
        return Verdict.AMBER if Level.AMBER in levels else Verdict.GREEN

    def add(self, level: Level, text: str) -> None:
        self.flags.append(Flag(level, text))

    @property
    def rank_key(self) -> tuple[int, float]:
        order = {Verdict.GREEN: 0, Verdict.AMBER: 1, Verdict.RED: 2}[self.verdict]
        pess = [
            r.cushion for r in self.results if self.pessimistic and r.scenario == self.pessimistic
        ]
        c = pess[0] if pess and pess[0] is not None else -1.0
        return (order, -c)


def _date_checks(s: Screened, cal: TradingCalendar, record_date: date | None, settle: int) -> None:
    if record_date is None:
        s.add(Level.AMBER, "record date UNKNOWN")
        return
    try:
        s.last_buy_day = cal.last_cum_date(record_date, settle)
    except CalendarCoverageError as exc:
        s.add(Level.AMBER, f"cannot compute last buy day: {exc}")
        return
    if s.as_of > s.last_buy_day:
        s.add(Level.RED, f"record date passed for new buyers (last buy day was {s.last_buy_day})")


def _price_checks(s: Screened, cal: TradingCalendar, cfg: EventArbConfig) -> None:
    px = s.event.price
    if px is None:
        s.add(Level.AMBER, "no price snapshot — scenarios not computed")
        return
    try:
        age = cal.trading_days_between(px.as_of, s.as_of)
    except CalendarCoverageError:
        age = (s.as_of - px.as_of).days
    if age > cfg.stale_price_trading_days:
        s.add(Level.AMBER, f"price is stale ({px.as_of}, {age} trading days old)")
    if px.price_at_announcement:
        runup = (px.price / px.price_at_announcement - 1) * 100
        if runup > cfg.runup_amber_pct:
            s.add(Level.AMBER, f"run-up {runup:.1f}% since announcement (reversion risk)")
    if px.high_52w and px.price >= px.high_52w * (1 - cfg.near_52w_high_pct / 100):
        s.add(Level.AMBER, "price near 52-week high")
    if px.avg_daily_value_inr is not None and px.avg_daily_value_inr < cfg.min_avg_daily_value_inr:
        s.add(Level.AMBER, f"thin liquidity (avg daily value ₹{px.avg_daily_value_inr:,.0f})")


def _screen_buyback(
    s: Screened,
    t: BuybackTerms,
    cal: TradingCalendar,
    cfg: EventArbConfig,
    rules: RuleInputs,
    cost_fn: CostFn,
) -> None:
    if s.event.type is EventType.OPEN_MARKET_BUYBACK:
        s.add(Level.RED, "open-market buyback: no reservation, no guaranteed acceptance")
        return
    _date_checks(s, cal, t.record_date, rules.settlement_days)
    size_pct = (
        buyback.size_fraction(t.shares, t.shares_outstanding) * 100
        if t.shares_outstanding
        else t.published_size_pct
    )
    if size_pct is None:
        s.add(Level.AMBER, "buyback size vs equity UNKNOWN")
    elif size_pct < cfg.small_buyback_pct:
        s.add(Level.AMBER, f"small buyback ({size_pct:.2f}% of equity) → low acceptance likely")
    if t.promoter_participation is Tri.YES:
        s.add(Level.AMBER, "promoters participating (lowers acceptance)")
    elif t.promoter_participation is Tri.UNKNOWN:
        s.add(Level.AMBER, "promoter participation UNKNOWN")
    _price_checks(s, cal, cfg)

    scen = buyback.acceptance_scenarios(
        t,
        rules.reservation_pct,
        participations=cfg.participations,
        optimistic_holdings_factor=cfg.optimistic_holdings_factor,
        explicit=cfg.assumed_acceptances,
    )
    if not scen:
        s.add(
            Level.AMBER,
            "acceptance UNKNOWN: add small-holder SHP data, past ratios or the "
            "published entitlement",
        )
    if s.event.price is None or not scen:
        return
    p_buy = s.event.price.price
    s.pessimistic = min(scen, key=lambda x: x.acceptance)
    a_pess = s.pessimistic.acceptance
    s.sizing = buyback.size_position(
        p_buy,
        capital_inr=cfg.capital_inr,
        max_holding_value_inr=cfg.max_holding_value_inr,
        small_value_limit_inr=rules.small_value_limit_inr,
        risk_budget_inr=cfg.risk_budget_inr,
        worst_loss_per_share=buyback.worst_loss_per_share(
            p_buy, t.price, a_pess, cfg.worst_case_drop
        ),
    )
    s.results = [
        buyback.evaluate(
            p_buy=p_buy,
            p_bb=t.price,
            shares=s.sizing.shares,
            scenario=sc,
            drops=cfg.drops,
            cost_fn=cost_fn,
            stcg_rate_pct=rules.stcg_rate_pct,
        )
        for sc in scen
    ]
    pess_cushion = buyback.cushion(p_buy, buyback.breakeven_exit_price(p_buy, t.price, a_pess))
    if pess_cushion is not None:
        if pess_cushion * 100 < cfg.cushion_red_pct:
            s.add(
                Level.RED,
                f"cushion at pessimistic acceptance {pess_cushion:.1%} < {cfg.cushion_red_pct}%",
            )
        elif pess_cushion * 100 < cfg.cushion_amber_pct:
            s.add(
                Level.AMBER,
                f"cushion at pessimistic acceptance {pess_cushion:.1%} < {cfg.cushion_amber_pct}%",
            )
    mid_drop = cfg.drops[len(cfg.drops) // 2] if cfg.drops else 0.10
    s.tender_vs_sell = buyback.tender_vs_sell_now(p_buy, t.price, a_pess, mid_drop)
    if s.tender_vs_sell.prefer_sell:
        tv = s.tender_vs_sell
        s.add(
            Level.AMBER,
            f"negative expected value: at A={a_pess:.1%} and a {mid_drop:.0%} "
            f"post-record drop, tendering is worth ₹{tv.tender_value_per_share:.2f}/share "
            f"vs ₹{tv.sell_now_per_share:.2f} market — do not enter / sell instead "
            "of tendering",
        )
    if s.sizing.shares == 0:
        s.add(Level.AMBER, f"position size 0 (binding cap: {s.sizing.binding})")


def _screen_open_offer(s: Screened, t: OpenOfferTerms, cfg: EventArbConfig) -> None:
    if t.tender_close and s.as_of > t.tender_close:
        s.add(Level.RED, f"tendering period closed ({t.tender_close})")
    if t.pending_approvals:
        s.add(Level.AMBER, f"pending approvals: {t.pending_approvals}")
    if t.public_shares is None:
        s.add(Level.AMBER, "public shareholding UNKNOWN — acceptance not computed")
    _price_checks_simple(s)
    if s.event.price is None:
        return
    scen = open_offer.scenarios(t, cfg.participations)
    s.other["open_offer"] = [
        open_offer.evaluate(s.event.price.price, t, sc, s.as_of) for sc in scen
    ]
    s.other["premium_pct"] = (t.price / s.event.price.price - 1) * 100


def _price_checks_simple(s: Screened) -> None:
    if s.event.price is None:
        s.add(Level.AMBER, "no price snapshot")


def _screen_rights(
    s: Screened, t: RightsTerms, cal: TradingCalendar, cfg: EventArbConfig, rules: RuleInputs
) -> None:
    if t.re_trading_end and s.as_of > t.re_trading_end:
        s.add(Level.RED, f"RE trading window closed ({t.re_trading_end})")
    if t.partly_paid:
        s.add(Level.AMBER, "partly-paid shares: later call money due — model assumes fully paid")
    _price_checks_simple(s)
    if s.event.price is None:
        return
    p = s.event.price.price
    cush = rights.issue_price_cushion(p, t.issue_price) * 100
    s.other["issue_cushion_pct"] = cush
    s.other["re_fair_value"] = rights.re_fair_value(p, t.issue_price)
    if cush < cfg.rights_cushion_amber_pct:
        s.add(Level.AMBER, f"share only {cush:.1f}% above issue price")
    gap = rights.re_gap(p, t, cfg.round_trip_cost_pct)
    if gap:
        s.other["re_gap"] = gap
        if gap.cheap_after_costs and gap.gap_pct_of_share >= cfg.re_gap_alert_pct:
            s.add(
                Level.INFO,
                f"RE trades {gap.gap_pct_of_share:.1f}% of share price below fair value "
                "(a discounted LONG — not hedgeable for retail)",
            )


def _screen_ofs(s: Screened, t: OFSTerms, cfg: EventArbConfig) -> None:
    if t.retail_date and s.as_of > t.retail_date:
        s.add(Level.RED, f"retail OFS day passed ({t.retail_date})")
    _price_checks_simple(s)
    if s.event.price is None:
        return
    disc = ofs.floor_discount(t, s.event.price.price) * 100
    s.other["floor_discount_pct"] = disc
    if disc >= cfg.ofs_discount_alert_pct:
        s.add(Level.INFO, f"floor {disc:.1f}% below last price (price often gravitates to floor)")


def screen_event(
    event: Event,
    as_of: date,
    cal: TradingCalendar,
    cfg: EventArbConfig,
    rules: RuleInputs,
    cost_fn: CostFn = no_costs,
) -> Screened:
    s = Screened(event=event, as_of=as_of)
    if event.announced_on > as_of:  # point-in-time: not known yet
        s.add(Level.RED, f"announced after as_of ({event.announced_on})")
        return s
    t = event.terms
    if isinstance(t, BuybackTerms):
        _screen_buyback(s, t, cal, cfg, rules, cost_fn)
    elif isinstance(t, OpenOfferTerms):
        _screen_open_offer(s, t, cfg)
    elif isinstance(t, RightsTerms):
        _screen_rights(s, t, cal, cfg, rules)
    elif isinstance(t, OFSTerms):
        _screen_ofs(s, t, cfg)
    else:
        s.add(Level.INFO, "demerger: research note only (no model)")
    if event.unverified_fields:
        s.add(Level.INFO, "from secondary/unchecked sources: " + ", ".join(event.unverified_fields))
    return s


def screen_all(
    events: Sequence[Event],
    as_of: date,
    cal: TradingCalendar,
    cfg: EventArbConfig,
    rules: RuleInputs,
    cost_fn: CostFn = no_costs,
) -> list[Screened]:
    out = [
        screen_event(e, as_of, cal, cfg, rules, cost_fn) for e in events if e.announced_on <= as_of
    ]
    return sorted(out, key=lambda x: x.rank_key)
