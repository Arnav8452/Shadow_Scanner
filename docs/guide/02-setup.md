# 2. Setup

This sets the system up on a Windows PC (the owner's platform). macOS and Linux work for everything except the scheduled-task scripts in `ops/scheduler/windows/`.

## 2.1 What you need

| Item | Why | Cost |
|---|---|---|
| Python 3.12+ and [uv](https://docs.astral.sh/uv/) | Runs everything | Free |
| An **Upstox** account with F&O enabled, and a developer app | Market data for the recorder and the engine | Free |
| **Upstox Plus** plan on that account | 30-level depth (`full_d30`) for the engine | Free to switch; orders placed *on Upstox* then cost ₹30 instead of ₹20 |
| A PC that stays on and awake 08:45–15:45 on mains power | The recorder cannot recover lost minutes | — |
| Internet with stable latency | Feed gaps over 5 s fail the day's quality check | — |
| Optional: a Telegram bot | Morning/evening alerts and QC verdicts | Free |
| Optional: Fyers account | Fallback data source (100 symbols on the free plan: too few for the recorder) | Free |

Disk: a full recording day is several hundred MB of Parquet; the 30-level books add more. Keep 50+ GB free.

## 2.2 Install

```bash
pip install --user uv
```

```bash
uv sync --group dev
```

If `uv` is not on your PATH, use `python -m uv` in place of `uv` in every command in this guide. If your network intercepts TLS (some corporate and antivirus setups), `pyproject.toml` already sets `[tool.uv] system-certs = true`, so uv reads certificates from the Windows store.

Check the install:

```bash
uv run python tasks.py test
```

```bash
uv run python tasks.py lint
```

Both should pass (about 390 tests; ruff and strict mypy on `engine/`).

## 2.3 Secrets: the `.env` file

Copy the template and fill in only what you use. `.env` is gitignored; never commit it, paste it into chat, or print a token.

```bash
cp .env.example .env
```

| Variable | Needed for | Where to get it |
|---|---|---|
| `MODE` | Leave empty or `free` | — |
| `ENGINE_VERSION` | Leave empty (`v1_shadow`) | — |
| `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_REDIRECT_URI` | Daily login fallback | https://account.upstox.com/developer/apps → create an app; redirect URL e.g. `https://127.0.0.1/` |
| `UPSTOX_ANALYTICS_TOKEN` | The data feed, no daily login | Developer console → Analytics Token (1 year, read-only) |
| `UPSTOX_ACCESS_TOKEN`, `UPSTOX_TOKEN_DATE` | Written by `tasks.py upstox-login` | Do not fill by hand |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alerts | @BotFather; without them, alerts only print |
| `FYERS_*` | Fallback feed only | myapi.fyers.in; `uv sync --extra fyers`, then `tasks.py fyers-login` each morning |
| `DHAN_*`, `KITE_*` | Paid mode only | Not used today |

Secrets are read through `engine.core.env.get_secret()`, which masks them in logs and error messages.

## 2.4 Check broker access

These work outside market hours (Upstox sends a snapshot on subscribe even when the market is closed).

```bash
uv run python tasks.py upstox-probe
```

After switching to Upstox Plus:

```bash
uv run python tasks.py upstox-probe D30=1
```

It prints, per segment (indices, F&O, stocks), whether prices, depth and OI arrived. `NSE_FO NOTHING RECEIVED` means F&O is not active on the account. If the analytics token is refused (`auth_failed`), use the daily login instead:

```bash
uv run python tasks.py upstox-login
```

It opens the login page; sign in and paste the redirect URL back. The token lasts until about 03:30 the next morning.

Download today's contract files and check lot sizes against `config/rules` (no login needed):

```bash
uv run python tasks.py instruments
```

## 2.5 First runs

1. **Dry run on the synthetic feed** (any time; writes under `data/sim/`, never mixed with real data):

   ```bash
   uv run python tasks.py record SIM=1 MINUTES=2
   ```

2. **Open the page:**

   ```bash
   uv run python tasks.py cockpit
   ```

   It opens http://127.0.0.1:8765. Bookmark it. With no engine running, the page says "Live engine not running" and offers Replay.

3. **Engine on the synthetic feed** (to see every panel move without a market):

   ```bash
   uv run python tasks.py shadow-live SIM=1 MINUTES=10
   ```

4. **First live test, during market hours** (5 minutes of real recording):

   ```bash
   uv run python tasks.py record MINUTES=5
   ```

   Then open `reports/qc/<today>.md` and check: the verdict, `oi_present: true`, strike coverage ±25 or more, and no `auth_failed` or `limit` feed events.

5. **Engine for real**, alongside the recorder (Upstox allows 2 connections, 5 on Plus):

   ```bash
   uv run python tasks.py shadow-live MINUTES=5
   ```

6. **Full day by hand** once both short tests work: run `record SEND=1`, `shadow-live SEND=1` and `cockpit` in three terminals before 08:50.

## 2.6 Load history (once)

Several features need history before they say anything: IV percentile (20+ sessions), the screener (60+ sessions for its IV percentile), the swing scanner and the delivery study.

```bash
uv run python tasks.py eod-backfill FROM=2025-11-01 TO=2026-11-12
```

About 10 minutes. Then:

```bash
uv run python tasks.py iv-history
```

```bash
uv run python tasks.py screener-history
```

About 15 minutes; one summary per session in `data/state/screener/`.

## 2.7 Automate with Windows Task Scheduler

After one manual day has worked, register each task once. Each script prints how to remove it.

| Task | Script | Default time | Runs |
|---|---|---|---|
| `QuantEngine-Recorder` | `ops\scheduler\windows\register_recorder.ps1` | 08:45 Mon–Fri | `tasks.py record SEND=1` |
| `QuantEngine-ShadowLive` | `ops\scheduler\windows\register_shadow_live.ps1` | 08:46 Mon–Fri | `tasks.py shadow-live SEND=1` |
| `QuantEngine-Cockpit` | `ops\scheduler\windows\register_cockpit.ps1` | 08:45 Mon–Fri | `tasks.py cockpit OPEN=0` |
| `QuantEngine-EOD` | `ops\scheduler\windows\register_eod.ps1` | 20:00 Mon–Fri | `eod`, then `iv-history`, then `evening` (runs on battery, 60-min limit) |
| `QuantEngine-EventArb` | `ops\scheduler\windows\register_event_arb.ps1` | 23:30 daily | `event-arb SEND=1` |

```powershell
powershell -ExecutionPolicy Bypass -File ops\scheduler\windows\register_recorder.ps1
```

The recorder and engine exit by themselves on exchange holidays (from `config/rules/holidays.csv`). Each scheduled task's console output goes to `logs/<task>_task.log`.

## 2.8 PC settings that matter

- **Clock:** enable Windows time sync (Settings → Time → Sync now, or `w32tm /resync`). The quality check compares receive time with exchange time.
- **Sleep:** disable sleep and hibernate during market hours; schedule Windows Update outside them. If the laptop sleeps, everything pauses.
- **Power:** stay plugged in. The EOD task is allowed to run on battery; the market-hours tasks should not need to.
- **The Claude app's terminal tabs:** processes started there stop if the app quits. The scheduled tasks do not.

## 2.9 Where things end up

| Path | What |
|---|---|
| `data/parquet/quotes/` | Every recorded quote (P1) |
| `data/parquet/depth30/` | 30-level books (P2) |
| `data/parquet/eod/` | End-of-day tables, `iv_history.parquet` |
| `data/raw/` | Downloaded exchange files and contract masters |
| `data/state/` | Paper journals (`paper_<account>.jsonl`), setups, signal study, ledger, screener cache, swing state, page key and token |
| `data/replay/<date>/` | Journals from replays (wiped at the next replay of that date) |
| `data/private/` | Reference trade logs (never committed, never in outputs) |
| `C:\QuantBackup\` | Nightly backup of `data/parquet` and `data/state` ([§9.10](09-data-and-research.md#910-backup)) |
| `reports/qc/` | Daily recording quality reports |
| `reports/shadow_scanner/` | Scanner dashboard HTML, rewritten every minute |
| `reports/event_arb/` | Event-arbitrage reports |
| `logs/` | `engine_<task>.log`: each process's full log, DEBUG level, rotated daily, 30 days kept (`engine_record.log`, `engine_shadow-live.log`, `engine_cockpit.log`, `engine_replay.log`…). `<task>_task.log`: the scheduled tasks' console output. Secrets are redacted. |

All of `data/`, `reports/` and `logs/` are gitignored.
