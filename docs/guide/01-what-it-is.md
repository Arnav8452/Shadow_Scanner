# 1. What the system is

## In one paragraph

The engine records the Indian index derivatives market tick by tick, computes its own analytics from that data (implied volatility, greeks, order-book pressure, open-interest changes), turns those analytics into readings and setups, and runs about twenty strategies on separate paper accounts with full trading costs. A local web page, the **Quant Cockpit**, shows all of it live and lets you place manual paper trades through the same risk checks. Every evening it downloads the exchange's end-of-day files to build history, a screener and nightly research. Over weeks, the paper journals answer the only question that matters: does any of this make money after costs?

## What it is for

- **Measuring edges honestly.** Each strategy books paper trades at the bid/ask it would really have got, pays full charges from the effective-dated fee tables, and is also scored at 2× slippage.
- **Finding out which market conditions help.** Every paper trade and every signal stores the conditions at entry (IV percentile, VIX, dealer gamma, PCR, gap, trend vs chop, breadth…), and the Research tab splits results by them.
- **Practising discretionary trading safely.** The manual paper ticket goes through the same risk gate a live account would.
- **Building data that cannot be bought.** The recorder keeps every option's bid, ask, depth and open interest for the strikes that matter.

## What it is not

- Not a live trading bot. Live orders are locked (see [10-risk-and-config.md](10-risk-and-config.md)).
- Not a signal service. It is for one person's own account and must not redistribute market data or signals.
- Not proven. No strategy here has passed the definition of done in [08-new-strategies.md](08-new-strategies.md).

## The parts

The system runs as separate processes, so any one can stop or restart without taking down the others.

| Process | Command | What it does | Runs |
|---|---|---|---|
| **P1 Recorder** | `uv run python tasks.py record` | Subscribes to about 400–570 contracts on Upstox (5-level depth, OI). Writes every quote to Parquet, a 1-minute option-chain snapshot, and a quality-check report at the close. | 08:50–15:45 on trading days |
| **P2 Engine** | `uv run python tasks.py shadow-live` | A second Upstox connection with 30-level depth on up to 50 contracts (the indices, their front futures, near-ATM options, VIX, up to 5 delivery-spike stocks). Runs the order-flow scanner, the setup tracker, every paper strategy and the signal study. Publishes a snapshot to the page 5 times a second. Records the 30-level books. | 08:46 to the close |
| **P3 Gateway** | `uv run python tasks.py cockpit` | Serves the Quant Cockpit page at http://127.0.0.1:8765 and relays between the page and the engines. Local only. | Any time |
| **P4 Replay** | Started from the page (or `tasks.py replay DATE=…`) | Plays a recorded day back through the same engine at 1–50× or full speed, with its own paper journals. | Evenings, weekends |
| **Nightly EOD** | `tasks.py eod`, `iv-history`, `evening` | NSE/BSE end-of-day files, IV history, screener summaries, delivery watchlist, results dates, fundamentals, the swing paper account. | 20:00 on trading days |
| **Event arbitrage** | `tasks.py event-arb` | Screens buybacks, open offers, rights issues and OFS announcements; information only. | 23:30 daily |

The ports are 8765 (page), 8766 (live engine bus) and 8767 (replay bus), all on 127.0.0.1.

## How data flows

```
Upstox full feed (5 levels, ~500 contracts) ──► P1 Recorder ──► data/parquet/quotes, 1-min chains, QC report
                                                                     │
Upstox full_d30 feed (30 levels, 50 contracts) ─► P2 Engine ◄────────┘ (reads the 1-min chains: OI walls,
                                                    │                     PCR, GEX, strikes by delta, calendar)
                     scanner ─► readings ─► setups ─► strategies ─► paper accounts ─► journals (data/state/*.jsonl)
                                                    │
                                                    ▼ snapshot every 200 ms
                                             P3 Gateway ─► Quant Cockpit page (your browser)

Nightly: NSE/BSE files ─► data/raw, data/parquet/eod ─► IV percentile history, screener, swing, research
```

Two consequences of this layout:

- **Most strategies need the recorder running.** The engine's own feed holds only 50 contracts. OI walls, the option chain, strikes picked by delta, the condor, the calendar and most playbook strategies read the recorder's 1-minute chain snapshots.
- **The page is only a window.** Closing the browser or stopping the gateway never stops recording or paper trading.

## The idea chain: from order book to trade

