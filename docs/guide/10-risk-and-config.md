# 10. Risk rules, configuration and regulation

## 10.1 The non-negotiable trading rules

These are enforced in code and covered by tests. They come from [CLAUDE.md](../../CLAUDE.md) and cannot be switched off from the page.

1. **Defined risk only.**
   - Never sell an option unless its hedge leg has already filled: buy protection first, and on exit buy the short back first (`engine/execution/leg_sequencer.py`).
   - A short future counts as a short call: it needs a filled long call. A long future needs a long put.
   - On partial fills, the short quantity is capped at the filled hedge quantity.
2. **Limit or market-with-protection orders only.** `MARKET` in `execution.allowed_order_types` is refused at start-up.
3. **Under 10 orders per second per exchange** (the SEBI retail-algo threshold). The cap is 5 (`execution.max_orders_per_second`); orders above the freeze quantity are sliced into whole lots strictly below it (`engine/execution/slicer.py`).
4. **Kill switch** (`engine/risk/kill_switch.py`) flattens and blocks entries on any of:
   - the daily loss limit;
   - max open risk;
   - a reconciliation mismatch;
   - a feed or broker disconnect longer than `execution.disconnect_kill_after_s` (10 s).

   On paper, a stale feed freezes entries and pauses exits; it never flattens on prices it doesn't have.
5. **One lot per strategy** until 20–30 live trades are logged. No averaging down, no stacking beyond the cap, no size increase right after a loss, a cool-off after a loss. (Paper accounts may use 2 lots within the loss limits, by owner decision on 12 Nov 2026; live stays at 1.)
6. **Reconcile** internal positions with the broker every 30–60 s (`engine/execution/reconcile.py`; 45 s configured). This only matters once live.

## 10.2 The live-trading lock

`live_gate()` in `engine/core/config.py`. Every order goes to the paper broker unless **all five** hold at once:

1. `MODE=paid`;
2. `execution.live_enabled: true` in config (refused in free mode);
3. a static IP configured and whitelisted with the broker (SEBI retail-algo framework, mandatory since 1 Apr 2026);
4. a fresh 2FA session from today;
5. the owner's explicit confirmation in the session.

None is met today. Do not try to work around this lock; changing it requires the owner's decision.

## 10.3 Approved risk limits

`config/settings.yaml` → `risk:` and `capital:`, approved by the owner on 11 Nov 2026. Changing them needs the owner's explicit OK.

| Setting | Value | Meaning on ₹2,00,000 |
|---|---|---|
| `capital.inr` | 200000 | Account size |
| `per_trade_max_loss_pct.directional` | 2.5 | ₹5,000 per directional trade |
| `per_trade_max_loss_pct.defined_risk_spread` | 5.0 | ₹10,000 per spread |
| `daily_loss_limit_pct` / `weekly_loss_limit_pct` | 2 / 5 | ₹4,000 / ₹10,000 |
| `max_open_risk_pct` | 10 | ₹20,000 at risk across open positions |
| `max_lots_per_strategy` | 1 | Live; paper may use `cockpit.paper_max_lots: 2` |
| `margin_headroom_max_used_pct` | 50 | Live. Paper: `cockpit.paper_margin_cap_pct: 100`, i.e. each strategy's own broker margin up to ₹2,00,000 on its own account (owner, 13 Nov 2026) |
| `max_trades_per_day` | 5 | Per account |
| `cooloff_after_loss_min` | 15 | — |
| `block_first_minutes` | 15 | No entries 09:15–09:30 except opening-range strategies |
| `directional_no_entry_after` | 14:00 | — |
| `margin_headroom_max_used_pct` | 50 | Used + proposed margin ≤ ₹1,00,000 |
| `loss_streak_size_cut_after` / `loss_streak_halt_after` | 3 / 4 | Half size after 3 losses in a row; stop for the day after 4 |

## 10.4 `config/settings.yaml`, section by section

Each value is marked inline as **APPROVED** (owner decision: do not change without asking) or **HYPOTHESIS** (a starting guess to be measured).

