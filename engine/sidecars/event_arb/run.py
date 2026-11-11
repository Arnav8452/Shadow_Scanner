"""Daily event-arbitrage scan: fetch candidates → screen curated events → HTML report → alerts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Literal

from engine.backtest.costs import ChargeBreakdown, equity_delivery_charges
from engine.core.calendar import TradingCalendar
from engine.core.config import Settings
from engine.core.rules import RuleBook
from engine.sidecars.event_arb.report import AlertState, build_alerts, render_html, write_report
from engine.sidecars.event_arb.screen import EventArbConfig, RuleInputs, Screened, screen_all
from engine.sidecars.event_arb.sources import (
    NSE_ANNOUNCEMENTS_RSS,
    Announcement,
    CandidateStore,
    http_get,
    load_events,
    parse_nse_rss,
)

log = logging.getLogger(__name__)


def rule_inputs(book: RuleBook, as_of: date) -> RuleInputs:
    unverified: list[str] = []

    def get(table: str, **keys: str) -> str:
        row = book.lookup(table, as_of, allow_unverified=True, **keys)
        if not row.verified:
            unverified.append(f"{table}:{','.join(keys.values())}")
        return row.value

    head = get("tax_rules", item="buyback_tax_head")
    stcg = float(get("tax_rules", item="stcg_listed_equity")) if head == "capital_gains" else None
    return RuleInputs(
        reservation_pct=float(
            get("corporate_action_rules", rule="buyback_small_shareholder_reservation")
        ),
        small_value_limit_inr=float(
            get("corporate_action_rules", rule="small_shareholder_max_value")
        ),
        settlement_days=int(get("corporate_action_rules", rule="equity_settlement_days")),
        stcg_rate_pct=stcg,
        unverified=tuple(unverified),
    )


def make_cost_fn(
    book: RuleBook, as_of: date, broker: str
) -> Callable[[Literal["buy", "sell"], float, bool], ChargeBreakdown]:
    def fn(side: Literal["buy", "sell"], value: float, dp: bool) -> ChargeBreakdown:
        return equity_delivery_charges(
            book,
            as_of,
            side=side,
            value_inr=value,
            broker=broker,
            include_dp=dp,
            allow_unverified=True,
        )

    return fn


@dataclass(frozen=True)
class ScanResult:
    report_path: Path
    alerts: list[str]
    screened: list[Screened]
    new_candidates: list[Announcement]
    fetch_error: str | None


def run_scan(
    settings: Settings,
    root: Path,
    as_of: date,
    *,
    fetch: bool = True,
    fetcher: Callable[[str], str] = http_get,
) -> ScanResult:
    raw = dict(settings.raw.get("event_arb") or {})
    cfg = EventArbConfig.from_mapping(raw, settings.risk.capital_inr)
    book = RuleBook.load(root / "config" / "rules")
    cal = TradingCalendar.from_rulebook(book)
    rules = rule_inputs(book, as_of)
    cost_fn = make_cost_fn(book, as_of, str(raw.get("broker", "zerodha")))
    probe = cost_fn("buy", 1.0, False) + cost_fn("sell", 1.0, True)
    rules = replace(
        rules, unverified=rules.unverified + tuple(f"costs:{t}" for t in probe.unverified)
    )

    store = CandidateStore(
        root / str(raw.get("candidates_file", "data/event_arb/candidates.jsonl"))
    )
    new: list[Announcement] = []
    fetch_error = None
    if fetch:
        try:
            new = store.add(parse_nse_rss(fetcher(NSE_ANNOUNCEMENTS_RSS)))
        except Exception as exc:  # report and continue with what we have
            fetch_error = f"{type(exc).__name__}: {exc}"
            log.warning("announcement fetch failed: %s", fetch_error)

    events = load_events(root / str(raw.get("events_file", "config/event_arb/events.yaml")))
    screened = screen_all(events, as_of, cal, cfg, rules, cost_fn)
    html = render_html(screened, store.recent(as_of, 60), as_of, cfg, rules)
    path = write_report(html, root / str(raw.get("report_dir", "reports/event_arb")), as_of)

    state_path = root / str(raw.get("state_file", "data/event_arb/alert_state.json"))
    state = AlertState.load(state_path)
    alerts = build_alerts(screened, new, as_of, cal, state)
    if fetch_error:
        alerts.append(f"Announcement feed fetch failed: {fetch_error}")
    state.save(state_path)
    return ScanResult(path, alerts, screened, new, fetch_error)
