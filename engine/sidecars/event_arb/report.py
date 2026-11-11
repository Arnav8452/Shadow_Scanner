"""Daily HTML report and alert texts (Addendum A.5). Information only — not advice."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from html import escape
from pathlib import Path

from engine.core.calendar import CalendarCoverageError, TradingCalendar
from engine.sidecars.event_arb.models import BuybackTerms, OpenOfferTerms, RightsTerms, show
from engine.sidecars.event_arb.screen import EventArbConfig, RuleInputs, Screened, Verdict
from engine.sidecars.event_arb.sources import Announcement

DISCLAIMER = (
    "Information only. Not investment advice. The tool never places orders or tenders; "
    "every decision and application is manual."
)

_CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e3e6;--red:#b42318;--amber:#9a6700;
--green:#1a7f37;--card:#f7f7f8}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececee;--muted:#a0a0a8;--line:#2c2c30;
--red:#ff6b5e;--amber:#e3b341;--green:#3fb950;--card:#1c1c1f}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px;
max-width:1100px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:24px 0 8px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:12px 14px;margin:12px 0}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;margin:8px 0;display:block;
overflow-x:auto}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
.RED{color:var(--red);font-weight:600}.AMBER{color:var(--amber);font-weight:600}
.GREEN{color:var(--green);font-weight:600}.INFO{color:var(--muted)}
ul{margin:4px 0;padding-left:18px}a{color:inherit}
"""


def _safe_url(url: str) -> str:
    """Only http(s) links are rendered (feed content is untrusted)."""
    return escape(url) if url.startswith(("https://", "http://")) else "#"


def _inr(x: float | None, dp: int = 0) -> str:
    return "UNKNOWN" if x is None else f"₹{x:,.{dp}f}"


def _pct(x: float | None, dp: int = 1) -> str:
    return "—" if x is None else f"{x * 100:.{dp}f}%"


def _buyback_block(s: Screened, t: BuybackTerms) -> str:
    rows = []
    for r in s.results:
        drops = "".join(f"<td>{_inr(r.net_pnl_by_drop[d])}</td>" for d in sorted(r.net_pnl_by_drop))
        rows.append(
            f"<tr><td>{escape(r.scenario.label)}</td><td>{r.scenario.acceptance:.1%}</td>"
            f"<td>{r.accepted_whole} ({r.accepted_exact:.1f})</td><td>{_inr(r.gain_on_accepted)}</td>"
            f"<td>{show(r.breakeven_exit, '₹{:.2f}')}</td><td>{_pct(r.cushion)}</td>{drops}</tr>"
            f"<tr><td colspan=9 class=muted style='text-align:left'>assumes: {escape(r.scenario.assumption)}</td></tr>"
        )
    drop_hdr = (
        "".join(
            f"<th>net P&amp;L, −{d:.0%} after record</th>"
            for d in sorted(s.results[0].net_pnl_by_drop)
        )
        if s.results
        else ""
    )
    sizing = ""
    if s.sizing:
        caps = ", ".join(f"{k}: {v}" for k, v in s.sizing.caps.items())
        sizing = (
            f"<p>Position: <b>{s.sizing.shares}</b> shares (binding: {escape(s.sizing.binding)}; "
            f"caps — {escape(caps)})</p>"
        )
    tvs = ""
    if s.tender_vs_sell:
        tv = s.tender_vs_sell
        tvs = (
            f"<p class=muted>Tender value/share at pessimistic A: ₹{tv.tender_value_per_share:.2f} "
            f"vs sell now ₹{tv.sell_now_per_share:.2f}</p>"
        )
    terms = (
        f"<p>Buyback ₹{t.price:,.2f} × {t.shares:,} shares · record date {show(t.record_date)} · "
        f"last buy day {show(s.last_buy_day)} · tender {show(t.tender_open)} → {show(t.tender_close)} · "
        f"promoters {t.promoter_participation.value} · SHP small holders "
        f"{show(t.small_holder_shares_shp, '{:,}')} (as of {show(t.shp_as_of)})</p>"
    )
    table = (
        f"<table><tr><th>scenario</th><th>A</th><th>accepted</th><th>gain on accepted</th>"
        f"<th>breakeven exit</th><th>cushion</th>{drop_hdr}</tr>{''.join(rows)}</table>"
        if rows
        else ""
    )
    return terms + sizing + table + tvs


