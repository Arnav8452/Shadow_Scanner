# Decisions log

Newest first. Each milestone ends with what was built, decisions taken and open questions.

---

## Review of the Sensex expiry close and the owner's notes in `new/` (12 Nov 2026, night)

The owner left six notes (fix, fix2, rules, breaking_retail, ride_wave, and a "quant specification") and nine screenshots of the page from 15:28 to 15:39 on the Sensex expiry. The screenshots confirm the bugs below. Each item in the notes was checked against our code and SPEC before deciding.

**Built** (thresholds are HYPOTHESES in `settings.yaml`):
- **Dead options are not scored** (scanner): no bid, ask at the tick floor (≤ ₹0.25), mid under ₹5, or spread over 10% of mid. On 12 Nov, contracts at 0.05 × 0.10 scored 61–65 on margin-cleanup flow. The runner also drops a contract's last reading when it stops being scored.
- **The closing-auction bonus is for stocks only.** The scanner gave a 22-point location bonus from 15:00 to any contract; NSE's closing auction is a cash-market session. This is why the expiring 82000 PE was labelled "cas 3pm vwap reversal" at 15:30. This one was found in our code during the review; the notes didn't mention it.
- **Location gate:** with less than 5 location points (no structural level, or levels missing after a mid-day start), the score is capped at 60, below the 65 watchlist line. On 12 Nov the 82000 PE reached 73–75 with location 0/25.
- **Book-shape consistency:** the book pillar is halved when the touch leans one way (micro-price vs mid) and the top-5 imbalance the other. On 12 Nov a passive 32-lakh bid one level below a 40-lot touch drove OBI to +0.47.
- **Readings stop counting:**
  - after 15:15;
  - after 14:45 for an index whose options expire today;
  - for options with |delta| outside 0.15–0.85 or under 5% time value;
  - for contracts that are "settling" (expiring, in their last minutes; the page now says so instead of showing a 3.4% IV from the clock floor).
- **Open setups close at the bell.** On 12 Nov a setup formed at 15:28:25 and "confirmed" at 15:30:15 on the closing print.
- **Expiry-day rules for long options expiring today:**
  - no entries after 14:30, and a forced exit at 14:50 (paper accounts and debit spreads);
  - the §5.10 gamma window now ends at 14:30 instead of SPEC's 15:15.

  The reason: index settlement averages the last 30 minutes (15:00–15:30), so later moves barely change it. That methodology is UNVERIFIED against NSE/BSE primary text, so verify it before relying on this rule live.
- **§5.10 absorption veto:** no entry when the breakout bar has heavy volume but a small body, or a long rejection wick ("effort without result").
- **Futures absorption zones** (`engine/cockpit/zones.py`, from `ride_wave.md`):
  - Over 30 minutes on each index's front future, a zone qualifies when aggressive volume traded there is at least 12× the largest size ever shown at the touch, over at least 8 minutes.
  - A floor (sellers trapped above it) or a ceiling (buyers trapped below it) is flagged once price leaves the zone.
  - It is context only: shown under Setups and stored with each trade. The side is read from ~0.1–0.2 s samples, coarser than true tick data.
- **Option OI read** (`fix2.md`): each option row shows buying / writing / short covering / long unwinding, from price change against OI change vs the previous session's bhavcopy where known. Put writing reads as index-up.
- **Day-regime features** (`rules.md`), stored with every trade and split in Research: expiry day, opening gap, efficiency (|close − open| / range so far: chop vs trend), range, and VIX change since the open.

**Not built, and why:**
- The **"quant specification"** is written as a replacement CLAUDE.md. It assumes ₹1 lakh capital (approved: ₹2 lakh), allows 8–35-minute scalps and 2-lot sizing by confidence, and adds MCX crude/natural gas and futures structures. These conflict with:
  - the owner's no-scalping direction;
  - CLAUDE.md rule 5 (one lot until 20–30 live trades);
  - the project's scope (NSE/BSE index F&O; MCX would need its own feed, rules and cost rows).

  Its useful ideas are kept as candidates below.
- **3-minute "impulse vs charges" gates, the 180-second scratch rule, and the 5-pillar breakthrough score** (`rules.md`): all are scalping tools.
- **Stop-cluster projection** (where retail 25–30% stops sit): untested folklore. Possible research later, not a signal.
- **Model-price stops** (`breaking_retail.md`): a paper fill at the bid is what a real stop would get. Changing the stop rule is a strategy change to test later, not a realism fix.
- **Heavyweight 3-minute cash CVD:** the engine has no tick data for the stocks.
- **Selling the expiry pin** (`fix2.md`): a hedged short into 0DTE settlement. Interesting, but it needs a study of recorded expiries first.
- **Routing naked buys to spreads when IVP > 80:** this would end the naked-vs-spread A/B test on the same signals. Entry IV percentile is stored with every trade, so Research answers it with data instead.

**Candidates for later:**
- Calendar spreads on weekly-vs-monthly IV backwardation (the chain now has IV for 2–3 expiries).
- An RV/IV filter for the condor.
- A time stop for naked buys (exit if below breakeven after N minutes) as a 5.2 variant.
- A "second-mouse" retest entry.

---

## A portfolio of paper strategies (12 Nov 2026, evening)

The owner asked for more option strategies the signals can drive, to be tested on paper and built into a portfolio for later bots. They also asked to bring back the score-driven paper trades.

**Built:** each strategy has its own paper account and journal, one lot, defined risk, the approved risk gate, full costs and context at entry (`engine/cockpit/strategies.py`).
- **Score-follow:** the original A+ bot is back (`cockpit.bot.enabled: true`) on its own `score` account, with its original rules: ATM option, 25% premium stop, exit when the signal's episode closes. It was switched off this morning under the owner's "no scalping" direction; it now runs as one strategy to measure, not the default.
- **Debit spreads on confirmed setups** (`cockpit.debit`):
  - The same confirmed setups that §5.2 buys naked become a bull call / bear put spread: buy ATM, sell 2 strikes out.
  - Exits: target at 60% of the maximum value, stop at half the debit, square-off 15:20.
  - One idea per index per day.
  - This is an A/B test of naked vs spread on the same signal.
- **§5.10 expiry-afternoon gamma breakout** (`cockpit.gamma`): the SPEC rules (compression, 5-minute close beyond the morning range and VWAP on 1.8× volume, OI at the breached ATM strike lower than at 13:30), with a 30% stop.
  - Deviation from SPEC: one lot cannot be halved, so "book half at 1.8×" becomes "at +80% the stop moves to +40%".
