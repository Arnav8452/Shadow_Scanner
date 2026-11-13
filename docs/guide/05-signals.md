# 5. Signals: how each reading is made

Every number here is computed by the engine from raw quotes, books and exchange files. Broker and NSE analytics fields (their IV, greeks, PCR) are never used. Every threshold is a **HYPOTHESIS** in `config/settings.yaml` (`shadow_scanner`, `cockpit`) or in the strategy's code, until 20–30 sessions of results say otherwise.

The chain, from raw data to a trade:

```
quotes + 30-level books ─► scanner score (0–100, four pillars) ─► reading (75+, held 5 s)
                         ─► setup (index follows through: CONFIRMED) ─► strategies
1-min option chain ─► OI walls, PCR, GEX, smile, strikes by delta ─► strategies
minute tape ─► CVD, ATR, VWAP, opening range, straddle, IV changes, OI velocity ─► strategies
```

---

## 5.1 Pricing foundations

### Synthetic forward
Options are priced off the forward, not spot or the future. For 3–5 strikes near the money, `F = K + e^(rT)·(C_mid − P_mid)`; the median is used. `r` is the 91-day T-bill rate from `config/rules/risk_free_rate.csv`.

### Implied volatility and greeks
Black-76 on the **mid price** of each option, against that expiry's synthetic forward, solved with `scipy`. Time to expiry is counted in **trading minutes** to the session close (15:40 since 3 Aug 2026), so overnight and weekends don't inflate IV. Delta, gamma, theta (₹/lot/day), vega (₹/lot/vol point), vanna and charm follow from the same model.

**One IV per strike.** Greeks for both the call and the put at a strike use the **out-of-the-money option's IV**: the call at or above the forward, the put below it. This is how the market quotes the smile (Sensibull does the same).
- **Why:** a deep in-the-money option trades inside a wide spread with little time value, so the IV solved from its own mid is unreliable. On 13 Nov at 09:30 the 24400 call's own mid gave delta 0.99 and vega 0.7, while parity with the 24400 put (delta −0.05) says 0.95.
- **What you see:** the option's own-mid IV is kept as `iv_own`, shown on hover.
- **Fallbacks:** live contracts take the strike IV from the live OTM option at the same strike, else from the 1-minute chain, else their own.
- **Remaining small differences from Sensibull** come from the forward, read a few seconds apart, and from the time convention: trading minutes here, calendar time there, which mainly changes theta and vega per day.

An option is **settling** (no IV, no greeks, no readings) when it expires today and is in its last minutes: its price then tracks the settlement average, not an option model.

### IV percentile (IVP)
Today's ATM IV ranked against the last 252 sessions of our own end-of-day ATM IV (`iv_history.parquet`). Needs 20+ sessions on the Desk, 60+ in the screener. High IVP = rich premium.

### Smile and skew
OTM IVs across strikes, fitted with an arbitrage-free SVI curve. **Skew 98/102** = IV at 98% of the forward minus IV at 102%. The fit error (vol points) says how much to trust it.

### Dealer gamma (GEX) — HYPOTHESIS
Sum of gamma × OI across the chain, signed on the assumption that dealers are long calls and short puts. Positive: dealers' hedging dampens moves. Negative: it amplifies them. The **flip** is the index level where the sign changes. Nobody publishes dealer positions in India, so this is a guess.

### OI walls, PCR, max pain
From the recorder's 1-minute chain: the call and put strikes with the most OI, put OI ÷ call OI, and the strike where option buyers in total would lose the most at expiry.

---

## 5.2 The scanner score (0–100)

Code: `engine/shadow/scanner.py` (`evaluate_instrument`). Every book update, each eligible contract gets four pillar scores. The total is capped at 100.

### Who is scored (eligibility)
An option is skipped when:
- its mid is below **₹5** (Sensex **₹15**);
- its spread is wider than **5% of mid**;
- its ask is at the tick floor (≤ ₹0.25) or it has no bid.

An option carries no **reading** when:
- its |delta| is outside **0.15–0.85**;
- its time value is under **5%** of its price;
- it is after **15:15**, or after **14:45** on its index's expiry day.

### Pillar 1 — Location (0–25)
Is price at a level where stops pool? The levels are the previous-day high/low, the opening-range high/low, equal highs/lows, the OI walls and round numbers.

