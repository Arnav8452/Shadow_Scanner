# Addendum A — Event-Arbitrage Tracker

Add-on to `docs/SPEC.md` (already handed over). Read SPEC first; everything in `CLAUDE.md` still applies.
Date: 9 Nov 2026. Scope: corporate-event "arbitrage" in Indian cash equities, sized for ₹1–2 lakh. **The tool screens and calculates; every trade decision and every tender/application stays manual.**

## A.0 What this changes in SPEC.md
- **Replaces** the "Buyback tracker" bullet in SPEC §10 and the file `engine/sidecars/buyback_tracker.py` from SPEC §2.
- New package: `engine/sidecars/event_arb/` (layout in A.6).
- Uses SPEC §3 L6 (events/announcements) and L11 (daily prices); alerts via `ops/alerts/telegram.py`; runs on the existing scheduler.
- Milestone: **M13-EA** (A.8). Independent of the options engine; can be built any time after M0 and M3.

## A.1 Events covered

| Event | Edge | Main risk | Status |
|---|---|---|---|
| **Tender-offer buyback** | Buyback price above market; 15% of the buyback reserved for small shareholders (holding value ≤ ₹2L on the record date); many retail holders don't tender, so acceptance can exceed entitlement | Price drop on unaccepted shares after the record date; low acceptance when the buyback is small vs equity | Full model |
| **Open offer** (SEBI Takeover Code, 26% offer) | Fixed offer price vs market | Low acceptance; regulatory delays (e.g., CCI); stock falls once ex-offer | Full model |
| **Rights issue & rights entitlements (REs)** | RE fair value ≈ share price − issue price; REs can misprice during their trading window; unclaimed shares can be applied for | Share price sliding toward the issue price before new shares list | Screener + calculator |
| **OFS (offer for sale)** | Floor price at a discount to market; retail reservation | Price falls to the floor or below during/after the OFS | Screener + calculator |
| **Demerger / spin-off** | Forced selling of the new small entity at listing | Valuation misjudgement | Research note only |
| **Open-market buyback** | — | No reservation, no guaranteed acceptance | Flag and ignore |
| **Pre-IPO / unlisted shares** | — | Illiquid, opaque pricing, 6-month post-IPO lock-in | Out of scope |

## A.2 Data sources
- Exchange corporate announcements and filings (NSE/BSE): board approvals, record-date notices, public announcements, letters of offer, rights-issue schedules, OFS notices. Store the filing URL with every event.
- Quarterly **shareholding pattern** per company (needed for the acceptance model).
- Daily OHLCV (SPEC L11): price at announcement, run-up since, 52-week high/low, average volume.
- Company history: past buybacks and their **actual small-shareholder acceptance ratios** (published in post-offer reports).
- Trading-day calendar (SPEC L3) for record-date and T+1 logic.

All facts are point-in-time (`as_of`), and every field records its source URL. If a field (e.g., promoter participation, entitlement ratio) is not yet published, store `UNKNOWN` — never guess silently.

## A.3 Tender-buyback model

**Inputs**: buyback price `P_bb`; shares to be bought `N_bb`; total shares outstanding `N_tot`; current price `P_buy`; record date; promoter participation (yes/no/unknown); estimated small-shareholder holdings `H_small`; tender participation scenarios; past acceptance ratios.

**Calculations**
1. Size: `N_bb / N_tot` (% of equity). Flag `< 3%` as "small buyback → low acceptance".
2. Reserved shares: `R = 0.15 × N_bb` (verify the current reservation rule; SPEC rules-table pattern).
3. Small-shareholder holdings `H_small`: from the shareholding-pattern category "individuals holding nominal share capital up to ₹2 lakh". This counts by **face value**, so it includes more holders than the ₹2L **market-value** test → it overstates `H_small` → treat as the pessimistic case. Also compute an optimistic case at 50% of it.
4. Entitlement: `E = R / H_small`.
5. Acceptance under participation `p` ∈ {40%, 60%, 100%}: `A = min(1, E / p)`. Blend with the company's past acceptance ratios when available.
6. Position: shares `q = floor(capital / P_buy)`, capped so `q × price on record date ≤ ₹2L` (keep a buffer for a pre-record-date rally; default use ≤ ₹1.5L). Tender **all** `q` shares.
7. Per scenario: accepted `q·A`; gain on accepted `q·A·(P_bb − P_buy)`; **breakeven exit price for unaccepted shares**
   `P_exit* = P_buy − A·(P_bb − P_buy) / (1 − A)`;
   cushion `% = (P_buy − P_exit*) / P_buy`;
   net P&L if the stock falls 5 / 10 / 15% after the record date; costs (delivery brokerage, DP charge per debit, STT, stamp) from SPEC §11 tables.
