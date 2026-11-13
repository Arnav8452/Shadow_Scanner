# 8. Building and testing a new strategy

This is the path from an idea to a verdict. Most ideas should die on it; that is the point.

## 8.1 The definition of done (CLAUDE.md)

A strategy is ready for one live lot only after **all** of these:

1. **Rules written down in config.** Every threshold has a name and a value in `config/settings.yaml` or the strategy's dataclass, marked HYPOTHESIS.
2. **Backtest** on recorded data with bid/ask fills, full costs and margin, rerun at **2× slippage**, walk-forward, Monte Carlo, and a **deflated Sharpe** that accounts for every variant tried.
3. **≥ 20 paper sessions** on the live engine.
4. **Kill rules written:** the conditions under which you stop trading it.
5. **Owner approval.**
6. Then **1 lot live**, in paid mode only, with every lock condition met.

Today no strategy has passed step 2, because the event-driven backtest over recorded data is not wired to the cockpit strategies yet (`tasks.py backtest` and `paper` only run on synthetic inputs and require `DEMO=1`). The practical path is therefore **unit tests → replays → paper sessions → research tables**, with the backtest library used for the statistics.

## 8.2 Step 1 — Write the hypothesis

Before code, write three or four lines:
- **Edge:** who is on the other side, and why would they lose? (e.g. "trapped option writers cover when the index crosses their strike").
- **Trigger:** the observable conditions, with numbers.
- **Vehicle and risk:** which contract, how many lots, worst-case loss in rupees, how it stays defined-risk.
- **Exits:** stop, target, time stop, end of day / overnight.
- **What would prove it wrong:** e.g. "after 20 sessions, net after costs ≤ 0, or t < 2".

Check it against the hard rules ([10-risk-and-config.md](10-risk-and-config.md)): no naked shorts, limit orders, defined risk, one lot live, no averaging down.

## 8.3 Step 2 — Code it as a playbook strategy

Most new ideas fit one of two base classes in `engine/cockpit/plays_base.py`:

- **`LongPlay`**: buys one option and manages it. Implement `look(now, m)`, call `self.enter(...)` with the exits you want: `stop_pct` or `stop_inr`, `target_pct`, `ratchet=True` (charges ratchet), `time_stop=(minutes, gain%)`, `candle_stop_min`, `ema_after_pct`, `delta_exit`, `spot_stop`.
- **`SpreadPlay`**: multi-leg structures. Implement `look(now, m)`, build a list of `Leg`s (protective legs are always bought first by the account, whatever order you give), and call `self.plan(m, und, structure, legs, width, reason, SpreadTrade(...), max_loss_inr=…)`. The plan waits until every leg has a live book, then opens. Add `extra_exit(pos, trade, now, m)` for custom exits; `spread_exit` already handles credit/debit targets, stops, spot stops and the square-off.

The `Market` object `m` gives read access to everything the engine knows:

| Call | Returns |
|---|---|
| `m.underlyings()`, `m.spot(und)`, `m.fut(und)` | Indices, spot, front future state (LTP, OI, volume, book) |
| `m.tape(und)` | The minute tape: bars, `cvd()`, `cvd_z()`, `atr()`, `vwap()`, `opening_range(30)`, straddle stats, IV changes, OI history |
| `m.otape(sym)` | One option's tape: VWAP, EMA, 09:20 price |
| `m.book(sym)`, `m.detail(sym)`, `m.lot(sym)` | Live book, IV and greeks, lot size |
| `m.chain_rows(und)`, `m.by_delta(und, kind, target)` | The 1-minute chain; the strike nearest a delta |
| `m.ivp(und)` | IV percentile |
| `m.breakeven(entry, qty)`, `m.scalp_gate(sym, qty, mult, und)` | Charges breakeven; Gate 1 |
| `m.context(und)`, `m.event_block()` | Entry conditions; event window |
| `m.pin(owner, syms)` | Add contracts to the 50-key live feed |

Rules for the code:
- Put every threshold in a dataclass field with a default, named clearly; no magic numbers in the logic.
- Never hard-code a fee, tax, lot size, expiry day or holiday: read them through `config/rules` (`RuleBook`, `fno_charges`, the instrument master).
- Use point-in-time data only: nothing the market didn't know at `now`.
- Set `self.decision` to a short human sentence each step ("waiting: OI down 2.1% of the 3% needed"). It is what the Overview and detail view show.
- Research-only code goes in `engine/research/` and must never be imported by live paths.

## 8.4 Step 3 — Register it

In `engine/cockpit/build.py` → `make_plays`, add an instance with its own account:

```python
L.MyIdeaPlay("my_idea", "My idea, in plain words", "notes ref", long_acct("my_idea")),
```

and a one-paragraph rule summary in the `rules` dict (what the page shows). It then appears in the Overview with its own journal, `data/state/paper_my_idea.jsonl`. To switch it off without deleting it:

```yaml
cockpit:
  plays: {my_idea: false}
```

If it is a SPEC strategy, update its row in `engine/strategies/registry.py` (status `paper`, with the journal as evidence) so Research → Strategies stays honest.

## 8.5 Step 4 — Test it

Write the test first if you can. `tests/data/test_plays.py` has a scripted `FakeMarket` (`mkt` fixture) with helpers to feed a trend (`feed_trend`), build a chain (`chain`) and books (`bk`). A good strategy test:

1. **Fires** when every condition holds, with the right contract, side and lots.
2. **Does not fire** when any single condition is missing (one test per condition is ideal).
3. **Exits** on each exit rule.
4. **Respects defined risk:** a short is refused without its hedge; the worst case is what you expect.

Run:

```bash
uv run python tasks.py test
```

```bash
uv run python tasks.py lint
```

Both must be green (strict mypy on `engine/`).

## 8.6 Step 5 — Replay it

Replay a few recorded days ([07-paper-trading.md §7.5](07-paper-trading.md#75-replays-practise-on-a-recorded-day)), including a trending day, a range day and an expiry day. Check:
- it fires roughly where you expected on the chart, and not constantly;
- every refusal reason makes sense (Events tab and its Refused list);
- the worst trade's loss is what the rules allowed.

Replays are for debugging, not for evidence: you chose the days and you know what happened.

## 8.7 Step 6 — Paper sessions and the verdict

Let it run live on paper for **20+ sessions** without changing it. Then:

- **Research → Paper accounts by day:** net after costs.
- **Research → conditions table:** where the results came from.
- **The statistics:** export the journal's closed trades and run them through `engine/backtest/`:
  - `evaluate_backtest_with_stress`: net at 1× and 2× slippage, and a 50 ms latency check;
  - `compute_deflated_sharpe(pnls, n_trials=…)`: count every variant you tried, honestly;
  - `run_block_bootstrap_mc`: drawdown distribution (plan for the 99th percentile);
  - `evaluate_walk_forward` (`engine/backtest/walkforward.py`): rolling in/out-of-sample folds on daily returns, if you fitted anything.

**Verdict:**
- **Promising:** net > 0 after costs *and* at 2× slippage, t ≈ 3, a deflated Sharpe that survives the trial count. Write kill rules, ask the owner.
- **Unproven:** anything else. Keep it running or switch it off; do not tweak it to fit the last 20 sessions (that just adds a trial and uses up your data).

Record what you built, the decision and open questions in `docs/DECISIONS.md` (3–5 lines, newest first).

## 8.8 Tuning an existing strategy

Changing a threshold is a **new variant**:
- write down the old and new values and why;
- count it in the trial total;
- judge it only on sessions *after* the change.

Risk limits (`risk:` in settings) are owner-approved: never change them without the owner's explicit OK.
