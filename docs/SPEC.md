# Retail Quant Engine — Architecture & Build Spec

Owner: Arnav · Capital: ₹1–2 lakh · Use: personal only · Markets: NSE/BSE F&O (Nifty, Sensex, stock F&O), NSE cash/ETF
Goal: steady, risk-controlled income from defined-risk option strategies and a few documented edges, plus research tools.
Status: v1.0 spec, 9 Nov 2026. Every rate, fee and rule below must be re-verified before it is coded (see §11).

---

## 0. How to use this document

- `CLAUDE.md` (repo root) holds the standing rules. This file holds *what* to build.
- Build in the milestone order of §13. Each milestone has acceptance criteria; do not start the next one until they pass.
- §15 is the coverage table: every topic from the planning conversation, mapped to a module or marked out of scope.
- Using another agentic IDE (e.g., Antigravity)? Give it `CLAUDE.md` as its rules file and this spec unchanged.

---

## 1. Two operating modes

| | **Mode A — Free (₹0/month)** | **Mode B — Paid (~₹500–600/month)** |
|---|---|---|
| Market data | Free broker API: **Fyers** (real-time + historical included free) — primary. Fallbacks: **Shoonya (Finvasia)**, **Angel One SmartAPI** (free data), **Upstox** (free market data). | **Dhan Data API** (₹499 + 18% GST ≈ **₹589/month**) *or* **Zerodha Kite Connect** (**₹500/month**; check whether GST is added). Data and execution at the same broker. |
| Execution | **Paper only** for the bot (own `PaperBroker`). Real trades placed **manually** in any broker app. | Paper first, then **live 1-lot** via the same broker's order API. |
| Why | Zero cost; enough to record data, research, backtest, paper-trade. | One broker for data + orders → paper and live fills come from the same feed; usually better reliability/support; long-run simpler. Verify current feature differences before choosing. |
| Static IP | Not needed (no API orders). | **Needed** for API orders (SEBI retail-algo framework, mandatory since 1 Apr 2026). Extra cost, see §11. |
| Daily login | Daily OAuth/TOTP login (Fyers/Angel/Upstox tokens expire daily). | Kite: daily token. Dhan: long-lived token reported, but SEBI framework requires daily 2FA for API sessions — verify. |
| Historical intraday options | Not available free for expired contracts → **self-record from day one**; EOD from NSE bhavcopy. | Same limitation for expired intraday options at most brokers → self-record. Optional paid vendor (§11). |

Mode is a config switch: `config/modes/free.yaml` vs `config/modes/paid.yaml`. Code must run identically in both; only adapters and the `execution.live_enabled` flag differ.

Honest note: free Fyers/Angel data may be all that is needed. Mode B mainly buys simplicity (one broker for data and orders) and reliability, and is required only when the **bot** places live orders — and that also needs a static IP.

---

## 2. Repository layout

```
quant-engine/
  CLAUDE.md
  README.md
  pyproject.toml            # Python 3.12+, uv
  .env.example              # keys only as names; real .env gitignored
  config/
    settings.yaml           # global: capital, risk limits, paths, timezone Asia/Kolkata
    modes/free.yaml  modes/paid.yaml
    rules/                  # EFFECTIVE-DATED tables, each row: value, effective_from, effective_to, source_url, verified_on
      lot_sizes.csv  stt.csv  exchange_charges.csv  stamp_duty.csv  sebi_fee.csv  gst.csv
      brokerage.csv  freeze_qty.csv  expiry_calendar.csv  holidays.csv  circuit_breakers.csv
      margin_rules.yaml     # expiry-day ELM add-on, calendar-spread benefit removal, physical-delivery escalation
  engine/
    core/       clock.py (IST, trading-minutes clock, variance-time clock) calendar.py types.py events.py config.py
    data/
      adapters/  base.py fyers.py angel.py upstox.py dhan.py kite.py      # one interface
      recorder/  ws_recorder.py chain_snapshot.py tick_window.py          # → Parquet
      eod/       bhavcopy.py (own downloader; `nselib` / `jugaad-data` acceptable for EOD archives) participant_oi.py fii_dii.py delivery.py bulk_block.py ban_list.py
      reference/ instrument_master.py corporate_actions.py dividends.py
      constituents/ nifty_members.py weights.py earnings_calendar.py sector_map.py
      macro/     fred.py rbi_dbie.py ccil.py amfi.py nsdl_fpi.py mospi_calendar.py global_cues.py
      events/    event_calendar.py announcements.py announcement_nlp.py index_reviews.py ipo_lockins.py insider.py
      quality/   checks.py   # stale, crossed, zero-bid, missing strikes, gaps, clock skew
      store.py   # DuckDB over Parquet, point-in-time query helpers
    analytics/
      forward.py      # synthetic forward from put–call parity
      density.py      # risk-neutral density, probabilities, expected move
      exposures.py    # ₹ Greeks, scenario grid, GEX/vanna/charm exposure estimates
      microstructure.py # spread, microprice, depth imbalance, OFI, trade sign, impact
      technicals.py   # VWAP, opening range, ATR, relative volume, volume profile
      iv.py           # Black-76 IV on mid, trading-minute T
      surface.py      # cubic spline per expiry (v1), SVI (v2); no-arbitrage checks
      greeks.py       # delta gamma theta vega + vanna charm (v2)
      realized_vol.py # close-close, Parkinson, Garman-Klass, Yang-Zhang
      vrp.py          # IV vs subsequent RV
      term_skew.py    # weekly vs monthly ATM IV slope, 25-delta skew
      positioning.py  # PCR, max pain, OI build-up (lag-adjusted), participant-OI gamma sign, basis
      constituents.py # contribution, breadth, realized & implied correlation, event-risk score
      parity.py       # put–call parity / box-spread monitor (education + data sanity)
    strategies/
      base.py
      iron_condor.py  credit_spread.py          # core premium-selling
      directional_intraday.py                   # "Log-3-style"
      opening_breakout.py                       # "Log-2-style" ORB (needs tick data)
      calendar_spread.py  pre_earnings_straddle.py
      trend_sleeve.py                           # Nifty ETF / futures time-series momentum
      swing.py  swing_scanner.py                # 2-day to few-week swing trades: cash stocks + ATM options
      vol_target_overlay.py                     # volatility-managed sizing, applied to any strategy
    risk/
      sizing.py limits.py kill_switch.py stress.py margin.py attribution.py kill_rules.py
    execution/
      broker.py         # interface
      paper_broker.py   # bid/ask + depth fills, costs, margin simulation
      live_broker.py    # Mode B only
      order_fsm.py      # order state machine
      leg_sequencer.py  # hedge-first entry, short-first exit
      slicer.py         # freeze-quantity slicing
      reconcile.py      # broker vs internal positions every 30–60 s
      ops_limiter.py    # hard cap < 10 orders/second
      compliance.py     # static-IP check, daily login state, algo-ID passthrough
    backtest/
      replay.py fills.py costs.py walkforward.py montecarlo.py metrics.py deflated_sharpe.py stress_windows.py
    trade_mining/     # learn strategies from reference logs + review my own journal
      parse_report.py validate.py metrics.py reverse_engineer.py self_review.py report.py
    research/        # notebooks + scripts, never imported by live code
      vrp_study  momentum_iima  event_filter_ab  orb_study  log3_entry_features  pairs_hdfc_icici  parity_monitor
    sidecars/
      llm_research.py   # optional TradingAgents wrapper
      buyback_tracker.py yield_stack.py   # manual-decision helpers
  ops/
    scheduler/ (systemd timers or cron)  alerts/telegram.py  manual_ui/ (Telegram buttons + small local web page)
    journal/ export.py (trade log for F&O tax filing)  health.py  logging.yaml
  tests/  unit/  integration/  golden/ (known contract notes, known IV values, known PDF totals)
  data/   # gitignored
    raw/ parquet/ duckdb/ private/   # private = reference trade logs, never committed
  docs/  SPEC.md DECISIONS.md RUNBOOK.md DATA_DICTIONARY.md
```