| Section | What it controls |
|---|---|
| `engine_version`, `capital` | v1_shadow / v2_bot; account size |
| `shadow_scanner` | Scanner thresholds, eligibility filters, grades, Kelly guards, paper ledger times, cost plan |
| `cockpit` | Ports, publish rates, the bot, directional, setups, condor, debit and gamma settings, reading cut-offs, expiry-day rules, paper lots, manual stop default, `plays` on/off, vol-managed sizing (`vol_sizing`, `vol_sizing_skip_below`), gap stress (`gap_scenarios`, `gap_carry`, `gap_carry_time`) |
| `analytics` | The variance clock weights (`variance_clock.overnight` 0.49, `non_trading_day` 0.31), measured from daily candles |
| `risk`, `execution` | The approved limits and order rules above |
| `event_arb` | The event-arbitrage sleeve (approved; information only) |
| `recorder` | Symbol caps, what to record, strike band, expiries, QC thresholds |
| `shadow_live` | The engine's 50-key watchlist, alerts, delivery watchlist |
| `swing`, `swing_paper`, `trend` | Nightly swing scanner and trend sleeve |
| `fundamentals`, `paths`, `logging` | Filings, folders, log config |

`config/modes/free.yaml` and `paid.yaml` are deep-merged on top according to `MODE`.

## 10.5 `config/rules/`: regulatory and fee tables

Never hard-code a fee, tax, lot size, expiry day, holiday or SEBI/NSE rule in strategy code. Each lives in an effective-dated table:

| File | Contents |
|---|---|
| `stt.csv` | Securities transaction tax (options sell 0.15% of premium; exercised intrinsic 0.15%; futures sell 0.05%) |
| `exchange_charges.csv`, `sebi_fee.csv`, `stamp_duty.csv`, `gst.csv`, `brokerage.csv`, `dp_charges.csv` | Every other charge, per broker plan |
| `lot_sizes.csv`, `freeze_qty.csv` | Contract sizes (Nifty lot 65) and per-order freeze limits |
| `expiry_calendar.csv`, `holidays.csv`, `market_sessions.csv` | Expiry days, holidays, session times (F&O close 15:40 from 3 Aug 2026) |
| `sebi_algo_rules.csv` | Retail-algo framework (static IP, 2FA, Algo ID, < 10 orders per second) |
| `margin_rules.yaml`, `circuit_breakers.csv`, `corporate_action_rules.csv`, `tax_rules.csv`, `risk_free_rate.csv` | Margin, circuit limits, corporate actions, tax, the T-bill rate for IV |

Every row has `effective_from`, `effective_to`, `source_url`, `verified_on` and a status. To change one:
1. Find the **primary source**: an exchange circular, SEBI, or the broker's own docs.
2. Add a new row with the new `effective_from`, and close the old row's `effective_to`. Never overwrite history: old trades must still price at old rates.
3. Record the URL and today's date in `verified_on`. If you cannot verify it, mark it `UNVERIFIED` and tell the owner.
4. Run `uv run python tasks.py rules`, which validates every table and lists the UNVERIFIED rows, then run the tests.

**Golden tests:** computed costs must match real contract notes to ₹1. Contract notes are still needed to settle the IPFT levy question and a few UNVERIFIED rows (see [DECISIONS.md](../DECISIONS.md)).

## 10.6 Privacy and security

- **Secrets** live only in `.env` (gitignored). Never print or log tokens; the logging config redacts them.
- **The page** listens on 127.0.0.1 only. A shared key (`data/state/cockpit.key`) secures the engine–gateway link, and a page token secures the browser link. Never commit either.
- **Reference trade logs** live only in `data/private/`. Strip names and account IDs on import, and never put them in outputs, fixtures or commits.
- **Market data** is for the owner's own trading. Don't build anything that redistributes it.
- **Scope:** this is white-box personal software. Don't add features that offer signals or the bot to other people; that would need SEBI registration (Research Analyst / algo provider).
- **The repository is public.** Before every commit, check the diff for secrets, personal names and values copied from private logs. Never commit `data/`, `reports/`, `logs/` or `new/`.
