# 7. Paper trading and reading results

## 7.1 How a paper fill works

- **Price:** a buy fills at the **ask**, a sell at the **bid**, walking down the recorded book level by level when the size is larger than the touch (`engine/execution/paper_broker.py`). Never at the last traded price. If the visible book cannot fill the whole quantity, the trade is marked *thin book*.
- **Charges:** every fill pays the full charges for the configured plan (`shadow_scanner.cost_broker: upstox_plus`, ₹30 an order; the owner chose it on 13 Nov 2026 because the bots will execute on it later): brokerage, STT, exchange transaction charges, SEBI fee, stamp duty and GST. All rates come from `config/rules/*.csv` with effective dates; nothing is hard-coded.
- **Margin to open:** every position and closed trade shows what the same trade would need in the market.
  - **Long options:** the premium paid plus entry charges, which is exact (no SPAN).
  - **Spreads and anything with a short option or a future:** Upstox's margin calculator, quoted in the background at entry for the whole basket. The figure is NRML and includes hedge benefit; on 13 Nov a 25250/25000 bull put needed ₹42,985, a married future ₹64,654.
  - Hover the figure for the source and the pre-benefit amount. Without a quote (simulation, replay, no network) the worst-case loss is shown as a proxy, marked ≈.
  - The risk gate's 50% margin-headroom check uses this broker margin: the live quote for the exact legs when one is fresh (it is requested as soon as a plan is made), else the paper margin table in `config/rules/margin_rules.yaml`. The table holds 13 Nov quotes plus 15%: credit spread ₹49,500, debit spread ₹41,000, condor or fly ₹85,000, calendar ₹86,500, synthetic ₹66,000, married future ₹76,500 per lot.
- **Latency:** an order fills against the first book at least **200 ms** after the decision (`cockpit.paper_fill_latency_ms`), not the book that triggered it. The fill message shows the ask at the decision and how far the price moved. Multi-leg orders fill **one leg at a time, 300 ms apart, wings first** (`cockpit.paper_leg_gap_ms`); a leg that can't fill unwinds the filled ones. Exits still fill at once.
- **Stops held by the model price** give way after **5 s** at or below the stop, at once if the bid falls past **1.5×** the stop distance, or when the open loss reaches the per-trade limit (`cockpit.stop_hold_max_s`, `stop_override_mult`). The close reason says which.
- **Marks:** open long positions are marked at the **bid** (what you could sell for now). Spreads are marked at the cost to close (buy back shorts at the ask, sell longs at the bid).
- **Slippage stress:** the legacy signal ledger also records net P&L with slippage doubled. Use it as a sanity check: an edge that disappears at 2× slippage is not an edge.

## 7.2 The risk gate

Code: `engine/risk/limits.py` (`check_pre_trade_risk_gate`). Every paper entry, from every strategy and from your manual ticket, is checked against the **owner-approved** limits in `config/settings.yaml` → `risk` (11 Nov 2026), on ₹2,00,000 capital.

| Check | Limit | Refusal reason |
|---|---|---|
| Whole lots, within the lot cap | 1 lot live; up to 2 on paper (`cockpit.paper_max_lots`) | `no_whole_lot`, `max_lots_per_strategy_exceeded` |
| Daily loss limit | 2% = ₹4,000, **realized plus open losses** (marked at the bid / cost to close) | `daily_loss_limit_hit` |
| Weekly loss limit | 5% = ₹10,000, realized plus open losses | `weekly_loss_limit_hit` |
| Trades per day | 5 per account | `max_trades_per_day_reached` |
| Losing streak | Halt for the day after 4 losses in a row; size × 0.5 after 3 | `consecutive_loss_circuit_breaker` |
| Cool-off after a loss | 15 minutes | `in_loss_cooloff` |
| Opening block | First 15 minutes (except opening-range strategies) | `blocked_first_15_minutes` |
| Directional cut-off | No directional entries after 14:00 | `after_directional_cutoff` |
| Per-trade maximum loss | Directional 2.5% = ₹5,000; defined-risk spread 5% = ₹10,000 | `per_trade_risk_exceeded` |
| Open risk across positions | 10% = ₹20,000 | `max_open_risk_exceeded` |
| Margin cap per strategy | That strategy's own used + proposed broker margin ≤ **100% of capital (₹2,00,000)**, so each strategy stays executable alone in the real account. Strategies never block each other: each has its own account (owner, 13 Nov 2026). Live trading keeps the approved 50% (`risk.margin_headroom_max_used_pct`). Long options count their premium. | `margin_headroom_violated` |
| Charges vs target (credit spreads) | Take-profit ≥ 3× round-trip charges (`cockpit.credit_charges_mult`) | `charges: take-profit … is under 3x the round-trip charges …` |