| Situation | Points |
|---|---|
| Price swept through a level (by 0.15–0.40%) and reclaimed it (a stop hunt) | 25 |
| Price is inside the sweep zone beyond a level | 20 |
| Price is testing a level (within 0.25%) | 15 |
| Breaking above the opening-range high or previous-day high (breakout) | at least 22 |
| A pulled or eaten wall in the last 30 s (see 5.4) | at least 15 |

**Location gate:** with fewer than **8** location points the total is capped at **60**, below the 65 watch line. A good-looking book in the middle of nowhere is not a reading.

### Pillar 2 — Order flow and anti-spoofing (0–30)

| Input | Points |
|---|---|
| **RER ≥ 0.75** and the wall has rested ≥ 500 ms (genuine execution) | 15 (8 if RER ≥ 0.50) |
| **CVD z-score** \|z\| ≥ 2.0 / ≥ 1.2 / ≥ 0.6 | 15 / 9 / 4 |
| **Spoof pull** (a wall shrank with RER < 0.20) | the whole pillar is **0** and the grade is NOISE |

- **RER (real execution ratio)** = volume actually traded ÷ the size that left the book. Near 1: the wall was eaten by real trades. Near 0: it was pulled (spoofed).
- **CVD z-score** = aggressive buying minus selling (trades at the ask minus trades at the bid), in standard deviations of recent flow.

### Pillar 3 — Book shape (0–25)

| Input | Points |
|---|---|
| **OBI (top 5)** \|OBI\| ≥ 0.50 / ≥ 0.30 | 15 / 9 |
| **Air pocket** ratio < 0.55 / < 0.75 | 10 / 5 |
| The touch and the top-5 imbalance disagree | pillar × 0.5 |
| Levels 2–5 lean hard one way while the touch leans the other (suspected bait) | pillar × 0.25 |

- **OBI** = (bid size − ask size) ÷ total in the top 5 levels, exponentially weighted toward the touch. +1 is all bids.
- **Air pocket** = size in levels 6–25 relative to levels 1–5 on the side price is moving into. Low = thin ahead, so price can move fast.

### Pillar 4 — Derivatives (0–20)
The front future's price and OI change over about 3 minutes:

| Quadrant | Long points | Short points |
|---|---|---|
| Long build-up (price ↑, OI ↑) | 20 | 0 |
| Short covering (price ↑, OI ↓) | 18 | 0 |
| Short build-up (price ↓, OI ↑) | 0 | 20 |
| Long liquidation (price ↓, OI ↓) | 16 if this is a stop-hunt reclaim, else 2 | 4 / 14 |
| Mixed | 8 | 8 |
| Neutral drift | 5 | 5 |
| No OI history yet | 0 | 0 |

Plus **2** when heavyweight breadth agrees. Delivery bias would add **4**, but it is switched off (`delivery_watch.bias_in_score: false`) until the delivery event study supports it.

### Grades

| Score | Grade |
|---|---|
| 80+ | **A+** |
| 75–79.9 | **Actionable** |
| 65–74.9 | **Watchlist** |
| < 65, or a spoof pull | **Noise** |

A grade of 75+ counts only after it has held **5 seconds in the same direction** (`signal_confirm_s`); scores cross thresholds on noise every second. The page shows the **1-minute average** of the per-second score, which moves slowly on purpose.

### Patterns
The scanner names what it thinks is happening:

| Pattern | Meaning |
|---|---|
| **Stop hunt reclaim** | Price swept a level, triggered the stops behind it, and came back (default). |
| **Wall breakout squeeze** | A stop run or short squeeze, or a break above the opening-range or previous-day high. |
| **Initiative exhaustion fade** | Aggressive buying or selling ran out without moving price (a failed auction). |
| **VWAP delivery reclaim** | A delivery-spike stock reclaiming VWAP (stocks only). |
| **CAS 3 PM VWAP reversal** | Cash stocks only, 15:00–15:40, when the closing-auction price diverges from the trade price. Index options and futures have no closing auction. |
| **Spoof rug pull** | A displayed wall was pulled without trades (see 5.4). |
| **Eaten wall squeeze** | A wall was traded away; the side that ate it wins. |

### Reads (what it says about the index)
A reading on an option is translated into a view on the index: buying pressure on a call or selling pressure on a put reads **UP**; buying pressure on a put reads **DOWN**. A SHORT reading on an option would mean selling it naked, which is never traded; it is tracked as "not tradable".

### Stops, targets and size in the signal
Each signal carries an entry, a stop a couple of ticks behind the nearest wall or sweep extreme, and targets at ≥ 1.5R and ≥ 2.5R. A quarter-Kelly risk % from the last 100 signals (capped at 1.5%) suggests a size, always shown as one lot and never above the lot cap.

