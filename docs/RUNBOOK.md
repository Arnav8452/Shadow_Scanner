# Runbook

Operational checklists. The engine never places live orders, tenders or applications by itself.

## Daily — market-data recorder (M2)

Before 08:45 IST on trading days:
- [ ] Only if the Upstox Analytics Token is refused by the feed: `uv run python tasks.py upstox-login` — sign in in the browser, paste the redirect URL back. The token goes into `.env` (never printed) and lasts until about 03:30 the next morning.
- [ ] PC on mains power, sleep disabled, Windows Update not scheduled for market hours.

During the day (automatic once `QuantEngine-Recorder` is registered; otherwise run `uv run python tasks.py record SEND=1` by hand):
- [ ] The recorder starts at 08:50, records until 15:45, then writes the QC report and compacts the day's files. It exits by itself on holidays. Ctrl+C stops it cleanly.
- [ ] Check the Telegram line or `reports/qc/<date>.md`. The verdict is FAIL if there is any gap over 5 s, a missing 1-min snapshot, fewer than ±25 strikes around ATM, or no open interest.
- [ ] `uv run python tasks.py qc LAST=5` shows whether M2 acceptance (5 passing sessions) is met.

Useful without a broker login:
- `uv run python tasks.py instruments` — downloads today's contract files and prints expiries, lot sizes and lot-size mismatches against `config/rules`.
- `uv run python tasks.py record SIM=1 MINUTES=2` — dry run on the synthetic feed (writes under `data/sim/`, never mixed with real data).

## Daily — cockpit (live page, paper trading, replay)

Three processes, each restartable on its own:
- **Recorder** (`record`): the system of record. Unchanged.
- **Engine** (`shadow-live`): scanner, 30-level recording, paper tracks. It publishes to the page.
- **Gateway** (`cockpit`): the page itself. Stopping it never stops recording or paper trading.

