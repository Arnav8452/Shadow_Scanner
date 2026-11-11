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

## Daily — event arbitrage

- [ ] 23:30 IST: the event-arb scan runs from Task Scheduler (`QuantEngine-EventArb`). Check the Telegram digest or `reports/event_arb/<date>.html`. To run it manually: `uv run python tasks.py event-arb SEND=1`.
- [ ] Add any relevant new candidates (buyback / open offer / rights / OFS) to `config/event_arb/events.yaml`. Enter the terms from the **exchange filing**, write `UNKNOWN` where a value is not yet published, and list secondary-sourced fields under `unverified_fields`.
- [ ] Update `price:` snapshots for live events until M3 automates prices.

## Monthly / when NSE announces

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
