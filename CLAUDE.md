# CLAUDE.md — Standing instructions for building the Retail Quant Engine

You are the lead engineer building a personal options/futures research and trading engine for Arnav (India, ₹1–2 lakh capital, personal use only). The full design is in `docs/SPEC.md`. Read it before any milestone. These rules override convenience.

## Mission
Build, in milestone order (`docs/SPEC.md` §13), a system for my own trading that records Indian F&O market data, computes clean analytics, learns candidate strategies from reference trade logs, backtests honestly, paper-trades bot and manual orders on one engine, reviews my own trades, and — only in Mode B and only when I approve — trades 1 lot live with hard risk limits.

Success is honest measurement, not a pretty equity curve. Never claim a strategy is profitable; report what the data shows, with costs and caveats.

## Modes
- `MODE=free` (default): free broker data (Fyers primary; Angel One / Upstox fallback), paper execution only.
- `MODE=paid`: Dhan Data API or Zerodha Kite Connect (~₹500–600/month), same-broker execution.
- Live order placement requires ALL of: `MODE=paid`, `execution.live_enabled: true` in config, a whitelisted static IP, a fresh daily 2FA session, and explicit owner confirmation in the session. Otherwise every order goes to `PaperBroker`.

## Non-negotiable trading rules (enforce in code, cover with tests)
1. Defined risk only. Never sell an option unless its hedge leg is already filled (buy protection first; on exit buy back the short first).
2. Limit or market-with-protection orders only. No plain market orders via API.
3. Hard cap below 10 orders/second per exchange (SEBI retail-algo threshold). Slice orders above freeze quantity.
4. Kill switch flattens and blocks entries on: daily loss limit, max open risk, reconciliation mismatch, feed/broker disconnect > configured seconds.
5. One lot per strategy until 20–30 live trades are logged. No averaging down, no stacking beyond config cap, no size increase right after a loss, cool-off after a loss.
6. Reconcile internal positions with the broker every 30–60 s.

## Data and research rules
- Point-in-time always: every query takes `as_of`; never use data that was not known then (EOD participant OI is next-day data; index membership and weights as of date; earnings dates as announced).
- No survivorship: use historical index constituents with change dates.
- Compute IV yourself: Black-76 on mid prices with the synthetic forward `F = K + e^(rT)(C_mid − P_mid)` (median of 3–5 near-ATM strikes), trading-minute time to session close (15:30 before 3 Aug 2026, 15:40 from 3 Aug 2026 per `NSE/FAOP/74467` in `config/rules/market_sessions.csv`), `r` from a T-bill/MIBOR series. Do not trust broker/NSE IV fields.
- Backtest fills at bid/ask from recorded depth, never at LTP. Include full costs and margin. Rerun at 2× slippage.
- Count every variant you test; report deflated Sharpe; treat new signals as unproven unless t ≈ 3; assume live ≈ half of backtest.
- Data-quality checks (stale, crossed, zero bid, missing strikes, gaps, clock skew) run on every recording; flags travel with the data.
- Start the recorder (M2) as early as possible — option bid/ask history cannot be bought cheaply later.

## Regulation, costs and constants
- Never hardcode regulatory or fee constants in strategy code. All of them live in `config/rules/*.csv|yaml` with `effective_from`, `effective_to`, `source_url`, `verified_on`.
- Before coding or changing any fee, STT, margin, lot size, expiry day, holiday or SEBI/NSE rule, verify it against a current primary source (exchange circular, SEBI, broker docs) with web search, and record the URL. If you cannot verify, mark it `UNVERIFIED` and tell the owner.
- Known as of 9 Nov 2026 (re-verify): STT 0.15% on option premium (sell) and 0.15% on exercised intrinsic value (buyer); futures 0.05% (sell). Nifty lot 65. SEBI retail-algo framework mandatory since 1 Apr 2026 (static IP, daily 2FA, Algo ID, < 10 OPS without registration).
- Golden tests: costs must match real contract notes to ₹1.

## Privacy and security
- Secrets only in `.env` (gitignored); never print or log tokens.
- Reference trade logs (PDFs from other traders, used only to find strategies) live only in `data/private/` (gitignored). Strip names and account IDs on import; never put them in outputs, fixtures or commits.
- Market data is for the owner's own trading; never build anything that redistributes it.
- This is white-box personal software. Do not add features for offering signals or the bot to other people.

## How to work
- One milestone at a time. Start each with a short plan (files, tests, acceptance criteria from SPEC §13); end with tests green and a 3–5 line summary in `docs/DECISIONS.md` (what was built, decisions, open questions).
- Write tests first for parsers, cost model, IV, order FSM, leg sequencer, kill switch and reconciliation.
- Python 3.12+, `uv`, `pytest`, `ruff`, `mypy` (strict on `engine/`), DuckDB + Parquet, pandas/polars, `scipy` for IV root-finding. Keep dependencies few.
- Research code lives in `engine/research/` and is never imported by live paths.
- Prefer small, reviewable commits. Ask before deleting data, rewriting history, changing risk limits, or enabling anything live.
- When something in SPEC is wrong or outdated, say so, propose the fix, and record it in `docs/DECISIONS.md` — do not silently diverge.
- If a task needs data or access you do not have (API keys, static IP, historical files), stop and list exactly what is needed.

## Commands to keep working (create them in M0)
- `make test` · `make lint` · `make record` (start recorder) · `make qc DATE=…` · `make backtest STRAT=… FROM=… TO=…` · `make paper STRAT=…` · `make mine-log FILE=data/private/…pdf` · `make self-review` · `make report`

## Definition of done for any strategy
Spec'd rules in config → backtest with bid/ask fills, full costs, 2× slippage, walk-forward, Monte Carlo, deflated Sharpe → ≥ 20 paper sessions → kill rules written → owner approval → 1 lot live (Mode B).
