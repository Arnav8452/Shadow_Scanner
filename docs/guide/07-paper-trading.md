# 7. Paper trading and reading results

## 7.1 How a paper fill works

- **Price:** a buy fills at the **ask**, a sell at the **bid**, walking down the recorded book level by level when the size is larger than the touch (`engine/execution/paper_broker.py`). Never at the last traded price. If the visible book cannot fill the whole quantity, the trade is marked *thin book*.
- **Charges:** every fill pays the full charges for the configured plan (`shadow_scanner.cost_broker: upstox_plus`): brokerage, STT, exchange transaction charges, SEBI fee, stamp duty and GST. All rates come from `config/rules/*.csv` with effective dates; nothing is hard-coded.
- **Marks:** open long positions are marked at the **bid** (what you could sell for now). Spreads are marked at the cost to close (buy back shorts at the ask, sell longs at the bid).
- **Slippage stress:** the legacy signal ledger also records net P&L with slippage doubled. Use it as a sanity check: an edge that disappears at 2× slippage is not an edge.

## 7.2 The risk gate

Code: `engine/risk/limits.py` (`check_pre_trade_risk_gate`). Every paper entry, from every strategy and from your manual ticket, is checked against the **owner-approved** limits in `config/settings.yaml` → `risk` (11 Nov 2026), on ₹2,00,000 capital.

| Check | Limit | Refusal reason |
|---|---|---|
| Whole lots, within the lot cap | 1 lot live; up to 2 on paper (`cockpit.paper_max_lots`) | `no_whole_lot`, `max_lots_per_strategy_exceeded` |
| Daily loss limit | 2% = ₹4,000 realized loss | `daily_loss_limit_hit` |
| Weekly loss limit | 5% = ₹10,000 | `weekly_loss_limit_hit` |
| Trades per day | 5 per account | `max_trades_per_day_reached` |
| Losing streak | Halt for the day after 4 losses in a row; size × 0.5 after 3 | `consecutive_loss_circuit_breaker` |
| Cool-off after a loss | 15 minutes | `in_loss_cooloff` |
| Opening block | First 15 minutes (except opening-range strategies) | `blocked_first_15_minutes` |
| Directional cut-off | No directional entries after 14:00 | `after_directional_cutoff` |
| Per-trade maximum loss | Directional 2.5% = ₹5,000; defined-risk spread 5% = ₹10,000 | `per_trade_risk_exceeded` |
| Open risk across positions | 10% = ₹20,000 | `max_open_risk_exceeded` |
| Margin headroom | Used + proposed ≤ 50% of capital | `margin_headroom_violated` |

On top of the gate, the paper accounts refuse:
- anything but **long options** on single-leg accounts (no naked shorts, ever);
- a second position in the same contract;
- a stop outside 1–90% of premium;
- entries inside a **macro-event window**;
- long options **expiring today** after 14:30.

**Why a stop is required:** a long option with no stop risks its whole premium. For a Nifty lot that is often above the ₹5,000 per-trade limit, so the gate would refuse it. The stop % tells the gate the planned maximum loss. Lower the stop % or pick a cheaper strike if you see `per_trade_risk_exceeded`.

Every refusal is shown in a red pop-up, in the account's **Refused** list and in the **Events** tab, with the reason.

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

Speed `max` runs as fast as the PC can. On `main` today a replay loads the whole day first, which for a full recorded day takes minutes and several GB of memory. The `replay-streaming` branch (not merged yet) reads the day in 5-minute windows instead: it starts in under a second and peaks at about 375 MB. Command-line form:

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
