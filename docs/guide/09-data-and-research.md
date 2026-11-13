# 9. Data, research and the nightly jobs

## 9.1 The recorder (P1)

Code: `engine/data/recorder/`. Settings: `recorder:` in `config/settings.yaml`.

**What it records, 08:50 until a few minutes after the close:**
- **Nifty:** spot, the front future, and options for the current weekly, next weekly, current monthly and **next monthly** expiries.
- **Sensex:** spot, the front future, and current-weekly options.
- **Context:** India VIX and the 8 largest Nifty members (for breadth and correlation).
- **Strike band:** at least **±25 strikes** around ATM (more when ±3σ of the expected move is wider), plus 2 buffer strikes. Re-centred every 60 s; strikes that were recorded stay recorded for the day.
- **Priority:** nearer strikes and nearer expiries come first, within the Upstox cap of 2,000 keys. On 12 Nov 2026 the set was 567 contracts.
- **Every message** for every contract 09:00–09:35 (the opening study); 5-level depth all day.
- **A 1-minute snapshot** of every chain (bid, ask, LTP, OI, volume). The engine, the option chain page and most strategies read these.

**At the close** it writes the QC report and compacts the day's files. The contract file is refreshed every 30 minutes for newly listed strikes.

## 9.2 Data quality checks

Code: `engine/data/quality/checks.py`. Every recording is checked, and the flags travel with the data:

| Check | Fails when |
|---|---|
| Gaps | Any feed gap over 5 s in session |
| Snapshots | A 1-minute chain snapshot is missing |
| Coverage | Fewer than ±25 strikes around ATM |
| OI | No open interest |
| Stale | A quote unchanged for 120 s |
| Crossed / zero bid | Bid ≥ ask, or no bid |
| Clock skew | Receive time ahead of exchange time by more than 1 s |

```bash
uv run python tasks.py qc DATE=2026-11-12
```

```bash
uv run python tasks.py qc LAST=5
```

Reports go to `reports/qc/<date>.md`, also shown in Cockpit → Research → **Recorder QC**.

## 9.3 Storage

Parquet files queried through DuckDB (`engine/data/store.py`). Every query takes an `as_of` so research never sees data that wasn't known at the time.

| Path | Contents |
|---|---|
| `data/parquet/quotes/` | Recorded quotes and 5-level depth, by date |
| `data/parquet/depth30/` | The engine's 30-level books, by date |
| `data/parquet/recorder_events/` | Feed connects, disconnects, re-subscriptions, limits |
| `data/parquet/eod/` | End-of-day tables, `iv_history.parquet` |
| `data/parquet/fundamentals/` | Quarterly results and shareholding |
| `data/raw/` | Downloaded exchange files and contract masters |

Field definitions: [DATA_DICTIONARY.md](../DATA_DICTIONARY.md).

## 9.4 End-of-day files (20:00)

`tasks.py eod` downloads, for the day (default today, after ~19:30):

| Dataset | Used for |
|---|---|
| F&O bhavcopy | Previous-session OI for ΔOI and the OI read; IV history; the screener |
| Cash bhavcopy and delivery | Delivery spikes, swing, fundamentals watchlist |
| Participant OI and volume | Research → Flows (FII/DII/Pro/Client) |
| Index closes | Trend sleeve, context |
| F&O ban list, bulk/block deals | Exist only on the day; saved when run without `DATE` |

**Point in time:** a day's bhavcopy is known at about 19:30 that evening. Participant OI is next-day data. The code never uses either before it was published.

Related commands:
- `eod-backfill FROM= TO= [DATASETS=delivery,fo]`: history.
- `iv-history`: rebuilds our ATM IV per day from closes (tier C: EOD closes, not intraday).
- `screener-history`: per-session screener summaries.

## 9.5 The evening job

`tasks.py evening` (after `eod`):
1. **Delivery files** for the last 21 sessions, then the **delivery-spike watchlist** for tomorrow (`data/state/delivery_watchlist/<date>.json`):
   - only F&O stocks with liquid options; ban-list names are dropped;
   - classified as *LONG_ACCUMULATION* (delivery ≥ 2× average on a ≥ 1% up day) or *RETAIL_CHURN_FADE* (a ≥ 5% spike on below-average delivery %);
   - up to 5 of them join the engine's 30-level feed the next day. They decide what to **watch**, not what to trade.
