# 11. Troubleshooting and FAQ

## The page

**"Live engine not running" / the live-engine dot is red.**
The engine (P2) isn't running. Outside 08:46–15:45 that is normal. During market hours, start it:

```bash
uv run python tasks.py shadow-live SEND=1
```

Check `logs/shadow_live_task.log` if the scheduled task should have started it.

**The page dot is red or the page is blank.**
The gateway isn't running:

```bash
uv run python tasks.py cockpit
```

An open page reconnects by itself within a second of the gateway coming back.

**"FEED STALE: no quote for N s".**
No quote for 10 s or more. Paper entries are frozen and exits paused. The feed reconnects on its own; check your internet. If it lasts minutes, restart the engine.

**`DROPPED` in the header.**
The engine dropped feed messages because it couldn't keep up. Close heavy programs; check the CPU. Persistent drops mean the PC is too slow for 50 keys at 30 levels.

**`cockpit errors N` in the header.**
An exception inside the cockpit step. It never stops recording, but read the log and fix the cause.

**No option chain: "Waiting for the 1-minute option chain".**
The chain comes from the **recorder** (P1). Start `tasks.py record`. Strategies that need strikes by delta, OI walls or the chain also wait.

**IV percentile shows "(no history)".**
Fewer than 20 sessions in `iv_history.parquet`. Run `eod-backfill` once, then `iv-history` ([02-setup.md §2.6](02-setup.md#26-load-history-once)).

**ΔOI or the OI read is empty for Sensex.**
OI change uses the previous session's NSE bhavcopy; Sensex is on BSE and not in it. Its ΔOI counts from engine start instead.

**A contract I searched for says "not in the live feed yet".**
It is being added. Only 3 extra contracts can be pinned; remove one (× on its pill) to add another.

## Paper trades

**`per_trade_risk_exceeded`.**
The stop % × premium × lot is above ₹5,000 (directional). Lower the stop %, or choose a cheaper (further OTM or nearer expiry) contract.

**`blocked_first_15_minutes`, `after_directional_cutoff`, `in_loss_cooloff`, `max_trades_per_day_reached`, `daily_loss_limit_hit`.**
The approved risk gate is working. See [07-paper-trading.md §7.2](07-paper-trading.md#72-the-risk-gate).

**`event window: …`.**
A macro event (RBI, Budget, CPI, FOMC…) or a heavy results cluster is active; new entries are blocked on every account until it ends.

**`expires today: no new entries after 14:30`.**
The expiry-day rule for long options.

**A strategy never trades.**
Open its row on the Overview: the decision line says what it's waiting for. Common causes:
- it only runs on certain days (iron fly, expiry strategies);
- it needs history (calendar: 10 sessions; compression: 20 widths);
- the recorder isn't running;
- it is switched off in `cockpit.plays`.

**My stop was crossed but the trade wasn't closed.**
Model-price confirmation: the stop fires only if the option's model price (from the index and a smoothed IV) also says it should. A quote air pocket the index didn't make is ignored. The detail view shows when a stop is being held for this reason.

**Positions after a restart.**
Open positions and the day's loss streak are rebuilt from `data/state/paper_<account>.jsonl`. Overnight strategies keep earlier days' positions.

## Data and jobs

**QC verdict FAIL.**
Read `reports/qc/<date>.md` for which check failed. Gaps usually mean the internet or the PC slept. A failed day is kept, with its flags.

**`auth_failed` in feed events.**
The Upstox token was refused. Run `uv run python tasks.py upstox-login`.

**`NSE_FO NOTHING RECEIVED` in `upstox-probe`.**
F&O isn't active on the Upstox account; enable the segment with Upstox.

**The nightly job exited non-zero.**
One step failed, often NSE blocking a request; the others ran. Read `logs/eod_task.log` and `logs/evening_task.log`. The next night catches up 7 days.

**Swing refuses a date.**
Nights must run in order, and a processed date is never redone. Run the missing nights first.

**A date next year is refused.**
`config/rules/holidays.csv` has no rows for that year yet. Add next year's holiday list (each December).

**A full-day replay is slow or uses a lot of memory.**
On `main`, a replay loads the whole day first. The `replay-streaming` branch fixes this and is waiting to be merged.

## Development

**Tests or lint fail after a change.**

```bash
uv run python tasks.py test
```

```bash
uv run python tasks.py lint
```

`engine/` is checked with strict mypy; fix the types rather than ignoring them.

**`uv` not found.**
Use `python -m uv` in place of `uv`.

**TLS errors from uv or downloads.**
The network intercepts TLS. `pyproject.toml` already makes uv use the Windows certificate store; for Python downloads check the system clock and certificates.

## FAQ

**Can I turn on live trading?**
Not from this system as it stands. It needs paid mode, the config switch, a whitelisted static IP, a daily 2FA session and the owner's in-session confirmation, plus a strategy that has passed the definition of done ([08-new-strategies.md §8.1](08-new-strategies.md#81-the-definition-of-done-claudemd)). Until then, place real trades yourself in your broker's app if you choose; the cockpit is research.

**Are the signals buy/sell recommendations?**
No. They are unvalidated measurements being forward-tested. Nothing here is investment advice.

**Why does the score on the page move so slowly?**
It is the 1-minute average of per-second scores, so you can read it. Hover the bar for the latest raw score.

**Why Upstox and not my broker's data?**
Its free V3 feed gives 5-level depth on 2,000 contracts and 30-level depth on 50 (Plus). Fyers' free plan allows 100 symbols, too few for the recorder. Paid options (Dhan, Zerodha Kite) are planned for Mode B.

**Can I add Bank Nifty or stocks?**
- **Bank Nifty:** add it as an underlying under `recorder.underlyings` and `shadow_live.watch`, mind the 50-key and 2,000-key caps, and check its lot size, freeze quantity and expiry rows in `config/rules`.
- **Stock F&O:** deferred at ₹2 lakh capital.
