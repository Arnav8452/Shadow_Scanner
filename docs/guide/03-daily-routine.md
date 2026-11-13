# 3. Daily routine

Times are IST. With the scheduled tasks registered ([02-setup.md §2.7](02-setup.md#27-automate-with-windows-task-scheduler)), most of this happens on its own; your job is to check it happened.

## Before 08:45

- [ ] PC on mains power, awake, internet up. No Windows Update scheduled for market hours.
- [ ] Only if the Upstox analytics token was refused yesterday: `uv run python tasks.py upstox-login`.
- [ ] Glance at `config/events/macro_calendar.csv` for today: RBI, Budget, CPI, FOMC or a heavy results day open an **event window** that blocks new paper entries.

## 08:45–09:15: start-up

- Recorder starts at 08:50, engine at 08:46, gateway at 08:45 (or start them by hand in three terminals).
- Open http://127.0.0.1:8765. In the header, the **page** and **live engine** dots should be green.
- The header line shows the feed status, lag, contracts live and readings today. `DROPPED` or `cockpit errors` means: read `logs/shadow_live_task.log`.

## 09:15–15:40: market hours

What happens without you:
- The scanner scores every live contract every book update. Readings start setups; setups confirm or fail.
- Each strategy checks its own rules (most every second, the condor every minute) and opens or closes paper trades.
- Open positions are marked at the bid, their stops and targets checked continuously.
- The first 15 minutes are blocked for new entries (risk gate), except opening-range strategies.

What you can do:
- **Watch.** The Overview tab of the Strategies panel shows every strategy's state and today's net. Click a row for its rules and decision.
- **Paper-trade by hand.** Pick a contract, set a stop %, press **Buy 1 lot @ ask** (key B). See [07-paper-trading.md](07-paper-trading.md).
- **Study.** Hover the scanner's score bar for its components; open the Analytics tab for greeks and the four pillars.

Times that matter:

| Time | What changes |
|---|---|
| 09:15 | Market opens. Opening range (15-min for the strip, 30-min for the playbook) starts forming. |
| 09:30 | Entry window opens for directional, debit spreads and other setup-driven strategies. |
| 10:00 | Condor and married-future windows open. |
| 14:00 | Directional cut-off (risk gate): no new directional entries. |
| 14:30 | Condor's last entry. On an index's **expiry day**, no new long entries in options expiring today. |
| 14:45 | On expiry day: long options expiring today are closed; no more readings on that index. |
| 15:00 | No new paper entries anywhere. Condor exits on the day before expiry. |
| 15:15 | No new readings or setups (normal days). |
| 15:20 | Intraday positions squared off at the bid. Overnight strategies (condor, calendar, compression, synthetic and married futures) keep their positions. |
| 15:10–15:40 | NSE F&O closing-price window (VWAP of the last 30 minutes, per `NSE/FAOP/74467`). Options expiring today settle to an averaged index value, so late in the day they stop behaving like options; their greeks and readings are switched off. |
| 15:40 | F&O close (15:30 before 3 Aug 2026; `config/rules/market_sessions.csv`). |
| ~15:45 | Recorder stops, writes the QC report, compacts the day's files. |

## After the close

- [ ] Check the QC verdict: Telegram line, `reports/qc/<date>.md`, or Cockpit → Research → **Recorder QC**. FAIL means a gap over 5 s, a missing 1-minute snapshot, fewer than ±25 strikes around ATM, or no OI. A failed day is still useful, but its gaps are flagged.
- [ ] Skim Cockpit → Research → **Paper accounts by day** and the strategy rows. Write down anything surprising; do not change a rule because of one day.

## 20:00: nightly jobs (automatic)

`QuantEngine-EOD` runs, in order:
1. `eod`: NSE/BSE end-of-day files (F&O and cash bhavcopies, delivery, participant OI and volume, indices), plus the F&O ban list and bulk/block deals, which exist only on the day. It also catches up anything missing from the last 7 days and adds the day to the screener.
2. `iv-history`: rebuilds `data/parquet/eod/iv_history.parquet` (our ATM IV from closes), which feeds the IV percentile.
3. `evening`: delivery files and tomorrow's **delivery-spike watchlist**; Nifty members' **results dates** into the macro calendar; **quarterly results and shareholding** for the watchlist and heavyweights; the **swing** paper run (fills last night's orders at the open, stops, exits, queues tomorrow's entries; monthly trend sleeve).

A non-zero exit means one step failed (often NSE refusing a request); the others still ran. Read `logs/eod_task.log` and `logs/evening_task.log`.

## 23:30: event arbitrage (automatic)

Screens new corporate announcements. Check the Telegram digest or `reports/event_arb/<date>.html`. Curate anything worth tracking into `config/event_arb/events.yaml` (see [09-data-and-research.md](09-data-and-research.md#98-event-arbitrage)).

## Evenings and weekends

- **Replay** a recorded day on the page: header → **Replay** → pick a date and speed → **Start**. Practise manual trades on it; nothing touches the live journals.
- **Research tab**: the signal study, which conditions results came from, swing candidates, flows, event arbitrage.
- **Screener tab**: tomorrow's F&O names with rich or cheap premium, unusual activity, results dates.

## Weekly and monthly

- **Weekly:** run `uv run python tasks.py qc LAST=5` to see whether the last five sessions passed.
- **Monthly:**
  - After the NSE Indices factsheet comes out, run `uv run --extra mining python tasks.py index-weights-verify`.
  - Add new official dates to `config/events/macro_calendar.csv`, with their source URLs.
  - Check for lot-size and freeze-quantity circulars, then add rows to `config/rules/lot_sizes.csv` and `freeze_qty.csv`.
  - Run `uv run python tasks.py rules`.
- **December:** add next year's holidays to `config/rules/holidays.csv`. Until then, the calendar refuses dates in the new year.

## If the internet drops

Nothing has to be restarted by hand.

| Part | What happens | Comes back |
|---|---|---|
| Recorder | The socket pings every 20 s; a dead link closes within ~30 s and reconnects at 1, 2, 4… up to 30 s apart, re-subscribing everything. A watchdog restarts the feed after 20 s of silence. | By itself. The gap is lost for good and QC flags it. |
| Engine | Same reconnect loop; watchdog after 60 s of silence. After 10 s without a quote the page shows **FEED STALE**: new paper entries freeze and exits pause (nothing is closed on stale prices). | On the first fresh quote. A stop or target crossed during the outage is acted on at the first real price. |
| Positions held overnight | Restored from the journals on restart; their contracts are re-added to the feed. | Yes. |
| Page and gateway | Local only, so unaffected. The page token survives a gateway restart. | n/a |
| Nightly jobs | That night's download fails; the next run catches up 7 days. The ban list and bulk/block deals for a missed night are lost. | Next run, or `tasks.py eod` by hand. |

If the PC sleeps, everything pauses. If a process crashes, restart it; open paper positions and the day's loss streak are rebuilt from the journals.