**Vol-managed sizing** (SPEC 5.7, `cockpit.vol_sizing`). Code: `engine/cockpit/vol_sizing.py`.
- **Factor:** the median 20-day realized vol of the index over the last year ÷ its latest 20-day realized vol, clamped to 0.5–1.5. Closes up to yesterday only; stock options use Nifty. Shown in the engine log at start.
- **Never sizes up.** Below 1, a 2-lot entry becomes `floor(2 × factor)` = 1 lot. One lot stays one lot.
- **Skip:** below **0.6** (realized vol about 1.7× its median), new entries are refused on every account, spreads included. Exits are never touched.
- **13 Nov 2026:** Nifty 0.71 (RV20 17.0% vs 12.1%), Sensex 0.70, Bank Nifty 0.75, so 2-lot entries trade 1 lot for now.

**Gap stress** (SPEC 6, `cockpit.gap_scenarios`). Code: `engine/cockpit/stress.py`.
- **What it shows:** every open structure's P&L if the index gapped −8%, −5%, +5% or +8% now. Each option leg is repriced with Black-76 on our forward, IV and variance clock; IV rises 8 / 5 vol points on the down gaps and falls 2 / 3 on the up gaps. The night's time decay is ignored and wider spreads after a gap are not modelled, so real fills would be worse.
- **Where:** the **Gap stress** line on every spread card; red when the worst case exceeds the per-trade spread limit (₹10,000).
- **Carry rule:** from **15:10**, a structure on an overnight account (condor, calendar, compression, synthetic, married future) whose worst gap loss exceeds that limit is **closed instead of carried** (`cockpit.gap_carry`). A synthetic future's stop-based planned loss does not bound a gap through the stop; this does.
- **Why ±5/8%:** 4 Jun 2024 (election results) closed about 6% down.

On top of the gate, the paper accounts refuse:
- anything but **long options** on single-leg accounts (no naked shorts, ever);
- a second position in the same contract;
- a stop outside 1–90% of premium;
- entries inside a **macro-event window**;
- long options **expiring today** after 14:30.

**Why a stop is required:** a long option with no stop risks its whole premium. For a Nifty lot that is often above the ₹5,000 per-trade limit, so the gate would refuse it. The stop % tells the gate the planned maximum loss. Lower the stop % or pick a cheaper strike if you see `per_trade_risk_exceeded`.

Every refusal is shown in a red pop-up, in the account's **Refused** list and in the **Events** tab, with the reason. The same refusal for the same contract (numbers aside) is journaled once a minute at most; on 13 Nov, before this was per contract, one strategy cycling through 12 contracts wrote 33,592 lines.

## 7.3 Manual paper trading (your own account)

1. **Pick a contract:** click a scanner row, click a strike in an option chain, or type in the search box (`/`), e.g. `NIFTY 25000 PE`. A contract outside the live feed is added to it (up to 3 extra).
2. **Look before you buy:**
   - **Market depth:** spread (as % of mid), walls, imbalance, micro-price lean.
   - **Analytics:** IV vs the index's ATM IV, delta, theta per lot per day, time value, and the four pillars of any reading on it.
3. **Set the stop %** (default 25%). Think in rupees: stop % × premium × lot size is your planned loss.
4. **Buy 1 lot @ ask** (key `B`). The pop-up shows the fill or the refusal reason.
5. **Manage:** the position appears under **Strategies → Manual** with its greeks, mark, stop and net. The stop is checked continuously (and only fires if the model price agrees). **Close** (key `C`) or **Flatten manual** to exit. At 15:20 everything open is squared off.

Your manual trades are journaled in `data/state/paper_manual.jsonl` with their entry conditions, so the Research tab treats them exactly like a strategy's. This is how you find out whether your discretion beats the bots.

## 7.4 Journals and restarts