Start of day:
- [ ] `uv run python tasks.py cockpit` (opens http://127.0.0.1:8765; bookmark it). Automatic once `QuantEngine-Cockpit` is registered: `powershell -ExecutionPolicy Bypass -File ops\scheduler\windows\register_cockpit.ps1`.
- [ ] Header dots: **page** and **live engine** green. The header also shows feed status, lag (exchange time is whole seconds, so ±1 s), contracts watched and signals. "DROPPED" or "cockpit errors" in the header means check the engine log.

On the **Desk**:
- **Market strip**, per underlying:
  - spot, future and basis;
  - synthetic forward, our ATM IV and its 1-year percentile;
  - VIX, the 15-min opening range, previous-day H/L/C;
  - OI walls, PCR, GEX (labelled HYP: it assumes dealers are long calls and short puts).
- **Scanner:** contracts ranked by score. A gold edge marks 75+; grey marks 65–75. "not tradable" marks a SHORT on an option (a naked short, so tracked only).
- **Centre:** click a row for its 30-level book, a price line with entry/SL/T1, and the ticket.
- **Manual paper ticket (Track C):**
  - **Buy 1 lot @ ask** (long options only) or **Take signal** (buys the option that expresses it), with a stop as % of premium;
  - **Close** and **Flatten manual**. Keys: B, T, C, ↑↓.
  - Every order goes through the approved risk gate. Refusals show as pop-ups and in Events, with the reason (e.g. `per_trade_risk_exceeded`: lower the stop % or pick a cheaper strike).
- **Paper panel:**
  - **Bot (B):** takes A+ signals as 1 lot of a long ATM option.
  - **Manual (C):** your trades.
  - **Signals (A):** every 75+ signal with no limits.
  - Net P&L is after all charges for `cost_broker` (Upstox Plus). Track A also shows net at 2× slippage.
- Nothing new opens after 15:00; everything open is squared off at 15:20.

Evenings and weekends:
- **Replay** (header): pick a date and speed (1–50× or max), then Start; Pause, Resume, change speed or Stop as it plays. You can paper-trade during a replay; its journals live under `data/replay/<date>/` and are wiped at the next replay of that date.
- **Research** tab:
  - Track A results by score band, setup, hour and exit;
  - paper results by day;
  - Nifty IV/VIX history, QC verdicts, trade-mining reports;
  - **Flows and reference (EOD):** participant OI and volume (FII/DII/Pro/Client, contracts), tomorrow's F&O ban list, the largest bulk and block deals.
- **On the Desk strip:**
  - next macro events (an amber "EVENT WINDOW" line means new paper entries are blocked);
  - heavyweight breadth and realized correlation (high = broad macro move, low = stock-specific dispersion);
  - the SVI smile with ATM IV, 98/102 skew and fit error.
- **Open paper positions** show IV and Greeks (delta, gamma, theta ₹/day, vega ₹/vol pt, vanna, charm).

  The score-band table is the one that matters: if 80–90 doesn't beat 75–80 after costs and at 2× slippage over 20+ sessions, the ranking is not worth trading.

Files: `data/state/paper_bot.jsonl`, `paper_manual.jsonl` (open positions are restored on restart); `data/state/shadow_ledger.jsonl` (Track A); `data/state/cockpit.key` (local shared secret, never commit).

### If the internet (Wi-Fi) drops

Nothing has to be restarted by hand. Each process recovers on its own:

| Process | What happens | Resumes |
|---|---|---|
| Recorder (P1) | Upstox socket pings every 20 s; a dead link closes within ~30 s. Reconnects at 1, 2, 4… up to 30 s apart, re-subscribes everything. Its own watchdog also restarts the feed after 20 s of silence. | By itself. The gap is recorded and QC flags it; that market data is lost for good. |
| Shadow engine (P2) | Same reconnect loop, plus a watchdog that restarts the socket after 60 s of silence in market hours. In the cockpit, after 10 s without a quote (`execution.disconnect_kill_after_s`, approved): **new paper entries freeze and exits pause** (nothing is closed on stale prices). The page shows "FEED STALE". | By itself; entries and exits resume on the first fresh quote. A stop or target crossed during the outage is acted on at the first real price after it. |
| Condor held overnight | Unaffected while flat or between sessions; its legs are re-pinned into the feed on restart. | Yes. |
| Gateway / page (P3) | Local only (127.0.0.1), so a Wi-Fi drop does not touch it. The page token is kept in `data/state/cockpit.page_token`, so an open page keeps working after a gateway restart. | n/a |
| Nightly EOD (20:00) | Fails for that night if offline. The next night's run fetches anything missing from the previous 7 days. The F&O ban list and bulk/block deals exist only on the day; a missed night of those is lost. | Next run (or `tasks.py eod` by hand). |
| Upstox login | Not affected by a drop; the daily token still expires at the usual time. | n/a |

If the laptop **sleeps**, everything pauses: keep it awake and plugged in during market hours. Processes in the Claude app's terminal tabs stop if the app quits; the scheduled tasks do not.

## Daily — end-of-day data (after 20:00 IST)

- [ ] Automatic once `QuantEngine-EOD` is registered (`powershell -ExecutionPolicy Bypass -File ops\scheduler\windows\register_eod.ps1`). By hand: `uv run python tasks.py eod`, then `uv run python tasks.py iv-history`.
- First time only: `uv run python tasks.py eod-backfill FROM=2025-11-01 TO=<today>` (about 10 minutes; F&O bhavcopies are ~1.3 MB a day).
- The nightly `eod` also catches up on missing files from the last 7 days (`CATCHUP_DAYS=`) and adds that day to the screener's summaries.
- Screener first time only: `uv run python tasks.py screener-history` (about 15 minutes; one summary per session in `data/state/screener/`).

## Daily — event arbitrage

- [ ] 23:30 IST: the event-arb scan runs from Task Scheduler (`QuantEngine-EventArb`). Check the Telegram digest or `reports/event_arb/<date>.html`. To run it manually: `uv run python tasks.py event-arb SEND=1`.
- [ ] Add any relevant new candidates (buyback / open offer / rights / OFS) to `config/event_arb/events.yaml`. Enter the terms from the **exchange filing**, write `UNKNOWN` where a value is not yet published, and list secondary-sourced fields under `unverified_fields`.
- [ ] Update `price:` snapshots for live events until M3 automates prices.

## Monthly / when NSE announces

- [ ] **Macro calendar** (`config/events/macro_calendar.csv`): add each new official date with its source URL. Mark anything not yet confirmed `UNVERIFIED`. Fill in the Budget date when announced and confirm India CPI dates against the MoSPI calendar.
- [ ] **Results season:** add each heavyweight's board-meeting/results date as a `CONSTITUENT_EARNINGS` row with its index weight.
- [ ] **Index weights:** check the weights in `config/reference/nifty50_constituents.csv` against the latest NSE Indices factsheet, then set `weight_status` to empty (verified). After NSE's semi-annual rebalance, update the member list (`ind_nifty50list.csv`) with `effective_from`/`effective_to`.

- [ ] Freeze quantities: NSE revised these almost monthly in 2026. Add rows to `config/rules/freeze_qty.csv`.
- [ ] Lot-size revisions: add rows to `config/rules/lot_sizes.csv`.
- [ ] December: add next year's holidays to `config/rules/holidays.csv`. Until then, the calendar refuses dates in that year.
- [ ] After any rule change, run `uv run python tasks.py rules` and the test suite.

## Event arbitrage — manual checklist (Addendum A.5)

Before buying for a **tender buyback**:
- [ ] The report verdict is not RED, and every flag has been read. In particular, check the "negative expected value" and cushion flags at **pessimistic** acceptance.
- [ ] Count all your existing shares of this company, across every demat account with the same PAN sequence (holdings are clubbed). Shares held × the expected record-date price must stay ≤ ₹2L; the default buffer is ₹1.5L.
- [ ] The position fits the risk budget the report shows. The binding cap is listed beside the share count.
- [ ] Buy no later than the **last buy day** in the report (the trading day before the record date, under T+1).

Record date to tender:
- [ ] Confirm the shares are in demat before the record date. Do not sell on the record date.
- [ ] If the shares are **pledged** as F&O collateral, unpledge them before tendering. Pledged shares cannot be tendered.
- [ ] When the letter of offer arrives, enter `published_small_entitlement`, `tender_open`, `tender_close` and `unaccepted_return_date` in events.yaml.
- [ ] Tender **all** shares through the broker's corporate-actions flow (e.g. Zerodha Console → Corporate actions) inside the window. Have the CDSL TPIN ready.

After the tender:
- [ ] On the unaccepted-return date, decide on the exit for the returned shares.
- [ ] Record everything in the journal: buy, tender, accepted shares, sale of unaccepted shares, and charges. The SPEC §9 self-review covers these trades.

**Open offer:** tendered shares are locked until payment, and approvals can delay it. Compare the annualised return with a liquid fund before tendering.

**Rights / RE:**
- Link bank ASBA to the demat before the issue opens.
- An RE bought below fair value is a discounted long position, not an arbitrage: retail cannot short the stock overnight.
- Watch the share price against the issue price until the new shares list.

**OFS:** retail bids go in on the retail day only. The price often gravitates to the floor; size as a directional trade.

## Setup notes (this PC)

- uv reads TLS certificates from the Windows store (`[tool.uv] system-certs = true` in `pyproject.toml`) because this network intercepts TLS.
- Telegram: put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. Without them, alerts only print.
- Upstox (data feed for M2, chosen 11 Nov 2026; free):
  1. Open an Upstox account if you don't have one. The recorder uses V3 `full` mode (5-level depth, OI, up to 2,000 contracts), which does not need Upstox Plus. Plus is only needed for 30-level depth (50 contracts); switching to it is free but makes orders placed on Upstox cost ₹30 instead of ₹20.
  2. At https://account.upstox.com/developer/apps create an app (redirect URL e.g. `https://127.0.0.1/`). Put `UPSTOX_API_KEY`, `UPSTOX_API_SECRET` and `UPSTOX_REDIRECT_URI` in `.env`.
  3. Generate the 1-year read-only **Analytics Token** in the developer console and put it in `.env` as `UPSTOX_ANALYTICS_TOKEN`. Upstox does not document whether it opens the live feed; if the first test shows `auth_failed`, use `tasks.py upstox-login` each morning instead.
  4. Access check, any time of day (Upstox sends a price snapshot on subscribe even when the market is closed): `uv run python tasks.py upstox-probe`, then `uv run python tasks.py upstox-probe D30=1` after switching to Upstox Plus. It prints, per segment (indices, F&O, stocks), whether prices, depth and OI arrived. "NSE_FO NOTHING RECEIVED" means F&O is not active on that account.
  5. First live test, during market hours: `uv run python tasks.py record MINUTES=5`. In the QC report check: verdict, `oi_present` true, strike coverage ±25 or more, no `auth_failed` / `limit` feed events.
  5. Our client verifies TLS certificates (Upstox's own Python SDK switches that off; we don't use it).
- Live shadow scanner (V1, watch-only, 30-level depth):
  1. Switch the Upstox account to **Upstox Plus**: free, but orders placed on Upstox then cost ₹30 instead of ₹20. `full_d30` (30 levels, 50 keys) needs it.
  2. Run it alongside the recorder (two connections; Upstox allows 2, or 5 on Plus): `uv run python tasks.py shadow-live MINUTES=5` for a first test, then the full day with `uv run python tasks.py shadow-live SEND=1`.
  3. Dashboard: `reports/shadow_scanner/<date>.html` (rewritten every minute). Forward-test ledger: `data/state/shadow_ledger.jsonl`. Raw 30-level books: `data/parquet/depth30/`.
  4. Signals are unvalidated hypotheses. Do not trade on them; they are being forward-tested.
  5. Automate with `powershell -ExecutionPolicy Bypass -File ops\scheduler\windows\register_shadow_live.ps1` once a manual day has worked.
- Fyers (fallback, free plan = 100 symbols, too few for M2): API app at myapi.fyers.in, `FYERS_*` keys in `.env`, `uv sync --extra fyers`, `tasks.py fyers-login`. To use it, set `data.primary: fyers` in `config/modes/free.yaml`.
- Clock: enable Windows time sync (`w32tm /resync`; Settings → Time → Sync now). The QC clock-skew check compares receive time with exchange time.
- Recorder scheduler: after one manual session works, run `powershell -ExecutionPolicy Bypass -File ops\scheduler\windows\register_recorder.ps1` once.
