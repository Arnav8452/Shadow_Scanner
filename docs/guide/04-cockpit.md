# 4. The Quant Cockpit, element by element

The cockpit is a single local page at **http://127.0.0.1:8765**, served by `uv run python tasks.py cockpit`. It shows what the live engine (or a replay) is doing, five times a second. The amber **PAPER ONLY** badge is a fact, not a mode: the page cannot place a live order.

Everything labelled **HYPOTHESIS** is an unvalidated starting rule. Hover almost any label, number or bar for a tooltip that explains it.

The page has four tabs: **Desk**, **Option chain**, **Screener** and **Research**.

---

## 4.1 Header (always visible)

The header is kept to what you use. It is paper-only by construction: the page cannot place a live order.

| Element | What it shows | How to use it |
|---|---|---|
| **Desk / Option chain / Screener / Research** | Tab switch. | — |
| **Live / Replay** | Which engine the page shows. **Live** is today's engine; **Replay** shows the replay bar and the replay engine. | Switch to Replay in the evening to practise on a recorded day. |
| **Status line** (right) | `LIVE <date> <time> · feed <status> · lag <ms> · <n> contracts live · <n> readings today`, plus `DROPPED <n>` or `cockpit errors <n>` when something is wrong. | See below. |

**Status line:**
- **Lag** is receive time minus the exchange's timestamp. The exchange stamps whole seconds, and on 13 Nov this PC's clock was 1.3 s slow and had never synced. A steady negative lag of about −700 ms therefore means the PC clock, not the feed. Fix it in **Settings → Time & language → Date & time → Set time automatically → Sync now**.
- **Contracts live** is the engine's 30-level watchlist (up to 50).
- **Readings today** counts confirmed scanner episodes.

A line under the header appears when something is down: the page lost its link to the gateway (it reconnects by itself), the live engine is offline, or the feed is stale.

### Replay bar (Replay selected)

| Element | Use |
|---|---|
| **Date** | Any recorded day (default: yesterday). |
| **synthetic** | Replay a simulated recording from `data/sim/` instead. |
| **Speed** | 1×, 2×, 5×, 10×, 20×, 50× or *max*. Can be changed while playing. |
| **Start / Pause / Resume / Stop** | Control the replay engine. Start wipes that date's replay journals and begins again. |
| **Progress bar and text** | Replay clock time, % done, speed, paused or finished. |

### Notice banner

An amber line under the header appears when something needs attention:
- *Live engine offline — showing the last state it published*: the engine stopped; the page keeps the last snapshot.
- **FEED STALE: no quote for N s**: after 10 s without a quote, new paper entries freeze and exits pause until quotes return. Nothing is closed on stale prices.
- *No replay running* / *Replay engine starting*: replay status.

### Pop-ups (bottom right)

Green: a paper order filled or a command worked. Red: refused, with the reason (for example `per_trade_risk_exceeded (5400 > 5000)`: lower the stop % or pick a cheaper strike).

---

## 4.2 Desk tab

The Desk is a two-column layout. Left: events, index cards, setups, scanner, strategies. Right: the instrument panel (book, analytics, chain, ticket). Every divider can be dragged; double-click a divider to reset it. Sizes are remembered in your browser.

### Events line

`Events · <next macro events>` with time to each (`in 3.5 h`, `in 2 d`). Click the line or **All events ▾** to open the full list: date and time, title, how far away (or *NOW* while its window is open), and whether the date is confirmed from a primary source. The choice is remembered. A `?` after an event means its date is `UNVERIFIED` in the calendar. When an event window is active, an amber **EVENT WINDOW: … — new entries blocked** leads the line: every paper strategy's new entries are blocked until it ends. *results within 24 h: X% of index weight* shows how much of Nifty reports results in the next day. (The condor will not open a position it would hold through results worth 10% or more of the index.)

**One-line view / Full view** (button on the right) switches the index cards between one line each and the full grid.

### Index cards (one per index: NIFTY, SENSEX)

Top line: index, spot, day change, the nearest expiry, **trading minutes left** to that expiry (counted only in session hours; this is the time used for IV), and the time the numbers were computed.