8. Tax note: since 1 Apr 2026, listed buyback proceeds are taxed as capital gains (net of the loss on unaccepted shares) — show the estimated tax using the rules table; mark `UNVERIFIED` until checked against the current Act.

**Screening flags (red = skip unless overridden)**
- Record date already passed (no eligibility) — red.
- Open-market buyback — red.
- Promoters participating — amber (lowers acceptance).
- Run-up since announcement > 8–10% — amber (reversion risk).
- Price near 52-week high, or thin liquidity — amber.
- Cushion at the pessimistic acceptance < 3% — amber/red (config).
- Pre-record-date exit rule: if price rallies so the open-market gain beats the expected tender value, alert "consider selling instead of tendering".

**Worked example (golden test, 9 Nov 2026 data — re-verify before any use)**
Medi Assist tender buyback: `P_bb` ₹425; `N_bb` 11,59,199 (~1.55% of equity → `N_tot` ≈ 7.48 crore); record date 20 Nov 2026 (last buy day 19 Nov); `P_buy` ≈ ₹289; capital ₹1,00,000 → 346 shares; `R` ≈ 1,73,880.
| Acceptance A | Accepted shares | Gain on accepted | Breakeven exit `P_exit*` | Cushion |
|---|---|---|---|---|
| 5% | ~17 | ~₹2,300 | ~₹281.8 | ~2.5% |
| 10% | ~35 | ~₹4,700 | ~₹273.9 | ~5.2% |
| 50% (a third-party guide's assumption) | 173 | ~₹23,500 | ₹153 | ~47% |
Test asserts the formula outputs; the narrative point is that a 1.55% buyback with a 15% quota rarely gives 50% acceptance, so the realistic cushion is single-digit %. Unverified at time of writing: promoter opt-out, institutional holding %, 52-week low, entitlement ratio, and whether a separately approved preferential allotment changes the share count before the record date.

## A.4 Other event models
- **Open offer**: offer price vs market; offer size vs public float; expected acceptance from public float and likely tender rate (scenarios); timeline and approvals; days to completion → annualised return; alert before the ex-offer date if not tendering.
- **Rights issue / RE**: issue price, ratio, RE trading window (dates), RE price vs fair value `share price − issue price` (flag gaps above a threshold after costs); warn when the share price is approaching the issue price; reminder that applying (incl. for additional shares) is done through the bank's ASBA facility.
- **OFS**: floor price vs last close, retail reservation and its separate day, indicative price; alert on large discounts.
- **Demerger**: record date, ratio, expected listing date; research note template only.

## A.5 Outputs and alerts
- Daily after-close scan → `reports/event_arb/YYYY-MM-DD.html` (table of live events, scenario P&L, flags, filing links).
- Telegram alerts: new qualifying event; last buy day tomorrow; tender window opens/closes; RE window opens; price-rally exit condition; unaccepted shares returned to demat.
- **Manual runbook checklist** (in `docs/RUNBOOK.md`): confirm holdings in demat before the record date; do not sell on the record date; tender all shares through the broker's corporate-actions flow (e.g., Zerodha Console → Corporate actions); CDSL TPIN ready; bank ASBA linked to the demat for rights/IPO; sell unaccepted shares when they return; record everything in the journal (SPEC §9 self-review covers these trades too).

## A.6 Layout
```
engine/sidecars/event_arb/
  sources.py        # announcements, filings, shareholding pattern, past acceptance ratios
  models.py         # dataclasses: Event, BuybackTerms, OpenOfferTerms, RightsTerms, OFSTerms
  buyback.py        # A.3
  open_offer.py rights.py ofs.py   # A.4
  screen.py         # flags and ranking
  report.py         # HTML report + Telegram messages
tests/sidecars/event_arb/   # formula tests incl. the A.3 golden example
```

## A.7 Rules for this module
- Never place orders or tenders automatically. Output is information and alerts only.
- Show assumptions next to every number (acceptance scenario, participation, data date).
- Verify reservation rules, record-date/T+1 logic and tax treatment against primary sources before coding; store `source_url` and `verified_on` in `config/rules/`.
- Not financial advice; the owner decides.

## A.8 Milestone M13-EA — acceptance criteria
1. Scanner lists all tender buybacks, open offers, rights issues and OFS announced in the last 60 days with filing links.
2. Buyback model reproduces the A.3 golden table exactly (formula level).
3. Pessimistic/optimistic acceptance from shareholding-pattern data computed for at least three live buybacks.
4. Daily report and Telegram alerts run from the scheduler without manual steps.
5. Runbook checklist added to `docs/RUNBOOK.md`.