1. **Quotes and books** arrive from the feed.
2. The **scanner** scores each contract 0–100 on four pillars (location, order flow, book shape, derivatives). See [05-signals.md](05-signals.md).
3. A score of 75+ that holds for 5 seconds is a **reading**: a view on the index (a bid-heavy put reads DOWN).
4. Readings for one index and direction form a **setup**, which is CONFIRMED only when the index itself follows through.
5. **Strategies** use readings, setups and their own triggers (OI changes, IV moves, volatility compression, spoofed walls, absorption zones…) to open paper trades.
6. Each trade passes the **risk gate**, fills at the recorded bid/ask, pays full charges, and is written to a **journal** with its entry conditions.
7. The **Research tab** and the **signal study** measure whether any of it worked.

Nothing trades on a single reading. Readings flicker in and out every second.

## Two versions and two modes

- **Engine version** (`engine_version` in `config/settings.yaml`): `v1_shadow` (default) means watch, score and paper-trade; `v2_bot` routes strategy signals to the paper broker through the bot path. Neither places live orders today.
- **Mode** (`MODE` in `.env`): `free` (default) uses free broker data and paper execution only. `paid` would allow a paid data API and, only with every lock condition met, live orders. Setting `execution.live_enabled: true` in free mode is refused at start-up.

## What is on, off, partial or not built (12 Nov 2026)

**Running on paper every session**
- The order-flow scanner, setups and the signal study.
- 19 paper strategies, each on its own account: 6 long-option playbook strategies, 7 multi-leg playbook strategies, the high-IV debit-spread router, and the condor, directional, debit-spread, gamma-breakout and score-follow strategies. The full list is in [06-strategies.md](06-strategies.md).
- The manual paper ticket.
- Nightly: swing paper account (cash stocks and stock options), NIFTYBEES trend sleeve, screener, event-arbitrage screen.

**Built but waiting for data**
- **Calendar spread:** needs 10 sessions of term-spread history (collected at each close) and the next-monthly Nifty expiry, which the recorder now keeps.
- **Compression debit spread:** needs 20 sessions of 15-minute Bollinger-width history.

**Switched off**
- **MCX crude/natural-gas minis:** coded, dormant. No MCX feed, instrument list or verified commodity-tax cost rows.
- **Live trading:** off by design.
- **Delivery bias in the score:** off by owner decision until the delivery event study supports it.

**Partial**
- **Heavyweight check** in the playbook uses the six largest Nifty members' day returns as a stand-in for their order flow (no stock tick data).
- **Index weights:** derived nightly; two of the top ten differ from the NSE factsheet, and weight history is not kept.
- **Margin:** SPAN is not modelled from exchange files. The risk gate uses broker margin: live quotes from Upstox's margin calculator, else a table built from those quotes plus a 15% buffer.

**Not built**
- Pre-earnings straddles and stock F&O (deferred at ₹2 lakh), volatility-managed sizing, pairs, index-rebalance and post-earnings drift ideas.

**Unverified**
- The expiry-day rules (no long entries after 14:30, exit at 14:45) rest on the index's settlement value being an average over the last half hour. NSE's F&O closing-price window (15:10–15:40 since 3 Aug 2026) is verified in `config/rules/market_sessions.csv`; how the index's own settlement value is averaged, and BSE's equivalent, are not yet checked against primary text.
- Several fee rows and the proposed risk limits need real contract notes or owner sign-off; `uv run python tasks.py rules` lists every `UNVERIFIED` row.

## Repository layout

```
CLAUDE.md                  standing rules for anyone (or any AI) changing the code
tasks.py, Makefile         every command (make <target> or uv run python tasks.py <target>)
config/settings.yaml       all tunable settings; HYPOTHESIS vs APPROVED marked inline
config/modes/              free.yaml / paid.yaml overlays
config/rules/              effective-dated fees, taxes, lot sizes, holidays, sessions, SEBI rules
config/events/             macro calendar (RBI, Budget, CPI, results dates)
config/event_arb/          curated corporate-event terms
engine/core/               settings, rule tables, calendar, clock, secrets
engine/data/               broker adapters, recorder, instrument master, EOD files, quality checks
engine/analytics/          IV, greeks, forward, surface, order flow, microstructure, GEX
engine/shadow/             the order-flow scanner, spoof tracker, live engine runner
engine/cockpit/            the page, gateway, paper accounts, setups, strategies, research
engine/execution/          paper broker, order state machine, leg sequencer, rate limiter, slicer
engine/risk/               pre-trade gate, kill switch, stops, sizing
engine/backtest/           costs, event replay, walk-forward, Monte Carlo, deflated Sharpe
engine/strategies/         SPEC strategies (condor rules, swing, trend) and the status registry
engine/trade_mining/       reference trade-log parser and self-review
engine/sidecars/event_arb/ corporate-event screen
ops/                       logging config, Telegram alerts, Windows scheduled tasks
tests/                     pytest suite (unit, data, sidecars)
data/, reports/, logs/     gitignored: recordings, journals, reports
```