Each account writes one JSON line per open, close and refusal to `data/state/paper_<account>.jsonl`. On restart the engine replays today's lines, restoring open positions and the day's loss streak. Overnight strategies (condor, calendar, compression, synthetic, married) restore their positions from earlier days too, and their contracts are added back into the feed.

A restart **during the session** also rebuilds what the engine learned since the open, from the recorder's quotes: the 15-minute opening range of every recorded contract, and the index tapes (30-minute opening range, VWAP, CVD, ATR, relative volume) from 09:15. The married-future strategy's once-a-minute futures OI and basis are rebuilt the same way, so it no longer waits 30 minutes after a restart. Option-side tape items (ATM straddle, IVs, per-strike OI changes) restart from the restart time, so strategies that need 6–15 minutes of OI history wait that long. The only data lost is the 30-level order book for the minute or so the engine was down; the main recorder is a separate process.

Other state files in `data/state/`:

| File | Contents |
|---|---|
| `setups.jsonl` | Every setup state change |
| `signal_study.jsonl` | Every confirmed reading and its outcome at each horizon |
| `shadow_ledger.jsonl` | The legacy Track A ledger (micro stop/target exits) |
| `term_spread_history.jsonl`, `bb_width_history.jsonl` | Daily history for the calendar and compression strategies |
| `swing_paper_state.json`, `swing/<date>.json` | The nightly swing account and each night's plans |

Do not edit journals by hand; delete a day's lines only if you know why (ask first if the data matters).

## 7.5 Replays: practise on a recorded day

1. Header → **Replay**. Pick a **Date** and **Speed**, press **Start**.
2. The replay engine plays the day's recorded quotes and 30-level books in time order, setting the engine clock to each event's receive time. Every timer (opening range, re-centring, square-off) behaves as it did live.
3. All strategies run, the manual ticket works, and every panel behaves as live.
4. Replay journals go to `data/replay/<date>/` and are wiped at the next replay of that date. Replays never touch live journals or recordings.

Speed `max` runs as fast as the PC can. The day is read in 5-minute windows, so a replay starts in under a second; the reader peaks at about 375 MB and the whole replay process at about 1.1 GB at max speed. At max speed the header may show a large DROPPED count; use a finite speed when you want every event processed. Command-line form:

```bash
uv run python tasks.py replay DATE=2026-11-12 SPEED=0
```

Use replays to:
- check that a new strategy fires where you expect;
- practise manual entries on a day you already know the outcome of (be honest about hindsight);
- regenerate a day's study rows after changing a rule. Every change is a new trial (see [08-new-strategies.md](08-new-strategies.md)).

## 7.6 Reading results without fooling yourself

**Where to look**
- **Overview tab:** today's and total net per strategy. Interesting, not conclusive.
- **Research → Paper accounts by day:** the day-by-day record.
- **Research → Signal study (by score band):** does a higher score predict the index better, and does the ATM option make money after costs? If 80–90 doesn't beat 75–80 over 20+ sessions, the ranking is noise.
- **Research → Which conditions did the results come from?:** each strategy's trades split by IV percentile, VIX, gamma regime, gap, trend vs chop, breadth and more. This is how you find the regime a strategy belongs in.

**Rules of thumb (from CLAUDE.md)**
- **Sample size:** at least 20 paper sessions and 20–30 trades before any conclusion.
- **Significance:** a new signal is unproven unless its t-statistic is about 3. The Research tab shows t for every cell and highlights |t| ≥ 3.
- **Multiple testing:** every variant you try is a trial. The more cells and thresholds you look at, the more "edges" appear by chance; report the deflated Sharpe ratio (`engine/backtest/deflated_sharpe.py`), which corrects for the number of trials.
- **Haircut:** assume live results will be about **half** of paper or backtest results.
- **Costs and slippage:** judge on net after all charges, and check that it survives 2× slippage.
- **No size increase after a loss, no averaging down, no stacking.** These are enforced for live trading and should be your habit on paper.

**A worked check:** a strategy shows ₹+6,000 over 8 sessions and 11 trades. That is too few sessions and trades for anything. Wait for 20+ sessions, then look at its condition table: if all the profit came from IVP < 25 days, write that down as a hypothesis ("works only when premium is cheap"), make it a rule, count it as a new trial, and test it on the *next* 20 sessions, not the ones that suggested it.