2. **Results dates** for Nifty members from NSE board-meeting notices into `config/events/macro_calendar.csv` (rows tagged `[auto:nse-board-meetings]`; hand-entered rows win).
3. **Fundamentals** from XBRL filings (`data/parquet/fundamentals/`) for the watchlist and the 10 heavyweights: revenue, PAT, EBITDA margin with YoY, and promoter/FII/DII holdings.
4. **Swing** paper run (see [06-strategies.md §6.4](06-strategies.md#64-nightly-paper-strategies-not-in-the-cockpits-intraday-panel)). Nights must run in order; the account refuses a date it has already processed.

## 9.6 Reference data

| File | Contents | Maintained |
|---|---|---|
| `config/reference/nifty50_constituents.csv` | Members with `effective_from`/`effective_to` (no survivorship) and weights | Weights derived nightly; `index-weights-verify` monthly against the NSE factsheet; members after each semi-annual rebalance |
| `config/events/macro_calendar.csv` | RBI MPC, Budget, CPI, FOMC, results dates, each with source URL and status | Monthly; results dates automatic |
| `config/event_arb/events.yaml` | Curated corporate events | By hand from exchange filings |
| Instrument master (`data/raw/`) | Every contract, lot size, expiry, tick | Downloaded daily (bitemporal: what was known when) |

## 9.7 Research tools

- **Cockpit → Research** ([04-cockpit.md §4.5](04-cockpit.md#45-research-tab)): the signal study, condition tables, paper results, flows, swing, event arbitrage, QC.
- **Delivery event study:** do delivery spikes predict 1/3/5-session returns net of costs?

  ```bash
  uv run python tasks.py delivery-study
  ```

  The report goes to `reports/research/`. `delivery_watch.bias_in_score` stays false unless a setting passes (deflated Sharpe ≥ 90%, |t| ≥ 3) and the owner approves.
- **Trade-log mining:** parse a reference trader's trade-history PDF to reverse-engineer their style (entry times, holds, MFE/MAE):

  ```bash
  uv run --extra mining python tasks.py mine-log FILE=data/private/log1.pdf LABEL=log1
  ```

  The PDF must live in `data/private/` (gitignored). Names and account IDs are stripped on import. Never put values from these logs into fixtures, outputs or commits.
- **Log-3 entry features** (SPEC 5.2 `log3_entry_features`): where was the index at each of the funded trader's entries?

  ```bash
  uv run python tasks.py log3-features
  ```

  - **Input:** `data/private/mined/log3_fills.csv` (from `mine-log`). Scale-ins in the same direction within 10 minutes count as one trade idea.
  - **Data:** Upstox 1-minute index candles for each session.
  - **Features, signed in the trade's direction:** time of day, distance from the session's mean price (a VWAP proxy; the index has no volume), position vs the 15-minute opening range, trend since the open, 15-minute momentum, distance from the day's high (calls) or low (puts), and whether the last 5 minutes made a new extreme.
  - **Control:** the same features every 5 minutes from 10:00 to 14:00 in the same sessions. The best one-condition rules are ranked by entries caught minus other minutes caught.
  - **Output:** a report next to the CSV, private and gitignored.
  - **First run, 13 Nov 2026:** 15 trade ideas in 10 sessions (Sensex options, Sep–Oct 2026). 13 of 14 measured entries came after a **15-minute push of at least about +0.05% in the trade's direction**, a condition true at only 29% of other minutes. Entries also sat closer to the day's extreme (median 0.04% vs 0.15%) and above the opening range. This is a **HYPOTHESIS** from 10 sessions: test it on sessions the log never saw before it changes the §5.2 trigger.
- **Self-review** of your own journal (behaviour: revenge trades, size after losses, time-of-day):

  ```bash
  uv run python tasks.py self-review FILE=<journal>
  ```

- **Backtest library** (`engine/backtest/`): costs, event replay, walk-forward, block-bootstrap Monte Carlo, probabilistic and deflated Sharpe. See [08-new-strategies.md](08-new-strategies.md#87-step-6--paper-sessions-and-the-verdict).
- **Analytics library** (`engine/analytics/`): forward, IV, greeks, SVI surface, risk-neutral density, GEX/vanna/charm, realized vol, VRP, OI positioning, microstructure and order-flow mechanics.

## 9.8 Event arbitrage

Code: `engine/sidecars/event_arb/`. Information only; it never trades, tenders or applies.

- **Screens** NSE announcements nightly at 23:30 for tender buybacks, open offers, rights issues and entitlements, and OFS. Each curated event gets a verdict (GREEN / AMBER / RED), the last buy day, a share count within the ₹2 lakh small-shareholder test (₹1.5 lakh buffer), a cushion at **pessimistic** acceptance, and flags.
- **Curate** relevant candidates into `config/event_arb/events.yaml`, with terms from the exchange filing. Write `UNKNOWN` where a value isn't published; list secondary-sourced fields under `unverified_fields`.
- **Before acting**, follow the manual checklist in [RUNBOOK.md](../RUNBOOK.md#event-arbitrage--manual-checklist-addendum-a5). It covers clubbed holdings across demat accounts, unpledging and the tender window.

## 9.9 Alerts

With `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`, the system sends:
- the recorder's QC verdict;
- A+ scanner alerts (labelled HYPOTHESIS, at most 10 a day, one per episode, 15-minute cool-down);
- the event-arbitrage digest.

Without them, alerts only print to the logs.

## 9.10 Backup

Recorded option books cannot be bought back, so the last step of the nightly `evening` job copies them away (`tasks.py backup`; settings under `backup:`).

- **What:** `data/parquet` (recordings, 30-level books, EOD tables, fundamentals) and `data/state` (paper journals, setups, study, swing). `data/raw` is skipped because it can be downloaded again.
- **How:** new or changed files only (size or time), keeping the folder layout. **Nothing is ever deleted from the target**, so an accidental delete or a corrupted day on the source cannot reach the backup.
- **Where:** `C:\QuantBackup`. This PC has one physical disk (C: and D: are partitions of the same 512 GB NVMe), so this guards against deletion and corruption, not against the disk failing. For that, plug in an external drive and set `backup.target`, e.g. `"E:/QuantBackup"`.
- **No cloud upload by default:** the private trade logs and personal data stay on this machine.
- **Space:** the run warns when the source or target drive has under 5 GB free. On 13 Nov D: had 16 GB and C: 40 GB free, and a full recording day adds several hundred MB, so plan an external drive within weeks.
- **By hand:** `uv run python tasks.py backup`, or `TARGET=E:/QuantBackup` for a one-off copy elsewhere.

## 9.11 History you can fetch instead of waiting for

Checked on 13 Nov 2026 with the read-only analytics token (Upstox Plus):

| Data | Source | Depth | Usable for |
|---|---|---|---|
| Index 1-minute candles | Upstox `GET /v3/historical-candle/…/minutes/1/…` | Since Jan 2022 | Tapes, opening ranges, band widths, realized vol |
| Expired Nifty/Sensex options and futures, 1-minute OHLC **with OI** | Upstox `GET /v2/expired-instruments/…` (Plus only) | 111 Nifty expiries, back to Oct 2024 | OI velocity, writer pain, straddles, breakouts: research backtests |
| End-of-day bhavcopies | NSE (already downloaded nightly) | Years | IV percentile, screener, swing |
| Historical **bid/ask and order-book depth** | **Nowhere**, at retail prices | — | Only what our recorder has captured |

What this means:
- **Done:** `tasks.py backfill-history` seeded the compression strategy's 20 sessions of 15-minute band widths from index 1-minute candles. Re-running it is safe.
- **Done (13 Nov):** the same task now seeds the calendar's **term spread** history from 1-minute option candles (`engine/cockpit/term_backfill.py`), not from end-of-day files (far-month bhavcopy closes are stale: 12 Nov showed IV 27.7% vs ~14% live).
  - **Near:** the first expiry on or after the session. **Far:** the first monthly at least 14 days away, as the live 14-day rule picks.
  - **Prices:** every minute from 14:30 to 15:25 in which both the call and the put at one of the 3 strikes nearest the index traded. Expired contracts come from the expired-instruments API, live ones from the v3 candle API.
  - **IV:** synthetic forward at the strike where call and put are closest, Black-76 on the variance clock, mean of call and put, median over the minutes.
  - **Caveat:** last trades, not mids; the median over about 55 minutes keeps that noise small.
  - **First run:** 15 sessions (23 Oct–12 Nov). Normal days −1 to +2 vol points; near-expiry days spike (9 Nov +8.9). The live strategy keeps adding one value at each close.
- **Possible next:** a backtest runner on the expired-option 1-minute candles, giving about two years of OI and price history for the OI, straddle and breakout strategies. Candles have no bid/ask, so fills would use a spread model taken from our recorded spreads, doubled. That is a documented exception to "fills at recorded bid/ask", fit for screening ideas, not for the final verdict.
- **Only recording can supply:** anything that reads the order book (the score's book pillar, RER, OBI, spoofed and eaten walls, absorption zones, the opening-range breakout), and the final bid/ask-fill verdict on any strategy.

## 9.12 The paper-trade audit

`tasks.py audit` checks every journaled paper trade against the hard rules and writes `reports/audit/<date>.md`. It also runs every night at the end of `evening`. The rules:
- **R1:** defined risk, and wings before shorts;
- **R2:** per-trade risk;
- **R3:** lots;
- **R4:** entry window;
- **R5:** trades per day;
- **R6:** no entries after the daily loss limit;
- **R7:** square-off;
- **R8:** a losing scalp is never held more than 10 minutes;
- **R9:** charges and net arithmetic.

It reads the journals only. `DIR=data/replay/<date>` audits a replay. The first run, on 13 Nov, found exactly one breach: breakthrough's 09:37 put ran without its rules after an engine restart and lost ₹2,801 on its old 20% stop. Restored scalps are now re-adopted.
