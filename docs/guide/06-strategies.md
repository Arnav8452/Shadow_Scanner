# 6. Strategies

Every strategy runs **on paper only**, on **its own account and journal** (`data/state/paper_<key>.jsonl`), so results never mix. Every trade:

- goes through the approved risk gate ([07-paper-trading.md](07-paper-trading.md#72-the-risk-gate));
- fills at the recorded bid/ask, walking the book for size;
- pays full charges from `config/rules` for the Upstox Plus plan;
- stores its entry conditions.

Every number below is a **HYPOTHESIS** in the strategy's code or in `config/settings.yaml`. None is proven.

**Shared rules**
- **Defined risk only.** A short option leg is placed only after its protective long leg has filled, and bought back first on exit (CLAUDE.md rule 1; `engine/execution/leg_sequencer.py`). A short future needs a filled long call as cover; a long future needs a long put.
- **Lots:** one lot, except where a strategy says otherwise; paper accounts may use up to `cockpit.paper_max_lots: 2` within the loss limits. Live trading would stay at one lot (CLAUDE.md rule 5).
- **Times:**
  - multi-leg positions whose short leg expires today: closed at 15:00, before the 15:10 settlement window;
  - no entries in the first 15 minutes or inside an event window;
  - no new entries after 15:00;
  - intraday positions are squared off at 15:20;
  - long options expiring today: no entries after 14:30, closed at 14:45.
- **High-IV routing (rule 4):** when ATM IV is at or above its **80th percentile**, a naked-buy signal from the put-panic, OI-ignition or breakthrough strategy becomes a **debit spread** on the *High-IV routed debit spreads* account instead (buy ~0.55 delta, sell ~0.25 delta).
- **Choosing the option by delta:** a contract already in the live feed (live book and greeks) within **0.05** of the target delta is preferred, so the strategy can score and trade it at once. Otherwise the 1-minute chain's pick is used and the strategy asks for it to be added to the feed (at most every 30 s); the decision line says "live feed full" when there was no room. Before 13 Nov, Sensex picks were usually outside the feed, so the breakthrough scalper could not score Sensex at all.
- **Stops fitted to the per-trade limit:** when a long option's planned loss (premium × stop % × quantity) would exceed the **₹5,000** directional limit, the stop is tightened to fit, and the reason says so ("stop 25% of premium, was 35%"). If a fitting stop would be under **10%** of premium, the contract is skipped instead. Before 13 Nov the zone rider's fixed 35% stop made every Sensex entry a refusal.
- **Daily trade cap:** once an account has its 5 trades for the day, its strategy stops sending orders (the decision line says so) instead of being refused on every trigger.
- **Stops on long options** need the model price to agree ([05-signals.md §5.8](05-signals.md#58-the-charges-gates-why-short-holds-are-allowed-at-all)).

Sources:
- *SPEC* = `docs/SPEC.md` §5.
- *spec S1–S3, A–D, futures 1–3* = the owner's `shadow_scanner_quant_specification.md`.
- `rules.md`, `ride_wave.md`, `breaking_retail.md`, `fix.md`, `fix2.md` = the owner's notes in `new/`.

## Summary

| Key | Strategy | Vehicle | Holds | Source | State |
|---|---|---|---|---|---|
| `condor` | Iron condor / credit spreads | 4 legs, Nifty weekly | Overnight | SPEC 5.1 | on |
| `bot` | Directional | ATM option | Intraday | SPEC 5.2 | on |
| `debit` | Debit spreads on confirmed setups | 2 legs | Intraday | SPEC 5.2 variant | on |
| `gamma` | Expiry-afternoon gamma breakout | ATM option | Intraday | SPEC 5.10 | on |
| `score` | Score-follow (original A+ bot) | ATM option | Minutes | test | on |
| `put_panic` | Put-panic breakdown | 0.55–0.60Δ put | Minutes | spec S1 | on |
| `oi_ignition` | OI-unwinding ignition | 0.55Δ option | Minutes–hours | spec S2 + second mouse | on |
| `zero_dte` | 0DTE afternoon gamma | ATM option, 2 lots | Under 1 hour | spec S3 | on |
| `breakthrough` | Breakthrough scalper (5 pillars) | 0.55Δ option, 1–2 lots | Minutes | rules.md | on |
| `spoof_fade` | Spoof rug-pull / eaten wall | 0.55Δ option | Minutes | spoofing notes | on |
| `zone_rider` | Absorption-zone wave rider | 0.60Δ option | To 15:20 | ride_wave.md | on |
| `log3_mom` | Log-3 momentum (mined rule) | ATM option | Minutes to 15:20 | log3_entry_features | on |
| `iron_fly` | Straddle-VWAP iron fly | 4 legs | Intraday | spec A | on |
| `oi_credit` | OI-velocity credit spread | 2 legs | Intraday | spec B | on |
| `calendar` | Term-structure calendar | 2 legs, 2 expiries | Overnight | spec C | waiting for 10 sessions of history |
| `compression` | Compression + skew debit spread | 2 legs | Up to 2 days | spec D | needs 20 width readings; full meaning after 10 sessions |
| `expiry_pin` | Expiry pin seller | 2 legs | To 15:00 | fix2.md | on (expiry days) |
| `synthetic` | Hedged synthetic future | 3 legs | Up to 3 days | futures 1 | on |
| `married` | Married future, both directions | option + future | Up to 5 days | futures 2 | on |
| `mcx` | MCX mini futures | future | Intraday | futures 3 | **off: no MCX data or costs** |
| `hiv` | High-IV routed debit spreads | 2 legs | Intraday | rule 4 | on (fed by others) |
| `manual` | Your own trades | long options | Your call | — | on |

Turn a playbook strategy on or off with `cockpit.plays: {<key>: false}` in `config/settings.yaml`; the others have `enabled:` in their own `cockpit.<name>` section.

---

## 6.1 SPEC strategies

### Iron condor / credit spreads — `condor` (SPEC 5.1)
Code: `engine/cockpit/condor.py`. Settings: `cockpit.condor`. Checked once a minute.

**Entry, in order:**
1. **Window and expiry:** 10:00–14:30, on the nearest Nifty weekly with at least 2 calendar days left. One structure a day.
2. **IV percentile ≥ 50.**
3. **No event window**, and no RBI MPC, Budget or election exit poll, or results worth ≥ 10% of index weight, before the planned exit.
4. **Order-flow veto** over the last 30 minutes of setups:
   - confirmed setups both ways → no trade;
   - only UP → **bull put spread**; only DOWN → **bear call spread**;
   - a setup still forming → wait;
   - none → **iron condor**.
5. **Strikes:** shorts at about **0.16 delta**; wings **200, 150 or 100** points further out, the widest whose worst case fits the 5% per-trade limit. The credit must be ≥ **10% of the width**. All four contracts are added to the feed, and the structure is traded only when each has a live book.
6. **Hedge first:** wings bought, then shorts sold, one leg at a time (300 ms apart); a failed short unwinds the filled legs.
7. **Charges and margin:** the 50% take-profit must be worth at least **3×** the round-trip charges. At ₹30 an order a typical 16-point credit doesn't qualify, so expect fewer condors. The broker margin, about ₹74k live for 150-pt wings, must fit the 50% headroom.

**Exits** (checked on every update, about 5 times a second; entries are checked once a minute):
- **50%** of the credit captured;
- cost to close ≥ **2×** the credit;
- the index touches a short strike;
- a confirmed setup toward a short strike with the index within **0.5%** of it;
- **15:00 on the day before expiry**.
- **Either side** costing **2.2×** its own credit to close. A combined stop lets the winning side hide the tested one.
- **Before a weekend or holiday**, from 15:10: book the structure if it has captured **≥ 80%** of its take-profit, rather than carry gap risk for the last few rupees.

**Adjustments (SPEC 5.1):**
- **Untested side booked:** when one side has decayed **85%** and the other costs at least its credit to close, that side is closed and the tested side stays on. At most one adjustment per condor. Take-profit and stops then count what the booked side cost.
- **Gap at the open:** no stop exits in the first 15-minute candle, so a gap through a short strike is judged from 09:30. The time exit still applies.
- **Wing cost:** recorded as a % of the premium sold and shown on the card (far wings are often overpriced).

Held overnight. Margin is the live broker quote (≈ ₹74k for one lot with 150-pt wings).

### Directional — `bot` (SPEC 5.2)
Code: `engine/cockpit/core.py` (`_directional_entry`). Settings: `cockpit.directional`.

- **Entry:**
  - The first **confirmed setup** scoring **≥ 80**, between **09:30 and 14:00**, on Nifty or Sensex.
  - Buys 1 lot of the ATM **call** (index up) or **put** (index down).
  - One idea per index per day: never flips direction. At most 2 entries (one same-direction re-entry after a stop).
- **Exits:** premium stop **−10%**, target **+30%**, trailing (at +15% lock +7%; at +25% lock +12%), square-off 15:20. Order-book changes do not close it.

### Debit spreads on confirmed setups — `debit`
Code: `engine/cockpit/strategies.py` (`DebitSpreadStrategy`). Settings: `cockpit.debit`.

- **Entry:** the same confirmed setups (≥ 80, 09:30–14:00) as directional, expressed as a **bull call** or **bear put** spread: buy ATM, sell **2 strikes out** (Nifty 100 points, Sensex 200). An A/B test of naked vs spread on the same signal. At most 2 a day.
- **Exits:** spread worth debit + **60%** of the remaining max value; worth **half** the debit; 15:20.

### Expiry-afternoon gamma breakout — `gamma` (SPEC 5.10)
Code: `engine/cockpit/strategies.py` (`GammaBreakoutStrategy`). Settings: `cockpit.gamma`.

- **Entry:**
  - On an index's weekly expiry day, after a quiet morning: the 09:15–13:30 range is under **0.45%** of spot.
  - 13:45–14:30: a completed 5-minute candle closes beyond the morning high/low **and** the futures VWAP, on **1.8×** the day's average 5-minute futures volume.
  - OI at the breached ATM strike has fallen since 13:30 (short covering).
  - Buys 1 lot ATM. One attempt per expiry.
- **Exits:** **30%** premium stop; once the premium is 1.8× entry, the stop moves to +40% (one lot cannot be half-booked); 14:45 expiry-day exit.

### Score-follow — `score` (test)
Settings: `cockpit.bot`. The original bot, kept as one strategy to measure: on an **A+ (80+)** reading, buy the ATM option for 1 lot with a **25%** premium stop; exit when the signal's episode ends. It holds for minutes, so it is the purest test of whether the raw score has any edge after costs.

---

## 6.2 Long-option playbook (short holds through the charges gates)

Code: `engine/cockpit/plays_long.py`. These may hold for minutes only because each passes **Gate 1** (expected 3-minute move ≥ 3.5× the charges breakeven) and is managed by **Gate 2** (the breakeven ratchet with the 180-s scratch). See [05-signals.md §5.8](05-signals.md#58-the-charges-gates-why-short-holds-are-allowed-at-all). At most 3 entries per strategy per day.

### Put-panic breakdown — `put_panic` (spec S1)
- **Window:** 09:45–14:00.
- **Trigger:** all of the following:
  - the index breaks the **30-minute opening-range low** (or rejects VWAP);
  - futures **CVD z ≤ −2.5**;
  - ATM put IV up **> 0.4 points in 3 minutes**.
- **Vehicle:** a **0.55–0.60 delta put** with 2–5 days left.
- **Exits:**
  - stop **20%** of premium, capped at **₹1,500**;
  - out if not **+10% within 12 minutes**;
  - breakeven ratchet;
  - out if still below breakeven after **15 one-minute candles**.

### OI-unwinding ignition — `oi_ignition` (spec S2 + breaking_retail "second mouse")
- **Window:** 09:30–14:30.
- **Trigger:** all of the following:
  - the index crosses the **highest-OI strike** (within 15 points);
  - that strike's OI falls **> 3% in 6 minutes** (writers covering);
  - 1-minute futures volume **> 2×**;
  - CVD is rising.
- **Second mouse:** instead of chasing, it waits 1–3 minutes for a **retest** that holds: opposite-side OI building and the option above its own VWAP.
- **Filters:** absorption veto, heavyweight alignment, charges gate.
- **Exits:** ratchet; after **+25%** the stop trails the option's 1-minute EMA-9.

### 0DTE afternoon gamma — `zero_dte` (spec S3)
- **Expiry day only.**
- **Setup:** 11:00–13:30 the index stays inside the ATM strike ± the straddle price (pinned).
- **Trigger:** 13:30–14:30, a 1-minute close **outside** that band on **> 3×** futures volume.
- **Vehicle:** **2 lots** ATM in that direction, about **₹11 per unit** at risk.
- **Exits:** at **delta 0.90**, or 14:45.
- **Skipped** when 0DTE IV rises while next-expiry IV falls (a vol squeeze, not a move).

### Breakthrough scalper — `breakthrough` (rules.md, 5 pillars)
- **Window:** 09:30–14:30.
- **Score:** a separate 0–100 score per index:

  | Pillar | Points | Measures |
  |---|---|---|
  | Order book | 20 | Decay-weighted OBI |
  | Volume delta | 25 | CVD and relative volume |
  | Writer pain + OI unwind | 25 | Option vs its 09:20 price and VWAP, with OI falling |
  | Gamma impulse | 15 | Expected move vs charges breakeven |
  | IV velocity | 15 | IV rising with the move |

  The score is multiplied by **0.4** unless the impulse clears **2.5×** breakeven.
- **Size:** **65–79 → 1 lot**, **80+ → 2 lots** (with an EMA trail).
- **Thesis floor** (owner, 13 Nov 2026): the writer-pain + OI-unwinding pillar must be **≥ 30**. A 65 made of book, volume and the direction-blind gamma pillar alone does not qualify.
- **Page extras:** its detail view shows the latest scores, writer pain, and the **25% / 30% stop-cluster levels**: where writers who sold at the anchor would be stopped out, a projected fuel level.

### Spoof rug-pull / eaten wall — `spoof_fade` (spoofing notes)
- **Window:** 09:30–14:30.
- **Trigger:** a **pulled** wall (spoof rug-pull) or an **eaten** wall on an index future or option ([05-signals.md §5.4](05-signals.md#54-spoofing-and-walls)).
- **Trade:** the side the event reveals (pulled bid wall → down; eaten ask wall → up) with a 0.60-delta option (0.55–0.65 band).
- **Gates (the full charges-breakeven protocol):**
  - no entry in the first 45 s of a round minute;
  - expected 3-minute move ≥ 3.5× the charges breakeven;
  - vacuum score 65+;
  - no absorption on the trigger bar;
  - breakeven ratchet with the 180-s scratch.
- **Note:** the wall detector fires several events a second on flickering option books, so this strategy relies on those gates to filter noise; the thresholds are a tuning question.

### Absorption-zone wave rider — `zone_rider` (ride_wave.md)
- **Window:** 09:45–14:00.
- **Trigger:** either of:
  - a futures **absorption floor (ceiling)** cleared by **10 points**, with 5-minute CVD agreeing;
  - a **30-minute CVD divergence** (price makes a new low but CVD doesn't, or the mirror).
- **Vehicle:** a **0.60 delta** option with ≥ 5 days left.
- **Exits:** stop when the index crosses back through the zone; otherwise held to the 15:20 square-off.

---

## 6.3 Multi-leg playbook

Code: `engine/cockpit/plays_spread.py`. Wings are always bought first and shorts bought back first. One structure per strategy per day.

### Log-3 momentum — `log3_mom` (mined from a reference log, 13 Nov 2026)
- **Where the rule comes from:** `tasks.py log3-features` measured the index at each of a funded trader's entries (10 sessions). 93% of the entries came after a 15-minute push of at least 0.05% in the trade's direction, against 29% of other minutes. See [09-data-and-research.md §9.7](09-data-and-research.md#97-research-tools).
- **Window:** 10:00–14:00.
- **Trigger (all three, on completed 1-minute index bars):** 15-minute move **≥ +0.05%** in the trade's direction; within **0.10%** of the day's high (calls) or low (puts); beyond the 15-minute opening range that way.
- **Trade:** buy the ATM option (~0.50 delta) that way, 1 lot.
- **Exits, as the log:** **−10%** stop, **+30%** target, the 15:20 square-off.
- **Limits:** one entry per index and direction per 30 minutes; the shared risk gate.
- **Why it runs:** a paper test of a mined rule against the other directional accounts. 10 sessions are not evidence; this account is how the rule gets its 20+.

### Straddle-VWAP iron fly — `iron_fly` (spec A)
- **Days:** Nifty Monday and Tuesday; Sensex Thursday. **Window:** 09:45–13:00.
- **Trigger:** all of the following:
  - 30-minute realized vol ÷ ATM IV **< 0.85** (options overpriced vs actual movement);
  - the ATM straddle **below its VWAP for 3 bars**;
  - after the **09:20 stop flush**: a straddle leg traded 25% above its 09:20 price and a 1-minute selling candle printed.
- **Structure:** sell the ATM call and put; buy wings **300 points** out (scaled for Sensex).
- **Exits:** the straddle closes above VWAP + **1.5σ**; net delta beyond **±0.30** per unit; square-off.

### OI-velocity credit spread — `oi_credit` (spec B)
- **Window:** 09:45–14:00.
- **Trigger:** all of the following:
  - the index within **20 points** of the call (put) OI wall;
  - that wall's OI down **> 4% in 10 minutes**;
  - futures CVD z **≥ +2** (≤ −2).
- **Structure:** a **bull put** (bear call) spread at the money, wing **250 points**.
- **Exits:** 50% of the credit; 2× the credit; the index back through the wall; 15:20.

### Term-structure calendar — `calendar` (spec C)
- **Window:** 10:00–14:30.
- **Trigger:** near-expiry ATM IV minus far-expiry ATM IV (the far expiry 14+ days out, now the recorded **next monthly**) **> +2.5 vol points**, with a z-score **> +2** against up to 20 past sessions.
- **Structure:** sell the near ATM call, buy the far ATM call at the same strike.
- **Exits:** term spread back to 0; the short leg's |delta| outside 0.25–0.75; the day before the near expiry.
- **Holding:** held overnight.
- **Needs:** at least **10 sessions** of term-spread history (written at each close to `data/state/term_spread_history.jsonl`). Until then its decision line shows the spread and the session count.

### Compression + skew breakout debit spread — `compression` (spec D)
- **Window:** 10:15–14:30.
- **Trigger:** all of the following:
  - 15-minute Bollinger width in the **bottom 10%** of the last 10 sessions' widths;
  - a close out of the **60-minute box**;
  - **25-delta skew** moving the breakout's way;
  - 15-minute futures volume **> 2.5×**.
- **Structure:** buy ~0.55 delta, sell ~0.25 delta.
- **Exits:** 70% of max value; half the debit; back inside the box; 2 days.
- **Needs:** at least 20 width readings (about five hours of its first day); the "bottom 10%" only means what it says after 10 sessions of history.

### Expiry pin seller — `expiry_pin` (fix2.md)
- **Expiry day, 13:45–14:45.**
- **Trigger:** all of the following:
  - fresh put writing at a strike: **OI +30% since 13:35**;
  - call OI there unwinding **≥ 20%**;
  - that put spiking **≥ 50%** above its 13:45 price.
- **Structure:** **sell** that put with a cheap wing below (ask under ₹3): a credit spread.
- **Exits:** 90% of the credit; 2× the credit; otherwise closed at **15:00**, before the 15:10 settlement window.

### Hedged synthetic future — `synthetic` (futures 1)
- **Window:** 09:45–14:00.
- **Trigger:** a confirmed setup.
- **Structure:** a delta-one position from options, defined risk:
  1. buy the protective wing **350 points** away first;
  2. then **long call + short put** at the money (or the mirror for down).
- **Risk:** the planned loss is a **0.5%** index stop × quantity.
- **Holding:** up to 3 days.

### Married future, both directions — `married` (futures 2)
- **Window:** 10:00–14:30.
- **Long** (from the notes):
  - **Trigger:** the index **above the previous day's high**, futures OI **+2% in 15 minutes**, and the annualized basis positive and rising.
  - **Order:** buy the **ATM put first**, then **buy the future**.
- **Short** (the mirror, added by owner decision on 12 Nov 2026):
  - **Trigger:** the index **below the previous day's low**, futures OI **+2%** (short build-up), and the basis shrinking.
  - **Order:** buy the **ATM call first**, then **sell the future**. The call caps the loss on a rise, so the leg sequencer allows the short future only once the call has filled.
- **Exits:** **200 points** in the trade's direction; stop **0.5%** beyond the broken level; up to **5 days**; expiry-day exit.
- **Worst case:** (|future − strike| + hedge premium) × lot.

### MCX mini futures — `mcx` (futures 3) — off
VWAP breakout with CVD z ≥ 2 and 2× volume on CRUDEOILM / NATGASMINI, with the charges ratchet. **Dormant:** the engine has no MCX feed or instrument master, and the 0.01% commodity transaction tax is not in `config/rules` yet (it must be verified against a primary source first).

### High-IV routed debit spreads — `hiv` (rule 4)
Not a strategy of its own: when IVP ≥ 80, it receives the naked-buy signals of put-panic, OI-ignition and breakthrough, and places them as ~0.55/0.25-delta debit spreads. Comparing it with the naked accounts answers whether the rule helps.

---

## 6.4 Nightly paper strategies (not in the cockpit's intraday panel)

These run in the `evening` job on end-of-day data; see Research → **Swing**.

- **Swing: cash stocks and stock ATM options (SPEC 5.8):**
  - **Trigger:** multi-touch levels (touched ≥ 2 times in 60 sessions), cleared by 0.5% on 1.5× volume.
  - **Vehicle:** an ATM option when IV rank ≤ 50 and ≥ 5 days to expiry, else cash.
  - **Fills and exits:** fills at the next open; stops at the line, 25% of premium or 6% for cash; 2-session time stop; 15 sessions maximum.
  - **Results:** no long option within 10 days of results.
- **Trend sleeve (SPEC 5.6):** monthly 12-month momentum of NIFTYBEES vs the 91-day T-bill, 20% allocation, 10% disaster stop.

## 6.5 Not built or deferred

Pre-earnings straddles and stock F&O (deferred at ₹2 lakh capital), volatility-managed sizing, stock momentum, pairs, index-rebalance and IPO lock-in ideas, open-market buybacks. Research → **Strategies** keeps the honest status of every SPEC item.