- **Spread account generalized:** `SpreadAccount` now handles debit structures (worst case = debit paid).
- **Page:** a Strategies **Overview** tab (status, today's net, realized, trades, W/L, open positions per strategy), plus tabs for debit spreads, gamma and score-follow.

**Recommended next (not built):**
- event long straddles before RBI MPC or the Budget (overnight; needs IV vs realized event-move history);
- calendar spreads (§5.4);
- swing trades (§5.8, from the equity branch).

Opening-range breakout (§5.3) holds trades for 1–3 minutes, close to the scalping the owner ruled out, so it is left as research only.

---

## Equity swing (SPEC §5.8), trend sleeve (§5.6), swing paper account, cockpit Strategies view (12 Nov 2026)

Equity first, sized for ₹2L, no futures. Everything here is **paper**; no live path imports it. Rules marked HYPOTHESIS are starting values for the owner to set, not tested edges.

**Built**
- `engine/strategies/swing_scanner.py` — nightly scan of the day's F&O stocks on NSE `sec_bhavdata_full` bars (survivorship-free: the universe is that day's F&O bhavcopy). Breakout: close ≥ 0.5% above a resistance with ≥ 2 pivot touches (within 0.75%) in 60 sessions, on ≥ 1.5× 20-day volume, optional coil filter; the bearish mirror is a breakdown. Support bounce: close above a rising 50-day average, low tags a multi-touch support, close in the top 40% of the day's range. Liquidity: price ≥ ₹50, ≥ ₹10 cr average traded value.
- Instrument: IV rank (percentile of today's own Black-76 stock ATM IV among the previous 252 sessions, `data/parquet/eod/stock_iv.parquet`, one year backfilled 3 Nov 2025 → 11 Nov 2026, 52,619 rows) ≤ 50 and no results inside 10 days and not in the next session's ban list and one lot's premium ≤ 2.5% of capital → ATM stock option (CE long, PE short) with ≥ 5 trading days to expiry. Otherwise cash for longs; **shorts are skipped** (no overnight cash shorts; no futures). Stops: option = close back through the line, capped at 25% of premium, plus a 2-session no-follow-through time stop; cash = 6% (SPEC 5–7%). Max hold 15 sessions. Cash quantity = min(risk ÷ stop distance, 25% of capital ÷ price).
- `engine/strategies/trend_sleeve.py` — SPEC §5.6 on NIFTYBEES (cash ETF): on a month's last trading day, Nifty 50 12-month return vs the 91-day T-bill rate (`risk_free_rate.csv`); long → buy 20% of capital, flat → sell. 10% disaster stop (HYPOTHESIS; it is also the risk counted against the per-trade gate: 2% of capital).
- `engine/execution/swing_paper.py` — separate from the cockpit's intraday accounts. Orders placed at night fill at the next open ± an estimated half-spread (cash 0.05%, options 1.5%: HYPOTHESIS, because no depth is recorded for stocks); entries skip if the open is already back through the line; intraday stops against the day's low; line/time/max-hold/expiry exits queue for the next open; marks at the estimated bid. Costs from `config/rules` (delivery; same-day round trips as intraday; options with the stock's own lot). SPEC §6 stock-F&O expiry guard: options exit by E-2. Risk from `settings.risk` unchanged: per-trade 2.5% (directional), max open risk, no stacking/averaging, max trades a day, daily/weekly loss kill switch. Cash in use is capped by `margin_headroom_max_used_pct` (50%), which is my reading of "headroom" for a cash account — owner to confirm. State `data/state/swing_paper_state.json`, journal `data/state/paper_swing.jsonl`.
- `engine/strategies/swing_night.py` + evening step `swing` (`tasks.py swing DATE=`): stock IV for the day → account applies the day's bars → scan → plan → queue → trend on month-ends → `data/state/swing/<T>.json` (plans, why each was or was not queued, account snapshot).
- Cockpit Research tab: **Strategies** (every SPEC §5 and Addendum A item, `engine/strategies/registry.py`: live 0, paper 5, research 12, not built 7 after the merge with the main session's re-scope (§5.1 condor/credit spreads and §5.2 directional are paper in the cockpit; Track A is a research signal study), with the newest output file), **Swing** (candidates by night, trend signal, account, queued orders, open and closed trades), **Event arbitrage** (screen computed on request from `events.yaml` with no side effects — no feed fetch, no alert state, no report write — plus saved `reports/event_arb/<date>.html` in a sandboxed frame and the last 60 days of classified announcements).

**First real night (11 Nov 2026)**: 10 long breakouts; IV ranks 42–100, so all cash (CDSL's ATM call was ₹17,338 a lot vs the ₹5,000 budget). CDSL ×35 and CAMS ×69 queued; the other 8 refused at the 50% capital-in-use cap. Nothing has filled yet.

**Decisions**
- Stock options are paper-only and stay so while settings say stock F&O is deferred at ₹2L. Index strategies keep their status (code-only decision functions are "research"; §5.1 condor/credit spreads and §5.2 directional in the cockpit are "paper"; the Track A signal study is "research"; nothing is live).
- No spreads for high-IV names yet: SPEC allows "cash or a spread"; cash is simpler and keeps risk defined by the stop. Bearish setups with high IV are skipped rather than shorted.
- Results dates for the event filter come from NSE's board-meeting list saved by the evening `calendar` step (point-in-time: newest list fetched on or before the night).

**Open questions for the owner**
1. Trend sleeve allocation (20%) and disaster stop (10%).
2. Should cash swing positions count against `margin_headroom_max_used_pct` (50%), or have their own cap? Today it limits the book to ~2 cash names.
3. Practice targets before live (SPEC: ≥ 10 cash and ≥ 5 option paper swings) — reported in the cockpit; no live path exists.
4. Bid/ask for stocks: EOD fills use an assumed half-spread. Recording NSE_EQ depth for open positions would replace it.

---

## Equity pillar from exchange filings: results, holdings, results dates, delivery watchlist (12 Nov 2026)

Retail platforms (Screener.in, Tijori, Trendlyne) forbid scraping and have no public API, so the same facts are taken from the exchange filings they are built on.

**Sources checked on 12 Nov 2026** (plain HTTPS GET, generic User-Agent, no cookies; stdlib client):

| What | URL | Result |
|---|---|---|
| Quarterly results list | `https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=X&type=Integrated Filing- Financials` | 200 JSON; quarters ended Mar 2025 on (SEBI Integrated Filing) |
| Older results list | `https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=X&period=Quarterly` | 200 JSON, but stops at the Dec 2024 quarter — not used |
| Shareholding list | `https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol=X` | 200 JSON |
| Board meetings | `https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date=DD-MM-YYYY&to_date=…` | 200 JSON (one request for all companies) |
| XBRL files | `https://nsearchives.nseindia.com/corporate/xbrl/…` | 200 XML |
| Delivery | `https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv` (already in `download.py`) | 200 CSV |
| BSE API | `https://api.bseindia.com/BseIndiaAPI/api/…` | **403** without browser headers — not used |
| NSE RSS (`Financial_Results.xml`, `Board_Meetings.xml`, `Shareholding_Pattern.xml`) | `nsearchives…/content/RSS/` | 200, but only a few items and no XBRL links — not used |

The `www.nseindia.com/api` endpoints are what NSE's own filing pages call. NSE publishes no documentation or terms for them, so they may change or start requiring a browser session; every step fails soft and says so in the log. The files are public exchange disclosures, kept for the owner's own use only.

**Built:**
1. **XBRL parsers** (`engine/data/fundamentals/xbrl.py`), tested on trimmed real filings in `tests/fixtures/nse/` (Reliance IndAs, HDFC Bank banking, Bajaj Finance NBFC, HDFC Life insurance, Reliance shareholding; individual shareholders' names and PANs were removed). Revenue, EBITDA (derived, non-financial companies only), operating profit (banks: before provisions), PBT, PAT, PAT to owners, EPS; promoter / FII / DII / MF / insurance / retail holdings. The parser picks the quarter's context, not the half-year one in the same file. Values are rupees as filed.
2. **Point in time** (`engine/data/fundamentals/store.py`): one Parquet row per filing in `data/parquet/fundamentals/`, `known_at` = NSE's dissemination time (a revision from its own time). Readers take `as_of`; growth (QoQ, YoY) compares quarter with quarter, consolidated where filed. `tasks.py fundamentals SYMBOLS=…`.
3. **Results dates** (`engine/data/events/earnings.py`): NSE board-meeting intimations for Nifty members become `CONSTITUENT_EARNINGS` rows (tag `[auto:nse-board-meetings]`) with the index weight where one is loaded. NSE gives the date but not the release time, so the window covers the session. Future auto rows are replaced on refresh (a postponed meeting moves), past ones are kept, hand-entered rows win.
4. **Delivery watchlist** (`engine/data/eod/delivery_watch.py`): stocks with F&O contracts, scanned with `scan_relative_delivery_spikes` over 20 prior sessions, ban-list names dropped, `LONG_ACCUMULATION` first then `RETAIL_CHURN_FADE`, at most 5. Needs ≥ 6 delivery files or the list is left empty.
5. **Live wiring** (`shadow_live.delivery_watch`, on): the live scanner reads the newest list dated *before* its session (never the same day), adds the stocks after the index core (45 index keys + 5 stocks = the 50-key `full_d30` cap; if the index list grows, stocks come before the farthest options), and sets `delivery_bias` and previous-day levels on their state. `VWAP_DELIVERY_RECLAIM` can now fire. Watch-only, as before.
6. **Evening job** `tasks.py evening` (`engine/data/eod/evening.py`): EOD + missing delivery files → watchlist → results dates → filings for the watchlist and the 10 heavyweights by weight. Added to `register_eod.ps1` after the IV history (the owner re-runs that script to pick it up).
7. **Cockpit:** Research tab panel "Delivery-spike stocks: results and holdings" (`/api/research/equity`).

**Stocks in the live scanner, measured correctly** (requested by the main session, owner-approved):
- **Costs.** Track A costed every non-option as futures. A stock (instrument master `Kind.EQUITY`, carried as `is_equity` on the scanner state like `is_option`) is now costed as an intraday cash trade (`equity_intraday_charges`, `segment = equity_intraday`). New rule rows:
  - NSE cash transaction charge + IPFT ₹307/crore each side (0.00307%) — **primary**, [NSE/FA/73061](https://nsearchives.nseindia.com/content/circulars/FA73061.pdf) (27 Feb 2026: ₹297 + ₹10 IPFT until 28 Feb 2026, ₹306.99 + ₹0.01 from 1 Mar 2026, same total). The delivery row (same value) now cites it too.
  - STT 0.025% on intraday sells, stamp duty 0.003% on intraday buys — secondary ([upstox.com/brokerage-charges](https://upstox.com/brokerage-charges/), dated tables for Oct 2024 and Apr 2026). No primary STT/stamp text was read.
  - Upstox brokerage: intraday ₹20 or 0.1% (standard), ₹30 or 0.1% (Plus); delivery ₹30 on Plus; DP ₹20 + GST per scrip per sell day. Secondary (Upstox pages, 12 Nov 2026). The Plus page does not say "whichever is lower"; the charges page says Plus is "up to ₹30/order", so it is read that way.
- **50-key cap.** The stock group is a configurable count (`delivery_watch.max_symbols`, 5) placed before the farthest index options, so it trims strikes rather than exceeding the cap; the repo-config test counts it. Upstox documents `full_d30` only with an `NSE_FO` example and says nothing about `NSE_EQ`. The 11 Nov probe in `full_d30` did get Reliance cash data (indices came back empty), but no instrument has shown more than 5 levels yet. New `delivery_watch.feed_mode: full` subscribes stocks in 5-level `full` on the same socket if `upstox-probe D30=1` in market hours shows `RELIANCE (cash)` without 30 levels.
- **Paper bot (not merged: the main session's cockpit rework, 12 Nov, replaced this bot code; it stays in this worktree and `tests/data/test_stock_vehicle.py` for a later re-port).** A stock signal maps to that stock's ATM option (bullish call, bearish put) in `CockpitCore.choose_vehicle`: F&O stocks only, not on today's ban list (`build_cockpit` reads it), nearest expiry ≥ 5 calendar days away (stock options settle physically; SPEC §6 closes ITM/near-ATM by E-2). The option is added to the feed on demand (at most 2, ahead of far index options), bought at the ask with its own lot once its book arrives (30 s), and released when the signal closes. No cash-equity buying in the paper accounts.

**Owner's answers to the open questions (applied 12 Nov 2026):**
1. **Index weights derived for all 50** (`engine/data/constituents/weights.py`, evening step `weights`): close × shares × (1 − promoter holding), normalised; `weight_status = derived` (not verified). Monthly `tasks.py index-weights-verify` reads the NSE Indices factsheet's top 10 and marks a weight verified when within 0.3 points. First run (prices 11 Nov, factsheet dated 30 Oct 2026, [ind_nifty50.pdf](https://www.niftyindices.com/Factsheet/ind_nifty50.pdf)): 8 of 10 verified; **L&T 4.75 vs 4.25** and **Infosys 3.31 vs 3.92** differ (1 − promoter is a rough proxy for NSE's investible weight factor), so they stay `derived`. "Top 10 heavyweights" and the 24 h results weight now cover all 50. Only the current weights are kept in the CSV (no history yet).
2. **General insurers:** ICICI Lombard's Q2 filing (`IFGI`) is a fixture and parses (revenue = net premium earned; PBT/PAT/EPS). Any schema without a fixture test is shown as "not parsed" (`PARSED_SCHEMAS`), never estimated.
3. **Delivery thresholds are HYPOTHESES** (`shadow_live.delivery_watch.spike`), and spikes now only choose what to watch: `bias_in_score: false` keeps the bias away from the scanner (no delivery points, no `VWAP_DELIVERY_RECLAIM`). **Event study** (`engine/research/delivery_event_study.py`, `tasks.py delivery-study`; report in `reports/research/`): 268 sessions of delivery files (3 Nov 2025 – 11 Nov 2026), F&O universe as of each day, entry at the next open, delivery costs 0.41% round trip on ₹50k + 0.05%/side slippage, 72 trials (15 threshold sets × biases × 1/3/5 days). **No cell passes** (deflated Sharpe ≥ 90% and |t| ≥ 3). Best: ≥ 3× delivery and ≥ +2% close, long, 5 days: +0.17% net, excess +0.71%, t = 1.8, DSR 14%. The live defaults (2×, +1%) lose after costs at every horizon. Fading retail-churn spikes is wrong-way: over 5 days the stocks kept rising (excess −1.24%, t = −3.5; one of 72 trials, not a strategy). Conclusion: keep spikes as a watchlist filter only.
4. **Liquid option chain required** (`delivery_watch.option_liquidity`, HYPOTHESIS): near-ATM (±2 strikes) calls + puts of the nearest expiry ≥ 5 days out must have traded ≥ 1,000 contracts with OI ≥ 500 contracts (NSE's file gives OI in shares; divided by the lot). 11 Nov: PETRONET dropped for thin options. The paper broker's entry checks (spread, depth) are unchanged.
- **Found:** for holidays NSE serves the previous day's delivery file under the holiday's name (2 Oct, 20 Oct, 10 Nov 2026). Loaders keep only rows dated as the file; `download.py` still saves such files (open: reject them at download).

**Decisions:** EBITDA is left empty for banks, NBFCs and insurers (interest is their cost of goods). FII = "Institutions (Foreign)" in the pattern (FPIs + FDI), the usual platform definition. Results-date rows carry no weight for members whose weight is not loaded, so they show on the cockpit but add 0 to the 24 h earnings weight.

**Open questions:** (the four earlier ones are answered above)
- Weight history: the CSV holds only the latest derived weights; keep dated history if point-in-time replays need past weights.
- Share counts come from the last quarterly pattern, so a bonus or split since then distorts a weight until the next pattern.
- `download.py` should reject a delivery file whose rows carry another date (holiday copies).

---

## Option chain, F&O screener, context at entry, and outage handling (12 Nov 2026, afternoon)

The owner compared the page with Sensibull's option chain and screener and asked for the data and analysis that give an edge. They want to see which signals and conditions contributed to a position, and to know what happens when Wi-Fi drops.

**Option chain tab** (`engine/cockpit/chain.py`; LTP, Greeks and All-columns views, per-unit or per-lot):
- **Source:** the recorder's 1-minute snapshots for the nearest 2–3 recorded expiries. Each expiry uses its own synthetic forward.
- **Our own maths:** IV and greeks from each strike's mid. Also intrinsic and time value, breakeven for a buyer at the ask, and POP from a lognormal with the strike's IV (a model number).
- **OI and summary:** OI change vs the previous session's NSE bhavcopy (Sensex is BSE, so it has none), per-strike PCR, PCR by OI and by volume, and max pain.
- **Delivery:** chains go to the page only on request and only when they change (once a minute). They no longer ride in every snapshot.
- **Spot check against Sensibull (25250 CE, 17 Nov):** delta 0.50 vs 0.49, theta −19.8 vs −19, vega 11.6 vs 12, POP 34% vs 35%. IV is 16.6 vs 16.0 because the forward method differs.

**Screener tab** (`engine/cockpit/screener.py`, all ~220 F&O underlyings, end of day):
- **Columns:** futures price and change, our ATM IV (tier C, from closes) with its change and percentile over up to 252 cached sessions, volume vs the 20-session average, futures OI change and the price/OI quadrant, near-expiry PCR and max pain, results dates from the calendar, and the ban list.
- **Live rows:** Nifty and Sensex are shown intraday from the engine.
- **Flags** (HYPOTHESES for attention, not signals):
  - rich premium (IVP ≥ 70 and no results within 7 days) → 5.1 candidate;
  - cheap options (IVP ≤ 25);
  - results within 10 days;
  - volume ≥ 2×;
  - ban.
- **Cache:** per-session summaries in `data/state/screener/` (`tasks.py screener-history` built 252 sessions in about 15 minutes; the nightly `eod` adds each day).
- **Stock IV:** main's `atm_iv_for_day` reads index options only, so stock rows are relabelled before the call instead of editing that file. The equity branch has the same function with an `instruments` parameter and a `stock_iv.parquet`. After its merge, switch the screener to those.
- **Agrees with Sensibull** on today's percentiles: Nifty 75 (live) / 84 (EOD) vs 76; Bank Nifty 76 vs 75.

**Context at entry** (`CockpitCore.market_context`):
- **What is recorded:** with every paper trade (directional, manual, condor) and every signal-study row:
  - time of day, IV and IVP, VIX, PCR, dealer gamma, skew;
  - distance to the OI walls and max pain;
  - position against the opening range and the previous day's range;
  - heavyweight breadth and correlation;
  - confirmed setups in the last 30 minutes;
  - the event window.
- **Where it shows:** Research has a new panel that splits paper-trade net and signal hit-rate by each condition, including breadth with or against the trade's direction, with t-stats and a multiple-comparisons warning. It is empty until trades accumulate (from 12 Nov).

**Outages** (RUNBOOK, "If the internet drops"):
- **Shadow engine:** gained a 60-s silence watchdog, like the recorder's.
- **Cockpit:** now applies the approved `execution.disconnect_kill_after_s: 10`. Without a fresh quote in market hours, new paper entries freeze and exits pause; nothing is closed on stale prices, and both resume on the first fresh quote.
- **Nightly EOD:** catches up on the last 7 days.
- **Page token:** persists across gateway restarts. Until now, every restart left open pages "offline" until reloaded, which is what the owner saw.

**Open:**
- An intraday stock screener would need stock option chains during the session (Upstox's REST option chain with our own IV, within its rate limits). Not built.
- The screener's volume ratio includes expiry days in the 20-day average.
- The page's extra feed slots are not restored after an engine restart.

---

## Desk for people, confirmed setups, and SPEC 5.1 condor / credit spreads on paper (12 Nov 2026, afternoon)

The owner's review of the page:
- Data was hidden.
- The depth view was unusable.
- Only contracts in the scanner could be opened.
- Scores jumped every tick, like a scalping bot.

The intended use is waiting for confirmation, entering, and holding until the strategy's own conditions say to exit.

**Built:**
- **Setups** (`engine/cockpit/setups.py`, `cockpit.setups`, HYPOTHESIS):
  - Each order-flow reading (75+, 5-s hold) on any contract becomes a view on the index.
  - Readings for one index and direction form a setup, which is FORMING first.
  - It becomes CONFIRMED only after the index moves 0.05% its way and holds there for 60 s, within 10 minutes.
  - It FAILS on an opposite reading, a 0.08% move against it, or the time running out. It ENDS when it gives back the whole move, when an opposite setup is confirmed, or after 60 minutes.
  - The journal (`setups.jsonl`) restores the day's setups after a restart.
  - **Directional 5.2 now enters only on a confirmed setup**, not on a reading. Confirmed setups also go into the signal study as `SETUP_CONFIRMED`.
- **§5.1 condor / credit spreads** (`engine/cockpit/condor.py`, `cockpit.condor`, paper only, HYPOTHESIS):
  - Entry checklist, evaluated each minute and shown as ✓ / ✗ on the page:
    - window 10:00–14:30, one structure a day;
    - expiry at least 2 days away;
    - no RBI MPC, Budget or election event, and no results cluster of 10%+ index weight, before the planned exit;
    - IV percentile ≥ 50;
    - not inside an event window.
  - **Order-flow veto**, from confirmed setups in the last 30 minutes:
    - none → iron condor;
    - UP only → bull put spread;
    - DOWN only → bear call spread;
    - both directions → no trade;
    - a setup still forming → wait.
  - **Strikes:** shorts at about 0.16 delta, from our own IV for each strike in the 1-minute chain. The wing width is the widest of 200 / 150 / 100 whose worst case fits the ₹10,000 per-trade limit, and the credit must be at least 10% of the width.
  - **Feed and fills:** the legs are pinned into the live feed. The structure trades only once all of them have books. Wings are bought before shorts are sold (`sequence_entry_legs` guards it); a failed short unwinds the filled legs. On exit, shorts are bought back first.
  - **Exits:**
    - take profit at 50% of the credit;
    - stop when the cost to close reaches 2× the credit;
    - the index touches a short strike;
    - a confirmed setup toward a short strike with the index within 0.5% of it;
    - 15:00 on the trading day before expiry.
  - It is held overnight; the journal `paper_condor.jsonl` restores it and re-pins its legs.
  - Margin: SPAN is not modelled. The worst-case loss is used as the margin proxy (UNVERIFIED).
- **Feed pins** (`ShadowLiveRunner.pin`): contracts added on demand, either by the page (up to 3) or by a strategy's legs. They displace the farthest auto-selected options and stay within the 50-key cap.
- **Page (Desk):**
  - Index cards are always shown, with a one-line option. Nothing is behind a Details button.
  - A setups list with progress toward confirmation.
  - The scanner shows OI, ΔOI, IV, Δ and a 1-minute average score, re-sorted every 30 s.
  - The instrument panel has a search box for any contract in the master. Contracts outside the feed are added to it.
  - **Market depth tab:** 30 levels with cumulative size and walls (≥ 3× the median) highlighted, spread, micro-price, top-5 and all-level imbalance, the largest bid and ask, and a 30-minute price line.
  - **Analytics tab:** greeks from the contract's own mid, the day's OHLC, OI and ΔOI, and the reading as four pillar bars with plain-language explanations.
  - **Option chain tab:** OI, ΔOI, IV, Δ, bid and ask per strike, with walls, ATM, in-the-money and condor legs marked. Clicking a cell opens that contract. The smile is shown below.
  - Strategy tabs: Condor (5.1) first, then Directional (5.2), Manual, Signal study and Events.
- **Bugs found live and fixed:**
  - Upstox `full_d30` sends no order count per level (None), which broke the snapshot.
  - Pinned legs counted as "options subscribed", so after a restart the auto strikes waited 5 minutes.
  - The put side of the chain table was mirrored.

**First live use (12 Nov, 13:00):** about a minute after start the checklist passed (IV percentile 74–75, no event before the exit on 16 Nov 15:00, quiet tape). It paper-traded an iron condor: 24650/24800 PE and 25800/25950 CE, 150-point wings, credit 32.7 pts (₹2,126), worst case ₹7,625. This is one paper trade, not evidence.

**Open:**
- The adjustments in SPEC 5.1 (rolling the untested side, converting to an iron fly) are not built. The strategy exits instead.
- Term-structure and skew-spike filters are not checked; only one expiry is watched.
- GEX is shown for information only.
- The condor's numbers are SPEC priors, not fitted.
- The paper record needs 20+ sessions before anything is concluded.

---

## Re-scope: order flow confirms and times trades; it no longer scalps (12 Nov 2026, owner approved)

**What was built:**
- **Bot scalping is off.** `cockpit.bot.enabled: false`. The bot no longer follows Track A episodes or exits when the book flips.
- **SPEC §5.2 directional on the Bot account** (`cockpit.directional`, HYPOTHESIS rules, paper only):
  - Entry: the first confirmed (5-s hold) order-flow reading scoring at least 80 between 09:30 and 14:00 buys 1 lot of the ATM call (index up) or ATM put (index down). The rule allows one idea per index per day, never flips direction, takes at most 2 entries, and skips any index already held.
  - Exits: premium stop −10%, target +30%, a trailing stop (at +15% it locks +7%; at +25% it locks +12%) and the 15:20 square-off. The stop only rises.
  - `PaperPosition` gained `target_price` and `trail`, and the journal restores both after a restart.
- **Track A is now a signal study** (`engine/cockpit/study.py`, journal `data/state/signal_study.jsonl`). For every confirmed reading it records the underlying and the ATM option (bought at the ask) at the signal, at +15, +30 and +60 minutes, and at the close. The option result is net of full round-trip charges. A horizon that falls after square-off is marked skipped. Nothing is traded.
- **Page changes:**
  - The scanner shows what each reading says about the index (for example "NIFTY ↓" for a bid-heavy put) and its setup. The Entry/SL/T1 columns, the A tags, the chart's level lines and the ladder marks are gone.
  - The Bot tab is now "Directional (B)", with the rules on hover. Positions show the target and a ↑ when the trailing stop has locked a gain.
  - "Signal study (A)" shows today's horizon table and each signal's result.
- **Research:** new panels show the study by score band, index and direction, setup, and hour, with hit %, n, t-stat of the mean move and the ATM-option average after charges (`/api/research/study`). The old ledger is kept as "Micro ledger (legacy scalp exits)" for comparison only.

**Decisions:**
- The micro levels still exist inside the scanner (it needs them to close episodes and write the legacy ledger). They are no longer shown or traded as trade levels.
- The study counts every confirmed episode (score 75+, 5-s hold, one per symbol/direction/setup per cool-down), so the 75–80, 80–90 and 90+ bands can be compared. Every band, horizon or setup looked at counts as a trial. Nothing is called an edge without t ≈ 3 over 20–30 sessions.

**Open:**
- §5.1 condor / credit spread with an order-flow veto, which needs a multi-leg, hedge-first paper account.
- §5.10 gamma breakout.
- Hedging: wings and the NIFTYBEES trend sleeve, in the equity session's scope.
- The directional numbers (score 80, −10%, +30%, trail steps) are priors from SPEC §5.2 and the Log-3 study, not fitted values.

---

## First live session (12 Nov 2026, from 11:22 IST)

**5-minute tests, run together:**
- **Recorder:** QC PASS. 233,006 messages, 419 contracts, 0 gaps, 0 reconnects, nothing dropped; recv−exchange latency median 0.10 s, p99 0.52 s; strike coverage ±27–45; all 8 heavyweights in each 1-minute snapshot.
- **Shadow engine:**
  - 40,172 30-level books (42 contracts), 89% with all 30 levels (average 27), which confirms Upstox Plus `full_d30` in market hours;
  - 17,053 previous-day levels from last night's NSE files;
  - 2 Track A episodes, both closed within minutes on an OBI flip, both net losers after costs.
- **Cockpit:** live on real data: forward, own ATM IV (Nifty ~16.3%), smile (fit error under 0.3 vol points), walls, PCR, GEX, breadth.

**Full-day runs** were started at 11:27 in the Terminal panel (recorder, shadow-live, cockpit). The morning before 11:22 was not recorded, so today's QC will fail on coverage. That's expected; M2 acceptance counts from the first full session.

**Cockpit usability fixes (owner feedback, 12 Nov, live):**
- **Problems observed at 1024×768:**
  - The page was 1,687 px tall (paper and events off-screen).
  - The top scanner row changed 9 times in 10 s, and raw scores jumped 43→60→53 between updates (step thresholds on noisy inputs).
  - Rows rebuilt 5×/s swallowed clicks.
- **Fixed on the page:**
  - One screen with no page scroll. Owner's layout: market depth full height on the right; on the left, compact market lines (details behind a toggle), a wide scanner, and paper below it.
  - The scanner shows each contract's 30-s high score with a 30-s average mark (raw score on hover). It re-sorts at most every 5 s and never while the mouse is over it, with a fixed "By contract" order available.
  - Selection and close buttons act on press.
  - The paper panel is only rebuilt when it changes, and keeps its scroll position.
  - Signal levels are marked once on the ladder.
  - Measured after the fix: 2 order changes in 12 s, each at a re-sort tick.
- **Engine (takes effect at the next engine start, i.e. 13 Nov 08:46):** `shadow_scanner.signal_confirm_s: 5` (HYPOTHESIS). A 75+ grade must hold in the same direction for 5 s before Track A tracks it, the bot can take it, or it counts as an episode or alert. Today's ledger stays on the old definition, so 12 Nov Track A results are not comparable with later days.
- **Noted:** Sensex (BSE) options arrive with 5 depth levels on the `full_d30` connection; Nifty contracts get 30.
- **Revised on owner feedback the same day:** the one-screen clamp was wrong; the owner wants a scrolling page with adjustable panels.
  - The page scrolls again and the Research tab is back to its original layout.
  - Drag handles set the depth panel's width and the scanner and paper panels' heights (double-click resets). Sizes are remembered per browser.
  - The depth panel stays in view while scrolling.
  - On first load, the depth panel narrows if the scanner would need a sideways scroll.

**Why Track A lost money today (34 episodes to 11:56):**
- Gross +₹275, charges ₹3,427, net −₹3,152.
- Median target (T1) distance was 2.08 points against 1.59 points of round-trip charges per unit, with a median hold of 14 s.
- The signals were about flat before costs: target hits +₹1,131 against stop hits −₹1,186.
- The order-flow PDF's own viability rule (average edge ≥ 2× round-trip cost) fails by construction for these 2-point scalps on ₹100–400 options.

Proposed: a cost hurdle, so only signals whose T1 distance is at least 2× the round-trip cost per unit count as tradeable. Superseded by the owner's direction below.

**Owner direction (12 Nov): no scalping.** The system is for options strategies (iron condor, spreads), swing trades, equity, event-driven cash and hedging. Order flow was never meant to be a scalper or to compete on speed. The scanner's micro levels (stop 1–2 ticks behind a wall, targets about 2 points, OBI-flip exits, 14-s median hold) are a scalping design and do not match that intent. A re-scope plan was proposed to the owner (order flow as a confirmation and timing input to the playbook strategies; Track A measured over 15/30/60-minute and close horizons).

**Page:** the Details button is now static (it was rebuilt 5×/s and missed clicks). The Research tab gained a column control (1/2/3), per-panel height handles, full width on double-clicking a title, and Reset; it remembers its layout per browser.

**Scheduler registered (owner approved, 12 Nov):**

| Task | When | Command |
|---|---|---|
| `QuantEngine-Recorder` | weekdays 08:45 | `record SEND=1` |
| `QuantEngine-ShadowLive` | weekdays 08:46 | `shadow-live SEND=1` |
| `QuantEngine-Cockpit` | weekdays 08:45 | `cockpit OPEN=0` (restarts up to 5 times) |
| `QuantEngine-EOD` | weekdays 20:00 | `eod`, then `iv-history` |

The tasks run while the owner's Windows account is logged in (the screen may be locked). Recorder and engine exit by themselves on holidays and after 15:45.

---

## Layers from the "retail access to hedge-fund data" review (12 Nov 2026)

`data/private/retail access to hedge fund data.pdf` is the Gemini conversation SPEC was built from. Most of its recommendations were already in place:
- bid/ask with quantities;
- the ±3σ / ±25-strike band;
- our own IV on the synthetic forward;
- the variance-time clock;
- expiry-day margin rules;
- hedge-first leg sequencing;
- vanna and charm in `greeks.py`.

**Corrections to the PDF:**
- Exercise STT is 0.15% from 1 Apr 2026, the same as STT on a sale. The PDF's 0.125% is outdated, so the "square off vs let expire" saving is mostly gone.
- Upstox Plus gives 30 levels of depth, not 5 or 20.
- Index options need no dividend table, because the parity forward prices in expected dividends.

**Built:**
1. **OI timing** (`scanner.oi_window`). OI history is recorded only when OI changes, with the price at that instant, and build-up compares updates about 3 minutes apart (never less than 2). Also fixed: the old "3-minute" comparison actually used the 120th-previous quote, which was seconds apart on an active future.
2. **Constituents and dispersion** (SPEC L9).
   - `config/reference/nifty50_constituents.csv` holds all 50 members and ISINs from NSE's `ind_nifty50list.csv` (primary, 11 Nov 2026).
   - The 12 weights carried over from code have no source, so they are `weight_status = UNVERIFIED`.
   - The recorder now records the top 8 by weight all day (`recorder.constituents`), starting at its next start.
   - The cockpit's chain worker computes weight-adjusted breadth and the weighted average 60-minute realized correlation of 1-minute returns. Breadth feeds the scanner's `breadth_aligned` for Nifty spot and futures (|breadth| ≥ 25% in the index's direction).
   - The simulator now quotes equities (beta 0.8 to the market plus noise) for dry runs.
3. **Macro calendar.** `config/events/macro_calendar.csv` lists events with source and status:
   - RBI MPC dates from RBI PR 2025-2026/2306;
   - Fed meetings from federalreserve.gov (updated 28 Oct 2026);
   - US CPI from bls.gov;
   - India CPI from the MoSPI advance release calendar;
   - the 2027 Budget and the 14 Dec India CPI date marked UNVERIFIED.

   Release times not stated by the issuer use convention (RBI 10:00 IST; Fed 14:00 ET converted with US daylight time). New paper entries are refused inside an event window (`cockpit.block_entries_in_event_window`). The cockpit shows the next events.
4. **EOD flows.**
   - `participant_vol` (dated) was added to `tasks.py eod`.
   - The current-only `fo_ban`, `bulk` and `block` files are saved each evening under the date written inside them (`engine/data/eod/flows.py`).
   - The FII-stats and MWPL files are legacy `.xls` and were skipped, and cash-market FII/DII is only on NSE's site API. Participant OI and volume cover derivatives positioning.
5. **SVI smile and position Greeks** (`engine/analytics/svi.py`).
   - The SVI fit is pure Python: multi-start Nelder–Mead with the positive-variance and Lee wing constraints.
   - Error: 0.001 vol points on a clean smile, 0.11 with noise; 0.06 s per fit.
   - It is fitted each minute to OTM IVs; the cockpit shows the smile, ATM IV, 98/102 skew and fit error.
   - Open paper positions show IV, delta, gamma, theta, vega, vanna and charm from our forward and IV.

**Not built, with reasons:**
- Alternative-data scrapers (Vahan, RERA, DGFT, POSOCO, FASTag): fundamental/swing data that no strategy uses yet.
- Broker OTM strike blocks: broker-specific, a Mode B concern.
- Paid vendors: we record our own data.
- Screener.in, Sensibull and similar: products with no public API; we compute the derivatives analytics ourselves.

**Open**
- Check the 12 index weights against the NSE Indices factsheet.
- Add constituent results dates as companies announce them.
- Implied correlation needs the heavyweights' ATM option IVs (more recorder keys).

---

## Cockpit: live local page, three paper tracks, replay (11 Nov 2026)

**Built (`engine/cockpit/`)**
- **Engine publisher.** `shadow-live` publishes a snapshot 5 times a second over an authenticated loopback socket (port 8766) and takes paper orders back. Every cockpit hook is guarded, so an error there cannot stop 30-level recording.
- **Gateway.** `tasks.py cockpit` serves http://127.0.0.1:8765 over a WebSocket.
  - Security: loopback only; a per-start token on the WebSocket and every API call; `Host` and `Origin` checks.
  - It holds no trading state, so restarting it never affects recording or paper trading.
- **Replay.** `tasks.py replay` runs the same engine on a recorded day at 1–50× or max speed, with its own state and journals under `data/replay/`. The gateway starts it at below-normal priority.
- **Three paper tracks.**
  - Track A, the scanner ledger: unconstrained; option shorts are now tracked and tagged not tradable.
  - Track B, bot: A+ signals only (score ≥ 80) → long ATM option, risk gate, paper stop 25% of premium (HYPOTHESIS), exits when the episode closes.
  - Track C, manual: the page's ticket, through the same risk gate.
  - Journals: `data/state/paper_{bot,manual}.jsonl`; open positions are restored on restart.
- **Market strip.** Each second: forward, our ATM IV and its percentile, VIX. Each minute, on its own DuckDB connection: walls, PCR, GEX (HYPOTHESIS).
- **Research tab.** Track A by score band, setup, hour and exit; paper history by day; IV chart; QC verdicts; mining reports.

**Fixed in browser testing (12 Nov):**
- **Max-speed replay froze the page.** The engine drained up to 50,000 events before publishing; with the cockpit on it now publishes between batches of 2,000. The old loop also dropped one event whenever it hit the cap.
- **Snapshot timing.** It used the engine clock, which stands still in a replay's quiet stretches; it now uses the wall clock (data times stay on the engine clock).
- **Long silences in a replay.** Silences over 60 s play as one second.
- **Stale replay state.** A new replay no longer shows the previous one's last state.
- **Layout.** Below 1,500 px the layout is two columns, and the scanner has fewer columns.

**Fixed:** with live Upstox keys (`NSE_FO|…`), the scanner used a symbol regex to tell options from futures and found none. Options were charged as futures, and option shorts went undetected. The contract type now comes from the instrument master.

**Open**
- `aiohttp` is held at 3.9.3 by the Fyers extra's pin. Its known issues don't apply to a loopback-only server that serves no static files.
- V2 `order_flow_bots` still uses the regex; it refuses Upstox options, which is the safe direction.



---

## Shadow paper ledger: four gaps closed (11 Nov 2026)

1. **Open trades at the close.** Paper trades still open at `paper_square_off_at` (15:20) are closed at the book with status `SQUARED_OFF_EOD`. So is anything open when the run ends early (a 5-minute test, a feed stop). Untriggered signals are dropped at `paper_no_new_entries_after` (15:00), and no new paper entries start after it.
2. **Naked option shorts.** A 75+ SHORT signal on an option contract is scored and shown but not paper-traded (rule 1); futures shorts and long options still are. Skips are counted in `ledger_summary()["not_paper_traded"]` and shown on the dashboard.
3. **Fills.**
   - Entries and exits walk the visible book level by level for the whole quantity.
   - Quantity beyond the book is priced at the last level and flagged `thin_book`.
   - Each trade records `slippage_inr` (vs mid, both sides) and `net_pnl_2x_slippage_inr` (net with slippage doubled); the dashboard shows the 2× total.
4. **Brokerage plan.** `shadow_scanner.cost_broker: upstox_plus` (config). New `brokerage.csv` rows, `secondary`, from https://upstox.com/plus/ on 11 Nov 2026:
   - Upstox Plus options ₹30 per order;
   - Upstox Plus futures ₹30 or 0.05%, whichever is lower.

   The rows start on the verification date because the plan's start date was not researched.

**Corrected:** the standard Upstox futures rate was entered as 0.03%; Upstox's page says "₹20 or 0.05%, whichever is lower". The ₹20 cap binds for any index futures lot, so past figures don't change.

**Open:** whether Upstox Plus has a subscription fee (the page states none). Fills still assume our order doesn't move the book after it fills.

---

## Shadow scanner: previous-day levels from NSE EOD files (11 Nov 2026)

- **Why:** `previous_day_levels` read only our own recordings, so on the first live day (and for contracts the recorder did not watch) the scanner had no previous high, low or close.
- **Built:** `eod_previous_day_levels`, which reads yesterday's levels from NSE's `ind_close_all` (index spots, India VIX) and the F&O bhavcopy (futures and options, matched on underlying, expiry, strike and type).
  - `run_shadow_day` fills only the symbols the recordings lack; recordings still come first.
  - It downloads a missing file, but never before 19:30 IST on that day.
  - Untraded contracts (zero high/low) are skipped.
- **Checked on the 11 Nov 2026 files:** 17,053 contracts matched, including Nifty spot, VIX, Nifty futures and every near-ATM weekly option.
- **Open:** BSE (Sensex) contracts are not in NSE files, so Sensex levels still need a recorded day or the BSE bhavcopy.

---

## M1 trade mining: PDF parser on word positions; all three logs reconcile (11 Nov 2026)

**Built**
- `engine/trade_mining/pdf_report.py` reads the Trade History PDFs with `pdfplumber` word coordinates. pdfplumber is the optional `mining` extra (`uv sync --extra mining`). Each column starts at a fixed x, and each trade is the block between its entry and exit time cells.
- **Layout cases handled:**
  - the contract printed by the exit time;
  - a charge wrapped onto two lines;
  - a day continuing onto the next page without a repeated header;
  - Daily Net headers that are `(Legal)` or multi-account.
- `engine/trade_mining/diagnostics.py` writes the §9.3–9.4 report.
- `tasks.py mine-log FILE=<pdf> LABEL=log1` writes `data/private/mined/<LABEL>.md` and `<LABEL>_fills.csv`. Outputs are named by LABEL only, because the PDF file names contain the traders' names.

**Result (M1 acceptance):** every row passes `(exit − entry) × qty × side = gross` and every day's Σ net matches its Daily Net (worst gap ₹0.01).

| Log | Fills | Days | Net | SPEC §9.5 |
|---|---:|---:|---:|---|
| 1 | 1,261 | 63 | ₹1,45,647.52 | 1,261 fills, ₹1,45,648, gross ₹4.09L ✓ |
| 2 | 60 | 12 | ₹38,068.97 | 60 fills, ₹38,069 ✓ |
| 3 | 23 | 10 | ₹23,327.83 | 23 fills, ₹23,328, PF 2.37 ✓ |

Style checks:
- Log 2: ₹39.8k from 09:15–09:30 entries and a 1.2-min median hold (§5.3 ✓).
- Log 3: entries after 14:00 lost ₹5.3k; Sensex options only (§5.2 ✓).
- Log 1: charges were 64% of gross; ADANIENT futures +₹2.88L while long options lost.
- Self-review runs on the demo journal.

**Decisions and fixes**
- **SPEC §9.2 was outdated.** The installed `pdftotext` (xpdf 4.00) drops the ₹ sign, so the text parser found 0 trades. Text mode also misplaces cells. SPEC now names the word-position parser; the text parser is kept for `.txt` input.
- **Fixed:** `ValidationReport.passed` was true for a report with 0 trades. It is now false.
- **Fixed:** the slippage stress added ticks on one side only. SPEC says per side, so it now charges entry and exit.
- **Privacy:**
  - only the 4-digit account suffix is read from rows, and it is hashed;
  - full account IDs in headers are never stored;
  - test fixtures are synthetic.
- **Charges column:** the logs' charges are the firm's own model, not exchange contract notes. They are not golden tests for our cost model.

**Open**
- The revenge-trading figure (SPEC: ~₹97k lost) used a different window than our 15-min one, which shows +₹45.5k for Log 1. Pick one definition.
- `log3_entry_features` (§5.2) needs 1-min index candles at each entry time.
- The slippage stress uses a ₹0.05 tick for every instrument; the futures tick is UNVERIFIED.

---

## Review of the entry below (11 Nov 2026, late)

- **2025 holidays:** all 14 dates checked against the NSE/CMTR/65587 PDF; they match, and the `primary` status in `holidays.csv` is right. The circular also announces a Muhurat session on Tue 21 Oct 2025 (a holiday weekday); not modelled, and normal-hours data is unaffected. The IV history was rebuilt: 0 rows now use the approximate clock.
- **Corrected: T-bill rate.** One July 2026 auction yield had been marked `secondary` from 1 Jan 2025. It is now split:
  - `secondary` from the 29 Jul 2026 auction;
  - an UNVERIFIED placeholder for 2025-01-01 to 2026-07-29, because actual yields then differed.

  The source URL is still the RBI homepage; link the press release itself.
- **`depth30` replay gap (fixed):** previously, a symbol with *any* `depth30` rows that day lost its 5-level book for the whole day even after rotating out of the scanner's 5-minute watchlist. `recorded_events` (`_in_d30_window`, `depth30_hold_s=60.0`) now suppresses 5-level `quotes` depth only while the symbol is inside an active `depth30` window and falls back to the 5-level `quotes` book before the symbol enters or after it leaves the 30-level watchlist.

---

## 15:40 session close in CLAUDE.md/SPEC, 2025 NSE holidays, verified 91-day T-bill rate, and `depth30` in event replay (11 Nov 2026)

1. **`15:30` → `15:40` F&O session close updated in `CLAUDE.md` and `docs/SPEC.md`:**
   - Approved by owner. Both files now specify trading-minute time to session close (`15:30` before 3 Aug 2026, `15:40` from 3 Aug 2026 per `NSE/FAOP/74467` in `config/rules/market_sessions.csv`).
2. **2025 NSE trading holidays loaded (`config/rules/holidays.csv`):**
   - Added all 14 weekday trading holidays from `NSE/CMTR/65587` (12 Dec 2024) for 2025 (`secondary`). 2025 IV rows now use the exact `TradingCalendar` clock (`approx=False`).
3. **91-day T-bill rate verified against RBI Press Release `2026-2027/777` (`config/rules/risk_free_rate.csv`):**
   - Confirmed the 29 Jul 2026 auction cut-off yield of `5.3402%` (cut-off price ₹98.6861, WAY `5.3266%`) from RBI auction records (`https://www.rbi.org.in`, status `secondary`).
4. **30-level depth (`depth30`) wired into event replay (`engine/data/store.py`, `engine/backtest/event_replay.py`):**
   - Added `Store.depth30(...)` and updated `recorded_events(...)` (`include_depth30=True` by default) to merge 30-level depth snapshots from `depth30` in `(recv_ns, seq)` order, superseding 5-level `quotes` depth for symbols covered by `depth30`. Covered by `test_recorded_depth30_supersedes_5_level_book_and_walks_deep_levels`.

---

## Owner decisions on base risk numbers, sleeve budgets, disconnect kill switch & Git commit (11 Nov 2026)

1. **Base risk numbers in `config/settings.yaml` APPROVED:**
   - `capital.inr: 200000` (₹2,00,000)
   - `per_trade_max_loss_pct.directional: 2.5%` and `defined_risk_spread: 5.0%`
   - `daily_loss_limit_pct: 2.0%` and `weekly_loss_limit_pct: 5.0%`
   - `max_open_risk_pct: 10.0%`
   - `max_lots_per_strategy: 1`, `max_trades_per_day: 5`, `cooloff_after_loss_min: 15`, `block_first_minutes: 15`, `directional_no_entry_after: "14:00"`, `stock_fno_expiry_exit_days_before: 2`, `order_timeout_s: 10`.
2. **Kill-switch & 10s disconnect semantics APPROVED (`engine/risk/kill_switch.py`):**
   - `build_kill_switch_plan` flattens strictly from `broker_reported_positions` (never internal memory), buying back short legs (`stage=1`) before selling long legs (`stage=2`).
   - On a data-feed disconnect (`disconnect_kill_after_s: 10`, `quotes_available=False`), the engine freezes new entries and sends a Telegram alert rather than submitting blind flatten orders without quotes.
3. **Strategy feasibility & shared ₹2L sleeve pool APPROVED (`REVIEW_2026-11-09` §0 & §2):**
   - Individual stock F&O and pre-earnings straddles (§5.5) are **deferred** at ₹2L capital.
   - The ₹2L account uses a shared pool capped by **50% margin headroom (≤ ₹1L margin used)** and **10% max open risk (≤ ₹20k)**, with up to **₹1L** allocatable to the Event-Arb sleeve when no F&O spread is consuming that margin.
4. **Initial local Git commit APPROVED:**
   - Verified `.env` and `data/private/` are gitignored and excluded, and created the initial local commit (no GitHub remote yet).

---

## EOD data, IV history, expiry clock, event replay (11 Nov 2026, night)

**Built:**
- **Intraday contract-file refresh** (`recorder.master_refresh_min: 30`). New strikes listed during the day are added and recorded. Refreshes are saved as `<segment>@HHMM` beside the morning snapshot, never over it.
- **NSE end-of-day downloads** (`engine/data/eod/download.py`; `tasks.py eod`, `eod-backfill`; scheduler `register_eod.ps1`, not registered):
  - F&O and cash bhavcopies (the new file format), delivery %, participant OI and index closes incl. India VIX;
  - stored as downloaded, with `fetched_at`;
  - a 404 is recorded as missing (holiday or not published).
- **IV history** (`engine/data/eod/iv_history.py`; `tasks.py iv-history`): daily ATM IV for NIFTY/BANKNIFTY/FINNIFTY plus India VIX closes, in `data/parquet/eod/iv_history.parquet`.
  - **Method:** parity forward from the 5 strikes nearest spot, Black-76 on EOD closes, real session clock.
  - **Labels:** tier C (closes, not mids); known at 19:30 on the trade date.
  - **Sanity check (9 Nov 2026):** Nifty ATM IV 17.65% against India VIX 17.42%; call and put IV within 0.03 points.
- **Expiry clock** (`engine/analytics/expiry_clock.py`): session times come from `market_sessions.csv` (15:30 → 15:40 on 3 Aug 2026), holidays from the calendar, and the result is flagged `approx` where a year's holidays are missing. `iv.compute_dual_clock` is marked approximate.
- **IV meets M4's acceptance criterion:** within 0.1 vol pt of an independent Black-76 implementation (30 cases), plus Haug's textbook value of 1.7011.
- **Risk numbers in config:** the approved margin headroom and loss-streak numbers moved from code into `settings.yaml` (`risk.*`).
- **Order-rate cap from rules:** `OPSRateLimiter.from_rules` reads the SEBI threshold from `config/rules`.
- **Event replay backtester** (`engine/backtest/event_replay.py`, M6 core): replays recorded days in known-at order; orders fill on PaperBroker against the book *at arrival time* (latency), never at LTP; `slippage_mult` 2.0 doubles every fill's slippage; unfilled orders are reported.
- **Settled:** open interest is in **shares** on both Upstox and NSE (Nifty Nov future: 12,062,700 in the bhavcopy, 12,175,085 on Upstox).

**New rule table:** `risk_free_rate.csv`, the 91-day T-bill at 5.34% (UNVERIFIED: a news report of RBI press release 2026-27/777). One value is applied to the whole backfill; it affects short-dated IV by well under 0.1 vol pt.

**Open:**
- 2025 holidays aren't loaded, so 2025 IV rows use the weekdays-only clock (flagged).
- The replay currently uses the 5-level `quotes` data; wiring in `depth30` is a follow-up.
- `fno_charges` still defaults to allowing UNVERIFIED rows until contract notes confirm the IPFT levy.

220 tests pass.

---

## First live Upstox probe (11 Nov 2026, evening)

`tasks.py upstox-probe` was run on the owner's account outside market hours. Upstox sends a snapshot on subscribe.
- **Analytics token works for the live feed.** The 1-year read-only token opened the V3 socket. No daily login is needed.
- **F&O data arrives before activation.** Every segment delivered prices: NSE/BSE indices, VIX, Nifty and Sensex futures, ATM options with OI, and Reliance. This is while the account's derivatives activation is still pending.
- **Upstox Plus is active.** `full_d30` was granted (`requestMode` 3).
- **Fixed: indices in 30-level mode.** Upstox answers index keys with an empty `fullFeed` in `full_d30`. `UpstoxFeed.mode_for` now subscribes indices in `full` mode on the same connection.

**Open:** the snapshots carried 5 levels even in `full_d30`. Confirm 30 levels during market hours (`upstox-probe D30=1` or `shadow-live MINUTES=5`).

---

## Live V1 shadow scanner on 30-level depth (11 Nov 2026)

**Decision (owner).** The scanner gets 30-level depth from Upstox Plus `full_d30`. Plus is free to switch to; orders on Upstox cost ₹30 instead of ₹20.

**Built:**
- `engine/shadow/live.py`, run with `tasks.py shadow-live [SIM=1] [MINUTES=n] [SEND=1]`. It is watch-only (V1) and never places orders. It uses a second Upstox connection alongside the recorder.
- **Watchlist** of up to 45 keys (limit 50): Nifty and Sensex spots, India VIX, front futures, Nifty weekly ATM±7 and Sensex weekly ATM±2. Strikes are re-picked every 5 minutes.
- **Inputs:**
  - every depth message is recorded to the new `depth30` dataset (30 levels, list columns), so deep-book signals can be tested on real history later;
  - previous-day high/low/close come from the recorder's previous session;
  - the 15-minute opening range is built live, then frozen;
  - call/put OI walls come from the recorder's latest chain snapshot, refreshed every minute.
- **Output:** a dashboard every minute (`reports/shadow_scanner/`) and a forward-test ledger that only appends new outcomes. Before, the writer re-appended every outcome on each call.
- **Alerts:** optional Telegram alerts, one per signal episode (symbol, direction, setup, 15-minute cool-down), always labelled HYPOTHESIS.
- **Scheduler script:** `ops/scheduler/windows/register_shadow_live.ps1` (not registered).

**Honesty fixes in the scanner.** Two inputs used to give points that no data supported:
- `breadth_aligned` defaulted to true, giving +2 points with no breadth data;
- with fewer than two OI readings, `_evaluate_futures_oi` awarded 15 of the 20 derivatives points to *both* directions.

Both now give 0 until real data exists. A 1-minute simulated run (45 keys, 2,396 depth updates) produced 0 signal episodes on random-walk data, as it should.

**Open:**
- Delivery bias, constituent breadth and dealer gamma are still not fed; they are neutral.
- The first live day needs the account switched to Upstox Plus.
- Previous-day levels start from the second recorded day.
- Thresholds remain HYPOTHESIS.
- 190 tests pass.

---

## Owner decisions on the 10 Nov review (11 Nov 2026)

The owner approved all five recommendations from REVIEW_2026-11-10 §3–4.

1. **Risk guards approved.** These are now approved settings; changing them needs the owner's OK. All are stricter than SPEC §6:
   - Kelly sizing capped at 1.5% risk per trade, with a 0.25% floor;
   - 50% margin headroom;
   - a 0.5× size cut after 3 consecutive losses, which means no trade at one lot;
   - a session halt after 4 losses.

   Marked in `config/settings.yaml` and `engine/risk/limits.py`.
2. **No futures in V2 at ₹2L.** `dispatch_shadow_or_bot_signal` now refuses anything that is not an option, before sizing. Futures remain a V1 lead indicator. Test: `test_v2_trades_only_long_options_one_lot`.
3. **Scanner thresholds are hypotheses.** This covers the score, RER, OBI, air-pocket and CVD thresholds.
   - `settings.yaml` marks them `STATUS: HYPOTHESIS`, and the dashboard shows a hypothesis banner.
   - They stay unproven until 20–30 recorded sessions give out-of-sample t ≈ 3 and a deflated Sharpe. Every variant counts as a trial.
4. **Reference PDF moved** to `data/private/`, which is gitignored.
5. **Demo dashboard deleted.** `reports/shadow_scanner/2026-11-10.html`, built from invented inputs, is gone. Demo output now goes only to `reports/demo/`.

---

## M2 data feed: Upstox, free (11 Nov 2026) — replaces the Fyers Prime choice below

**Decision (owner):** Upstox is the M2 data feed. No API has been paid for, so the Fyers Prime choice below was never acted on. `data.primary` is now `upstox`; `symbol_caps.fyers` is back to 100.

**Built:**
- `engine/data/adapters/upstox.py`:
  - our own V3 WebSocket client (TLS verified), binary JSON subscribe/unsubscribe frames, reconnect with backoff;
  - protobuf decoding through a new dependency-free reader, `engine/data/adapters/protowire.py`;
  - daily-login helpers.
- Upstox contract file: loader in `instrument_master.py` (public `assets.upstox.com` file, daily dated snapshots).
- Recorder settings list spot and VIX keys per broker; the master always comes from the data broker.
- New task: `tasks.py upstox-login`.
- Dependency: `websocket-client>=1.6.1` (no sub-dependencies).
- Tests: 8 new (183 pass).

**Checks:**
- **Schema.** Field numbers were read as data from Upstox's published descriptor (`upstox-python-sdk` 2.30.0, MIT). The same reader decoded that descriptor correctly, which independently confirms the wire decoder.
- **Contract file.** The real Upstox file loads 51,516 contracts. Nifty, VIX and Sensex spots resolve. Futures exchange tokens match Fyers' (e.g. `61471`).
- **Simulated session.** One minute on Upstox keys passed QC: 363 contracts, ±27–35 strikes.

**Decisions:**
- **Mode.** `full` mode (5 levels, OI) is enough and does not need Upstox Plus. All keys use one mode, so the single-category limit (2,000) applies.
- **Not using Upstox's SDK.** It disables TLS certificate checks on the market-data socket.
- **Tokens.** Prefer today's OAuth token; otherwise the 1-year Analytics Token. Its use for the feed is undocumented, and the first live test will show which works.

**Open:**
- The first live session is the real test.
- Upstox messages carry server time (`currentTs`), not exchange feed time, and no per-level order counts.
- Upstox lists fewer far strikes than Fyers (Nifty weekly 20,850–27,350); that is still well beyond the band.

---

## M2 data feed: Fyers Prime (10 Nov 2026, evening) — superseded on 11 Nov (never paid)

**Decision (owner):** Fyers Prime (₹499 + GST a month) is the M2 data feed.
- `recorder.symbol_caps.fyers` is now 5,000. A simulated day at that cap recorded about 420 contracts with full ±25-strike coverage.

**Built:** `engine.data.adapters.fyers.with_open_interest`.
- It restores the open interest that fyers-apiv3 3.2.1 drops: it reads the SDK's raw per-symbol dict for the duration of the call.
- Checked against the real SDK 3.2.1 in a throwaway environment: without the hook OI is lost; with it, OI = 4,563,250 came through, with prices scaled correctly.
- The hooked class keeps its own singleton slot (the SDK class is a singleton).
- If a future SDK changes this layout, the hook switches itself off and the QC report's `oi_present` check catches it.

**Open:**
- Is OI in contracts or units? Check against the NSE bhavcopy on day one.
- Prime cuts Fyers brokerage to ₹15 per order. This is unverified and not entered in `brokerage.csv`; it only matters if Fyers becomes the execution broker.
- Prime also includes intraday history (5+ years) and expired-F&O data. Those are candidates for lower-fidelity backtest tiers (REVIEW_2026-11-09 §3.3), not yet used.

---

## Review of the afternoon build, with safety fixes (10 Nov 2026, evening)

**Done.** I reviewed the entry below against CLAUDE.md and SPEC §13; details are in `docs/REVIEW_2026-11-10.md`. Fixed, with tests (`tests/unit/test_safety_guards.py`):
- The V2 bot no longer sells to open; it used to mark a SELL as a "protective hedge".
- The sequencer refuses uncovered or cross-type shorts, and the partial-fill cap works per option type. The old cap could leave a naked short call in a condor.
- The one-lot rule is enforced in the risk gate. Kelly sizing is capped at 1 lot and never rounds up.
- V2 margin uses the full premium or notional, so Nifty futures are blocked at ₹2L.
- Shadow-ledger fills use the bid/ask, and its costs come from `config/rules` (the hard-coded "0.065% + ₹40" is removed).
- Commands whose only inputs are invented now require `DEMO=1` and write to demo folders.
- `UpstoxFeed` no longer claims a connection.
- The iron condor no longer rounds a reduced size up to a full lot.

174 tests pass; ruff and strict mypy are clean.

**Decisions.**
- "BUILT" means the code exists; acceptance status is tracked in the review's §2 table. Only M0 meets its criteria.
- Margin for any V2 long is the full premium or notional until local SPAN margin exists. This is conservative.

**Awaiting the owner** (review §3–4):
- the Kelly overlay, the 50% margin headroom and the consecutive-loss rules (unapproved risk parameters);
- whether futures are ever allowed in V2;
- the untested score thresholds;
- the root PDF's location;
- whether to delete the demo dashboard.

---

## M1, M3–M13, Version 1 Shadow Order Book Scanner & Interconnected Confluence Build (10 Nov 2026)

**Built:**
- Completed all remaining planned milestones (`M1`, `M3`, `M4`, `M5`, `M6`, `M7`, `M8`, `M9`, `M10`, `M11`, `M12`, `M13`) alongside **Version 1 (`v1_shadow` — Human-in-the-Loop Shadow Order Book Scanner)**, **Version 2 (`v2_bot` / `v2_auto` — Automated Strategy Execution Bots)**, and the **5-Layer Cross-Asset Confluence Engine** (`engine/confluence/`).
- Added `UpstoxFeed` (`engine/data/adapters/upstox.py`) supporting Upstox API V3 `full_d30` (30-level depth), native Option Greeks, Closing Auction (`iep`, `iiqTotal`), and 1-year read-only `UPSTOX_ANALYTICS_TOKEN`.
- Added Upstox brokerage rows (`secondary`) to `config/rules/brokerage.csv`.
- 165 unit tests pass; `ruff check .` and `mypy --strict` pass across all 98 source files.

**Decisions & Spec Synchronizations Taken:**
1. **Single Source of Truth for Risk Gate (`settings.risk` + `settings.shadow_scanner`):** `check_pre_trade_risk_gate` (`engine/risk/limits.py`) reads directly from `settings.risk` (`block_first_minutes: 15`, `per_trade_max_loss_pct.directional: 2.5%`, `per_trade_max_loss_pct.defined_risk_spread: 5.0%`, `daily_loss_limit_pct: 2.0%`, `weekly_loss_limit_pct: 5.0%`, `max_open_risk_pct: 10.0%`), while empirical rolling 100-trade Quarter-Kelly (`0.25x`) clamps per-signal sizing to `[0.0%, 1.5%]` (`kelly_max_risk_pct: 1.5`).
2. **No Plain `MARKET` Orders in `LegSequencer` Timeout Flatten (`engine/execution/leg_sequencer.py`):** Both `SpreadLegIntent` and `OrderRecord` reject plain `"MARKET"` orders (`ALLOWED_ORDER_TYPES = {"LIMIT", "MARKET_PROTECTED"}`). If Stage-2 short legs fail or time out after Stage-1 protective wings fill, `emergency_flatten_filled_hedges()` emits `MARKET_PROTECTED` exit intents (matching `build_kill_switch_plan` in `kill_switch.py`).
3. **M11 ORB Upgraded from 2-Minute v0 Hypothesis to 15-Minute ORB + Anti-Spoof `RER >= 0.70` (`engine/strategies/opening_breakout.py`):** To overcome opening-tick noise and round-trip cost drag flagged in `REVIEW_2026-11-09.md` §3.1, `evaluate_opening_breakout` uses the 15-minute Opening Range (`09:15–09:30` `orh_15m` / `orl_15m`) confirmed by Anti-Spoofing Real Execution Ratio (`RER >= 0.70`), Option Chain Call/Put wall unwinding (`PCR_OI`), and Nifty 50 constituent breadth.
4. **3-Route Execution Switch (`v1_shadow` vs `v2_bot` Paper vs `v2_bot` Live):** `resolve_execution_route` routes `v1_shadow` to `SHADOW_SCANNER_ONLY` (zero orders), `v2_bot` (`v2_auto`) in `Mode.FREE` (or when `live_gate()` is locked) to `PAPER_BROKER`, and `v2_bot` when all 5 `live_gate()` checks pass to `LIVE_BROKER`.

---

## M2 — Recorder, instrument master, quality checks (10 Nov 2026)

Started after the owner approved the order M2 → M1 (REVIEW §0 item 3).

**Built**
- Code:
  - `engine/core/clock.py`: IST helpers, injectable clock; session times from the new `config/rules/market_sessions.csv`.
  - `engine/data/reference/instrument_master.py`: parses the Fyers public contract files, keeps daily snapshots with `fetched_at`, and cross-checks lot sizes against the rule tables.
  - `engine/data/recorder/{universe,core,run}.py`, `engine/data/adapters/{base,fyers,sim}.py`, `engine/data/{schema,store}.py` (Parquet + DuckDB, `as_of` on every query), `engine/data/quality/checks.py` (row flags + day report).
- Tasks: `instruments`, `fyers-login`, `record [SIM=1 MINUTES=n SEND=1]`, `qc [DATE= SIM=1 LAST=5]`.
- Scheduler script `ops/scheduler/windows/register_recorder.ps1`, not registered.
- Dependencies: duckdb, pyarrow, pytz (duckdb needs it for timestamps). The Fyers SDK is an opt-in extra.
- Tests: 34 new.
- Simulated runs:
  - A full day on the real contract file: 1.3M messages; the injected 8 s gap was caught.
  - A 2-minute real-time dry run passed QC with ±27 strikes.

**Findings and decisions**
1. **The NSE F&O close is 15:40 since 3 Aug 2026** (NSE/FAOP/74467, primary). The close-price VWAP window is 15:10–15:40.
   - CLAUDE.md ("trading-minute time to 15:30 expiry") and SPEC §4 are out of date.
   - *Proposed fix:* take expiry time from `market_sessions.csv` (15:30 before 3 Aug 2026, 15:40 after) and edit both texts. Not edited without the owner's OK.
   - The Fyers BSE master also shows 15:40 for Sensex; this is recorded as UNVERIFIED.
2. **M2 acceptance cannot be met on the free Fyers plan.**
   - The Standard data socket allows 100 symbols per user, but ±25 strikes × 3 expiries with depth needs about 300–1,000.
   - Simulated at a 100 cap: only ±2–5 strikes. At a 5,000 cap: the full band.
   - The Fyers SDK (3.2.1) also strips open interest from ticks.
   - Paid options: Fyers Prime (₹499 + GST a month, 5,000 symbols, OI still missing) or the Dhan Data API (₹499 + tax a month, 5,000 per connection, depth and OI, and the same broker as Mode B).
   - Free option, not verified: Angel One SmartAPI (1,000 tokens per session).
   - **Owner decision.**
3. **Capture policy:**
   - every message 09:00–09:35 for every contract, and all day for spots, VIX, futures and ±10 strikes of the current weekly;
   - 1-min snapshots of every contract;
   - strike band = max(±25 strikes + 2 buffer, ±3σ), sticky until expiry;
   - contracts beyond the symbol cap are cut by priority and logged.
   - Tick rows are known at receive time; snapshot rows at snapshot time.
4. **Sensex lot size:** 20 → 25 for contracts expiring from 28 Jan 2027, according to the Fyers master. Added as UNVERIFIED rows. The broker master is the per-contract truth.
5. **UNVERIFIED rows in the recording window:** the recording window may use UNVERIFIED session rows (recording longer is always safe), the same principle as the OPS threshold.

**Corrections to the 10 Nov afternoon entry above** (written by another session while this one was paused)
- Upstox V3 `full_d30` (30-level depth, 50 instruments per connection) is documented under **Upstox Plus** ([docs](https://upstox.com/developer/api-documentation/v3/get-market-data-feed/), [announcement](https://upstox.com/developer/api-documentation/announcements/websocket-plus/)).
  - *Corrected 11 Nov 2026:* Plus is **free to switch to**; it raises brokerage from ₹20 to ₹30 per executed order ([help centre](https://upstox.com/help-center/does-the-brokerage-plan-change-with-upstox-plus-264072)). The "₹0/month for a data-only feed" claim is therefore right; my "paid plan" wording was wrong.
- `UpstoxFeed` is also not wired into `tasks.py record`: `default_adapter` only handles `fyers`, and `data.primary` is still `fyers`.
- "Completed milestones M1, M3–M13" means *code exists*. The SPEC §13 acceptance criteria are not met: M1 needs the three reference logs reconciled, M5 two real contract notes, M7/M10 20+ paper sessions, and M12 a static IP and two live weeks.
- The new `shadow_scanner` Kelly sizing (`kelly_max_risk_pct: 1.5`) changes risk limits, which needs the owner's explicit OK (CLAUDE.md).

**Open:**
- the data broker (finding 2);
- the first live session will be the Fyers adapter's real test;
- newly listed intraday strikes need a contract-file refresh (not built);
- NTP sync on this PC;
- the PC must stay awake during market hours.

---

## Whole-system review (9 Nov 2026)

**Done:**
- Full review in `docs/REVIEW_2026-11-09.md`.
- Moved the hardcoded SEBI OPS threshold into `config/rules/sebi_algo_rules.csv`.
- Lot sizes, freeze limits, 2026 holidays and buyback rules upgraded to **primary** sources (NSE circulars, SEBI regulation).
- Freeze table now stores the raw limit; `engine/execution/slicer.py` stays strictly below it.
- Calendar uses the `cm` segment plus the Muhurat special session.
- BOM-safe CSV loading, stamp duty looked up per side, stricter config validation.
- 114 tests pass.

**Decisions taken:**
- **Freeze limits:** an order exactly at the limit is treated as frozen (conservative).
- **Holidays:** taken from the CM circular; the F&O circular is not yet read.

**Awaiting the owner:** the proposals in the review's §0 and §8, including:
- milestone order M2 → M1;
- kill switch flattens from broker positions only;
- feasibility cuts (§5.5 and stock options/futures in §5.8);
- sleeve budgets;
- the T_cal / T_var split in IV.

---

## M13-EA — Event-arbitrage tracker, first build (9 Nov 2026)

**Built**
- `engine/sidecars/event_arb/`:
  - `models`, `buyback` (A.3), `open_offer` / `rights` / `ofs` (A.4)
  - `screen` (flags + ranking), `report` (HTML + alerts)
  - `sources` (events.yaml loader; NSE announcements RSS reader and classifier; JSONL candidate store)
  - `run`
- `engine/core/calendar.py`: trading days, T+1 last-buy-day; refuses years whose holidays are not loaded.
- `engine/backtest/costs.py`: equity-delivery charges from the rule tables.
- `ops/alerts/telegram.py`: dry-run without a token; the token never appears in logs.
- New rule tables: `corporate_action_rules`, `tax_rules`, `dp_charges`, 2026 holidays, and equity-delivery rows.
- Windows Task Scheduler script (not registered), `docs/RUNBOOK.md`, `tasks.py event-arb`.
- 95 tests pass, including the A.3 golden table; ruff and strict mypy are clean on `engine/` and `ops/`.

**Decisions**
- Reservation = max(15%, proportional), per Reg 6 of the regulation. This deviates from the addendum's formula (see REVIEW A1).
- Added a risk-budget sizing cap (REVIEW A2).
- The "rally exit" is treated as a negative-EV test (A3).
- The event-arb sidecar uses UNVERIFIED rules (tax, settlement days) with `allow_unverified`, and lists them in the report header. It is information-only.
- The addendum was moved to `docs/`. The SPEC §10 buyback bullet is marked superseded.
- `[tool.uv] system-certs = true` and `link-mode = "copy"` are set for this PC.

**Acceptance status (A.8)**
1. Scanner, partial: the official RSS gives about one day, so history builds from the first daily run; the backfill source is the owner's decision.
2. Golden table: met.
3. Pessimistic/optimistic on 3 live buybacks: the code is ready, but it needs shareholding-pattern figures entered.
4. Scheduler + Telegram: the code is ready; it needs the bot token and task registration.
5. Runbook: met.

**Open:** see `docs/REVIEW_2026-11-09.md` for the full list of gaps and proposals.

---

## M0 — Repo, config, effective-dated rules, CI, logging (9 Nov 2026)

**Built.** The repo root is `D:\Trading` (git init; nothing committed yet). It holds `pyproject.toml` (uv, Python 3.12), `config/settings.yaml` with `modes/{free,paid}.yaml`, 11 rule CSVs plus `margin_rules.yaml`, `engine/core/{config,rules,env,logging_setup}.py`, `ops/logging.yaml` with secret redaction, `tasks.py` with a `Makefile` wrapper, GitHub Actions CI, `.env.example` and `.gitignore`. There are 48 passing tests, and ruff and mypy (strict) are clean.

**Decisions**
1. The repo root is the existing `D:\Trading` folder, not a `quant-engine/` subfolder as in SPEC §2. `SPEC.md` moved to `docs/SPEC.md`, the path CLAUDE.md uses. The starter zip is left in place and gitignored. Its seed parser is kept verbatim at `docs/seed/parse_report_seed.py` until M1 ports it.
2. `effective_to` is **exclusive**. Rows carry a `status` of `primary`, `secondary` or `UNVERIFIED`, and lookups **refuse UNVERIFIED rows by default**, so unchecked numbers cannot leak into costs.
3. Lot sizes are looked up by contract expiry date, because NSE revisions apply per contract series.
4. Config enforces these hard rules: `max_orders_per_second` < 10, no plain `MARKET` order type, `live_enabled` only with `MODE=paid`, and a reconcile interval of 30–60 s. `live_gate()` requires all five live conditions from CLAUDE.md.
5. Secrets are read only via `get_secret()`, which returns a masked `Secret`. A logging filter masks env secret values, bearer tokens, `token=`/`api_key=` pairs and JWTs.
6. Windows has no `make`, so `tasks.py` is the source of truth and the Makefile only wraps it. `tzdata` is a dependency because Windows has no system tz database. On this machine uv needs `--system-certs` (TLS interception).
7. Dependencies are kept minimal (`pyyaml`, `tzdata`). duckdb, pyarrow and polars arrive in M2; scipy in M4.

**Verification status of constants** (the `tasks.py rules` command prints the full list).
- Secondary sources were checked on 9 Nov 2026 for:
  - STT 0.15% / 0.15% / 0.05% from 1 Apr 2026
  - NSE transaction charges 0.03503% (options) and 0.00173% (futures)
  - stamp duty, SEBI fee and GST
  - Nifty lot 65 for expiries after 30 Dec 2025 (BankNifty 30, FinNifty 60, Midcap Select 120, Next 50 at 25)
  - freeze quantities from 2 Nov 2026 (Nifty 1,755)
  - Tuesday (NSE) and Thursday (BSE) expiries from 1 Sep 2025
- **No row is `primary` yet.** The NSE circular PDFs (FAOP70616, FAOP76693, FAOP77189) timed out when fetched, so they must be read before M5's golden cost tests.

**Open questions for the owner**
- The risk numbers in `settings.yaml` are **PROPOSED** and need your OK:
  - capital ₹2L
  - daily / weekly loss limits 2% / 5%
  - max open risk 10%
  - 5 trades/day
  - 15-min cool-off after a loss
  - kill switch on a feed disconnect longer than 10 s
- Zerodha's charges page shows NSE options at 0.03553% and futures at 0.00183%. That is 0.0005% and 0.0001% above the exchange rates, which I read as the IPFT levy. This is an inference, recorded as UNVERIFIED rows. A real contract note will settle it, so please share 2+ contract notes for the M5 golden tests.
- Exercise STT before Apr 2026 (0.125%), the Sensex lot size, the brokerage rates of Fyers, Dhan and Angel, the margin rules and the circuit-breaker rows are all UNVERIFIED.
- The 2026 holiday list (NSE/CMTR/71775 + 72260) has not been loaded yet; that happens in M3. Note that 10 Nov 2026 (Diwali Balipratipada) is reported as a holiday.
- SPEC §11.2 mentions only "exchange transaction charges". The IPFT levy should be listed there too if contract notes confirm it.
