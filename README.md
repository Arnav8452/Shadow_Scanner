# Shadow Scanner: Retail Quant Engine

[![ci](https://github.com/Arnav8452/Shadow_Scanner/actions/workflows/ci.yml/badge.svg)](https://github.com/Arnav8452/Shadow_Scanner/actions/workflows/ci.yml)

A personal research and **paper-trading** engine for Indian index options and futures (NSE Nifty, BSE Sensex), built for one owner's own trading on ₹1–2 lakh. It records the option market tick by tick, computes its own IV and greeks, runs a set of strategies on separate paper accounts with full costs and margin, and audits every trade against hard risk rules.

> **Paper only.** Live order placement is locked behind five conditions (paid data mode, a config switch, a whitelisted static IP, a fresh daily 2FA session and the owner's confirmation), none of which is met. Every threshold in this repository is a **hypothesis**; nothing here is shown to be profitable. This is personal software: it does not offer signals or trading to anyone else, it does not redistribute market data, and it is not investment advice.

## The full guide

**[`docs/guide/README.md`](docs/guide/README.md) is the reference for the whole system.** It is written for a first-time reader and covers, in order:

| # | Chapter | What it explains |
|---|---|---|
| 1 | [What the system is](docs/guide/01-what-it-is.md) | The processes, the data flow, what is on, off or not built |
| 2 | [Setup](docs/guide/02-setup.md) | Install, broker keys, scheduled tasks |
| 3 | [Daily routine](docs/guide/03-daily-routine.md) | Morning checks, market hours, evening jobs |
| 4 | [The Quant Cockpit](docs/guide/04-cockpit.md) | Every panel, column, tab and button of the web page |
| 5 | [Signals](docs/guide/05-signals.md) | IV and the variance clock, the score, setups, spoofing, zones, the charges gates |
| 6 | [Strategies](docs/guide/06-strategies.md) | Each paper strategy: entry, exit, size |
| 7 | [Paper trading](docs/guide/07-paper-trading.md) | Fills, the risk gate, vol sizing, gap stress, journals, replays |
| 8 | [New strategies](docs/guide/08-new-strategies.md) | From idea to verdict, honestly |
| 9 | [Data and research](docs/guide/09-data-and-research.md) | Recorder, quality checks, nightly jobs, backfills, audit |
| 10 | [Risk and configuration](docs/guide/10-risk-and-config.md) | The hard rules, the live lock, `settings.yaml`, `config/rules/` |
| 11 | [Troubleshooting](docs/guide/11-troubleshooting.md) | Something looks wrong |
| 12 | [Glossary](docs/guide/12-glossary.md) | OBI, RER, CVD, GEX, IVP, PCR, breakeven… |

Also: [`docs/SPEC.md`](docs/SPEC.md) (the design and milestones), [`docs/DECISIONS.md`](docs/DECISIONS.md) (the build log, newest first, with every open question), [`CLAUDE.md`](CLAUDE.md) (the standing engineering and trading rules).

## What it does

- **Records** the Indian F&O market: about 875 contracts on Upstox's full feed, plus 30-level order books for 50 contracts, to Parquet with data-quality flags. Option bid/ask history cannot be bought later, so the recorder is the foundation.
- **Prices options itself:** Black-76 on mid prices against a synthetic forward, one IV per strike from the out-of-the-money option, and a variance-time clock (nights and weekends weighted from 6 years of index candles). Broker IV fields are not used.
- **Runs paper strategies, each on its own account and journal:** iron condor, credit and debit spreads, calendar, iron fly, compression debit spread, synthetic and put-hedged futures, expiry-day plays, order-flow scalpers (breakthrough, spoof fade, zone rider, put panic, OI ignition), and a momentum rule mined from a reference trade log. Fills are at the bid/ask from recorded depth, with leg-by-leg latency and the full charges.
- **Shows margin in rupees** for every position: a live broker quote (SPAN + exposure, hedge benefit included), or a dated fallback table when the quote is unavailable.
- **Enforces risk in code:** defined risk only (wings filled before shorts), per-trade and daily/weekly loss limits counting open losses, a cool-off after a loss, a liquidity-vacuum guard, vol-managed sizing that only ever reduces size, ±5/8% gap stress on overnight structures, and a nightly audit of every paper trade.
- **Researches honestly:** walk-forward and Monte Carlo backtests at 2× slippage, deflated Sharpe, point-in-time data, and trade-log mining whose private inputs never leave `data/private/`.

## How it runs

```
P1  recorder        Upstox full feed (~875 contracts) -> data/parquet (quotes, 1-min chains, depth)
P2  shadow-live     Upstox 30-level feed (50 contracts) -> scanner, analytics, strategies, paper accounts
P3  cockpit         web page on 127.0.0.1:8765, fed by P2 over a local bus
20:00 evening job   EOD files, IV history, screener, swing scan, audit, backup
```

## Quick start

```bash
pip install --user uv
uv sync --group dev                 # add --extra mining to read trade-log PDFs
cp .env.example .env                # broker keys; .env is gitignored
uv run python tasks.py test         # 470+ tests
uv run python tasks.py lint         # ruff + strict mypy
```

Then follow [Setup](docs/guide/02-setup.md) for broker logins and the scheduled tasks.

## Commands

`uv run python tasks.py <task> [KEY=VALUE]` (or `make <task>` where `make` exists). The most used:

| Task | What it does |
|---|---|
| `record` | P1: start the recorder |
| `shadow-live` | P2: the live scanner, analytics and paper strategies |
| `cockpit` | P3: the web page |
| `replay DATE=` | Replay a recorded day through the same engine |
| `evening` | Nightly job: EOD files, IV history, screener, swing, audit, backup |
| `qc DATE=` | Data-quality report for a recorded day |
| `audit` | Check every paper trade against the hard rules |
| `backfill-history` | Seed strategy histories from Upstox candles instead of waiting for them |
| `rules` | Validate `config/rules/` and list UNVERIFIED rows |
| `test`, `lint` | pytest; ruff + mypy |

The full list, with options, is in [Daily routine](docs/guide/03-daily-routine.md) and [Data and research](docs/guide/09-data-and-research.md).

## Repository layout

```
config/settings.yaml          every setting, marked APPROVED (owner) or HYPOTHESIS
config/rules/                 fees, STT, lot sizes, margins, holidays, sessions: effective-dated, with sources
engine/data/                  broker adapters, recorder, instrument master, quality checks, EOD
engine/analytics/             forward, IV, greeks, variance clock, SVI, GEX, realized vol
engine/shadow/                the live scanner (P2)
engine/cockpit/               strategies, paper accounts, margin, stress, sizing, the web page
engine/risk/, execution/      risk gate, kill switch, order rules, leg sequencing
engine/backtest/              costs, event replay, walk-forward, Monte Carlo, deflated Sharpe
engine/research/              research only; never imported by live paths
tests/                        unit and data tests (synthetic fixtures only)
docs/                         the guide, SPEC, DECISIONS, runbook
data/                         gitignored: recordings, journals, private trade logs
```

## Rules this code keeps

From [`CLAUDE.md`](CLAUDE.md), enforced in code and covered by tests: defined risk only; limit or protected orders only; under 10 orders a second per exchange; a kill switch on loss limits, open risk, reconciliation mismatch or feed loss; one lot per strategy until 20–30 live trades; no regulatory or fee constant hardcoded in strategy code; secrets only in `.env`; private trade logs never in outputs or commits.
