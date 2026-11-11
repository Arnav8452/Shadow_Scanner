# Retail Quant Engine — Complete Repository Context & Technical Reference

> **Status correction (10 Nov 2026 review, see [`docs/REVIEW_2026-11-10.md`](docs/REVIEW_2026-11-10.md)):** read "BUILT" in this file as *code exists*. Only M0 meets its SPEC §13 acceptance criteria; M2 is waiting on a data-feed choice and 5 recorded sessions.
> - **Live shadow scanner (11 Nov 2026):** `tasks.py shadow-live` runs V1 watch-only on Upstox Plus 30-level depth (≤50 keys), records `depth30`, and builds opening range, previous-day levels and OI walls from real data. The thresholds are still HYPOTHESIS, and delivery, breadth and GEX inputs are not fed yet.
> - **M2 feed (11 Nov 2026):** Upstox V3 `full` mode is the recorder's data feed (free). `UpstoxFeed` now has a real, TLS-verified socket and protobuf decoding, but has not yet run live. See `docs/DECISIONS.md`.
> - **Upstox pricing (corrected 11 Nov 2026):** `full_d30` requires the Upstox Plus plan, which is free to switch to but charges ₹30 instead of ₹20 per executed order. A data-only feed therefore costs ₹0 a month ([help centre](https://upstox.com/help-center/does-the-brokerage-plan-change-with-upstox-plus-264072), [Plus page](https://upstox.com/plus/)).
> - **Demo-only commands:** `shadow-scan`, `report`, `backtest`, `paper` and `self-review` (without `FILE=`) run only on synthetic inputs and now require `DEMO=1`.
> - **Safety fixes after this file was written:** V2 never sells to open; uncovered shorts are refused; the one-lot cap is enforced; shadow-ledger fills use the bid/ask with costs from `config/rules`.

> **Purpose of this file:** Exhaustive architectural, domain, quantitative, operational, and code-level context for the **Retail Quant Engine** (`D:\Trading`). Covers every built module, every planned milestone, both operating modes (`free` and `paid`), all regulatory/cost tables, every data layer (L1–L11), every analytical signal/metric, and every trading & corporate-action strategy in the repository.

---

## 1. Mission, Scope & Core Philosophy

- **Owner & Capital Model:** Personal options, futures, and cash-equity research and trading engine built for **Arnav** (India), sized for **₹1–2 lakh capital** (configured default: `capital.inr: 200000` in [`config/settings.yaml`](file:///d:/Trading/config/settings.yaml)).
- **Target Markets:**
  - **NSE F&O:** NIFTY 50 (`NSE:NIFTY50-INDEX`, weekly & monthly options + futures), stock F&O, BANKNIFTY / FINNIFTY / MIDCPNIFTY / NIFTYNXT50 rules.
  - **BSE F&O:** SENSEX (`BSE:SENSEX-INDEX`, weekly & monthly options + futures).
  - **NSE/BSE Cash & ETFs:** Equity delivery (swing trading, corporate-event arbitrage) and index ETFs (`NIFTYBEES` for the trend sleeve, liquid ETFs for margin collateral).
- **Mandate:** White-box, strictly personal-use software. Never redistributes market data; never offers signals or bot execution to third parties (which would require SEBI Research Analyst / algo registration).
- **Epistemic Standard:** *"Success is honest measurement, not a pretty equity curve. Never claim a strategy is profitable; report what the data shows, with costs and caveats."* ([`CLAUDE.md`](file:///d:/Trading/CLAUDE.md)).

### Current Build Status by Milestone

> **Architecture & Interconnected System Guide:** See [`docs/INTERCONNECTED_SYSTEM.md`](file:///d:/Trading/docs/INTERCONNECTED_SYSTEM.md) for the complete work ledger across **M0–M13**, the **5-Layer Cross-Asset Confluence Engine** ([`engine/confluence/`](file:///d:/Trading/engine/confluence/)), switching between **Version 1 (`v1_shadow` — Human-in-the-Loop Shadow Order Book Scanner)** and **Version 2 (`v2_bot` — Automated Strategy Execution Bots)**, and the **Upstox Free API V3 (`full_d30` + 1-year `UPSTOX_ANALYTICS_TOKEN`) vs. Zerodha Paid API** comparison.

| Milestone | Scope | Status | Primary Code / Docs |
|---|---|---|---|
| **M0** | Repo skeleton, config loader (`EngineVersion`: `v1_shadow` / `v2_bot`), effective-dated rule tables, secret redaction, logging, task runner, CI, freeze slicer | **BUILT** | [`engine/core/`](file:///d:/Trading/engine/core/), [`config/rules/`](file:///d:/Trading/config/rules/), [`engine/execution/slicer.py`](file:///d:/Trading/engine/execution/slicer.py), [`tasks.py`](file:///d:/Trading/tasks.py) |
| **M1** | Reference trade-log mining (`parse_report`, contract-note `validate`, MFE/MAE `reverse_engineer`) & own-journal behavioral `self_review` | **BUILT** | [`engine/trade_mining/`](file:///d:/Trading/engine/trade_mining/), [`docs/seed/parse_report_seed.py`](file:///d:/Trading/docs/seed/parse_report_seed.py) |
| **M2** | Broker market-data adapters (`UpstoxFeed` V3 `full_d30` 30-level depth + `FyersFeed` + `SimFeed`), bitemporal instrument master, Parquet recorder, DuckDB store, QC | **BUILT** | [`engine/data/`](file:///d:/Trading/engine/data/), [`engine/data/adapters/upstox.py`](file:///d:/Trading/engine/data/adapters/upstox.py) |
| **M3** | EOD archives & reference data (NSE CM/F&O bhavcopy, Relative Delivery %, participant OI, MWPL 80%/95% ban gate, corporate actions & dividends) | **BUILT** | [`engine/data/eod/`](file:///d:/Trading/engine/data/eod/), [`engine/data/reference/corporate_actions.py`](file:///d:/Trading/engine/data/reference/corporate_actions.py) |
| **M4** | Analytics core (synthetic forward, Put-Call Parity, Black-76 IV, no-arb SVI/spline surface, Breeden–Litzenberger density, Greeks, GEX/Vanna/Charm, RV, VRP, 4-Quadrant OI, L1–L30 Microstructure, Anti-Spoofing RER & 6 PDF Order-Flow Mechanics) | **BUILT** | [`engine/analytics/`](file:///d:/Trading/engine/analytics/), [`engine/analytics/order_flow.py`](file:///d:/Trading/engine/analytics/order_flow.py), [`engine/analytics/microstructure.py`](file:///d:/Trading/engine/analytics/microstructure.py) |
| **M5** | Full F&O + Cash cost model (`fno_charges`, `equity_delivery_charges`), `PaperBroker` depth-walk simulator, `OrderFSM`, `< 8 OPS` `OpsLimiter`, 2-stage `LegSequencer`, `TelegramNotifier` | **BUILT** | [`engine/execution/`](file:///d:/Trading/engine/execution/), [`engine/backtest/costs.py`](file:///d:/Trading/engine/backtest/costs.py), [`ops/alerts/telegram.py`](file:///d:/Trading/ops/alerts/telegram.py) |
| **M6** | Event-driven replay backtester, 2× slippage stress + 50ms latency test, rolling walk-forward, 1,000-path block-bootstrap Monte Carlo, PSR & Deflated Sharpe (DSR) | **BUILT** | [`engine/backtest/`](file:///d:/Trading/engine/backtest/) |
| **M7** | Strategy v1: Defined-Risk Weekly Nifty Iron Condor (`VRP` + `GEX > 0` + Event-Risk Filter) | **BUILT** | [`engine/strategies/iron_condor.py`](file:///d:/Trading/engine/strategies/iron_condor.py) |
| **M8** | Risk layer: Empirical Quarter-Kelly (`0.25x`), pre-trade risk gate, Delta-Flip & Momentum Half-Life stops, Consecutive-Loss Circuit Breaker, `KillSwitch`, Greek/slippage P&L attribution | **BUILT** | [`engine/risk/`](file:///d:/Trading/engine/risk/) |
| **M9** | Context data: point-in-time Nifty 50 constituents & free-float breadth (no survivorship), cross-sectional dispersion, macro/earnings blackout gate | **BUILT** | [`engine/data/constituents/`](file:///d:/Trading/engine/data/constituents/), [`engine/data/events/`](file:///d:/Trading/engine/data/events/) |
| **M10** | Strategy v2: Defined-Risk Intraday Directional Spread (Futures 4-Quadrant OI + Breadth + Order-Flow confluence) | **BUILT** | [`engine/strategies/directional_intraday.py`](file:///d:/Trading/engine/strategies/directional_intraday.py) |
| **M11** | Strategy v3: 15m Opening-Range Breakout (ORB) + Anti-Spoofing RER + Option Wall Unwinding | **BUILT** | [`engine/strategies/opening_breakout.py`](file:///d:/Trading/engine/strategies/opening_breakout.py) |
| **M12** | Broker position reconciler (`reconcile_positions`) & 5-Gate Live Execution Lock (`resolve_execution_route` / `live_gate`) | **BUILT** | [`engine/execution/reconcile.py`](file:///d:/Trading/engine/execution/reconcile.py), [`engine/core/config.py`](file:///d:/Trading/engine/core/config.py) |
| **M13** | Optional sleeves (`evaluate_calendar_spread`, `evaluate_cash_swing_delivery`, `evaluate_expiry_gamma_breakout`) & Order-Flow Bot dispatcher (`dispatch_shadow_or_bot_signal`) | **BUILT** | [`engine/strategies/optional_sleeves.py`](file:///d:/Trading/engine/strategies/optional_sleeves.py), [`engine/strategies/order_flow_bots.py`](file:///d:/Trading/engine/strategies/order_flow_bots.py) |
| **M13-EA** | Corporate Event-Arbitrage Tracker (Tender Buybacks, Open Offers, Rights/REs, OFS, Demergers; NSE RSS scanner, HTML report, Telegram alerts) | **BUILT** | [`engine/sidecars/event_arb/`](file:///d:/Trading/engine/sidecars/event_arb/), [`docs/ADDENDUM_EVENT_ARB.md`](file:///d:/Trading/docs/ADDENDUM_EVENT_ARB.md) |
| **V1-SHDW** | **Version 1 ("Shadow Order Book" Scanner — Default)** & **5-Layer Interconnected Confluence Engine** (`100-Point Composite Score`, Stop-Pool Radar, Shadow Ledger, XSS-safe HTML dashboard) | **BUILT** | [`engine/shadow/`](file:///d:/Trading/engine/shadow/), [`engine/confluence/`](file:///d:/Trading/engine/confluence/), [`docs/INTERCONNECTED_SYSTEM.md`](file:///d:/Trading/docs/INTERCONNECTED_SYSTEM.md) |

---

## 2. Operating Modes: Free (`MODE=free`) vs Paid (`MODE=paid`)

Mode selection is controlled via the `MODE` environment variable (in `.env` or shell) or passed directly to `load_settings(mode=...)` in [`engine/core/config.py`](file:///d:/Trading/engine/core/config.py#L230-L252). Precedence:
1. Explicit `mode` argument to `load_settings()`
2. `MODE` in `os.environ` / `.env`
3. Default: `"free"`

`load_settings()` loads [`config/settings.yaml`](file:///d:/Trading/config/settings.yaml) and deep-merges [`config/modes/free.yaml`](file:///d:/Trading/config/modes/free.yaml) or [`config/modes/paid.yaml`](file:///d:/Trading/config/modes/paid.yaml) on top.

### 2.1 Side-by-Side Comparison

| Dimension | Mode A — Free (`MODE=free`, Default) | Mode B — Paid (`MODE=paid`) |
|---|---|---|
| **Config Overlay** | [`config/modes/free.yaml`](file:///d:/Trading/config/modes/free.yaml) | [`config/modes/paid.yaml`](file:///d:/Trading/config/modes/paid.yaml) |
| **Monthly API Cost** | **₹0 / month** | **~₹500–589 / month** (Dhan Data API: ₹499 + 18% GST ≈ ₹589/mo; or Zerodha Kite Connect: ₹500/mo; or **₹0/mo** with Upstox V3) |
| **Primary Data Broker** | **`upstox` (`UpstoxFeed` V3 `full_d30` 30-level depth + 1-year `UPSTOX_ANALYTICS_TOKEN` + live Greeks)** & `fyers` (`FyersFeed` v3 DataSocket) | `upstox` / `dhan` (or `kite` after feature/depth verification) |
| **Fallback Data Brokers** | `[fyers, shoonya, angel]` (Fyers, Finvasia Shoonya, Angel One SmartAPI) | `[upstox, fyers]` |
| **Bot Execution Broker** | `SHADOW_SCANNER_ONLY` in `v1_shadow`; `PAPER_BROKER` in `v2_bot` (`execution.live_enabled: false` enforced; setting `live_enabled: true` in `free` mode raises `ConfigError`) | `PAPER_BROKER` by default; `LIVE_BROKER` **only** when `engine_version=v2_bot` (`v2_auto`) and all 5 conditions of `live_gate()` pass |
| **Real-Money Execution** | Manual order placement in any broker app, informed by `v1_shadow` Shadow Order Book signals | Same-broker API execution (1-lot live after ≥20–30 paper sessions; supports ₹0 Upstox API or ₹0 Zerodha Personal order API) |
| **WebSocket Symbol Cap** | **Upstox V3 (free): `full_d30` (30-level depth) = 50 symbols**, `full` (5-level) = 500 symbols, `ltpc` = 5,000 symbols. **Fyers Standard (free): 100 symbols** (`symbol_caps.fyers: 100`). | Broker-dependent (typically 1,000–5,000 instruments per WebSocket) |
| **Per-Order API Fees** | None in `v1_shadow` / `PaperBroker` | Standard discount brokerage (₹20/executed F&O order at Upstox/Zerodha/Dhan/Fyers; parameterized in [`config/rules/brokerage.csv`](file:///d:/Trading/config/rules/brokerage.csv)) |
| **Static IP Requirement** | **Not required** (no live API orders) | **Mandatory** (`execution.static_ip` must be configured and whitelisted with the broker per SEBI retail-algo rules from 1 Apr 2026) |
| **Daily Authentication** | **Zero daily login** with Upstox 1-year `UPSTOX_ANALYTICS_TOKEN` (or daily browser OAuth `tasks.py fyers-login` for Fyers) | Daily 2FA session required (`session_2fa_on == today`) per SEBI retail-algo framework |
| **Expired Intraday Options** | Self-recorded to `data/parquet/` from day one + EOD NSE bhavcopy | Same (most Indian brokers do not provide historical intraday bid/ask for expired options) |

### 2.2 The 5-Gate Live Execution Lock (`live_gate`)

Implemented in [`engine/core/config.py`](file:///d:/Trading/engine/core/config.py#L254-L280), `live_gate()` returns a `LiveGate(allowed: bool, reasons: tuple[str, ...])`. **Every single order in the engine is routed to `PaperBroker` unless all 5 conditions hold simultaneously:**

1. `settings.mode is Mode.PAID` (`"MODE is not paid"`)
2. `settings.execution.live_enabled is True` (`"execution.live_enabled is false"`)
3. `bool(settings.execution.static_ip) and static_ip_whitelisted` (`"static IP not configured/whitelisted"`)
4. `session_2fa_on == today` (`"no fresh 2FA session for today"`)
5. `owner_confirmed is True` (`"owner has not confirmed live trading in this session"`)

---

## 3. Non-Negotiable Trading, Risk & Compliance Rules

Enforced in configuration validation ([`engine/core/config.py`](file:///d:/Trading/engine/core/config.py)), execution helpers ([`engine/execution/slicer.py`](file:///d:/Trading/engine/execution/slicer.py)), and covered by unit tests:

### 3.1 Hard Execution & Regulatory Rules
1. **Defined Risk Only & Leg Sequencing (`leg_sequencer.py`):**
   - Never sell an option without its protective hedge leg already filled.
   - **Entry sequence:** Buy protection wing → confirm fill → sell short leg (short quantity must never exceed *filled* hedge quantity on partial fills).
   - **Exit sequence:** Buy back short leg → confirm fill → sell protection wing.
   - If a far-OTM hedge strike is rejected by the broker (OI limit block), step the wing inward per config or abort the trade.
2. **Forbidden Plain Market Orders:**
   - `ALLOWED_ORDER_TYPES = frozenset({"LIMIT", "MARKET_PROTECTED"})` ([`engine/core/config.py`](file:///d:/Trading/engine/core/config.py#L27)).
   - Including `"MARKET"` in `execution.allowed_order_types` raises `ConfigError("plain MARKET orders are forbidden; use LIMIT or MARKET_PROTECTED")`.
3. **SEBI Retail-Algo OPS Cap (< 10 Orders/Second):**
   - SEBI's retail algo framework (mandatory since 1 Apr 2026, tracked in [`config/rules/sebi_algo_rules.csv`](file:///d:/Trading/config/rules/sebi_algo_rules.csv)) requires registration for algos exceeding `10` orders/second/exchange.
   - `config.py` looks up `retail_algo_ops_threshold` (`10`) and enforces `execution.max_orders_per_second < threshold` (configured at `5`).
4. **Freeze-Quantity Slicing ([`engine/execution/slicer.py`](file:///d:/Trading/engine/execution/slicer.py)):**
   - NSE publishes raw per-order quantity freeze limits in [`config/rules/freeze_qty.csv`](file:///d:/Trading/config/rules/freeze_qty.csv) (e.g., NIFTY = `1800` from 2 Nov 2026 per `NSE/FAOP/77189`; BANKNIFTY = `900`; FINNIFTY = `1800`; MIDCPNIFTY = `2800`; NIFTYNXT50 = `600`).
   - Because exchange circulars do not specify whether an order *at* the exact freeze number is rejected, `max_order_qty(freeze_limit, lot_size)` stays **strictly below** `freeze_limit` in whole lots:
     $$\text{max\_lots} = \left\lfloor \frac{\text{freeze\_limit} - 1}{\text{lot\_size}} \right\rfloor, \quad \text{max\_qty} = \text{max\_lots} \times \text{lot\_size}$$
   - For NIFTY (`freeze_limit = 1800`, `lot_size = 65`), `max_order_qty` = $27 \times 65 = 1755$. `slice_quantity(total_qty, freeze_limit, lot_size)` splits large orders into child orders of `[1755, 1755, ...]`.
5. **Broker Position Reconciliation (30–60s):**
   - `execution.reconcile_interval_s` is strictly validated to lie in `[30, 60]` seconds (configured at `45`).
   - Any mismatch between internal positions and broker positions freezes new entries and alerts the owner.
6. **Kill Switch Semantics (`kill_switch.py`):**
   - Triggers on: daily loss limit (`2.0%`), weekly loss limit (`5.0%`), max open risk (`10.0%`), reconciliation mismatch, or feed/broker disconnect $> 10\text{ s}$ (`disconnect_kill_after_s: 10`).
   - **Critical safety design (from [`docs/REVIEW_2026-11-09.md`](file:///d:/Trading/docs/REVIEW_2026-11-09.md) §0.2 & §4):** Never flatten blindly from *internal* state on a reconciliation mismatch (which could sell an option the broker doesn't hold and create a naked short), and never panic-flatten defined-risk positions without quotes. Always compute flatten orders from **actual broker positions**, closing short legs first, using stepped limit orders when quotes are available.

### 3.2 Capital, Sizing & Behavioural Guards (`config/settings.yaml` — Single Source of Truth)
Configured under `risk:` and `shadow_scanner:` in [`config/settings.yaml`](file:///d:/Trading/config/settings.yaml) and enforced directly by [`check_pre_trade_risk_gate`](file:///d:/Trading/engine/risk/limits.py#L40-L118) and [`quarter_kelly_fraction`](file:///d:/Trading/engine/risk/kelly.py):
- `capital.inr`: `200000` (₹2,00,000)
- `per_trade_max_loss_pct.directional`: `2.5%` (₹5,000 hard gate ceiling at ₹2L; SPEC range 2–3%)
- `per_trade_max_loss_pct.defined_risk_spread`: `5.0%` (₹10,000 hard gate ceiling at ₹2L)
- `shadow_scanner.kelly_fraction`: `0.25` (empirical rolling 100-trade Quarter-Kelly sizing, clamped to `[0.0%, 1.5%]` via `shadow_scanner.kelly_max_risk_pct: 1.5` inside the `2.5%` directional hard gate)
- `daily_loss_limit_pct`: `2.0%` (₹4,000; validated $\le$ `weekly_loss_limit_pct`)
- `weekly_loss_limit_pct`: `5.0%` (₹10,000)
- `max_open_risk_pct`: `10.0%` (₹20,000 across all open positions; plus `50%` margin utilization cap in `check_pre_trade_risk_gate`)
- `max_lots_per_strategy`: `1` (hard 1-lot cap until 20–30 live trades are logged)
- `max_trades_per_day`: `5` (prevents overtrading; derived from Log #1 where costs ate 64% of gross)
- `cooloff_after_loss_min`: `15` minutes (prevents revenge trading; plus Consecutive-Loss Circuit Breaker in [`limits.py`](file:///d:/Trading/engine/risk/limits.py): 50% size cut after 3 consecutive losses, full session halt after 4 consecutive losses)
- `block_first_minutes`: `15` minutes (blocks `09:15–09:30` entries for all strategies except ORB)
- `directional_no_entry_after`: `"14:00"` IST (derived from Log #3 where late-afternoon directional entries lost)
- `stock_fno_expiry_exit_days_before`: `2` (auto-closes ITM/near-ATM stock F&O positions at `E-2` before physical-delivery settlement and margin escalation)
- **No Lottery Buying:** Long options must have $|\Delta| \ge 0.35\text{–}0.50$ and an explicit time stop (Momentum Half-Life stop in [`stops.py`](file:///d:/Trading/engine/risk/stops.py)).
- **No Averaging Down / No Post-Loss Sizing Up:** Never add to a losing position or increase lot size immediately after a loss.

---

## 4. Complete Strategy Catalogue (F&O, Equities & Sidecars)

Every strategy requires: rules in config → backtest on recorded bid/ask depth with full effective-dated costs and 2× slippage → walk-forward & block-bootstrap Monte Carlo → Deflated Sharpe ($t \approx 3$ for new signals) → $\ge 20$ paper sessions → statistical kill rules → owner approval → 1-lot live in Mode B.

### 4.1 Strategy Summary Matrix

| ID | Strategy Name | Instrument / Universe | Holding Period | Direction / Vol | Status & Capital Feasibility (at ₹1–2L) |
|---|---|---|---|---|---|
| **§5.1** | **Iron Condor & Credit Spreads** (Bull Put / Bear Call) | NIFTY weekly & monthly options | Multi-day to expiry (exit before expiry close) | Short Vol / Defined Risk | **BUILT (`M7`, [`iron_condor.py`](file:///d:/Trading/engine/strategies/iron_condor.py)).** 200-pt wings fit 5% risk cap at ₹2L if credit $\ge 46\text{ pts}$; gated by `VRP > 0` & `GEX > 0`. |
| **§5.2** | **Directional Intraday ("Log-3-Style")** | SENSEX or NIFTY weekly options | Minutes to hours (10:30–14:00 IST) | Defined-Risk Debit Spread | **BUILT (`M10`, [`directional_intraday.py`](file:///d:/Trading/engine/strategies/directional_intraday.py)).** Requires 4-Quadrant Futures OI + Constituent Breadth + L1–L30 Order Flow (`score >= 70`). |
| **§5.3** | **Opening-Range Breakout ("Log-2-Style" ORB)** | NIFTY current-weekly ATM CE/PE | 09:30–10:30 IST (after 15m ORB `09:15–09:30`) | Long Gamma / Opening Momentum | **BUILT (`M11`, [`opening_breakout.py`](file:///d:/Trading/engine/strategies/opening_breakout.py)).** Upgraded from raw 2-min ORB to 15-min ORB (`orh_15m`/`orl_15m`) confirmed by Anti-Spoof `RER >= 0.70`, Option Wall Unwinding (`PCR_OI`), and Constituent Breadth. |
| **§5.4** | **Calendar Spread** | NIFTY weekly (short) vs monthly (long) options | Days (exit before weekly expiry) | Long Calendar / Term-Structure | **BUILT (`M13`, [`optional_sleeves.py`](file:///d:/Trading/engine/strategies/optional_sleeves.py)).** Enters when weekly IV > monthly IV (`near_iv - far_iv >= 1.5%`) outside macro blackouts. |
| **§5.5** | **Pre-Earnings Straddle** | Top Nifty heavyweight stock options | 2–5 days pre-results | Long Vol (Implied < Historical Move) | **Deferred (`REVIEW_2026-11-09.md`).** Stock F&O notional $\ge ₹5\text{L}$ makes 1-lot straddle premium ₹30–60k (15–30% of ₹2L). |
| **§5.6** | **Trend Sleeve** | `NIFTYBEES` ETF (or Nifty Futures at ₹15L+) | Weeks to months (12M TS-Momentum) | Long/Flat Trend | **BUILT (`M13`, [`optional_sleeves.py`](file:///d:/Trading/engine/strategies/optional_sleeves.py)).** Diversifier routed when EOD `LONG_ACCUMULATION` + `BULLISH_LEAD` align. |
| **§5.7** | **Volatility-Managed Sizing Overlay** | Applied across strategies | Dynamic daily/weekly | Inverse-RV / Quarter-Kelly | **BUILT (`M8`, [`kelly.py`](file:///d:/Trading/engine/risk/kelly.py), [`limits.py`](file:///d:/Trading/engine/risk/limits.py)).** Rolling 100-trade empirical Quarter-Kelly + Consecutive-Loss scale factor (`0.5x` after 3 losses). |
| **§5.8** | **Swing Trading (Delivery % + Anchored VWAP)** | Nifty 500 Cash Equities + Index ATM Options | 2 days to a few weeks | Directional Swing | **BUILT (`M13`, [`evaluate_cash_swing_delivery`](file:///d:/Trading/engine/strategies/optional_sleeves.py#L72-L133)).** Requires EOD Relative Delivery $\ge 1.35\times$ (`LONG_ACCUMULATION`) + reclaim of Anchored VWAP. |
| **§5.10** | **Expiry-Afternoon Gamma Breakout** | NIFTY / SENSEX weekly ATM CE/PE | 13:45–15:15 IST on weekly expiry day | Long Gamma / Short-Covering | **BUILT (`M13`, [`evaluate_expiry_gamma_breakout`](file:///d:/Trading/engine/strategies/optional_sleeves.py#L136-L202)).** Requires Negative Dealer GEX (`NEGATIVE_GAMMA_ACCELERATION`) + Short-Covering OI drop + `A_PLUS` Order Flow. |
| **PDF-OF** | **Order-Flow & Stop-Hunt Bots (`v1_shadow` / `v2_bot`)** | Top 50 F&O Stocks + NIFTY/SENSEX Futures & Options | Intraday (Stop-Hunt, Squeeze, Absorption, 3PM CAS) | L1–L30 Microstructure Confluence | **BUILT ([`order_flow_bots.py`](file:///d:/Trading/engine/strategies/order_flow_bots.py), [`scanner.py`](file:///d:/Trading/engine/shadow/scanner.py)).** 100-Point Composite Score; read-only in `v1_shadow`, executable in `v2_bot`. |
| **M13-EA** | **Corporate Event-Arbitrage Tracker** | NSE/BSE Cash Equities (Buybacks, Open Offers, Rights, OFS) | Days to weeks around record/tender dates | Event-Driven / Retail Quota | **BUILT (`engine/sidecars/event_arb/`).** Information & alerts only; manual execution. Sized with ₹1L sleeve & 3% account risk cap. |

---

### 4.2 Detailed Strategy Mechanics, Signals & Rules

#### 1. Iron Condor & Credit Spreads (`SPEC.md` §5.1)
- **Structure:** Sell OTM Call + OTM Put at $|\Delta| \approx 0.15\text{–}0.20$ (or outside the $1\sigma$ expected move); buy protective Call + Put wings `200` Nifty points further OTM (`100` pts at ₹1L capital). Protection wings are **always bought and filled first**.
- **One-Sided Variants:** Bull Put Spread or Bear Call Spread when 25-delta skew or the trend filter argues against a two-sided condor.
- **Entry Signals & Filters:**
  1. **IV Richness:** IV Percentile / Rank $> 50$ (using India VIX percentile / bhavcopy EOD ATM IV history in v1 until recorded chain history is 1 year deep).
  2. **Term Structure:** Weekly ATM IV $\le$ Monthly ATM IV (not inverted; inversion signals imminent stress).
  3. **Event-Risk Score:** Constituent + macro event-risk score below threshold; no RBI policy, Union Budget, election results, or heavyweight earnings cluster inside the expiry window.
  4. **Skew Stability:** 25-delta put–call skew not spiking abnormally.
  5. **Wing Cost Metric:** Record wing cost as a `% of gross credit` to avoid overpaying for illiquid far-OTM protection.
- **Exit Rules:**
  - **Profit Take:** Close at **50–70%** of initial credit received.
  - **Stop Loss:** Close at **1.5–2.0×** initial credit (or when spot touches the short strike).
  - **Time Exit:** Exit prior to expiry-day afternoon gamma risk and expiry-day 2% additional ELM margin escalation.
- **Delta-Band Adjustment Rules (Config-Driven):**
  - **Gradual Trend:** When net position $|\Delta| > 0.15$ per lot and the untested spread has decayed by **80–90%**, book profit on the untested side and roll it closer to spot for extra credit.
  - **Opening Gap Through Short Strike:** Do **not** auto-adjust during the first 15-minute candle (`09:15–09:30`). After `09:30`, either exit (default) or convert to an Iron Fly by rolling the untested spread to the tested strike.
  - **Whipsaw Cap:** Maximum **1–2 adjustments per trade**; if breached again, exit completely. Never roll both sides inward repeatedly.

#### 2. Directional Intraday — "Log-3-Style" (`SPEC.md` §5.2)
- **Origin:** Reverse-engineered from Reference Trade Log #3 (10 trading days, 23 fills, +₹23,328 net, Profit Factor 2.37, LIVE account).
- **Time & Frequency Rules:**
  - Maximum **1 directional idea per day**.
  - Primary entry window: **10:30–13:00 IST**.
  - Hard blocks: No entries in the first 15 minutes (`09:15–09:30`); **no entries after `14:00` IST**.
  - Re-entry rule: At most **1 same-direction re-entry** after an initial stop-out, then done for the day.
- **Stop, Target & Trailing Rules:**
  - Initial hard stop: **$-10\%$ of option premium** (observed range $-7\%$ to $-12\%$).
  - Profit target: **$+30\%$ of option premium** (observed winners $+29\%$ to $+39\%$).
  - Trailing stop: Once in profit, trail stop to lock in **$+7\%$ to $+12\%$** of premium.
- **Signal Research Task (`log3_entry_features`):**
  - Join Log #3 entry timestamps with 1-minute index & option candles to fit a simple, out-of-sample-validated trigger from: spot price vs VWAP, spot vs opening range, trend since `09:15`, and pullback vs breakout structure.

#### 3. Opening-Range Breakout (ORB) — "Log-2-Style" (`SPEC.md` §5.3 & [`opening_breakout.py`](file:///d:/Trading/engine/strategies/opening_breakout.py))
- **Origin:** Reverse-engineered from Reference Trade Log #2 (12 days, 60 fills, +₹38,069 net, almost entirely between `09:15` and `09:30`, 1–3 min holds; literature: Crabel 1990; Zarattini & Aziz 2023).
- **Data & Statistical Prerequisites:**
  - Requires $\ge 30$ recorded sessions of tick/every-update data in the `09:00–09:35` window.
  - Power analysis ([`docs/REVIEW_2026-11-09.md`](file:///d:/Trading/docs/REVIEW_2026-11-09.md) §3.1): With $+8\text{ pt}$ wins, $-4\text{ pt}$ losses, and $40\%$ win rate ($\mu = 0.8\text{ pts}, \sigma \approx 5.9\text{ pts}$), reaching $t = 3$ requires $n \approx (3 \times 5.9 / 0.8)^2 \approx 490$ trades, and round-trip costs (~₹62 ≈ 1 Nifty pt) heavily impact net expectancy.
- **Built M11 Implementation Rules ([`evaluate_opening_breakout`](file:///d:/Trading/engine/strategies/opening_breakout.py)):**
  - **Opening Range:** 15-minute Opening Range (`09:15:00–09:30:00` IST `orh_15m` / `orl_15m`, computed by [`compute_opening_range`](file:///d:/Trading/engine/analytics/technicals.py#L46-L70) alongside the 30-minute range; supersedes the noisy unconfirmed 2-minute v0 hypothesis).
  - **Confluence Entry Trigger:** Break above `orh_15m` (buy ATM `CE`) or below `orl_15m` (buy ATM `PE`) **only when**:
    1. **Anti-Spoofing Filter:** Real Execution Ratio $\text{RER} \ge 0.70$ (confirming the breakout level was cleared by genuine aggressive market executions rather than spoofed wall pulls).
    2. **Option Chain Wall Unwinding:** `PCR_OI >= 0.95` or trapped call-writer unwinding (`call_writers_trapped`) for longs (`PCR_OI <= 1.05` for shorts).
    3. **Constituent Breadth Confirmation:** Weighted Nifty 50 constituent breadth aligns with breakout direction.
  - **Risk & Exit Rules:** Hard stop at `20%` of option premium (`0.80 * option_ltp`) or underlying drop back inside the opening range; target at `+35%` of option premium (`1.35 * option_ltp`); Momentum Half-Life time-decay stop ([`stops.py`](file:///d:/Trading/engine/risk/stops.py)) if price stalls; strict 1-lot cap.

#### 4. Calendar Spread (`SPEC.md` §5.4 + Review Correction)
- **Signal:** Enter when the ATM IV term structure is **inverted** (front-week IV is rich relative to monthly IV, and implied forward volatility between weekly and monthly expiries is depressed) outside major macro crash events.
- **Structure:** Sell expensive current-weekly ATM option, buy cheaper current-monthly ATM option of the same strike and type.
- **Exit:** On term-structure normalization or strictly before weekly expiry day (since SEBI removes calendar-spread margin benefit on the day the near leg expires, per [`config/rules/margin_rules.yaml`](file:///d:/Trading/config/rules/margin_rules.yaml)).

#### 5. Pre-Earnings Straddle (`SPEC.md` §5.5)
- **Signal:** Compare implied event move (derived from pre-earnings ATM straddle minus normal-day straddle) against the stock's historical distribution of earnings moves (Gao, Xing & Zhang 2018). Buy ATM straddle a few days before results when implied move $<$ historical move; exit before or at the announcement.
- **Status:** Deferred at ₹1–2L capital (`REVIEW_2026-11-09.md` §2).

#### 6. Trend Sleeve (`SPEC.md` §5.6)
- **Signal:** 12-month time-series momentum on Nifty 50 (AQR trend methodology).
- **Instrument:** Cash ETF (`NIFTYBEES`) at ₹1–2L capital (Nifty Futures only when capital exceeds ₹15L+).
- **Role:** Portfolio diversifier that steps to cash during sustained bear markets when short-volatility strategies suffer drawdowns.

#### 7. Volatility-Managed Sizing Overlay (`SPEC.md` §5.7)
- **Signal:** Scales strategy notional inversely to recent realized volatility ($\text{weight}_t \propto c / \hat{\sigma}_{t}^2$ or $c / \hat{\sigma}_t$, Moreira & Muir 2017) subject to hard lot caps. Under the 1-lot cap, acts as a binary regime filter (0 lots when RV exceeds threshold, 1 lot otherwise).

#### 8. Swing Trading — Cash Equities & Index ATM Options (`SPEC.md` §5.8)
- **Scanner (`swing_scanner.py`):** Runs EOD on Layer L11 (Nifty 500 + F&O stocks daily/weekly corporate-action-adjusted OHLCV + delivery %).
- **Entry Setups:**
  1. **Resistance Breakout / Support Breakdown:** Daily close clearly beyond a horizontal level tested $\ge 2\text{–}3$ times on **$\ge 1.5\text{–}2.0\times$ 20-day average volume** (with optional pre-breakout tight-range "coil" filter).
  2. **Support Bounce:** Established uptrend pulling back to multi-touch support with a daily reversal candle.
  3. **IPO Anchor Lock-In Dip:** 30-day and 90-day post-IPO anchor lock-in expiry dates (Layer L6) creating forced-supply dips to support — **traded in cash equity only**.
- **Instrument Choice by IV Rank:**
  - **Low IV Rank:** Buy ATM option ($|\Delta| \approx 0.50$) with **$\ge 5$ trading days to expiry** (prefer next-month series when near expiry).
  - **High IV Rank (e.g., pre-results):** Trade cash stock or defined-risk spread; never buy naked high-IV options into earnings.
  - **Bearish View:** Use puts (retail cannot hold cash equity short overnight without SLB borrowing).
- **Twin Stops & Sizing:**
  - **Price Stop:** Daily close back inside the broken level, capped at **20–25% of option premium** (or **5–7%** for cash stock).
  - **Time Stop:** Exit if no follow-through occurs within **2 trading sessions** (prevents theta decay).
  - **Risk Cap:** Option premium / stop loss $\le 2\text{–}5\%$ of capital; position limits enforced per stock and sector.

#### 9. Expiry-Afternoon Gamma Breakout (`SPEC.md` §5.10)
- **Window:** Weekly expiry days only, **13:45–15:15 IST**.
- **Three Mandatory Entry Conditions:**
  1. **Morning Compression:** `09:15–13:30` high–low range $< 0.45\%$ of spot ($\approx 100$ Nifty points).
  2. **Volume Breakout:** A completed 5-minute candle closes outside both the morning high/low range **and** session VWAP on **$\ge 1.8\times$ average 5-minute volume**.
  3. **Short-Covering Confirmation:** Negative change in Open Interest ($\Delta\text{OI} < 0$) at the breached ATM strike.
- **Risk & Exit Rules:** Padded limit order entry; **30% premium hard stop**; book **50% of quantity at $1.8\times$ entry price** and trail the remaining 50% at cost; max **1 attempt per expiry**.

#### 10. Corporate Event-Arbitrage Sidecar (`docs/ADDENDUM_EVENT_ARB.md` & `engine/sidecars/event_arb/` — BUILT)
Screens and models 6 corporate-event types in Indian cash equities for a ₹1,00,000 sleeve (`event_arb.capital_inr: 100000`). **Strictly informational; all orders and tenders are manual.**
- **Tender-Offer Buyback ([`buyback.py`](file:///d:/Trading/engine/sidecars/event_arb/buyback.py)):**
  - Exploits the **15% small-shareholder reservation** under SEBI (Buy-Back of Securities) Regulations 2018 Reg 6 proviso for retail holders with record-date market value $\le ₹2,00,000$ (buffered at `max_holding_value_inr: 150000` minus `existing_shares` clubbed by PAN).
  - **Exact Formulas Implemented:**
    - Buyback Equity Fraction: $\text{size\_fraction} = \frac{N_{bb}}{N_{\text{tot}}}$ (flagged `AMBER` if $< 3\%$).
    - Reserved Shares: $R = \max\left(\frac{\text{reservation\_pct}}{100} \cdot N_{bb},\; N_{bb} \cdot \frac{H_{\text{small}}}{N_{\text{tot}}}\right)$.
    - Entitlement Ratio: $E = \frac{R}{H_{\text{small}}}$.
    - Acceptance Ratio under participation $p \in \{0.4, 0.6, 1.0\}$: $A = \min\left(1.0,\; \frac{E}{p}\right)$, evaluated for both **pessimistic** ($1.0 \times H_{\text{small, SHP}}$) and **optimistic** ($0.5 \times H_{\text{small, SHP}}$) small-holder share counts, or from `published_small_entitlement` (letter of offer), `past_small_acceptance`, or `assumed_acceptances`.
    - Worst-Case Loss per Share (for risk-budget sizing): $L_{\text{worst}} = (1 - A_{\text{pess}}) \cdot P_{\text{buy}} \cdot d_{\text{worst}} - A_{\text{pess}} \cdot (P_{bb} - P_{\text{buy}})$ where $d_{\text{worst}} = 15\%$.
    - 3-Way Position Sizing (`size_position`):
      $$q = \max\left(0,\; \min\left(\left\lfloor\frac{\text{capital}}{P_{\text{buy}}}\right\rfloor,\; \left\lfloor\frac{\min(\text{max\_holding}, 200000)}{P_{\text{buy}}}\right\rfloor - q_{\text{existing}},\; \left\lfloor\frac{\text{risk\_budget}}{L_{\text{worst}}}\right\rfloor\right)\right)$$
    - Breakeven Unaccepted Exit Price: $P_{\text{exit}}^* = P_{\text{buy}} - \frac{A \cdot (P_{bb} - P_{\text{buy}})}{1 - A}$ (for $A < 1$).
    - Cushion: $\text{cushion} = \frac{P_{\text{buy}} - P_{\text{exit}}^*}{P_{\text{buy}}}$ (flagged `RED` if pessimistic cushion $< 1\%$, `AMBER` if $< 3\%$).
    - Post-Record Drop P&L (`evaluate` across $d \in \{5\%, 10\%, 15\%\}$): Uses whole accepted shares $q_{\text{acc}} = \lfloor q \cdot A \rfloor$, unaccepted $q_{\text{unacc}} = q - q_{\text{acc}}$, and deducts full delivery charges (`buy` on $q \cdot P_{\text{buy}}$ without DP + `sell` on $q_{\text{acc}} \cdot P_{bb}$ with DP debit + `sell` on $q_{\text{unacc}} \cdot P_{\text{buy}}(1-d)$ with separate DP debit).
    - Pre-Record-Date Exit / Negative-EV Check (`tender_vs_sell_now`): Compares expected tender value $V_{\text{tender}} = A_{\text{pess}} P_{bb} + (1 - A_{\text{pess}}) P_{\text{now}}(1 - d_{\text{mid}})$ against selling immediately at $P_{\text{now}}$.
- **Open Offer ([`open_offer.py`](file:///d:/Trading/engine/sidecars/event_arb/open_offer.py)):**
  - Models SEBI SAST 26% open offers: $A = \min\left(1.0,\; \frac{\text{offer\_shares}}{p \cdot \text{public\_shares}}\right)$, gain per share $A(P_{\text{offer}} - P_{\text{buy}})$, breakeven exit, cushion, days to payment, and annualised return $(1 + r)^{365/\text{days}} - 1$.
- **Rights Issue & REs ([`rights.py`](file:///d:/Trading/engine/sidecars/event_arb/rights.py)):**
  - Computes RE intrinsic fair value $\max(0, P_{\text{share}} - P_{\text{issue}})$, Theoretical Ex-Rights Price $\text{TERP} = \frac{H \cdot P_{\text{cum}} + N \cdot P_{\text{issue}}}{H + N}$, issue-price cushion $\frac{P_{\text{share}} - P_{\text{issue}}}{P_{\text{share}}}$, and RE discount gap after round-trip costs (`round_trip_cost_pct: 0.25%`).
- **Offer for Sale ([`ofs.py`](file:///d:/Trading/engine/sidecars/event_arb/ofs.py)):**
  - Computes floor discount $\frac{P_{\text{close}} - P_{\text{floor}}}{P_{\text{close}}}$ and alerts when discount $\ge 5\%$.

---

## 5. Data Layers (L1–L11) & Analytics / Signals Catalogue

### 5.1 The 11 Data Layers (`SPEC.md` §3)

| Layer | Name | Content & Cadence | Point-in-Time (`as_of`) Rule |
|---|---|---|---|
| **L1** | **Option Chain & Futures Snapshots** | Full WebSocket fields (bid, ask, bid/ask qty, 5-level depth `bid_px_1..5`, `ask_px_1..5`, orders/level, LTP, LTQ, LTT, ATP, volume, OI, TBQ, TSQ, OHLC, upper/lower circuit, `exch_ts`, `recv_ts`). Every message in `09:00–09:35` tick window + all day for `tick_set` (spots, VIX, futures, weekly ATM $\pm 10$ strikes); 1-min snapshots for full band $\max(\pm 25\text{ strikes}, \pm 3\sigma)$ + sticky strikes. | `ts` = `recv_ns` for `capture='tick'`, snapshot timestamp for `capture='snap'`. |
| **L2** | **Underlying & Pre-Open** | 1-min Spot (`NIFTY50-INDEX`, `SENSEX-INDEX`), live Futures, `INDIAVIX-INDEX`, plus `09:00–09:08` equity/F&O pre-open equilibrium price & imbalance. | Real-time `recv_ns`. |
| **L3** | **Reference & Calendars** | Bitemporal broker instrument master (`NSE_FO`, `NSE_CM`, `BSE_FO`, `BSE_CM`), effective-dated lot sizes, freeze limits, holidays, market sessions, F&O ban list, MWPL, corporate actions, ex-dividend dates, daily price bands, NSE SPAN risk parameter files. | Stored with `fetched_at` metadata + `effective_from` / `effective_to`. |
| **L4** | **Costs & Margins** | Effective-dated CSV/YAML tables (`stt`, `brokerage`, `exchange_charges`, `sebi_fee`, `stamp_duty`, `gst`, `dp_charges`, `margin_rules`). | Looked up by trade date (or contract expiry date for `lot_sizes`). |
| **L5** | **Positioning (EOD)** | Participant-wise OI/volume (Client, DII, FII, Pro), FII derivative stats, F&O bhavcopy, cash delivery %, bulk/block deals, SLB. | Published after market close → **usable only from next trading session**. |
| **L6** | **Events & Macro** | RBI policy, Budget, Nifty 50 earnings calendar, US Fed/CPI, MSCI/FTSE reviews, NSE rebalances, IPO 30/90d anchor lock-ins, insider/pledge filings, NSE RSS corporate announcements, GIFT Nifty, VIX/MOVE/DXY/US10Y/Crude/USDINR, RBI DBIE liquidity, CCIL yields, AMFI/NSDL flows. | Tagged with announcement/publication timestamp (`published_on`). |
| **L7** | **Historical Expired Options** | Own Parquet recordings (`data/parquet/`) + EOD NSE F&O bhavcopy. | Fidelity Tier A (recorded depth) vs Tier B (bhavcopy + spread model). |
| **L8** | **Own Trade & Order Journal** | Signal → sent → ack → fill timestamps, fill price vs mid & touch, partial fills, rejection reasons, MAE/MFE, Greek + cost P&L attribution, tax-head tags. | Real-time event log. |
| **L9** | **Index Constituents** | Historical Nifty 50 membership with exact inclusion/exclusion dates (zero survivorship bias), monthly NSE factsheet weights, 1-min prices for all 50, 5-min ATM IV for top 10, sector map. | Queried as of exact historical date. |
| **L10** | **Broker Operational State** | Allowed strike ranges (OI limit blocks), symbol subscription caps (`100` on Fyers Standard), OPS rate limits, daily token expiry, static-IP status. | Runtime state. |
| **L11** | **Swing Universe (EOD)** | Corporate-action-adjusted daily & weekly OHLCV + delivery % for Nifty 500 & F&O stocks, stock option IV history, earnings & lock-in dates. | Available after EOD bhavcopy ingestion. |

### 5.2 Complete Analytics & Signals Catalogue (`SPEC.md` §4 & §4.1)

1. **Synthetic Forward & Black-76 Implied Volatility (`forward.py`, `iv.py`):**
   - **Synthetic Forward:** Computed via Put–Call Parity across the 3–5 strikes nearest ATM:
     $$F = \text{median}_{K \in \text{ATM}} \left( K + e^{r T_{\text{cal}}} (C_{\text{mid}}(K) - P_{\text{mid}}(K)) \right)$$
   - **Time Dual-Clock (`REVIEW_2026-11-09.md` §5.4):** $T_{\text{cal}}$ (calendar time) is used in $e^{r T_{\text{cal}}}$ because interest accrues daily; $T_{\text{var}}$ (trading-minute variance clock to `15:40` / `15:30` close, weighting trading minutes vs overnight vs weekend/holiday variance) is used inside Black-76 volatility $\sigma \sqrt{T_{\text{var}}}$.
   - **Root-Finder:** Bracketed Brent's method (`scipy.optimize.brentq`) on mid prices (never Newton-Raphson, which diverges when $\mathcal{V} \to 0$ on wings). Returns `NaN` with a diagnostic reason when price violates no-arbitrage bounds or within the last ~30 minutes before expiry.
2. **Volatility Surface, Smile & Risk-Neutral Density (`surface.py`, `density.py`):**
   - Log-moneyness $k = \ln(K/F)$; cubic spline per expiry in v1, SVI with butterfly/calendar no-arbitrage checks in v2.
   - Smile features: ATM IV, 10$\Delta$/25$\Delta$/50$\Delta$ IV, 25$\Delta$ Risk Reversal ($\text{IV}_{25\Delta C} - \text{IV}_{25\Delta P}$), 25$\Delta$ Butterfly ($\frac{1}{2}(\text{IV}_{25\Delta C} + \text{IV}_{25\Delta P}) - \text{IV}_{\text{ATM}}$), wing slopes, weekly-vs-monthly slope, forward volatility between expiries, VIX-style model-free implied variance, ATM straddle expected move, and Breeden–Litzenberger risk-neutral density $f(K) = e^{rT} \frac{\partial^2 C}{\partial K^2}$.
3. **Full Greeks & Scenario Stress Grid (`greeks.py`, `exposures.py`):**
   - **1st Order:** Delta ($\Delta$), Gamma ($\Gamma$), Theta ($\Theta$, in variance time), Vega ($\mathcal{V}$), Rho ($\rho$).
   - **2nd & 3rd Order:** Vanna ($\frac{\partial \Delta}{\partial \sigma}$), Charm ($-\frac{\partial \Delta}{\partial \tau}$), Vomma/Volga ($\frac{\partial \mathcal{V}}{\partial \sigma}$), Veta ($\frac{\partial \mathcal{V}}{\partial \tau}$), Speed ($\frac{\partial \Gamma}{\partial F}$), Zomma ($\frac{\partial \Gamma}{\partial \sigma}$), Color ($\frac{\partial \Gamma}{\partial \tau}$).
   - **Portfolio Aggregations:** ₹ Greeks (₹ per 1 index pt, per 1 vol pt, per trading day), beta-weighted Nifty delta for stocks, and full P&L scenario grid across $\text{Spot } \pm 1\%\text{ to }\pm 8\% \times \text{IV } \pm 5\text{ vol pts} \times \text{time steps}$.
4. **Realized Volatility & Variance Risk Premium (`realized_vol.py`, `vrp.py`):**
   - Estimators: Close-to-Close, Parkinson (high–low), Garman–Klass (OHLC), Rogers–Satchell (drift-independent), Yang–Zhang (overnight + open-to-close), plus 1-minute intraday RV and RV volatility cones.
   - $\text{VRP} = \text{IV}_{\text{ATM}} - \text{RV}_{\text{subsequent}}$ matched over the exact life of the option.
5. **Positioning, Open Interest & Dealer Exposure (`positioning.py`, `exposures.py`):**
   - PCR (by OI and volume: total, per expiry, and $\text{ATM} \pm N$ band), OI Walls (largest Call/Put OI strikes), Max Pain strike.
   - Lag-adjusted 4-quadrant Price $\times$ OI classification: **Long Build-Up** ($\Delta P > 0, \Delta\text{OI} > 0$), **Short Build-Up** ($\Delta P < 0, \Delta\text{OI} > 0$), **Short Covering** ($\Delta P > 0, \Delta\text{OI} < 0$), **Long Unwinding** ($\Delta P < 0, \Delta\text{OI} < 0$).
   - Futures annualised basis, rollover %, roll cost, FII/DII/Pro/Client net positioning ratios, and estimated Dealer GEX / Gamma-Flip / Vanna / Charm flows.
6. **Order-Book Microstructure (`microstructure.py`):**
   - Quoted spread, Effective spread, Realized spread, and **Size-Weighted Microprice**:
     $$P_{\text{micro}} = \frac{q_{\text{bid}} P_{\text{ask}} + q_{\text{ask}} P_{\text{bid}}}{q_{\text{bid}} + q_{\text{ask}}}$$
   - L1 and 5-level Order-Book Imbalance $\frac{\sum q_{\text{bid}} - \sum q_{\text{ask}}}{\sum q_{\text{bid}} + \sum q_{\text{ask}}}$, depth slope, and **Order-Flow Imbalance (OFI)** (Cont, Kukanov & Stoikov 2014) from best bid/ask price and size transitions.
   - Lee–Ready / quote-rule trade-sign classification, Cumulative Volume Delta (CVD), strike Liquidity Score ($\text{spread} \times \text{depth}$), and displayed-depth market-impact model for `PaperBroker`.
7. **Index Structure, Implied Correlation & Event-Risk Score (`constituents.py`):**
   - Constituent return contribution ($w_i r_i$), advance/decline breadth, $\%$ above 20/50/200-DMA.
   - **Implied Correlation:**
     $$\rho_{\text{imp}} \approx \frac{\sigma_{\text{index}}^2 - \sum_{i} w_i^2 \sigma_i^2}{\sum_{i \neq j} w_i w_j \sigma_i \sigma_j}$$
   - **Event-Risk Score:** $\sum_{i \in \text{reporting before expiry}} w_i \times \text{ExpectedMove}_i(\text{ATM straddle}) + \text{MacroEventWeight}$.
8. **Backtest & Trade-Mining Diagnostics (`backtest/metrics.py`, `deflated_sharpe.py`, `trade_mining/`):**
   - Sharpe, Sortino, Calmar, Profit Factor, Payoff Ratio, Expectancy, Tail Ratio, Skewness, Kurtosis, **Probabilistic Sharpe Ratio (PSR)**, and **Deflated Sharpe Ratio (DSR)** (Bailey & López de Prado 2014) adjusted for total trials in the Trial Registry.
   - Trade-mining behavioral diagnostics: Net vs Gross, Charges/Gross ratio, P&L by activity tercile, top-$N$ trade concentration, 1/2/4-tick slippage sensitivity, time-of-day P&L buckets (`09:15–09:30`, post-`14:00`), MAE/MFE capture, and **Revenge Metrics** (time to next trade and lot-size change after a loss vs after a win; P&L of trades entered within 2 minutes of a loss).

---

## 6. Deep-Dive: Built Engine Architecture & Modules

### 6.1 Core Layer (`engine/core/`)

1. **[`engine/core/config.py`](file:///d:/Trading/engine/core/config.py) — Settings, Validation & Live Gate:**
   - Defines frozen dataclasses `RiskLimits`, `ExecutionConfig`, `Settings`, `LiveGate`, and `Mode(StrEnum)` (`FREE`, `PAID`).
   - `load_settings()` deep-merges `config/settings.yaml` with `config/modes/<mode>.yaml` and validates all hard constraints (`max_orders_per_second < sebi_ops_threshold`, no `"MARKET"` in `allowed_order_types`, `30 <= reconcile_interval_s <= 60`, `daily_loss_limit_pct <= weekly_loss_limit_pct`, `HH:MM` 24-hour format for `directional_no_entry_after`, `live_enabled` requires `Mode.PAID`).
2. **[`engine/core/rules.py`](file:///d:/Trading/engine/core/rules.py) — Effective-Dated RuleBook:**
   - Loads every `.csv` and `.yaml` in [`config/rules/`](file:///d:/Trading/config/rules/) into `RuleBook` containing `RuleTable`s of `RuleRow`s.
   - Standard columns required on every table: `value`, `unit`, `effective_from` (inclusive), `effective_to` (**exclusive**, blank = open-ended), `source_url`, `verified_on`, `status` (`primary`, `secondary`, `UNVERIFIED`), `notes`. Any other column is a key column.
   - **Validation at load time:**
     - Rejects overlapping `[effective_from, effective_to)` intervals for identical keys (`RuleValidationError`).
     - Requires valid `http(s)://` `source_url` and `verified_on` date whenever `status != UNVERIFIED`.
     - Tolerates UTF-8 BOM (`utf-8-sig`) from Excel while rejecting Excel-corrupted date formats (`DD/MM/YYYY`).
   - **Lookup Safety:** `table.lookup(as_of, allow_unverified=False, **keys)` and `table.lookup_all(...)` convert timezone-aware datetimes to IST calendar dates (rejecting naive datetimes) and **raise `UnverifiedRuleError` by default** if a matching row is `UNVERIFIED`.
3. **[`engine/core/calendar.py`](file:///d:/Trading/engine/core/calendar.py) — Exchange Trading Calendar & T+1 Logic:**
   - `TradingCalendar.from_rulebook(book, exchange="NSE", segment="cm")` loads `holidays.csv`.
   - Supports `unit="holiday"` (closes a weekday) and `unit="special_session"` (opens a weekend day, e.g., Sunday 8 Nov 2026 Diwali Muhurat session).
   - **Uncovered Year Protection:** Raises `CalendarCoverageError` if queried for any calendar year whose holidays have not yet been loaded into `holidays.csv`.
   - Provides `is_trading_day()`, `previous_trading_day()`, `next_trading_day()`, `add_trading_days()`, `trading_days_between()`, and `last_cum_date(record_date, settlement_days=1)` (computes the T+1 last buy day before a corporate action record date).
4. **[`engine/core/clock.py`](file:///d:/Trading/engine/core/clock.py) — Nanosecond IST Clock & Market Sessions:**
   - `Clock` protocol with `SystemClock` (`time.time_ns()`) and deterministic `ManualClock` (`set`, `advance`) for unit tests and replay.
   - `market_session(book, exchange, segment, day)` looks up `pre_open_start`, `pre_open_end`, `normal_open`, and `normal_close` from [`config/rules/market_sessions.csv`](file:///d:/Trading/config/rules/market_sessions.csv) (automatically handling the 3 Aug 2026 NSE F&O normal close shift from `15:30` to `15:40` IST per `NSE/FAOP/74467`).
   - `recording_window(sessions, pre_buffer_min=10, post_buffer_min=5)` computes the union of all configured sessions widened by pre/post buffers (e.g., `08:50–15:45` IST).
5. **[`engine/core/env.py`](file:///d:/Trading/engine/core/env.py) & [`engine/core/logging_setup.py`](file:///d:/Trading/engine/core/logging_setup.py) — Secret Management & Log Redaction:**
   - `Secret` wraps credential strings; `__repr__` and `__str__` always return `"Secret(NAME=****)"`; the raw value is accessible only via `.reveal()`.
   - `update_dotenv()` atomically updates keys in `.env` via a `.tmp` file rename without echoing values or disturbing comments.
   - `RedactingFilter` (configured in [`ops/logging.yaml`](file:///d:/Trading/ops/logging.yaml)) scrubs every log message and exception traceback before output, masking:
     - Any environment variable value ($\ge 6$ chars) whose key matches `(TOKEN|SECRET|KEY|PASSWORD|PIN|TOTP|AUTH)`
     - `Bearer <token>` headers
     - `access_token=`, `api_key=`, `secret_key=`, `totp=`, `auth_code=`, etc. key-value pairs
     - Any JWT (`eyJ...`)

---

### 6.2 Market Data, Recorder & Quality Control Layer (`engine/data/`)

1. **Parquet Schemas ([`engine/data/schema.py`](file:///d:/Trading/engine/data/schema.py)):**
   - `QUOTES_SCHEMA` (**60 columns**):
     - Metadata & routing: `ts` (UTC microsecond `TIMESTAMPTZ` — known-at time), `capture` (`"tick"` | `"snap"`), `msg` (`"quote"` | `"depth"` | `"snap"`), `symbol`, `underlying`, `kind` (`IDX`, `EQ`, `FUT`, `CE`, `PE`), `expiry` (`date32`), `strike` (`float64`), `conn_id`, `seq`, `source`, `qc_flags` (`int32` bitmask).
     - Timestamps: `quote_recv_ts`, `depth_recv_ts`, `exch_ts`, `ltt`.
     - Top-of-book & stats: `ltp`, `ltq`, `atp`, `volume`, `oi`, `tbq`, `tsq`, `bid`, `bid_qty`, `ask`, `ask_qty`, `open`, `high`, `low`, `prev_close`, `upper_ckt`, `lower_ckt`.
     - 5-Level Order Book Depth (30 columns): `bid_px_1..5`, `bid_qty_1..5`, `bid_ord_1..5`, `ask_px_1..5`, `ask_qty_1..5`, `ask_ord_1..5`.
   - `EVENTS_SCHEMA` (**9 columns**): `ts`, `event` (`gap`, `feed_status`, `subscribe`, `unsubscribe`, `dropped`, `lot_mismatch`, `session`, `note`), `symbol`, `detail`, `start_ts`, `end_ts`, `value`, `conn_id`, `source`.
2. **Parquet Writer & Point-in-Time DuckDB Store ([`engine/data/store.py`](file:///d:/Trading/engine/data/store.py)):**
   - `DatasetWriter`: Buffers rows and flushes Zstandard-compressed Parquet parts to `<root>/<dataset>/date=YYYY-MM-DD/part-<HHMMSS>-<pid>-<seq>.parquet` via atomic `.parquet.tmp` rename.
   - `compact_day()`: Merges a day's part files at EOD into a single `day-YYYY-MM-DD.parquet` sorted by `(ts, symbol)`, verifying row counts before unlinking parts.
   - `Store`: DuckDB-backed reader enforcing point-in-time queries (`as_of` must be timezone-aware; every query filters `WHERE ts <= ?`). Provides `quotes(day, as_of=..., start=..., symbols=..., capture=...)`, `latest(day, as_of=..., underlying=..., expiry=...)` (using `QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ts DESC, seq DESC) = 1`), and `events(day)`.
3. **Bitemporal Instrument Master ([`engine/data/reference/instrument_master.py`](file:///d:/Trading/engine/data/reference/instrument_master.py)):**
   - Downloads public Fyers symbol-master CSVs (`https://public.fyers.in/sym_details/{NSE_FO,NSE_CM,BSE_FO,BSE_CM}.csv`) once per day and stores compressed bitemporal snapshots at `data/raw/instruments/fyers/YYYY-MM-DD/<segment>.csv.gz` alongside `.meta.json` (`url`, `fetched_at`, `sha256`, `bytes`, `instruments_parsed`). Falls back to `latest_snapshot()` if the network is down.
   - `classify_expiries(expiries, roles)` maps listed expiries to `current_weekly`, `next_weekly`, `current_monthly` (last listed expiry of the month, automatically handling holiday shifts like 23 Nov 2026 when 24 Nov is Guru Nanak Jayanti), and `next_monthly`.
   - `lot_size_mismatches(master, book, underlyings)` cross-checks listed contract lot sizes against `config/rules/lot_sizes.csv`.
4. **Feed Adapters ([`engine/data/adapters/`](file:///d:/Trading/engine/data/adapters/)):**
   - [`base.py`](file:///d:/Trading/engine/data/adapters/base.py): Defines `Quote`, `Depth`, `FeedStatus` (`CONNECTED`, `DISCONNECTED`, `SUBSCRIBED`, `ERROR`, `LIMIT`, `AUTH_FAILED`), and the `FeedAdapter` protocol.
   - [`fyers.py`](file:///d:/Trading/engine/data/adapters/fyers.py):
     - Daily OAuth v3 helpers: `login_url()`, `app_id_hash()` (SHA-256 of `app_id:secret`), `extract_auth_code()`, and `exchange_auth_code()` (never echoes response bodies on failure so tokens cannot leak).
     - `FyersFeed`: Wraps `fyers_apiv3.FyersWebsocket.data_ws.FyersDataSocket` (lazy-imported optional extra `uv sync --extra fyers`), subscribing `SymbolUpdate` on channel `11` and `DepthUpdate` on channel `12`.
     - Known Fyers SDK v3.2.1 quirk documented & handled: `fyers-apiv3` strips `OI` from `sf` scrip messages before invoking callbacks (which `qc_day` explicitly flags via `oi_present`), and sends `0` when circuit bands are absent (`_band` converts `0` to `None`).
   - [`sim.py`](file:///d:/Trading/engine/data/adapters/sim.py): `SimFeed` generates deterministic synthetic quotes and 5-level depth using a seeded geometric random walk for spots and the **Bachelier option pricing formula** (`bachelier(forward, strike, sigma_pts, call)`). Writes exclusively to `data/sim/parquet/` with `source='sim'`.
5. **Dynamic Strike-Band & Budget Universe Selector ([`engine/data/recorder/universe.py`](file:///d:/Trading/engine/data/recorder/universe.py)):**
   - Selects core symbols first (`NSE:NIFTY50-INDEX`, `BSE:SENSEX-INDEX`, `NSE:INDIAVIX-INDEX`, front-month futures), then option chains.
   - **Strike Band:** `band_indices()` selects at least `min_strikes_each_side + buffer_strikes` ($25 + 2 = 27$ strikes each side of ATM) widened to cover $\text{spot} \pm 3\sigma$ where $\sigma_{\text{pts}} = \text{spot} \times \text{IV} \times \sqrt{\max(T_{\text{years}}, 1/365)}$.
   - **Sticky Wings (`StickyStore`):** Any option contract that ever enters the band is persisted in `data/state/recorder_sticky.json` and continues to be recorded until expiry so wing history never has holes.
   - **Priority & Symbol-Cap Truncation:** Each option gets priority score $\text{distance\_in\_strikes} \times \text{expiry\_weight}$ (e.g., NIFTY `current_weekly: 1.0`, `next_weekly: 1.5`, `current_monthly: 2.0`; SENSEX `current_weekly: 2.0`). Contracts are packed in priority order up to `budget = adapter.max_symbols - budget_margin` (accounting for `depth_counts_separately`). Any contracts over budget are recorded in `Selection.dropped` and logged as a `"dropped"` recorder event — never silently omitted.
6. **Recorder Core & Runner ([`engine/data/recorder/core.py`](file:///d:/Trading/engine/data/recorder/core.py), [`run.py`](file:///d:/Trading/engine/data/recorder/run.py)):**
   - Single-consumer queue architecture (`queue.Queue(maxsize=500_000)`) decoupling WebSocket threads from Parquet writes.
   - **Capture Policy:**
     - Records a `"tick"` row on **every message** during `tick_windows` (`09:00–09:35` IST) for all subscribed contracts, and **all day** for `tick_set` (spots, VIX, futures, and current-weekly ATM $\pm 10$ strikes).
     - Records a `"snap"` row **every 60 seconds** on the minute boundary for every subscribed contract.
   - **Re-centring & Watchdog:** Re-centres the option strike band every `60 s` (and immediately as soon as the first spot quote arrives). If no message arrives for `watchdog_silence_s` (`20 s`) during normal session hours, triggers an automatic `adapter.restart()`.
7. **Data Quality Checks ([`engine/data/quality/checks.py`](file:///d:/Trading/engine/data/quality/checks.py)):**
   - **Row-level bitmask (`Flag(IntFlag)` in `qc_flags`):**
     - `CROSSED = 1` ($\text{bid} > \text{ask}$)
     - `LOCKED = 2` ($\text{bid} == \text{ask}$)
     - `ZERO_BID = 4` ($\text{bid} \le 0$ or missing, non-index)
     - `ZERO_ASK = 8` ($\text{ask} \le 0$ or missing, non-index)
     - `NO_QUOTE = 16` (no quote received yet)
     - `STALE = 32` ($\text{now} - \text{quote.recv\_ns} > 120\text{ s}$)
     - `NO_DEPTH = 64` (depth expected but not received yet)
     - `EXCH_TS_AHEAD = 128` ($\text{exch\_ts} > \text{recv\_ts} + 1\text{ s}$, local clock behind)
     - `LAG_HIGH = 256` ($\text{recv\_ts} - \text{exch\_ts} > 5\text{ s}$)
     - `DEPTH_DISORDER = 512` (bids not descending or asks not ascending)
     - `LTP_OUTSIDE_BAND = 1024` (LTP outside `[lower_ckt, upper_ckt]`)
   - **Daily QC Report (`qc_day` → `reports/qc/[sim/]YYYY-MM-DD.{json,md}`):**
     - Verifies M2 acceptance criteria: (1) no feed gap $> 5.0\text{ s}$ inside the session (checking open delay, inter-tick gaps, recorder gap events, and early close), (2) $0$ missing 1-minute snapshots, (3) $\ge 25$ strikes both below and above ATM across all snapshots for every chain, (4) `oi_present == True`, and (5) median clock skew/lag within `[-1.0s, +3.0s]`.

---

### 6.3 Cost Model, Trade Mining, Analytics, Execution, Risk, Shadow Scanner & Sidecar (`M1, M3–M13, V1-SHDW`)

1. **Transaction Cost Model ([`engine/backtest/costs.py`](file:///d:/Trading/engine/backtest/costs.py)):**
   - `equity_delivery_charges(...)` and `fno_charges(book, on, segment, side, turnover_inr, broker="upstox", exchange="NSE", num_orders=1, is_exercised=False, allow_unverified=False)` compute exact `ChargeBreakdown(brokerage, stt, exchange, sebi, stamp, gst, dp, unverified)` purely from `config/rules/`.
   - Tracks any table that supplied an `UNVERIFIED` row in `ChargeBreakdown.unverified`.
2. **Upstox API V3 30-Level Depth Adapter ([`engine/data/adapters/upstox.py`](file:///d:/Trading/engine/data/adapters/upstox.py)):**
   - `UpstoxFeed`, `parse_upstox_v3_feed()`, `build_upstox_sub_frame()`, and `resolve_upstox_token()` support `full_d30` (30-level market depth for up to 50 symbols), `full` (5-level depth for up to 500 symbols), and `ltpc` (up to 5,000 symbols), streaming native Option Greeks (`iv, delta, gamma, theta, vega, rho`) and Closing Auction (`iep`, `iiqTotal`) via the 1-year read-only `UPSTOX_ANALYTICS_TOKEN`.
3. **EOD Pipeline, Reference Data, Constituents & Events (`M3, M9` — [`engine/data/eod/`](file:///d:/Trading/engine/data/eod/), [`engine/data/constituents/`](file:///d:/Trading/engine/data/constituents/), [`engine/data/events/`](file:///d:/Trading/engine/data/events/)):**
   - [`bhavcopy.py`](file:///d:/Trading/engine/data/eod/bhavcopy.py): Parses NSE CM & F&O Bhavcopies, computes 20-day Relative Delivery % (`DeliveryBias`), and enforces MWPL `80%` early-warning / `95%` ban gates (`MWPLStatus`).
   - [`participant_oi.py`](file:///d:/Trading/engine/data/eod/participant_oi.py): Parses NSE `fao_participant_oi` CSVs (`FII`, `DII`, `PRO`, `CLIENT`) and computes `ParticipantRegime`.
   - [`corporate_actions.py`](file:///d:/Trading/engine/data/reference/corporate_actions.py): Present-value discrete dividend strip (`pv_discrete_dividends`) and split/bonus strike/lot adjuster (`adjust_strike_and_lot`).
   - [`nifty_members.py`](file:///d:/Trading/engine/data/constituents/nifty_members.py) & [`event_calendar.py`](file:///d:/Trading/engine/data/events/event_calendar.py): Point-in-time Nifty 50 constituents (`point_in_time_constituents`), free-float-weighted breadth (`compute_constituent_breadth`), and macro/earnings blackout gate (`evaluate_event_risk`).
4. **Analytics Core & L1–L30 Order-Flow Engine (`M4` — [`engine/analytics/`](file:///d:/Trading/engine/analytics/)):**
   - [`forward.py`](file:///d:/Trading/engine/analytics/forward.py), [`parity.py`](file:///d:/Trading/engine/analytics/parity.py), [`iv.py`](file:///d:/Trading/engine/analytics/iv.py), [`surface.py`](file:///d:/Trading/engine/analytics/surface.py), [`density.py`](file:///d:/Trading/engine/analytics/density.py), [`greeks.py`](file:///d:/Trading/engine/analytics/greeks.py), [`exposures.py`](file:///d:/Trading/engine/analytics/exposures.py), [`realized_vol.py`](file:///d:/Trading/engine/analytics/realized_vol.py), [`vrp.py`](file:///d:/Trading/engine/analytics/vrp.py), [`positioning.py`](file:///d:/Trading/engine/analytics/positioning.py), [`technicals.py`](file:///d:/Trading/engine/analytics/technicals.py), [`microstructure.py`](file:///d:/Trading/engine/analytics/microstructure.py), and [`order_flow.py`](file:///d:/Trading/engine/analytics/order_flow.py) (Anti-Spoofing RER, Resting Wall Age $\ge 500\text{ ms}$, Iceberg Reload $\text{RER} > 1.25$, L6–L25 Air-Pocket ratio $< 0.55$, and all 6 PDF Order-Flow Mechanics).
5. **Version 1 Shadow Order Book Scanner & 5-Layer Confluence Engine ([`engine/shadow/`](file:///d:/Trading/engine/shadow/), [`engine/confluence/`](file:///d:/Trading/engine/confluence/)):**
   - [`ShadowOrderBookScanner`](file:///d:/Trading/engine/shadow/scanner.py): Scores up to 50 instruments on the **100-Point Composite Confidence Formula**, computes microstructure Entry/SL/T1/T2, forward-tests every signal in the Shadow Ledger, and renders XSS-safe HTML dashboards ([`report.py`](file:///d:/Trading/engine/shadow/report.py)).
   - [`evaluate_interconnected_confluence`](file:///d:/Trading/engine/confluence/engine.py): Synthesizes all 5 layers into [`InterconnectedConfluencePacket`](file:///d:/Trading/engine/confluence/engine.py#L48-L90) with automatic vehicle selection (`recommended_vehicle`).
6. **Execution, Risk, Strategies, Trade Mining & Backtester (`M1, M5–M8, M10–M13`):**
   - [`engine/execution/`](file:///d:/Trading/engine/execution/): [`order_fsm.py`](file:///d:/Trading/engine/execution/order_fsm.py), [`ops_limiter.py`](file:///d:/Trading/engine/execution/ops_limiter.py), [`leg_sequencer.py`](file:///d:/Trading/engine/execution/leg_sequencer.py) (Hedge-First Entry, Short-First Exit, and `emergency_flatten_filled_hedges` using `MARKET_PROTECTED`), [`paper_broker.py`](file:///d:/Trading/engine/execution/paper_broker.py), and [`reconcile.py`](file:///d:/Trading/engine/execution/reconcile.py).
   - [`engine/risk/`](file:///d:/Trading/engine/risk/): [`kelly.py`](file:///d:/Trading/engine/risk/kelly.py), [`limits.py`](file:///d:/Trading/engine/risk/limits.py), [`stops.py`](file:///d:/Trading/engine/risk/stops.py) (Delta-Flip & Momentum Half-Life stops), [`kill_switch.py`](file:///d:/Trading/engine/risk/kill_switch.py), and [`attribution.py`](file:///d:/Trading/engine/risk/attribution.py).
   - [`engine/strategies/`](file:///d:/Trading/engine/strategies/): [`iron_condor.py`](file:///d:/Trading/engine/strategies/iron_condor.py), [`directional_intraday.py`](file:///d:/Trading/engine/strategies/directional_intraday.py), [`opening_breakout.py`](file:///d:/Trading/engine/strategies/opening_breakout.py), [`optional_sleeves.py`](file:///d:/Trading/engine/strategies/optional_sleeves.py), and [`order_flow_bots.py`](file:///d:/Trading/engine/strategies/order_flow_bots.py).
   - [`engine/trade_mining/`](file:///d:/Trading/engine/trade_mining/) & [`engine/backtest/`](file:///d:/Trading/engine/backtest/): [`parse_report.py`](file:///d:/Trading/engine/trade_mining/parse_report.py), [`validate.py`](file:///d:/Trading/engine/trade_mining/validate.py), [`self_review.py`](file:///d:/Trading/engine/trade_mining/self_review.py), [`reverse_engineer.py`](file:///d:/Trading/engine/trade_mining/reverse_engineer.py), [`replay.py`](file:///d:/Trading/engine/backtest/replay.py), [`walkforward.py`](file:///d:/Trading/engine/backtest/walkforward.py), [`montecarlo.py`](file:///d:/Trading/engine/backtest/montecarlo.py), and [`deflated_sharpe.py`](file:///d:/Trading/engine/backtest/deflated_sharpe.py).
7. **Event-Arbitrage Sidecar ([`engine/sidecars/event_arb/`](file:///d:/Trading/engine/sidecars/event_arb/)):**
   - [`sources.py`](file:///d:/Trading/engine/sidecars/event_arb/sources.py), [`screen.py`](file:///d:/Trading/engine/sidecars/event_arb/screen.py), [`report.py`](file:///d:/Trading/engine/sidecars/event_arb/report.py), and [`run.py`](file:///d:/Trading/engine/sidecars/event_arb/run.py) screen SEBI Tender Buybacks, Open Offers, Rights/REs, and OFS, writing HTML reports to `reports/event_arb/YYYY-MM-DD.html` and sending stateful Telegram alerts.

---

## 7. Effective-Dated Rule Tables (`config/rules/`)

All regulatory, tax, exchange, and broker constants live in [`config/rules/`](file:///d:/Trading/config/rules/) (16 tables; validated via `uv run python tasks.py rules`):

| Table File | Key Columns | Current Values (as of Nov 2026) | Verification Status |
|---|---|---|---|
| [`stt.csv`](file:///d:/Trading/config/rules/stt.csv) | `segment, leg` | Options `sell_premium`: **0.15%** (was 0.1% pre-Apr 2026); Options `exercise_intrinsic`: **0.15%** (was 0.125% `UNVERIFIED`); Futures `sell`: **0.05%** (was 0.02%); Equity delivery `buy`/`sell`: **0.1%** | `secondary` (except pre-Apr 2026 exercise `UNVERIFIED`) |
| [`exchange_charges.csv`](file:///d:/Trading/config/rules/exchange_charges.csv) | `exchange, segment, component` | NSE Options `transaction`: **0.03503%** + `ipft`: **0.0005%** (`UNVERIFIED`); NSE Futures `transaction`: **0.00173%** + `ipft`: **0.0001%** (`UNVERIFIED`); BSE Options: **0.0325%**; BSE Futures: **0%**; NSE Delivery: **0.00307%**; BSE Delivery: **0.00375%** | `secondary` (`ipft` rows `UNVERIFIED`) |
| [`sebi_fee.csv`](file:///d:/Trading/config/rules/sebi_fee.csv) | `segment` | `fno`: **₹10 / crore**; `equity`: **₹10 / crore** | `secondary` |
| [`stamp_duty.csv`](file:///d:/Trading/config/rules/stamp_duty.csv) | `segment, side` | Buy side: Options **0.003%**, Futures **0.002%**, Equity delivery **0.015%**; Sell side: **0%** for all segments | `secondary` |
| [`gst.csv`](file:///d:/Trading/config/rules/gst.csv) | `base` | `brokerage_exchange_sebi`: **18%** | `secondary` |
| [`brokerage.csv`](file:///d:/Trading/config/rules/brokerage.csv) | `broker, segment, component` | **Upstox** Options `flat_per_order`: **₹20** (`secondary`); **Upstox** Futures: lower of **0.03%** or **₹20** (`secondary`); **Upstox** Equity Delivery: lower of **2.5%** or **₹20** (`secondary`); **Zerodha** Options: **₹20**; **Zerodha** Futures: lower of **0.03%** or **₹20**; **Zerodha** Equity Delivery: **₹0** (`secondary`); Fyers/Dhan/Angel/Paper Options: **₹20** (`UNVERIFIED`) | **Upstox & Zerodha `secondary`**; others `UNVERIFIED` |
| [`dp_charges.csv`](file:///d:/Trading/config/rules/dp_charges.csv) | `broker, depository` | `zerodha, CDSL`: **₹15.34** per scrip per debit day (incl. GST) | `secondary` |
| [`lot_sizes.csv`](file:///d:/Trading/config/rules/lot_sizes.csv) | `exchange, underlying` (lookup by **expiry**) | NSE from 31 Dec 2025 (`FAOP70616`): `NIFTY` **65** (was 75), `BANKNIFTY` **30**, `FINNIFTY` **60**, `MIDCPNIFTY` **120**, `NIFTYNXT50` **25**. BSE `SENSEX`: **20** (to 28 Jan 2027), **25** (from 28 Jan 2027, `UNVERIFIED`) | NSE post-Dec 2025 `primary`; BSE & older NIFTY `UNVERIFIED` |
| [`freeze_qty.csv`](file:///d:/Trading/config/rules/freeze_qty.csv) | `exchange, underlying` | NSE raw freeze limits from 2 Nov 2026 (`FAOP77189`): `NIFTY` **1800** (was 3510), `BANKNIFTY` **900**, `FINNIFTY` **1800**, `MIDCPNIFTY` **2800**, `NIFTYNXT50` **600** | `primary` |
| [`expiry_calendar.csv`](file:///d:/Trading/config/rules/expiry_calendar.csv) | `exchange, underlying, kind` | From 1 Sep 2025: NSE `NIFTY` weekly=`TUE`, monthly=`LAST_TUE`; `BANKNIFTY`/`FINNIFTY`/`MIDCPNIFTY` monthly=`LAST_TUE`; BSE `SENSEX` weekly=`THU`, monthly=`LAST_THU` | `secondary` |
| [`holidays.csv`](file:///d:/Trading/config/rules/holidays.csv) | `exchange, segment` | 16 NSE `cm` holidays in 2026 (`CMTR71775` + `CMTR72260`) + 1 `special_session` (Sunday `2026-11-08` Muhurat trading) | `primary` |
| [`market_sessions.csv`](file:///d:/Trading/config/rules/market_sessions.csv) | `exchange, segment, session` | NSE `fo` (`FAOP74467`, from 3 Aug 2026): pre-open `09:00–09:08`, normal `09:15–15:40` (was `15:30`), close VWAP `15:10–15:40`, trade mod end `16:15`. NSE `cm`: `09:15–15:30` (`UNVERIFIED`). BSE `fo`: `09:15–15:40` (`UNVERIFIED`) | NSE `fo` `primary`; `cm` & BSE `fo` `UNVERIFIED` |
| [`sebi_algo_rules.csv`](file:///d:/Trading/config/rules/sebi_algo_rules.csv) | `rule` | `retail_algo_ops_threshold`: **10** orders/sec/exchange (from 1 Apr 2026) | `UNVERIFIED` |
| [`corporate_action_rules.csv`](file:///d:/Trading/config/rules/corporate_action_rules.csv) | `rule` | `buyback_small_shareholder_reservation`: **15%** (`primary`); `small_shareholder_max_value`: **₹2,00,000** (`primary`); `equity_settlement_days`: **1** (`UNVERIFIED`) | Reg 6 & Reg 2(1)(n) `primary`; T+1 `UNVERIFIED` |
| [`tax_rules.csv`](file:///d:/Trading/config/rules/tax_rules.csv) | `item` | `buyback_tax_head`: `capital_gains` (from 1 Apr 2026, was `deemed_dividend`); `stcg_listed_equity`: **20%**; `ltcg_listed_equity`: **12.5%** | `UNVERIFIED` |
| [`circuit_breakers.csv`](file:///d:/Trading/config/rules/circuit_breakers.csv) | `scope, level` | `market_wide` levels 1 / 2 / 3: **10% / 15% / 20%** index move | `UNVERIFIED` |
| [`margin_rules.yaml`](file:///d:/Trading/config/rules/margin_rules.yaml) | `name` | `expiry_day_elm_short_options` (+2% ELM), `calendar_spread_benefit_removed_on_expiry_day`, `physical_delivery_margin_escalation` (`E-4: 10%, E-3: 25%, E-2: 45%, E-1: 70%`), `cash_collateral_split` (50% min cash) | `UNVERIFIED` |

---

## 8. CLI Commands, Workflows & Developer Conventions

### 8.1 Task Runner (`tasks.py` / `Makefile`)
Run via `uv run python tasks.py <target> [KEY=VALUE ...]` (or `make <target>` where `make` is installed):

| Command | Parameters | Description |
|---|---|---|
| `uv run python tasks.py test` | — | Runs the full `pytest` suite (`165` unit tests across `tests/unit`, `tests/data`, `tests/sidecars`). |
| `uv run python tasks.py lint` | — | Runs `ruff check .` and strict `mypy` across all 98 source files in `engine/` and `ops/`. |
| `uv run python tasks.py fmt` | — | Auto-fixes lint/formatting issues with `ruff check --fix .`. |
| `uv run python tasks.py rules` | — | Validates all 16 rule tables in `config/rules/` and prints all `UNVERIFIED` rows. |
| `uv run python tasks.py shadow-scan` | `[DATE=YYYY-MM-DD] [VERSION=v1_shadow\|v2_bot]` | **Version 1 (Default):** Runs the Human-in-the-Loop Shadow Order Book Scanner & 5-Layer Interconnected Confluence Engine, rendering `reports/shadow_scanner/YYYY-MM-DD.html` with zero orders placed. |
| `uv run python tasks.py paper` | `[VERSION=v2_bot\|v1_shadow]` | **Version 2 (`v2_bot` / `v2_auto`):** Runs the automated execution pipeline through `check_pre_trade_risk_gate`, `OrderFSM`, `LegSequencer`, `PaperBroker`, and `fno_charges`. |
| `uv run python tasks.py backtest` | — | Runs the M6 event-driven replay backtester with **2× slippage stress**, **+50ms latency survival test**, 1,000-path block-bootstrap Monte Carlo, and Bailey–López de Prado Deflated Sharpe (DSR). |
| `uv run python tasks.py self-review` | `[CSV=path/to/tradebook.csv]` | Runs M1 behavioral leakage audit (win rate, revenge-sizing detector, holding-period buckets, statutory cost drag). |
| `uv run python tasks.py mine-log` | `[CSV=path/to/tradebook.csv]` | Parses normalized broker tradebooks and runs M1 setup reverse-engineering & MFE/MAE capture diagnostics. |
| `uv run python tasks.py report` | `[DATE=YYYY-MM-DD]` | Generates both the Shadow Order Book Scanner HTML dashboard and the Corporate Event-Arbitrage HTML report. |
| `uv run python tasks.py instruments` | `[DATE=YYYY-MM-DD] [FETCH=1\|0]` | Downloads today's public Fyers symbol masters (`NSE_FO`, `NSE_CM`, `BSE_FO`, `BSE_CM`), classifies expiries, and checks for lot-size mismatches against `lot_sizes.csv`. |
| `uv run python tasks.py fyers-login` | `[OPEN=1\|0]` | Opens Fyers OAuth login URL in browser, accepts pasted redirect URL, exchanges `auth_code` for today's token, and atomically writes `FYERS_ACCESS_TOKEN` and `FYERS_TOKEN_DATE` to `.env`. |
| `uv run python tasks.py record` | `[SIM=1] [MINUTES=n] [FORCE=1] [SEND=1]` | Runs the market-data recorder (`data/parquet/` for live, `data/sim/parquet/` for `SIM=1`), writes QC report at end, compacts Parquet parts, and optionally sends Telegram alert (`SEND=1`). |
| `uv run python tasks.py qc` | `[DATE=YYYY-MM-DD] [SIM=1] [LAST=n]` | Runs daily QC report on recorded Parquet data, or checks M2 acceptance across the last `n` saved QC reports (`LAST=5`). |
| `uv run python tasks.py event-arb` | `[DATE=YYYY-MM-DD] [FETCH=1\|0] [SEND=1]` | Polls NSE announcements RSS, screens curated corporate events in `config/event_arb/events.yaml`, writes `reports/event_arb/YYYY-MM-DD.html`, and sends Telegram digest (`SEND=1`). |

### 8.2 Standing Engineering & Security Rules ([`CLAUDE.md`](file:///d:/Trading/CLAUDE.md))
- **Tooling:** Python 3.12+, `uv` (`system-certs = true`, `link-mode = "copy"` in [`pyproject.toml`](file:///d:/Trading/pyproject.toml)), `pytest`, `ruff`, `mypy --strict` on `engine/` and `ops/`, `duckdb`, `pyarrow`.
- **Secrets & Privacy:**
  - `.env` and `data/private/` are gitignored and tested by `test_gitignore_excludes` ([`tests/unit/test_secrets_and_logging.py`](file:///d:/Trading/tests/unit/test_secrets_and_logging.py)).
  - Never print or log tokens; always read credentials via `engine.core.env.get_secret()`.
  - Strip personal names, client codes, and PANs when importing reference PDFs or contract notes from `data/private/`.
- **Research Isolation:** Experimental notebooks and scripts live in `engine/research/` and must **never** be imported by live or paper execution paths.
- **No Silent Spec Divergence:** Whenever a rule in `docs/SPEC.md` is found to be outdated or flawed, document the finding in `docs/DECISIONS.md` / `docs/REVIEW_2026-11-09.md` and ask the owner before changing risk limits or enabling live trading.
