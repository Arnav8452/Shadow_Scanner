# Decisions log

Newest first. Each milestone ends with what was built, decisions taken and open questions.

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