| Field | Meaning | Why it matters |
|---|---|---|
| **Future · basis** | Front-month future price and its premium over spot. | A rising basis means traders pay up for longs (used by the married future). |
| **Synthetic forward** | The price options are really priced off: `F = K + e^(rT)(C_mid − P_mid)`, the median over 3–5 strikes near the money. Difference from spot shown. | Every IV and greek on the page uses this, not spot or the future. |
| **ATM IV (ours) (Nth pct)** | At-the-money implied volatility from our own Black-76 on mid prices, and its percentile against the last 252 sessions (needs 20+ sessions of history). Amber at the 50th percentile or above. | Rich premium (high percentile) favours selling spreads; cheap favours buying. Above the 80th percentile the playbook stops buying naked options (see [06-strategies.md](06-strategies.md#high-iv-routed-debit-spreads--hiv-rule-4)). |
| **India VIX** | NSE's volatility index. | Context; not used for pricing. |
| **OI walls call / put** | The call strike and put strike with the most open interest. | Call wall often acts as resistance, put wall as support. Used by OI-credit and OI-ignition. |
| **PCR (OI)** | Put OI ÷ call OI across the chain. | Above 1: more puts written (often read as support). Context, not a signal. |
| **Dealer gamma** | `γ+ dealers dampen moves` or `γ− dealers amplify moves`, with the **flip** level where it changes sign. | Labelled HYPOTHESIS: it assumes dealers are long calls and short puts, which nobody publishes in India. |
| **Skew 98/102%** | Put IV minus call IV at 2% either side of the forward, from the SVI smile fit. | Rising skew = puts bid up (fear). The compression strategy wants skew moving the breakout's way. |
| **Opening range 15m** | High–low of the first 15 minutes (`forming` until 09:30). | A key location for the scanner. |
| **Prev day H / L / C** | Yesterday's high, low and close. | Key locations; the married future triggers on breaks of these. |
| **Breadth (heavyweights)** | Index-weighted share of the top Nifty members up vs down, and the count up/down. | Breadth that agrees with a move adds points to the scanner's derivatives pillar. |
| **Correlation (60 min)** | Average pairwise correlation of the heavyweights' 1-minute returns: `macro` (≥ 0.6), `mixed`, `stock-specific` (≤ 0.3). | A broad macro move vs a few stocks moving the index. |

### Setups panel

The bridge between flickering readings and tradable ideas. Header text states the live rules: *a reading starts a setup · confirmed when the index moves 0.05% its way and holds 60 s within 10 min · fails at −0.08% or an opposite reading*.

**Futures absorption zones** (first line, when any exist): `floor 25,410–25,420 (14×)` means aggressive selling of 14× the largest bid ever shown traded inside that 10-point zone over the last 30 minutes, and price is now above it, so those sellers are trapped. A *ceiling* is the mirror. Context only. See [05-signals.md](05-signals.md#55-futures-absorption-zones).

Each setup row:

| Part | Meaning |
|---|---|
| **NIFTY ↑ / SENSEX ↓** | Index and direction. |
| **State** | `FORMING` (gold): first reading seen, waiting for the index to follow through. `CONFIRMED` (green): it did. `FAILED` / `ENDED` (grey): see [05-signals.md](05-signals.md#53-readings-and-setups). |
| **Why line** | When it started and at what spot, number of readings and the best score. Hover for every reading's time, label and score. |
| **Progress bar** | Forming: index move so far vs the 0.05% needed. Confirmed: points and % moved since the first reading. |

*Earlier today* lists finished setups. Confirmed setups are what the directional, debit-spread and synthetic-future strategies enter on, and what the condor treats as a trending tape.

### Scanner panel

Every live contract, ranked by its order-flow score.

**Controls**
- **All / Options / Fut/Index / Stocks / Score 65+**: filter. **Stocks** shows the delivery-spike stocks from last night's watchlist (up to 5, cash market, watch only), each tagged **accum** (accumulation: high delivery on an up day) or **churn** (a retail spike on low delivery). They are scored like everything else but rarely reach 65.
- **By score / By contract**: sort. By score re-sorts every 30 s and **never while your mouse is over the list** (`order frozen`), so rows don't jump under the pointer.
- **Keys:** ↑ ↓ move the selection; `/` focuses the search box.

**Columns**

| Column | Meaning |
|---|---|
| **Contract** | Name. `+feed` = added to the live feed on demand. |
| **LTP** | Last traded price; flashes green/red on a tick up/down. |
| **Chg %** | Change from the previous close. |
| **Bid × Ask** | Best bid and offer. |
| **OI** | Open interest (contracts × lot). |
| **ΔOI** | OI change since the engine first saw it today. |
| **IV** | Implied vol, our Black-76 on the synthetic forward. |
| **Δ** | Delta. |
| **Score (1 min)** | Bar: the average of the last minute's per-second scores (so it moves slowly). Grey under 65, gold 65–75, deep gold 75+. A gold left edge on the row marks 75+, a grey edge 65–75. Hover for the latest raw score and its inputs (OBI, RER, air pocket, CVD z). |
| **Reads** | What a 65+ reading says about the **index**: a bid-heavy put reads `NIFTY ↓`. |
| **Pattern** | The scanner's pattern name for 65+ readings: *stop hunt reclaim, wall breakout squeeze, initiative exhaustion fade, spoof rug pull, eaten wall squeeze*… See [05-signals.md](05-signals.md#patterns). |
| **OI read** | Price change vs OI change for the contract: *buying*, *writing*, *short covering*, *unwinding*, coloured by what it means for the index (writing puts = writers expect no fall = up). Hover for the OI change and its baseline (previous session where known). |

Click a row to open it in the instrument panel.

### Strategies (paper) panel

**Expand ⤢** (panel header) grows the panel to the window height; **Restore ⤡** puts it back. Overview and Playbook update row by row, so scrolling and dragging the scrollbar work while the numbers change; long rule and status text is clipped to three lines (hover for all of it).

Tabs: **Overview**, **Playbook**, **Condor / spreads (5.1)**, **Directional (5.2)**, **Debit spreads**, **Gamma 5.10**, **Score-follow**, **Manual**, **Signal study**, **Events**. Your last tab is remembered.

**Overview**: one row per strategy (all of [06-strategies.md](06-strategies.md)), with *today net across all* at the top.

| Column | Meaning |
|---|---|
| **Strategy · Ref** | Name and the source (`spec S1`, `rules.md`, `SPEC 5.1`…). |
| **Status** | `on` / `off`. |
| **Today net · Realized** | Net ₹ after all charges, today and in total for this account. |
| **Trades · W / L · Open** | Count, wins/losses, open positions. |
| **Rules / now** | The rule summary and what it is doing right now (watching, waiting for a retest, holding…). |

**Playbook**: the strategies from the owner's notes (`plays_long.py`, `plays_spread.py`) in one table: state (watching / holding / off), today net, trades, open, and **Now**, a status line rebuilt every step that puts each condition's live value next to the threshold it needs. Examples: *NIFTY: index 25,840 vs PDH 25,910 / PDL 25,700; futures OI +0.6% in 15 min (needs > +2%)*, *not a trading day for it (trades NIFTY Mon/Tue; SENSEX Thu); today is Fri*, *entries 09:45–14:00 (opens later)*. A multi-leg plan that could not get every leg into the live feed says so (*only 1 of 2 legs fit in the live feed*), and the latest refused or dropped attempt stays on the line. The same text is the *Rules / now* column of Overview for these strategies.

Click a row (in Overview or Playbook) to open that strategy's detail view: a **decision box** (green = ready, blue = holding, amber = no trade, with the reason), the full rule text, open positions and closed trades. The breakthrough scalper also shows its latest **5-pillar scores** per index, the contract, writer pain and the 25%/30% stop-cluster levels.

**Condor / spreads (5.1)**
- Rule line (hover for every rule) and the **decision**: *Holding*, *Ready* or *No trade* with the reason, plus the order-flow tally for the last 30 minutes (confirmed UP, DOWN, forming).
- **Entry checklist**: each condition with ✓ / ✗ / i: entry window, one structure a day, expiry far enough, no major event before the exit, IV percentile high, no event window, order-flow veto, dealer gamma (information only), strikes and credit, risk gate.
- **Open structure card**: strikes, expiry, credit received (points and ₹), cost to close now, the take-profit and stop levels, distance of each short strike from the index, worst-case loss, wing width; a leg table (entry, bid, ask); **Close (shorts first)** button.
- **Closed** and **Refused** lists.

**Directional (5.2), Gamma 5.10, Score-follow, Manual**: single-leg accounts, shown the same way:
- **Cards:** Day net · Realized net · Open P&L (net) · Charges paid · Trades (today/limit) · Wins/losses · **Loss room left** (until the daily loss limit; red under 30%) · **Cool-off** (until when, or losses in a row).
- **Positions:** contract, opening time and reason, a greeks line (IV, Δ, Γ, Θ ₹/day, vega ₹/vol pt, vanna, charm), qty, average, **Bid** (the mark), **Stop** (`↑` when a trailing stop has locked in a gain), **Target**, net, **Close** button.
- **Closed today** and **Refused by the risk gate / fills** with the reason.

**Debit spreads**: the decision box, each open spread (debit paid, worth now, max value, worst case, legs), closed and refused lists.

**Signal study**: nothing is traded here. Each confirmed reading is measured on the index and on the ATM option (bought at the ask, sold at the bid, full charges) at +15, +30, +60 minutes and the close. A summary table (signals, % that moved our way, average move, option net) and one row per signal with each horizon's result.

**Events**: the engine's event log (signals, fills, refusals, setup changes, condor decisions, commands), newest first.

### Instrument panel (right column)

**Search box**: type words from a name (`NIFTY 25000 CE`, `SENSEX FUT`, `RELIANCE`); ↓ ↑ and Enter to pick. A contract that is not in the live feed is added to it (**up to 3 extra**, shown as pills top-right; × removes one).

**Quote header**: name, LTP, change, lot size, expiry; then Bid × Ask, Spread · mid, Open/High/Low, Previous close, Volume, Open interest, ΔOI (and since when), Previous day H/L.

Three sub-tabs:

**Market depth** (default)
- **Price chart**: the last ~30 minutes of LTP. Dashed lines mark the latest reading's **entry (blue), stop (red) and targets (green)** and any open paper position's **average (gold), stop and target**. Levels far outside the recent range are listed but not drawn, so the chart stays readable.
- **Levels box**:
  - **Latest order-flow reading:** direction, grade and score, entry, SL, T1, T2 and reward-to-risk. Below 75 it says *shown, not acted on*. A SHORT reading on an option is marked as a naked short that is never traded.
  - **Open paper positions** in this contract, from any account: quantity, average, stop, target, net.
  - These levels exist for every scored contract, but only readings of 75+ held 5 s start setups and strategies.
- **Spread**: ask − bid and as % of mid; what a round trip at the touch costs before charges.
- **Micro-price**: mid weighted by the size waiting on each side; *leans up* / *leans down* toward the side with less size.
- **Orders bid / ask**: order counts on the visible levels (Upstox 30-level depth does not send them, so often *not in this feed*).
- **Largest bid / ask**: the biggest resting order each side (possible support / resistance).
- **Imbalance bars**: bids vs asks for the **top 5 levels** and for **all levels**.
- **Ladder**: up to 30 levels each side: orders, cumulative quantity, quantity (with a size bar), price.
  - **Highlighted sizes are walls** (at least 3× the median level on that side).
  - Price cells carry tags where a level sits in the book: **E**, **SL**, **T1/T2**, **AVG**, **STOP**, **TGT**.
  - Cumulative columns show how much you could fill before reaching that price. A dashed line marks level 6.

**Analytics**
- **Options:** implied vol, delta, gamma, theta per lot per day, vega per lot per vol point, days and trading minutes to expiry, intrinsic, time value, strike vs forward.
  - Greeks use **one IV per strike**, taken from the out-of-the-money option (see [05-signals.md](05-signals.md#implied-volatility-and-greeks)); hover the IV for the option's own-mid IV and the source.
  - On an option expiring today in its last minutes it shows **Settling** instead: the price tracks the settlement average, so IV and greeks would be fiction and readings are ignored.
- **Trade lab** (options), for 1 lot bought at the ask:
  - **Strike IV vs ATM IV:** rich or cheap on the smile.
  - **Index IV percentile:** at 80+ the playbook routes naked buys into spreads.
  - **Breakeven at expiry:** in points and % from spot.
  - **Expected move to expiry (1σ)** = forward × IV × √T on trading time, and how many σ away the breakeven is.
  - **Risk at the ticket's stop %:** premium × lot × stop, against the per-trade limit the risk gate applies.
  - **Scenario grid:** P&L per lot for an index move of −2% … +2% now, after one trading day and at expiry. Black-76 with IV held constant, before charges: a model, not a forecast.
  - **Payoff chart:** at expiry (solid) and today (dashed), with spot and breakeven marked.
- **Index and futures:** previous day H/L, previous close, day range.
- **Order-flow reading:**
  - latest score and grade, 1-minute average, what it says about the index, pattern, futures OI quadrant, time;
  - **entry · SL**, **T1 · T2**, reward-to-risk;
  - the **four pillar bars** (Location /25, Order flow /30, Book shape /25, Derivatives /20; hover each for what it measures), with a reminder of the 75 threshold and the location cap;
  - **OBI**, **RER**, **Air pocket**, **CVD z-score**. Definitions in [05-signals.md](05-signals.md).

**Option chain** (compact):
- **Picker:** index buttons and an **expiry picker**, covering every recorded expiry including the next monthly.
- **Summary line:** forward, the **ATM straddle** (the move the market prices to expiry), PCR, **max pain** (amber strike), trading minutes left.
- **Columns:** ±12 strikes around the forward. Each side shows OI in lakh with a bar, ΔOI vs the previous session, delta and LTP with % change; the **strike IV** sits under each strike.
- **Hover** a cell for bid × ask, IV, delta, theta and volume.
- **Marking:** the ATM row is highlighted and ITM cells tinted. **OI wall strikes are gold**, **outlined cells are open condor legs**, and the **blue outline is the contract open in the panel**.
- **Click** any cell to open that contract.
- **Smile** underneath: OTM IVs as dots, the SVI fit as a line, the forward dashed, with ATM IV, 98/102 skew and fit error.

**Paper ticket** (bottom)
- **Stop __ %**: paper stop as % of premium (default 25%). Needed so the risk gate knows the maximum loss.
- **Buy 1 lot @ ask** (key **B**): long options only.
- **Close** (key **C**): close your manual position in this contract.
- **Flatten manual**: close every open manual paper position (asks to confirm).
- Every order goes through the same risk gate as the strategies. See [07-paper-trading.md](07-paper-trading.md).

---

## 4.3 Option chain tab

A full Sensibull-style chain from the recorder's 1-minute snapshots, with every number computed by us.

**Bar:** index buttons · **Expiry** picker (every recorded expiry, including the next monthly) · **LTP view / Greeks view / All columns** · **Per lot** (greeks and OI per lot instead of per unit) · a stats line: spot and change, synthetic forward, ATM IV and IVP, PCR by OI and by volume, **max pain**, total call and put OI with change, lot, trading minutes left, time.

**Columns** (calls on the left, puts mirrored on the right, strike and IV in the middle):

| View | Columns |
|---|---|
| **LTP** | OI chg %, OI (lakh) with a bar, LTP and % change |
| **Greeks** | Gamma, Vega, Theta, Delta, OI (lakh), LTP |
| **All** | POP, Volume, OI chg, OI chg %, OI (lakh), Bid, Ask, Intrinsic, Time value, Breakeven (and % away), Delta, LTP chg, LTP; plus a per-strike **PCR** column in the middle |

- **IV** (middle) is the out-of-the-money side's IV at that strike.
- **OI change** is vs the previous session's NSE bhavcopy (Sensex is BSE and not in that file, so it has none).
- **POP** is the chance a buyer at the ask ends past breakeven under a lognormal with the strike's IV: a model figure, not a forecast.
- Gold outline = open condor leg. The ATM row has a blue top border. Click any price to open that contract on the Desk.

---

## 4.4 Screener tab

All ~220 F&O underlyings (indices and stocks) from the latest NSE end-of-day files. Because a day's bhavcopy is published at about 19:30, during the session the screener shows **yesterday's close**; live index rows (marked `live`) sit on top.

**Filters:** All · Indices · Stocks · Flagged · Rich premium · Cheap options · Unusual activity · a symbol search · **Refresh**. Click a column header to sort; click a row to search its future on the Desk. The first run of a day parses about 21 bhavcopies (~40 s).

| Column | Meaning |
|---|---|
| **Fut price · Chg %** | Front future close and change. |
| **ATM IV · IV chg · IVP** | Our ATM IV from closes, its change, and its percentile over up to 252 sessions (needs 60+). IVP ≥ 70 is amber, ≤ 25 green. |
| **Results** | Next results date and days away. |
| **Volume ×** | Contracts traded vs the 20-session average (green at 2×+). |
| **Fut OI % chg** | Futures OI change. |
| **Price / OI** | Quadrant: *long build-up* (price ↑ OI ↑), *short build-up* (↓ ↑), *short covering* (↑ ↓), *long unwinding* (↓ ↓). |
| **PCR · Max pain** | Near-expiry put/call OI ratio; max-pain strike and its distance from price. |
| **Flags (HYPOTHESIS)** | `F&O ban: no new positions` · `rich premium: credit spread / condor candidate` (IVP ≥ 70, no results within 7 days) · `cheap options: buying candidate` (IVP ≤ 25) · `results in N d: IV crush risk` (≤ 10 days) · `unusual activity: N× volume` (≥ 2×). |

Flags are prompts for your attention, not signals. Stock F&O is out of scope at ₹2 lakh, so stock rows are context.

---

## 4.5 Research tab

Read-only panels computed on request from the journals and nightly files. **Columns 1/2/3** changes the layout; drag a panel's corner to resize; double-click a panel title to make it full width; **Reset layout**; **Refresh all**.

| Panel | What it answers |
|---|---|
| **Strategies (SPEC §5 and Addendum A)** | Every SPEC strategy with its honest status (*live* — none, *paper*, *research*, *not built*), where it lives, and when it last produced output. |
| **Signal study: does the reading predict the index?** | By **score band**: for 15/30/60 min and the close, the % of signals where the index moved the signal's way, n, the t-statistic, and the ATM option's average net result per trade after charges. **The key table**: if higher bands don't do better, the score is not worth trading. |
| **Study by setup, hour, index** | The same, split by index and direction, setup pattern and hour. |
| **Which conditions did the results come from?** | Closed paper trades (net ₹) and study signals (% moved our way, average move, t) split by the entry conditions recorded with them: entry hour, IV percentile, VIX, dealer gamma, PCR, position vs previous-day range and opening range, breadth, correlation, event window, expiry day, opening gap, trend vs chop, VIX change, absorption zone, breadth vs direction. A t of 3 or more is highlighted. Pick the signal horizon top-right. Many cells are compared, so treat a gap as a lead to test, not an edge. |
| **Micro ledger (legacy scalp exits)** | The retired Track A (fixed micro stop/target exits on the signal contract) by score band, setup, hour and exit, with net at 2× slippage. Kept for comparison only. |
| **Paper accounts by day** | Trades and net per day for bot, manual and condor. |
| **Nifty ATM IV (EOD) and India VIX** | History chart of our EOD ATM IV and VIX. |
| **Flows and reference (EOD)** | Participant OI and volume (FII, DII, Pro, Client: index futures net/long/short, index calls and puts net, stock futures net), tomorrow's **F&O ban list**, the largest **bulk/block deals**. |
| **Delivery-spike stocks: results and holdings** | Tomorrow's delivery watchlist with bias (accumulation / retail churn), delivery multiple and %, the latest quarter's revenue, PAT and margin with YoY, promoter/FII/DII holdings and changes, and a link to the filing. |
| **Swing** | Pick a night: candidates (setup, level, volume multiple, IV rank, vehicle, qty, entry, stop, risk, why, queued or not), the trend-sleeve signal, the swing paper account (equity, cash, open risk, exposure, kill switch), queued orders, open and closed positions. |
| **Event arbitrage** | Curated events with verdict (GREEN/AMBER/RED), last buy day, shares, cushion at pessimistic acceptance and flags; announcements to curate from the last 60 days; saved reports. |
| **Recorder QC** | Each recorded day: PASS/FAIL, rows, max gap, problems. |
| **Trade-mining reports (private)** | Reports mined from reference trade logs (`tasks.py mine-log`). Local only. |

---

## 4.6 Keyboard and mouse summary

| Key / action | Does |
|---|---|
| `/` | Focus the search box |
| ↑ ↓ | Move through the scanner |
| `B` | Buy 1 lot at the ask (selected option) |
| `C` | Close the manual position in the selected contract |
| Click a scanner row, chain cell or search result | Open that contract |
| Drag a divider · double-click it | Resize · reset |
| Hover | Tooltips on nearly every label, bar and number |