---

## 3. Data layers

Record from day one. Intraday option history with bid/ask cannot be bought cheaply later.

### Core (Version 1)

**L1 Option chain snapshots** — per expiry and strike, every field the broker's "full" WebSocket mode gives: bid, ask, **bid_qty, ask_qty and every depth level offered (5 levels standard; use 20+ levels if the chosen broker offers deeper depth — verify)**, number of orders per level where provided, LTP, last traded qty, last trade time, average traded price, cumulative volume, total buy qty / total sell qty, OI, day high/low/open/close, exchange timestamp, receive timestamp.
- Cadence: every 1 min normally; **every update 09:14–09:35** for the opening-window study (§5.3). Retail WebSocket feeds are conflated snapshots, not true tick-by-tick; record every message you receive and its arrival time.
- Strike band: **max(±25 strikes, ±3σ of current expected move)**, re-centred intraday, and *keep recording any strike once it enters the band* so wings have continuous history. Include strikes newly listed intraday.
- Expiries: current weekly, next weekly, current monthly (Nifty; Sensex for §5.2).
- Same full-mode fields for Nifty/Sensex futures and the stocks in L9.
- **Not available to retail** (do not design for it): exchange tick-by-tick (TBT) order-by-order feed, order IDs, hidden quantities, other participants' cancellations. Those come only from paid exchange/colocation products.
- Do **not** store vendor/exchange IV as truth; store raw prices, compute IV in `analytics/iv.py`.

**L2 Underlying** — 1-min spot index, futures (all live months), India VIX. Futures used for basis checks only; pricing uses the synthetic forward. **Equity pre-open session (09:00–09:08)**: indicative equilibrium price and buy/sell imbalance for the top Nifty constituents → predicted index open/gap for §5.3 (also check whether the broker exposes any F&O pre-open data).

**L3 Reference** — instrument master, lot sizes (effective-dated: Nifty 25→75→65), strikes, tick sizes, expiry calendar, holidays, F&O ban list, MWPL per stock, corporate actions, ex-dividend calendar (needed for stock F&O; for index options dividends are already in the synthetic forward), **daily price bands / operating ranges for options and futures** (orders outside are rejected), **NSE daily SPAN risk-parameter files** (free; lets us compute margin locally), daily settlement and final settlement prices.

**L4 Costs & margins** — effective-dated tables (§11.2). Margin: local SPAN calculation from the NSE risk-parameter files, cross-checked against the broker margin API; model expiry-day additional ELM on short options, loss of calendar-spread benefit on expiry day, and physical-delivery margin escalation for stock options (E-4 → E). Slippage model calibrated from L8.

### Signal & context

**L5 Positioning (EOD)** — participant-wise OI and volume (client/DII/FII/Pro; index futures long/short, index calls/puts long/short, stock futures), FII derivative statistics, FII/DII cash, F&O bhavcopy (OHLC, OI, change in OI, settlement price per contract), MWPL utilisation and ban-list candidates, bulk/block deals, delivery %, short-selling and SLB (securities lending) data. *Available after the close → usable only from next session.*

**L6 Events & macro** — RBI policy, Budget, earnings dates (all 50 Nifty members), expiry days, US Fed/CPI, MSCI/FTSE review dates (Feb/May/Aug/Nov), NSE semi-annual rebalances, IPO lock-in expiries (30/90 days), insider/promoter/pledge disclosures, corporate announcements. Global cues: GIFT Nifty, US futures, US VIX + term structure, MOVE, US 10Y, DXY, crude, USDINR, US high-yield spread (FRED). India: RBI system liquidity (DBIE), G-sec yields (CCIL), AMFI monthly flows, NSDL daily FPI flows, MOSPI release calendar.

**L7 Historical expired options with bid/ask** — not free. Mode A: own recordings + NSE bhavcopy (EOD). Optional paid vendor (§11.3).

**L8 Own trade data** — every order with timestamps (signal → sent → ack → fill), fill price vs mid and vs touch, partial fills, rejections with reason, MAE/MFE per trade, P&L by strategy, attribution.

**L9 Constituents** — historical Nifty membership with change dates, monthly weights (NSE factsheets), 1-min prices for all 50, ATM IV every 5 min for top 10, earnings calendar, sector map.

**L11 Swing universe (EOD)** — daily OHLCV + delivery for F&O stocks and Nifty 500 from bhavcopy (free), adjusted for corporate actions; weekly bars; per-stock options IV history from L1/bhavcopy; results and IPO lock-in dates from L6.

**L10 Broker/operational** — allowed-strike ranges (brokers block far-OTM strikes when OI limits are hit), freeze quantities, order-rate limits, instrument-subscription limits per WebSocket, token expiry, static-IP status.

### Data quality (`data/quality/checks.py`)
Stale quotes, crossed markets, zero bids, missing strikes, recorder gaps, clock skew (NTP-synced local clock; keep both exchange and receive timestamps). Log every issue; quality flags travel with the data.

### Point-in-time rule
Every query takes an `as_of` timestamp and returns only data known then (participant OI after close, earnings dates as announced, index membership as of that date, weights as published).

---

## 4. Analytics