---

## 5.3 Readings and setups

Code: `engine/cockpit/setups.py`. Settings: `cockpit.setups`.

A **reading** is a scanner episode that reached 75+ and held 5 s, expressed as UP or DOWN on its index. Readings are inputs, never trades.

A **setup** gathers readings for one index and one direction:

| State | Meaning |
|---|---|
| **FORMING** | First reading seen; waiting for the index to follow through. |
| **CONFIRMED** | The index moved **0.05%** the setup's way (about 13 Nifty points) from where the first reading appeared and stayed past that line for **60 s**, within **10 minutes**. |
| **FAILED** | An opposite reading came first, the index moved **0.08%** against it, or 10 minutes passed with no follow-through. |
| **ENDED** | A confirmed setup gave the whole move back, an opposite setup confirmed, or it is **60 minutes** old. |

Every state change is written to `data/state/setups.jsonl`, so the day's list survives a restart. The setup's score is the best reading's score.

**Used by:** directional (5.2), debit spreads, hedged synthetic future, the condor's order-flow veto and its defence exit, and the signal study.

---

## 5.4 Spoofing and walls

Code: `engine/shadow/spoof.py`. The Upstox 30-level feed sends no order counts, so walls are tracked by price across snapshots.

- A **wall** is a level holding at least **3×** the median quantity on its side.
- When a wall loses most of its peak size, the tracker asks whether it was **eaten** (volume traded at or within 2 ticks of its price) or **pulled** (gone without trades).
- **Phantom cancel ratio (PCR, not the put/call ratio)** = size that vanished without trading ÷ peak size.
  - **Spoof rug-pull:** PCR ≥ **0.75** while price had come within 10 ticks. The side that showed the wall was bait, so trade the opposite: a pulled bid wall reads **bearish**.
  - **Eaten wall squeeze:** PCR ≤ **0.20** with ≥ **70%** of the peak traded. The side that took it out wins: an eaten ask wall reads **bullish**.
- **Touch vs deep book:** levels 2–5 leaning beyond 0.45 one way while the touch leans beyond 0.35 the other way marks the deep wall as suspect bait; the book pillar is multiplied by **0.25**.

A wall event sets the scanner's pattern and direction for **30 s** and counts as **15** location points. The *Spoof rug-pull / eaten wall* strategy trades these events directly.

---

## 5.5 Futures absorption zones

Code: `engine/cockpit/zones.py`. Context and confirmation, not an entry trigger.

The front future's tape is split into **10-point zones** over a rolling **30 minutes**. For each zone the engine compares aggressive volume that traded there with the largest size ever displayed there:

- **Floor (bullish):** aggressive selling of at least **12×** the largest bid shown, over at least **8 minutes**, net delta negative, and price now **above** the zone. Sellers kept hitting a bid that kept reloading (an iceberg) and are now trapped.
- **Ceiling (bearish):** the mirror image.

Trade side is inferred from each ~0.2 s sample against the quote **before** the trade (Lee–Ready): at or above the previous ask is a buy, at or below the previous bid a sell, else by the side of the previous mid, with the tick direction for an exact mid print. Judging against the post-trade quote would read a sweep that lifted the touch as selling. Sampling is still coarser than true tick data. Zones show on the Setups panel, are stored with every trade's entry conditions, and drive the *absorption-zone wave rider* strategy.

---

## 5.6 The OI read (per option)

The **OI read** column compares the option's price change (vs previous close) with its OI change (vs the previous session's bhavcopy when available, else since the engine started):

| Price | OI | Label | Index view |
|---|---|---|---|
| ↑ | ↑ | buying (long build-up) | — |
| ↑ | ↓ | short covering | calls: UP · puts: DOWN |
| ↓ | ↑ | writing (short build-up) | puts written: UP · calls written: DOWN |
| ↓ | ↓ | long unwinding | — |

The read takes fresh writing as the writers' view of the index. That is a common market reading, not a tested fact here.

---

## 5.7 The minute tape (what the playbook strategies read)

Code: `engine/cockpit/tape.py`. Built every step from the index, the front future and the ATM options. Per 1-minute bar:

- index OHLC and futures close;
- futures volume split into **aggressive buys and sells** by the quote rule.

Measures derived from the bars:

| Measure | Meaning |
|---|---|
| **CVD** and its z-score | Cumulative aggressive buys minus sells. |
| **Relative volume** | This bar's volume ÷ recent average. |
| **ATR (1 min)** | Average true range: typical 1-minute movement. |
| **VWAP** | Volume-weighted average price for the session. |
| **Opening range (30 min)** | High/low of 09:15–09:45. |
| **Realized vol** | Annualized from 1-minute returns. |
| **ATM straddle** | Call + put mid, its VWAP and standard deviation. |
| **IV changes** | ATM, call, put and next-expiry IV, and 25-delta skew over the last minutes. |
| **Per-strike OI history** | For OI velocity (e.g. "OI down 3% in 6 min"). |
| **15-minute bars and Bollinger width** | Compression. |
| **60-minute box** | Range for breakouts. |
| **VWAP whipsaws** | How often price crossed VWAP (chop). |

Each traded option also gets its own tape: its VWAP, its 09:20 price and a 1-minute **EMA-9** (for trailing stops).

---

## 5.8 The charges gates (why short holds are allowed at all)

Code: `engine/cockpit/gates.py`. Owner rule (12 Nov 2026): a short hold ("scalp") is allowed only in the strategies the notes specify, and only when the expected move clears charges by a margin.

- **True breakeven:** the exit price at which a long option bought at the ask nets zero after the full round trip's charges (brokerage, STT, exchange fees, SEBI fee, stamp duty, GST, all from `config/rules`), plus a 1-point slippage buffer, rounded up to the tick.
- **Gate 1, impulse:** the option's expected 3-minute move, `ATR₁ₘ·√3·|Δ| + ½·Γ·(ATR₁ₘ·√3)²`, must be at least **3.5×** the breakeven points (**2.5×** for the 5-pillar breakthrough scalper). Otherwise there is no entry.
- **Gate 2, breakeven ratchet:**
  1. The initial stop is **−12% of premium or ₹1,200 on the position**, whichever is tighter. It is booked at entry, so the risk gate sees the real planned loss.
  2. When the bid clears entry + **2×** breakeven points, the stop moves to true breakeven: the trade can no longer lose money after costs.
  3. Once the bid is beyond **3×** breakeven points, the stop trails **1.5×** breakeven points behind it.
  4. If the trade is still below breakeven after **180 s**, it is scratched.
  5. **After an engine restart**, open scalps are put back under the ratchet using their real open time, so a scalp held past 180 s below breakeven is scratched on the first step.
- **Strike band:** scalps trade only **0.55–0.65 delta** options (target 0.60), so intrinsic movement dominates theta. 0DTE keeps its ATM spec and is the one exception.
- **Rule 2, the vacuum score:** a scalp also needs the rules.md 5-pillar confidence (book, volume delta, writer pain + OI unwinding, gamma impulse vs breakeven, IV velocity) at **65+**, and no absorption on the trigger bar. The breakthrough scalper is that score; spoof fade now checks it too.
- **Charges plan:** the breakeven uses the configured brokerage plan (`shadow_scanner.cost_broker`). On Upstox Plus (₹30 an order) a ₹150 Nifty lot needs about 2.6 points. The owner's worked example of 2.10 points assumes ₹20 an order, which the tests reproduce to within a few paise.
- **Model-price stop confirmation:** a long option's stop fires only if the Black-76 model price (from the forward and a smoothed IV) also says it should. A quote air pocket the index never made does not stop you out; the page shows when a stop is being held for this reason.
- **Off-cycle entries:** no entries in the first 45 s of minutes :00, :15, :20, :30, :45, when retail algorithms fire.
- **Absorption veto:** no entry on a trigger bar with ≥ 2.5× volume and a body under 0.35 ATR, or ≥ 2× volume with a rejection wick over 55% of the bar: effort without result.
- **Heavyweight alignment:** at least 4 of the 6 largest Nifty members (HDFCBANK, RELIANCE, ICICIBANK, INFY, LT, TCS) moving the trade's way today. This uses day returns as a stand-in for the notes' cash-market CVD, because the engine has no stock tick data.

---

## 5.9 Entry conditions stored with every trade

Every paper trade and every signal-study row stores the market conditions at entry, so the Research tab can split results by them:

- entry hour;
- IV percentile and VIX, and VIX change since the open;
- dealer gamma regime and PCR;
- position vs the previous-day range and vs the opening range;
- heavyweight breadth % and correlation;
- inside an event window or not, and expiry day or not;
- opening gap %;
- day efficiency (trend vs chop);
- absorption zone.

This is the main tool for finding *when* a strategy works, not just *whether*.