def _other_block(s: Screened) -> str:
    t = s.event.terms
    parts = []
    if isinstance(t, OpenOfferTerms):
        parts.append(
            f"<p>Offer ₹{t.price:,.2f} × {t.offer_shares:,} · public shares "
            f"{show(t.public_shares, '{:,}')} · tender {show(t.tender_open)} → {show(t.tender_close)} · "
            f"payment {show(t.expected_payment_date)}</p>"
        )
        for r in s.other.get("open_offer", []):
            parts.append(
                f"<p class=muted>{escape(r.scenario.label)}: A={r.scenario.acceptance:.1%}, "
                f"gain/share held ₹{r.gain_per_share_held:.2f}, cushion {_pct(r.cushion)}, "
                f"annualised if flat {_pct(r.annualised_if_flat)}</p>"
            )
    elif isinstance(t, RightsTerms):
        parts.append(
            f"<p>Issue ₹{t.issue_price:,.2f}, {t.rights_shares}:{t.held_shares} · RE window "
            f"{show(t.re_trading_start)} → {show(t.re_trading_end)} · issue {show(t.issue_open)} → "
            f"{show(t.issue_close)} · apply via bank ASBA</p>"
        )
    for k, v in s.other.items():
        if k != "open_offer":
            parts.append(f"<p class=muted>{escape(k)}: {escape(str(v))}</p>")
    return "".join(parts)