- **Synthetic forward**: `F = K + e^(rT) × (C_mid − P_mid)`; take the median over 3–5 strikes nearest ATM.
- **IV**: Black-76 on mids with F, solved with a bracketed root-finder (`scipy.optimize.brentq`, not Newton, which fails when vega → 0 on far-OTM strikes); return NaN with a reason when price is outside no-arbitrage bounds; `r` from a T-bill/MIBOR series; `T` in trading minutes to expiry (15:30), with a variance-time clock (weights for trading minutes vs overnight vs weekends/holidays).
- **Surface**: cubic spline in log-moneyness `k = ln(K/F)` per expiry (v1); SVI with no-arbitrage checks (v2).
- **Greeks**: full set in §4.1 (first-order in v1; vanna/charm and higher orders in v2).
- **Realized vol**: close-to-close, Parkinson, Garman-Klass, Yang-Zhang.
- **VRP**: ATM IV vs realized vol over the option's life; daily series; IV rank / percentile.
- **Term structure & skew**: weekly vs monthly ATM IV slope (inversion = stress); 25-delta put–call skew.
- **Positioning**: PCR, max pain, OI build-up classes (long/short build-up, unwinding) with OI timestamps lag-adjusted; dealer-gamma sign estimated from participant OI; futures basis.
- **Constituents**: daily contribution (weight × return); breadth (advancers, % above 50/200-DMA); realized average pairwise correlation; implied correlation
  `ρ ≈ (σ_index² − Σ wᵢ²σᵢ²) / Σᵢ≠ⱼ wᵢwⱼσᵢσⱼ`;
  **event-risk score** = Σ (weight × expected move from the stock's ATM straddle) over members reporting before expiry, plus macro events.
- **Parity / box monitor**: flags put–call parity breaks (data-error detector + teaching tool). Box spreads are *not* a strategy here: after four legs of spread, brokerage and STT they usually yield less than a liquid fund.

### 4.1 Derived-metrics catalogue (complete list to implement)

Module in brackets. Everything is computed by us from raw L1–L10 data, stored with `as_of`, and unit-tested.

**Pricing & volatility** [`forward.py`, `iv.py`, `surface.py`, `density.py`]
- Synthetic forward, implied futures basis and cost of carry, implied dividend/carry residual.
- IV per strike (bid IV, ask IV, mid IV), ATM IV, IV at fixed deltas (10/25/50) and fixed moneyness.
- Smile metrics: 25-delta risk reversal, 25-delta butterfly, smile curvature, put/call wing slopes.
- Term structure: ATM IV per expiry, weekly-vs-monthly slope, **forward volatility between expiries**.
- Model-free implied variance (VIX-style) recomputed from our own chain; check vs India VIX.
- **Expected move** from the ATM straddle (₹ and points) per expiry; implied event move (pre-event minus normal-day straddle).
- **Risk-neutral density** (Breeden–Litzenberger from the smoothed surface) → implied probability of finishing beyond any strike; probability of touch.
- IV rank and IV percentile (52-week), IV z-score, IV change intraday; IV crush measured after events.

**Greeks** [`greeks.py`, `exposures.py`]
- First order: delta, gamma, theta, vega, **rho**.
- Second/third order: **vanna, charm, vomma (volga), veta, speed, zomma, color**.
- Per-contract, per-position and portfolio; **₹ Greeks** (₹ per 1 point, per 1 vol point, per day); stock options **beta-weighted to Nifty delta**.
- Theta in trading time (variance clock), so weekend decay is not overstated.
- Scenario grid: P&L across spot ±1–8% × IV ±5 vol pts × time steps (feeds risk/stress).

**Realized volatility & correlation** [`realized_vol.py`, `constituents.py`]
- Close-to-close, Parkinson, Garman–Klass, Rogers–Satchell, Yang–Zhang; intraday realized vol from 1-min returns; volatility cone (RV percentiles by horizon).
- VRP = IV − subsequent RV, by expiry and horizon.
- Realized pairwise correlation, implied correlation, dispersion index; beta of heavyweights to Nifty.

**Open interest & positioning** [`positioning.py`, `exposures.py`]
- PCR by OI and by volume: total, per expiry, ATM ±N band; change in PCR.
- OI and change-in-OI per strike; **OI walls** (largest call/put OI strikes as resistance/support); max pain.
- Price × OI build-up classes for futures and options: long build-up, short build-up, short covering, long unwinding (OI lag-adjusted).
- Futures: basis, annualised premium, **rollover % and roll cost** near expiry, OI-weighted price.
- Participant-OI ratios: FII index-futures long/short ratio, FII net index calls/puts, client vs pro positioning, day-over-day changes.
- **Dealer-exposure estimates**: GEX (gamma exposure) by strike and total, gamma-flip level, vanna and charm exposure — sign inferred from participant OI (assumption logged; treat as an estimate).
- MWPL utilisation per stock; ban-list probability.

**Market depth & order book (microstructure)** [`microstructure.py`]
- Quoted spread (₹ and % of mid), mid, **microprice** (size-weighted mid), effective spread and realized spread on our fills.
- Depth at each level, cumulative depth, **order-book imbalance** (level 1 and multi-level), depth slope, number of orders per level when available.
- **Order-flow imbalance (OFI)** from successive best-bid/ask changes (Cont, Kukanov & Stoikov 2014).
- Trade-sign classification (quote rule / Lee–Ready) → aggressive buy/sell volume, cumulative volume delta; total buy qty vs total sell qty ratio.
- Quote update rate, quote stability, time-at-touch; liquidity score per strike (spread × depth) for strike selection and hedge-leg feasibility.
- Estimated market impact / slippage for N lots vs displayed depth (feeds PaperBroker and sizing).

**Price & volume features** [`technicals.py`]
- VWAP and bands, TWAP, opening range (1/2/5/15-min), previous-day high/low/close, gap % and gap vs GIFT Nifty, pre-open predicted open.
- ATR, intraday range vs ATR, relative volume (vs same time-of-day average), volume profile / point of control.
- Standard indicators for research (RSI, MACD, moving averages) — features only, never standalone signals.

**Breadth & index structure** [`constituents.py`]
- Advance/decline, % above 20/50/200-DMA, new highs/lows, contribution to index move, sector contribution, heavyweight concentration, event-risk score.

**Execution quality (own trades)** [`execution/` + `backtest/metrics.py`]
- Slippage vs mid and vs touch, fill ratio, partial-fill rate, rejection rate by reason, latency (signal → sent → ack → fill), MAE/MFE, edge captured vs MFE.

**Performance & robustness** [`backtest/metrics.py`, `deflated_sharpe.py`]
- Return, volatility, Sharpe, Sortino, Calmar, max drawdown and duration, profit factor, payoff, win rate, expectancy, tail ratio, skew/kurtosis of trade P&L.
- **Probabilistic Sharpe Ratio (PSR)** and **Deflated Sharpe Ratio (DSR)** (Bailey & López de Prado), minimum track-record length, Monte Carlo drawdown distribution.
- P&L attribution: delta, gamma, vega, theta, slippage, costs.

**Trade-log diagnostics** (reference logs and my own journal) — see §9.4.

---

## 5. Strategy catalogue

All strategies: one lot to start, defined risk, rules in config, every signal logged with its inputs.

### 5.1 Iron condor / credit spreads (core, Nifty)
- Expiry: weekly or monthly (test both). Short strikes at ~15–20 delta or outside the expected move; wings 200 points (Nifty), always bought first.
- Entry filters: **IV percentile high (e.g., > 50)** so premium is rich; term structure not inverted; event-risk score below threshold; skew not spiking; no RBI/Budget/election/heavyweight cluster inside the window (or shrink size).
- Wing choice: far-OTM wings are often overpriced (heavy retail/hedge demand) — pick wing width by cost vs margin benefit, and record wing cost as a % of credit.
- Exits: take profit at 50–70% of credit; stop at 1.5–2× credit or on short-strike touch; time exit before expiry-day margin/gamma risk.
- Variants: bull put spread, bear call spread (one-sided when skew/trend argues).
- **Adjustments** (delta-band rules, config):
  - Start near delta-neutral (short strikes ~±0.15 delta).
  - Gradual trend: when net position delta passes a band (e.g., |Δ| > 0.15 per lot), book the untested spread once it has decayed (~80–90%) and roll it closer to spot for extra credit.
  - Gap through a short strike at the open: no auto-adjust in the first candle; after the first 15-min candle either convert to an iron fly (roll untested side to the tested strike) or exit — config decides, default exit.
  - Whipsaw: **max 1–2 adjustments per trade**, then exit; never roll both sides inward repeatedly.
- Why stops matter: a 200-wide spread for ₹7 credit risks ₹193; 19 wins of ₹7 do not pay for one full loss — the 1.5–2× credit stop and early profit-taking are what create the edge, the wing is only for gaps.
- Reference numbers from planning (illustrative, Nifty 24,000, lot 65): 300-pt short strikes ±, 200-pt wings, ~60 pts credit → ₹3,900 max gain, ₹9,100 max loss.

### 5.2 Directional intraday — "Log-3-style" (Sensex or Nifty weekly options)
Derived from a funded trader's report (23 trades, PF 2.37, live account). Rules seen in the data:
- One directional idea per day; entries mostly 10:30–13:00; avoid the first 15 min; **no entries after 14:00** (his late trades lost).
- Scale in 2–4 one-lot tranches over minutes.
- Stop ≈ −10% of premium (range −7 to −12%); target ≈ +30% (winners +29 to +39%); trail stop to lock +7–12% once in profit.
- One same-direction re-entry after a stop, then done for the day.
- **Entry trigger unknown** → research task `log3_entry_features`: pull 1-min index candles at each entry; compute price vs VWAP, vs opening range, trend since 09:15, pullback vs breakout; fit a simple rule; validate out-of-sample.

### 5.3 Opening-range breakout — "Log-2-style" (Nifty weekly ATM options)
Derived from a funded trader: nearly all profit from entries 09:15–09:30; 1–3 min holds; ~8 pt wins vs ~4 pt losses; stacked up to 20 lots (we will not).
- Needs ≥30 recorded sessions of second-level data 09:14–09:35 before any test.
- v0 rules (hypothesis): opening range = futures high/low 09:15–09:17; break above → buy ATM CE, below → ATM PE (optionally require agreement with gap vs previous close / GIFT Nifty). Exit first of: +8 pts premium, 3-min time stop, index back inside range, −4 pts hard stop. Max 2 attempts/day; stop after first win; no entries after 09:35; **fixed lot cap**.
- Fills: buy at ask, sell at bid from recorded depth + extra opening slippage. Opening IV is elevated and tends to fall in the first hour — long options must move fast.
- Validation: run the rule on his trade days (1-min candles from broker history) and measure overlap with his entries.
- References: Crabel (1990) opening range breakout; Zarattini & Aziz (2023) 5-min ORB study (US).

### 5.4 Calendar spread
Sell expensive weekly IV, buy cheaper monthly IV when the term structure is steep; exit on normalization or before weekly expiry.

### 5.5 Pre-earnings straddle (heavyweights)
Buy ATM straddle a few days before results when implied move < the stock's historical earnings move (Gao, Xing & Zhang 2018). Exit before/at the announcement per test.

### 5.6 Trend sleeve (crash hedge for short-vol)
12-month time-series momentum on Nifty via **ETF (e.g., NIFTYBEES)** at ₹1–2L capital; Nifty futures only when capital allows (≈₹15L+ notional per lot). Purpose: tends to gain in crashes when short-vol loses (AQR trend papers).

### 5.7 Volatility-managed sizing overlay
Scale each strategy's size inversely to recent realized vol (Moreira & Muir 2017), within hard caps.

### 5.8 Swing trading (2 days to a few weeks) — cash stocks and ATM options
Scanner (`swing_scanner.py`) runs after the close on L11; trades are placed next session.
- **Breakout setup**: daily close clearly above a resistance touched ≥ 2–3 times (or below support for the bearish case) on **1.5–2× average volume**; optional tight-range (coil) filter before the break.
- **Support-bounce setup**: slow trend, pullback to multi-touch support, reversal candle.
- **Instrument choice by IV**: IV rank low → buy an ATM option (delta ≈ 0.5) with **≥ 5 trading days to expiry** (prefer next-month near expiry); IV rank high (e.g., before results) → trade the cash stock (or a debit/credit spread) instead of a naked long option.
- **Twin stops**: price stop = daily close back through the breakout/support line, capped at 20–25% of option premium; **time stop** = exit if no follow-through within 2 sessions (protects options from theta). Cash trades: 5–7% stop.
- **Sizing**: option trades risk ≤ 2–5% of capital (premium = max loss); cash trades sized from the stop distance.
- **Event filter**: no new long-option swing into results unless that is the trade (§5.5); IPO anchor lock-in dates (L6) give buy-the-dip-at-support candidates in cash only.
- **Gap risk**: swing positions carry overnight/weekend gaps — included in §6 stress tests; position limits per stock and sector.
- **Bearish view**: use puts or futures (with a stop). Retail cannot hold cash short positions overnight (only intraday; overnight shorting needs SLB borrowing).
- Practice first: ≥ 10 paper swing trades in cash and ≥ 5 in options (TradingView charts for manual review), then 1-lot live.

### 5.10 Expiry-afternoon gamma breakout (candidate, paper first)
Weekly expiry days only, 13:45–15:15. All three must hold before buying an ATM option:
1. Morning compression: 09:15–13:30 high–low range < 0.45% of spot (~100 Nifty points).
2. A completed 5-min candle closes outside the morning high/low **and** VWAP on ≥ 1.8× average 5-min volume.
3. Short-covering confirmation: negative change in OI at the breached ATM strike.
Risk: padded limit entry; 30% premium stop; book half at 1.8× entry, trail the rest at cost; 1 attempt per expiry. The claim that this fires on ~20–25% of expiries is unverified — measure it on recorded data before paper trading.

### 5.9 Research-only (not traded in v1)
- VRP measurement on Nifty from own chain data (first experiment).
- 12-month stock momentum using IIMA Indian factor data (pipeline practice: signal → OOS → costs → decay).
- Pairs trading HDFCBANK vs ICICIBANK (Gatev, Goetzmann & Rouwenhorst 2006; decayed edge, learning project).
- Index rebalance and MSCI/FTSE review flows; IPO lock-in expiries; insider-buy signals; PEAD.
- Dispersion (index vs constituent IV).

---

## 6. Risk management

- **Capital model**: ₹1–2L. Max 1 lot per strategy until 20–30 live trades are logged.
- **Per-trade max loss**: directional ≤ 2–3% of capital; defined-risk spreads ≤ 5% (a 200-pt Nifty condor ≈ ₹9k ≈ 4.5% of ₹2L).
- **Daily loss limit** and **weekly loss limit** (config) → kill switch flattens and stops new entries.
- **Never** sell an option without its hedge leg filled first. No naked shorts. No averaging down. No position stacking beyond config cap.
- Every futures position carries a hard stop (no naked leveraged futures).
- **Stock F&O expiry guard**: stock options and futures are physically settled — auto-close any stock F&O position that is ITM or near-ATM before the expiry-week margin escalation (configurable, default E-2) and never carry one into the expiry close. Index options are cash-settled.
- No far-OTM "lottery" option buying: long options need delta ≥ ~0.35–0.5 and a time stop.
- **Behavioural guards** (lessons from reference logs; tuned by my own weekly self-review): cool-off after a loss (e.g., 10–15 min), no size increase after a loss, cap trades per day (busy days lost money), block first 15 min except for the ORB strategy, block entries after 14:00 for intraday directional.
- **Vol-managed sizing** (§5.7).
- **Stress tests**: overnight gaps of 5–8% (June 4 2024 election day fell ~6% at close), market-wide circuit breakers at 10/15/20%, intraday exchange margin hikes, spreads widening with VIX (exit-cost model scales with VIX).
- **Kill rules**: before going live, define the drawdown/losing streak that would be statistically unlikely if the backtest were true; breach → stop and investigate.
- **P&L attribution** daily: delta, gamma, vega, theta, slippage, costs.
- **Diversify edges**: short-vol + trend sleeve.

---

## 7. Execution

- Interface `Broker` with `PaperBroker` and `LiveBroker`; strategies never know which.
- **Order FSM**: NEW → SENT → (PARTIAL)* → FILLED / REJECTED / CANCELLED / TIMED_OUT; handles partial fills, rejections, timeouts; idempotent client order IDs; restart-safe (on boot, rebuild state from broker + journal).
- **Leg sequencing**: entry = buy protection → confirm fill → sell short leg; exit = buy back short → confirm → sell protection. If a hedge leg is rejected (far-OTM strike blocked), step the wing inward per config, or abort.
- **Order types**: limit or market-with-protection only (no plain market orders via API).
- **Pre-trade liquidity gate**: reject an entry if quoted spread > X% of mid, depth at touch < required lots, or the price is near the day's price band; log the reason. Total buy/sell quantity is a weak, spoofable signal — never a gate.
- **Freeze-quantity slicing**; **< 10 orders/second** hard cap (keeps the bot under SEBI's registration threshold).
- **Reconciliation** every 30–60 s against broker positions; mismatch → alert + freeze new entries.
- **Kill switch**: daily loss, max open risk, disconnect > N seconds, reconciliation mismatch → flatten.
- **Liquidity-vacuum guard**: during sudden news, market makers can pull quotes for seconds (spreads blow out, depth vanishes). Detect spread/depth jumps vs the last minute; pause new entries, and for exits use stepped limit orders instead of chasing — unless a hard risk limit is breached.
- **Basket margin pre-check**: before entry, query the broker's basket/margin API for the full spread so the hedge benefit is confirmed; send legs in hedge-first order.
- **Cash-to-collateral rule**: exchanges require a share of F&O margin (50:50 split) in cash or cash-equivalents; pledged securities beyond that do not count. The margin model tracks both buckets (matters for the pledged-liquid-ETF yield stack) — verify which instruments count as cash-equivalent.
- **Compliance (live, Mode B)**: whitelisted static IP; fresh 2FA login each trading day (no overnight sessions); exchange Algo-ID tagging handled via broker; 5-year order audit trail kept by broker — also keep our own log.
- **Settlement**: index options settle on the official closing value; positions held into the close carry that risk. Near expiry, choose square-off vs expire by comparing bid-ask + brokerage vs exercise STT (both 0.15% since 1 Apr 2026).
- **PaperBroker fills**: buy at ask / sell at bid; partial fills limited by displayed depth; configurable extra slippage (opening window, high VIX); full cost model; SPAN-approximate margin with expiry-day add-ons.
- **Manual UI**: Telegram buttons and a small local web page that send *manual* orders into the same PaperBroker, so manual and bot trades share identical fills/costs.
- **Broker sandboxes are not paper trading**: Dhan's sandbox has no virtual money or market simulation; Kite's sandbox is demo credentials, LIMIT orders only; Upstox's sandbox is for integration tests. Use them only to test API plumbing (M12), never to judge a strategy.
- **External manual practice (optional)**: Sensibull virtual trading (free tier, manual options), AlgoTest simulator/forward test (free signup, no-code rule tests — quick check of §5.2 rules), Streak paper mode (Zerodha users), Neostox (3-day trial, then paid). Confirm prices are live, not delayed — useless for the opening window otherwise.

---

## 8. Backtesting & validation

- Event-driven replay of recorded Parquet (no vectorized shortcuts for options).
- Fills at bid/ask from depth; slippage model; full effective-dated costs; margin checks.
- **No look-ahead** (§3 point-in-time); **no survivorship** (historical constituents).
- Walk-forward / out-of-sample splits; rerun at **2× slippage**; stress windows: March 2020, Budget days, 4 June 2024 election, recent RBI days.
- Monte Carlo on trade order for drawdown distribution.
- Count every variation tried; report **Deflated Sharpe** (Bailey & López de Prado 2014); demand t-stat ≈ 3 for new signals (Harvey, Liu & Zhu 2016); assume live ≈ half of backtest (McLean & Pontiff 2016; Hou, Xue & Zhang 2020).
- Promotion path: backtest → paper ≥ 20–30 sessions → 1 lot live → scale only on kill-rule-safe results.

### First three experiments
1. **VRP study**: daily ATM IV vs next-week realized vol on Nifty (own data).
2. **Event filter A/B**: iron condor with vs without the event-risk filter — compare drawdowns and returns.
3. **Momentum replication** (IIMA data) — pipeline practice.

---

## 9. Strategy mining from trade logs + self-review

Purpose: learn strategies from other traders' logs, and review **my own** trading with the same metrics. This is not a scoring product for anyone else.

### 9.1 Inputs
- **Reference logs**: "Trade History" PDFs shared with me (three so far). Used only to extract strategy rules and behaviour lessons. Keep under `data/private/` (gitignored); strip names/account IDs on import.
- **My own journal**: every manual and bot trade from L8 (paper and live), plus broker contract notes/tradebooks.

### 9.2 Parser (proven on three reports; port into `parse_report.py`)
- Extract with `pdftotext -layout`; replace form-feeds `\f` with newlines (page breaks).
- Day header: `YYYY-MM-DD  True Equity: ₹X  Daily Net: ₹Y` — may contain `₹Y ₹Z (Legal)` (take first = net; second = after a daily profit cap) or multi-account `ACC: ₹a | ACC: ₹b` (sum).
- Two row layouts: (a) account, instrument, side, qty, margin%, entry, exit, ₹gross, -₹charges, ₹net on one line; (b) account on the time line, instrument line without account, prices + amounts on the next line. Times: entry time line above, exit time line below.
- Gross = first ₹ on the amount line; net = last ₹; charges = gross − net. Also parse the MFE/MAE columns (seed parser skips them).
- `QTY_MISMATCH` = partial exits → group by (account, date, entry time, instrument, side) into positions.
- **Validation (must pass)**: Σ net per day = stated Daily Net for every day; `(exit − entry) × qty × side = gross` for every row.

### 9.3 Strategy extraction (`reverse_engineer.py`)
For each reference log: classify the style (instrument, CE/PE, hold time, time of day, scaling in/out, stop and target as % of premium, re-entry behaviour); join each entry with 1-min index/option data at that timestamp (VWAP, opening range, gap, trend since 09:15, IV, depth) to infer the entry trigger; write the result as a candidate rule set for §5 and test it out-of-sample in §8. Outputs so far: §5.2 (directional, from log 3) and §5.3 (opening breakout, from log 2).

### 9.4 Diagnostics (applied to reference logs and to my own journal)
Net vs gross and charges/gross; win rate, payoff, profit factor; median hold; trades/day and P&L by activity tercile; instrument concentration; top-N trades share of net (and net ex-top-N); slippage sensitivity at 1/2/4 ticks per side; time-of-day P&L (09:15–09:30, after 14:00); exit type; margin use and max concurrent lots; revenge metrics (minutes to next trade and size change after a loss vs a win; P&L of trades within 2 min of a loss); max drawdown; % positive days. Weekly self-review report from my journal → feeds the behavioural guards in §6.

### 9.5 Lessons already taken from the three logs (anonymized)
| Log | Result | Lesson applied |
|---|---|---|
| 1 (63 days, 1,261 fills) | +₹1,45,648 net on ₹4.09L gross; one stock produced all profit; long options lost | Costs ate 64% of gross → cap trades/day; revenge trading after losses cost ~₹97k → cool-off rule; avoid first 15 min and far-OTM option buying. |
| 2 (12 days, 60 fills) | +₹38,069; nearly all from 09:15–09:30 | Opening breakout candidate (§5.3) — but no position stacking. |
| 3 (10 days, 23 fills) | +₹23,328; PF 2.37 | Directional one-idea-a-day with ~10% stop / ~30% target and trailing (§5.2). |

These figures double as regression tests for the parser.

---

## 10. Optional sidecars

- **LLM research (TradingAgents)**: run on top-10 Nifty heavyweights (`RELIANCE.NS` etc.) before results days to summarize news for the event-risk score. Provider: `openai_compatible` pointed at the owner's **Freeloader gateway**, or a free Gemini key, or local Ollama. Never trades; never in the live decision path. Only trust backtests dated after the model's training cutoff. No Claude-subscription provider exists for it. For Indian tickers, past-dated runs get fundamentals and insider data withheld, and its social/news feeds are US-centric — treat its output as a news digest only.
- **Buyback tracker** — *superseded by `docs/ADDENDUM_EVENT_ARB.md` (event-arbitrage tracker, `engine/sidecars/event_arb/`, milestone M13-EA); kept for history:* scrape announcements; for tender buybacks compute premium, record date (buy ≥1 trading day before under T+1), small-shareholder eligibility (≤ ₹2L holding), 15% reservation, historical acceptance ratios, expected value with post-buyback price fall; tax note (capital-gains treatment from 1 Apr 2026). Decision is manual.
- **Yield stack tracker** (manual): idle cash in liquid ETF pledged as margin; FD ladder across small finance banks ≤ ₹5L per bank (DICGC); arbitrage funds; record rates and dates. Advisory only.

---

## 11. Costs

### 11.1 Recurring platform costs

| Item | Mode A | Mode B | Notes |
|---|---|---|---|
| Broker data/API | ₹0 (Fyers / Angel / Upstox data) | **₹589** (Dhan Data API ₹499+GST) or **₹500** (Kite Connect; check GST) | Figures from a 25 Sep 2026 comparison — re-verify. |
| Per-order API fees | — (no API orders) | Angel: ₹20/order; Upstox: per-order API fee; others: normal brokerage | Choose broker with this in mind. |
| Static IP | — | ISP static-IP add-on or small VPS — typically a few hundred ₹/month | Required only for bot-placed live orders. A free-tier cloud VM may work if its public IP is static and whitelistable — verify. |
| Hosting | Own laptop/PC (₹0) | Same, or VPS above | Must be on 09:00–15:45 IST trading days. |
| Storage | Local disk (₹0) | Same | Chain recording ≈ a few GB/month; plan backups. |
| Alerts | Telegram Bot API (₹0) | Same | |
| Code hosting | Private GitHub repo (₹0) | Same | Never commit `.env` or `data/private/`. |
| LLM sidecar | ₹0 (Freeloader / Gemini free tier / Ollama) | Same, or paid API if wanted | Optional. |
| Claude Code | Owner's existing plan | Same | Used to build, not to run the engine. |

### 11.2 Per-trade costs (put in effective-dated tables; verify against real contract notes)
- **STT** (from 1 Apr 2026): options sell side **0.15% of premium**; exercised ITM options **0.15% of intrinsic value** (paid by buyer); futures sell side **0.05%**. History: options premium 0.0625% → 0.1% (Oct 2024) → 0.15%; exercise 0.125% → 0.15%; futures 0.0125% → 0.02% → 0.05%.
- Brokerage (discount brokers typically flat per executed order; Angel API ₹20/order).
- Exchange transaction charges, SEBI turnover fee, stamp duty (buy side), **GST 18%** on brokerage + exchange + SEBI fees. → Rates must come from current exchange/broker pages and be checked against contract notes; golden tests use real notes.

### 11.3 One-off / optional
- Historical intraday options data with bid/ask from a paid vendor (get quotes; skip in v1 — self-record instead).
- Chartered accountant for F&O tax filing (F&O is business income; ITR-3; check audit thresholds).
- Capital & margin: iron condor margin blocked per lot; keep idle margin in a pledged liquid ETF.

### 11.4 Non-money costs
Daily logins, monitoring during market hours, ~30–60 sessions of recording before ORB research, 20–30 paper sessions before live.

---

## 12. Compliance & safety

- SEBI retail algo framework (mandatory 1 Apr 2026): orders via registered broker API; whitelisted static IP; daily 2FA; exchange Algo-ID tagging; **< 10 orders/second/exchange** avoids strategy registration (we cap far below). White-box personal use only — do not offer the bot to others (would need RA registration).
- Market data: use broker data for own trading only; never redistribute. Do not scrape the live nseindia.com option chain for the engine (fragile, blocked often, and outside the data rules); EOD archive downloads are fine. Third-party paper apps using live NSE/BSE prices have been restricted (May 2024 circular, Nov 2024 advisory, Aug 2026 notification); our own engine on our broker feed avoids that.
- Secrets in `.env`; tokens never logged.
- Reference trade logs never leave `data/private/`.

---

## 13. Milestones (build order)

| # | Milestone | Mode | Acceptance criteria |
|---|---|---|---|
| M0 | Repo, config loader, effective-dated rule tables, CI, `.env.example`, logging | A | `pytest` green; rule lookup by date works; secrets excluded by `.gitignore`. |
| M1 | Trade mining & self-review (§9) | A | All three logs reconcile 100% (daily totals + price check); style summaries for logs 2–3 match §5.2–5.3; self-review runs on a sample journal; names stripped. |
| M2 | Broker data adapter (Fyers first) + instrument master + **recorder** + quality checks | A | Records chain (±25 strikes, depth) and opening ticks to Parquet for 5 sessions without gaps > 5 s; QC report produced. **Start this as early as possible.** |
| M3 | EOD + reference + calendars (bhavcopy, participant OI, ban list, holidays, expiry, lot sizes, corporate actions) | A | Point-in-time queries return only data known at `as_of`. |
| M4 | Analytics core (forward, IV, surface, Greeks, RV, VRP, term/skew) + the rest of the §4.1 catalogue in v1/v2 order (microstructure and positioning first, higher-order Greeks and density in v2) | A | IV matches an independent Black-76 implementation within 0.1 vol pt on golden cases; dashboards plot VRP daily. |
| M5 | Cost model + PaperBroker + order FSM + leg sequencer + manual UI + Telegram alerts | A | Paper fills at bid/ask; costs match two real contract notes to ₹1; manual and bot orders share the engine. |
| M6 | Backtester + validation pipeline | A | Replay of recorded days reproduces PaperBroker results; walk-forward, 2× slippage, Monte Carlo, deflated Sharpe reported. |
| M7 | Strategy v1: iron condor / credit spreads + event-risk filter (needs M9 partial) | A | Experiment 2 report; strategy paper-trades 20+ sessions. |
| M8 | Risk layer: sizing, limits, kill switch, stress, attribution, kill rules | A | Kill switch flattens in paper within 5 s on each trigger; stress report generated. |
| M9 | Context data: constituents, events, macro, announcements NLP | A | Event-risk score computed daily; survivorship test passes. |
| M10 | Strategy v2: Log-3-style directional (after entry-feature research) | A | OOS results + 20 paper sessions. |
| M11 | Strategy v3: ORB (after ≥30 recorded opening sessions) | A | Overlap study vs trader days; paper results with opening slippage. |
| M12 | Live 1-lot | **B** | Static IP whitelisted; daily login flow; OPS limiter; reconciliation; 2 weeks of live 1-lot with kill rules active. |
| M13 | Optional: swing module (§5.8), expiry gamma breakout (§5.10), calendar, pre-earnings straddle, trend sleeve, vol overlay, pairs, momentum, TradingAgents sidecar, buyback tracker, yield stack | A/B | Each with its own research note. |

---

## 14. Glossary
VRP variance/volatility risk premium · IV implied vol · RV realized vol · ORB opening-range breakout · OPS orders per second · ELM extreme loss margin · SPAN exchange margin model · PF profit factor · OOS out of sample.

---

## 15. Coverage check — every topic from the planning conversation

| Topic discussed | Where it lives | Status |
|---|---|---|
| How banks make money, leverage, credit creation, keeping ₹1 = ₹1 | — | Background education; not code. |
| Bank-style "yield stack": liquid ETF pledged as margin | §10 yield stack, §11.3 | Included (manual helper). |
| Small finance bank FDs ≤ ₹5L/bank (DICGC), arbitrage funds, corporate bonds | §10 yield stack | Included as tracker; decisions manual. |
| Credit-card float, loan against FD/shares, personal-loan trading | — | Out of scope (manual personal finance; the last two are explicitly discouraged). |
| Cash–futures arbitrage (needs lots worth several lakh) | §5.9 / §10 note | Out of scope at ₹1–2L; use arbitrage funds. |
| Buyback arbitrage (small-shareholder 15% quota, acceptance ratio, tax from Apr 2026) | §10 buyback tracker | Included. |
| Box spread & put–call parity | §4 parity monitor | Included (education/data check, not a strategy). |
| Options as insurance; bull put spread; iron condor; protective put/collar | §5.1, §6 | Included (collar deferred until capital allows hedging a lot). |
| 8 data layers + critique (depth, ±25/±3σ strikes, own IV via synthetic forward, corp actions, expiry-day ELM, physical delivery, Layers 9–10, vanna/charm, variance clock, OI lag, SVI) | §3, §4 | Included. |
| Critique's STT claim (0.125% exercise) — corrected to 0.15% both since Apr 2026 | §11.2 | Corrected. |
| SEBI algo framework: static IP, daily 2FA, Algo ID, < 10 OPS | §7, §12 | Included. |
| Order FSM, partial fills, freeze qty, reconciliation, kill switch, idempotent restarts | §7 | Included. |
| Effective-dated rules (lot sizes 25→75→65, STT history) | §2 config/rules, §11.2 | Included. |
| Risk-free rate for Black-76 | §4 | Included. |
| Recorder reliability: NTP, gaps, new strikes, subscription limits | §3 quality, L10 | Included. |
| Settlement mechanics (official close) | §7 | Included. |
| Validation: walk-forward, 2× slippage, stress windows, Monte Carlo, paper → 1 lot | §8 | Included. |
| Ops: Telegram alerts, logs, tax journal | §2 ops, §11 | Included. |
| Proven-strategy research: VRP, trend, momentum, value/quality/low-vol, carry | §5.6–5.9 | VRP/trend/momentum included; value/quality/low-vol/carry noted as research reading, not built (cash-equity factor strategies need more capital/time). |
| Event rules: PEAD, buyback drift, merger arb, index inclusion, short-term reversal, overnight returns, pre-FOMC drift | §5.9, L6 | PEAD/index flows/insider in research; merger arb, reversal, overnight, pre-FOMC listed as reading only. |
| Meta-research: McLean–Pontiff, Hou–Xue–Zhang, Harvey–Liu–Zhu, deflated Sharpe | §8 | Included. |
| Free data: Ken French, IIMA factors, AQR, Cboe, NSE archives | §3 L5–L6, §5.9 | Included (Ken French/AQR/Cboe for research only). |
| Nifty constituents: event-risk score, implied correlation, breadth, contribution, rebalancing, survivorship | §3 L9, §4 | Included. |
| Blind-spot list: term structure, skew, RV estimators, implied vs historical event move, pinning/participant-OI gamma, MSCI/FTSE, IPO lock-ins, insider/pledges, announcement NLP, AMFI/NSDL flows, delivery %, credit spreads, VIX/MOVE, RBI liquidity, US10Y/DXY, vol-managed sizing, gap stress, P&L attribution, kill rules, trend + short-vol pairing, calendar spreads, pre-earnings straddles, pairs | §3–§6 | All included. |
| "Five things first" (term/skew, event-risk score, vol sizing, gap stress, trend sleeve) | M4, M7–M9, §5.6 | Included. |
| First experiments: VRP, event-filter A/B, momentum | §8 | Included. |
| TradingAgents with Claude Code / Antigravity; Freeloader backend | §10 | Included as optional sidecar. |
| Trade-log analysis — used only to find strategies; same diagnostics for my own journal | §9 | Included (refocused: no A-book scoring). |
| Log #2 opening scalp → ORB bot | §5.3 | Included. |
| Log #3 strategy → directional bot + entry-trigger research | §5.2 | Included. |
| Paper trading for bot and manual; broker sandboxes are not paper trading; SEBI live-data rules | §1, §7 manual UI, §12 | Included. |
| Free vs ~₹500–600 broker API modes; all other costs | §1, §11 | Included. |
| Every derived metric: full Greeks (incl. rho, vomma, veta, speed, zomma, color, ₹ Greeks), IV/smile/term/forward vol, expected move, risk-neutral density, PCR, OI walls, build-up, rollover, GEX, depth/order-book (microprice, imbalance, OFI, trade sign, impact), technical features, PSR/DSR | §3 L1–L10, §4.1 | Included (added after review). |
| Data retail cannot get: exchange TBT order-by-order feed, order IDs, hidden quantity | §3 L1 | Explicitly excluded. |
| Trade-log MFE/MAE columns | §9.2 | To add to parser. |
| Beginner guide review: swing trading (breakout with volume, support bounce, ATM ≥ 5 days, twin price + time stops, IV-based stock-vs-option choice, 2–5% risk) | §5.8, L11 | Added. |
| Physical settlement of stock options; naked futures; far-OTM lottery buying | §6 | Added as hard rules. |
| Market-depth check before entry; total buy/sell qty spoofable | §7 pre-trade gate | Added. |
| Hedging with puts, credit spreads as the safe way to sell, theta acceleration, IV skew, PCR/OI walls | §4–§6 | Already covered. |
| IPO anchor unlock dips (cash only), no overnight cash shorts | §5.8, L6 | Added. |
| Clearing-corporation guarantee, option payoff basics, intrinsic + time value | — | Education; no code needed. |
| Second guide review: novation/clearing, who sells far-OTM options, collars | — / §5.1 / §6 | Education; wing-cost note and collar (deferred) covered. |
| IV percentile filter (sell when high, buy when low) | §5.1, §5.8 | Added. |
| Credit-spread blow-up math, 2× credit stop, early profit-take, pin risk | §5.1, §6 | Covered (stop 1.5–2×, profit 50–70%; stock-option pin risk via expiry guard). |
| Delta-neutral condor adjustments: roll untested side, iron fly on gap, max 1–2 rolls | §5.1 | Added. |
| Expiry-afternoon gamma breakout (compression + 5-min breakout + negative ΔOI) | §5.10 | Added as paper candidate. |
| Flash freezes / quote pulls; freak trades in illiquid strikes | §6 liquidity guard, §7 pre-trade gate | Added. |
| Basket orders, hedge-first, 50:50 cash-collateral margin rule | §6, §7 | Added. |
| Brent IV solver; time to expiry to the minute; Greeks incl. rho | §4, §4.1 | Covered. |
| nselib / jugaad-data for EOD; Shoonya API; scraping nseindia.com live chain | §1, §2, §12 | EOD libs and Shoonya added; live scraping rejected (broker feed instead). |

Known gaps deliberately left open: exact current exchange transaction charges, stamp duty and SEBI fee rates; current per-order API fees; Dhan/Kite feature differences (incl. how many depth levels each feed provides); whether a free-tier VM IP can be whitelisted. Each must be verified in M0/M5 and recorded with `source_url` and `verified_on`.