def render_html(
    screened: Sequence[Screened],
    candidates: Sequence[Announcement],
    as_of: date,
    cfg: EventArbConfig,
    rules: RuleInputs,
) -> str:
    cards = []
    for s in screened:
        e = s.event
        flags = "".join(
            f"<li class={f.level.value}>{f.level.value}: {escape(f.text)}</li>" for f in s.flags
        )
        links = " · ".join(
            f"<a href='{_safe_url(f.url)}'>{escape(f.title or 'filing')}</a>" for f in e.filings
        )
        px = (
            f"price {_inr(e.price.price, 2)} as of {e.price.as_of} ({escape(e.price.source)})"
            if e.price
            else "price UNKNOWN"
        )
        body = _buyback_block(s, e.terms) if isinstance(e.terms, BuybackTerms) else _other_block(s)
        cards.append(
            f"<div class=card><h2><span class={s.verdict.value}>{s.verdict.value}</span> "
            f"{escape(e.company)} ({escape(e.symbol)}) — {e.type.value.replace('_', ' ')}</h2>"
            f"<div class=muted>announced {e.announced_on} · compiled {e.compiled_on} · {px}</div>"
            f"<div>{links}</div><ul>{flags}</ul>{body}"
            + (f"<p class=muted>{escape(e.notes)}</p>" if e.notes else "")
            + "</div>"
        )
    cand_rows = "".join(
        f"<tr><td>{escape(a.published[:16].replace('T', ' '))}</td><td>{escape(a.kind)}</td>"
        f"<td style='text-align:left'>{escape(a.company)}</td>"
        f"<td style='text-align:left'><a href='{_safe_url(a.url)}'>{escape(a.text[:140])}</a>"
        + (f" <span class=AMBER>{escape(a.note)}</span>" if a.note else "")
        + "</td></tr>"
        for a in candidates
    )
    unverified = ", ".join(rules.unverified) or "none"
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Event arb {as_of}</title>
<style>{_CSS}</style></head><body>
<h1>Event-arbitrage scan — {as_of}</h1>
<p class=muted>{escape(DISCLAIMER)}</p>
<p class=muted>Assumptions: participation {", ".join(f"{p:.0%}" for p in cfg.participations)};
optimistic holdings factor {cfg.optimistic_holdings_factor:.0%}; post-record drops
{", ".join(f"{d:.0%}" for d in cfg.drops)}; sleeve capital ₹{cfg.capital_inr:,.0f}; holding cap
₹{cfg.max_holding_value_inr:,.0f}; risk budget {_inr(cfg.risk_budget_inr)} at a
{cfg.worst_case_drop:.0%} drop. Reservation {rules.reservation_pct:g}% (or proportional, whichever
higher). Net P&amp;L uses whole accepted shares and costs from config/rules; tax not deducted.
UNVERIFIED rule inputs used: {escape(unverified)}.</p>
{"".join(cards) or "<p>No curated events.</p>"}
<h2>Announcement candidates (last 60 days, from the NSE feed since polling began)</h2>
<p class=muted>Add the relevant ones to config/event_arb/events.yaml with their terms.</p>
<table><tr><th>published</th><th>kind</th><th>company</th><th>announcement</th></tr>{cand_rows}</table>
</body></html>"""


def write_report(html: str, report_dir: Path, as_of: date) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{as_of.isoformat()}.html"
    path.write_text(html, encoding="utf-8")
    return path


# ---------------------------------------------------------------- alerts


@dataclass
class AlertState:
    seen_event_ids: set[str] = field(default_factory=set)
    seen_candidate_urls: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> AlertState:
        if not path.is_file():
            return cls()
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(set(d.get("seen_event_ids", [])), set(d.get("seen_candidate_urls", [])))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "seen_event_ids": sorted(self.seen_event_ids),
                    "seen_candidate_urls": sorted(self.seen_candidate_urls),
                },
                indent=1,
            ),
            encoding="utf-8",
        )


def build_alerts(
    screened: Sequence[Screened],
    new_candidates: Sequence[Announcement],
    as_of: date,
    cal: TradingCalendar,
    state: AlertState,
) -> list[str]:
    """Alert texts for today. Mutates ``state`` (marks events/candidates as seen)."""
    try:
        nxt = cal.next_trading_day(as_of)
    except CalendarCoverageError:
        nxt = None
    out: list[str] = []
    for s in screened:
        e = s.event
        tag = f"{e.symbol} {e.type.value.replace('_', ' ')}"
        if e.id not in state.seen_event_ids:
            state.seen_event_ids.add(e.id)
            if s.verdict is not Verdict.RED:
                out.append(f"New {s.verdict.value} event: {tag}. {e.filing_url}")
        if s.verdict is Verdict.RED:
            continue
        if s.last_buy_day is not None:
            if s.last_buy_day == as_of:
                out.append(f"{tag}: TODAY is the last buy day for the record date.")
            elif nxt is not None and s.last_buy_day == nxt:
                out.append(f"{tag}: last buy day is the next trading day ({nxt}).")
        t = e.terms
        if isinstance(t, BuybackTerms | OpenOfferTerms):
            for label, d in (
                ("tender window opens", t.tender_open),
                ("tender window closes", t.tender_close),
            ):
                if d is not None and d in (as_of, nxt):
                    out.append(
                        f"{tag}: {label} {'today' if d == as_of else 'next trading day'} ({d})."
                    )
        if isinstance(t, BuybackTerms) and t.unaccepted_return_date == as_of:
            out.append(f"{tag}: unaccepted shares due back in demat today — plan the exit.")
        if (
            isinstance(t, RightsTerms)
            and t.re_trading_start is not None
            and t.re_trading_start in {as_of, nxt}
        ):
            out.append(f"{tag}: RE trading window opens {t.re_trading_start}.")
        if s.tender_vs_sell and s.tender_vs_sell.prefer_sell:
            out.append(
                f"{tag}: market price ≥ expected tender value at pessimistic acceptance — "
                "consider selling instead of tendering (or not entering)."
            )
    for a in new_candidates:
        if a.url not in state.seen_candidate_urls:
            state.seen_candidate_urls.add(a.url)
            out.append(f"Candidate {a.kind}: {a.company} — {a.text[:120]} {a.url}")
    return out
