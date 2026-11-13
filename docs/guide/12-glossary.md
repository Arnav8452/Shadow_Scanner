# 12. Glossary

| Term | Meaning |
|---|---|
| **0DTE** | An option on its expiry day ("zero days to expiry"). |
| **A+ / Actionable / Watchlist / Noise** | Scanner grades: 80+, 75–79.9, 65–74.9, below 65 (or a spoof pull). |
| **Absorption** | Large aggressive volume that fails to move price: a passive order soaking it up. |
| **Absorption zone (floor / ceiling)** | A 10-point futures zone where aggressive selling (buying) of 12× the largest displayed bid (offer) was absorbed over 8+ minutes, and price has since left it, trapping the aggressors. |
| **Air pocket** | Thin levels ahead of price in the book (levels 6–25 vs 1–5); price can move fast through them. |
| **APPROVED** | A setting the owner has signed off. Don't change it without asking. |
| **ATM / ITM / OTM** | At, in or out of the money: strike near, favourable to, or unfavourable to the current forward. |
| **ATR** | Average true range: the typical bar's movement (1-minute bars here). |
| **Basis** | Future minus spot. **Annualized basis** = basis ÷ spot × 365 ÷ days to expiry. |
| **Bhavcopy** | The exchange's end-of-day file of every contract's prices, volume and OI. |
| **Black-76** | The option-pricing model on a forward price, used for all IV and greeks here. |
| **Breakeven (true)** | The exit price at which a trade nets zero after every charge, plus a slippage buffer. |
| **Breakeven ratchet** | Gate 2: stop to true breakeven once the bid clears 2× breakeven points, then trailed; scratched if still below breakeven after 180 s. |
| **Breadth** | Share of index heavyweights (by weight) up vs down on the day. |
| **CAS** | Closing Auction Session in the cash market (from 3 Aug 2026). |
| **Charges** | Brokerage, STT, exchange transaction charges, SEBI fee, stamp duty and GST, all from `config/rules`. |
| **Condor / iron condor** | Sell an OTM put and call, buy further-OTM wings: defined-risk premium selling. |
| **Confirmed setup** | A setup whose index moved 0.05% its way and held 60 s within 10 minutes. |
| **Cost to close** | What it would cost to exit a spread now (shorts bought at the ask, longs sold at the bid). |
| **CVD** | Cumulative volume delta: aggressive buys minus aggressive sells. **CVD z** = in standard deviations. |
| **Debit / credit spread** | Two options of the same type: pay to enter (debit, directional) or get paid (credit, premium selling). |
| **Defined risk** | The maximum loss is known and capped at entry by a long hedge leg. |
| **Deflated Sharpe ratio (DSR)** | The probability that a Sharpe ratio is real after accounting for how many variants were tried. |
| **Delta (Δ)** | Option premium change per 1-point index move; also roughly the probability of finishing ITM. |
| **Depth / L1–L30** | Order-book levels. Upstox `full` gives 5; `full_d30` (Plus) gives 30. |
| **Eaten wall** | A displayed wall that was traded away; the side that took it out is read as winning. |
| **Event window** | A period around a macro event (RBI, Budget, CPI, FOMC…) when new paper entries are blocked. |
| **Forward (synthetic)** | `F = K + e^(rT)(C_mid − P_mid)`, median over near-ATM strikes: what options are priced off. |
| **Freeze quantity** | The exchange's maximum quantity per order; larger orders are sliced. |
| **Gamma (Γ)** | Change in delta per 1-point index move. |
| **Gate 1 / Gate 2** | Charges gates: the impulse test at entry, and the breakeven ratchet in the trade. |
| **GEX (dealer gamma)** | Estimated gamma of dealers' option positions. Positive dampens moves, negative amplifies. HYPOTHESIS: assumes dealers are long calls and short puts. |
| **Greeks** | Delta, gamma, theta, vega, vanna, charm. |
| **HYPOTHESIS** | An unvalidated starting threshold, to be measured before any trust. |
| **Iceberg** | A passive order that keeps reloading after being hit, so more trades there than was ever displayed. |
| **IV** | Implied volatility, solved here from mid prices with Black-76 and trading-minute time. |
| **IVP / IV percentile** | Where today's ATM IV ranks against the last 252 sessions. |
| **Kelly (quarter)** | A sizing formula from win rate and payoff, used at 25% and capped at 1.5% risk. |
| **Kill switch** | Flattens and blocks entries on loss limits, open risk, a reconciliation mismatch or a long disconnect. |
| **Location** | Pillar 1: is price at a level where stops pool (previous-day high/low, opening range, OI walls)? |
| **Lot** | The contract multiplier (Nifty 65). |
| **Married future** | A future hedged with an ATM option: long future + long put, or short future + long call. |
| **Max pain** | The strike at which option buyers in total would lose the most at expiry. |
| **Micro-price** | Mid weighted by the size on each side; leans toward the side with less size. |
| **Mode A / Mode B** | `MODE=free` (free data, paper only) / `MODE=paid` (paid data; live possible only with the lock). |
| **Model-price stop confirmation** | A stop fires only if the Black-76 model price agrees, ignoring quote air pockets. |
| **OBI** | Order-book imbalance in the top 5 levels: (bids − asks) ÷ total, −1 to +1. |
| **OI** | Open interest: contracts outstanding. **OI wall**: the strike with the most OI. |
| **OI quadrant** | Long build-up (P↑ OI↑), short build-up (P↓ OI↑), short covering (P↑ OI↓), long unwinding (P↓ OI↓). |
| **Opening range (OR)** | The first 15 (strip) or 30 (playbook) minutes' high and low. |
| **Paper trade** | A simulated trade at real recorded bid/ask with full costs. No money moves. |
| **PCR** | Put-call ratio of OI (or volume). Not to be confused with the **phantom cancel ratio** in the spoof tracker: wall size that vanished without trading ÷ peak size. |
| **Pin / pinned** | (1) An index held near a strike before expiry. (2) A contract added to the live feed on demand. |
| **Playbook strategies** | The strategies from the owner's notes in `new/` (`plays_long.py`, `plays_spread.py`). |
| **POP** | Probability that a buyer at the ask ends past breakeven, from a lognormal with the strike's IV. A model figure. |
| **Reading** | A scanner episode of 75+ that held 5 s, expressed as UP or DOWN on its index. |
| **RER** | Real execution ratio: traded volume ÷ size that left the book. Low = pulled (spoofed); high = genuinely eaten. |
| **Replay** | Playing a recorded day back through the engine, with separate journals. |
| **RV** | Realized volatility, from actual price moves. |
| **Second mouse** | Waiting for a retest that holds instead of chasing the first break. |
| **Setup** | A group of readings for one index and direction: FORMING → CONFIRMED / FAILED → ENDED. |
| **Settling** | An option expiring today in its last minutes, priced off the settlement average; no greeks or readings. |
| **Skew (98/102)** | IV at 98% of the forward minus IV at 102%. |
| **Slippage (2×)** | The stress test: double the observed fill cost vs mid. |
| **Smile / SVI** | IV across strikes; SVI is the arbitrage-free curve fitted to it. |
| **Spoof rug-pull** | A wall pulled without trades as price approached: bait. Trade against the side that showed it. |
| **Straddle** | ATM call + ATM put. Its price ≈ the expected move. |
| **STT** | Securities transaction tax. |
| **Synthetic future** | Long call + short put at one strike (or the mirror); here hedged with a further wing. |
| **t-statistic (t)** | Mean result ÷ its standard error. About 3 is the bar for a new signal. |
| **Tape** | The per-index minute series the playbook strategies read. |
| **Term spread** | Near-expiry ATM IV minus far-expiry ATM IV; the calendar's signal. |
| **Theta (Θ)** | Premium lost per day to time decay (₹ per lot per day on the page). |
| **Trading minutes** | Time to expiry counted only in session minutes; the T used for IV. |
| **Track A / B / C** | Older names: A = every signal's ledger, B = the bot account, C = manual. |
| **UNVERIFIED** | A rule row not yet confirmed against a primary source. |
| **Vanna / charm** | Delta change per vol point / per day. |
| **Vega** | Premium change per 1 vol point (₹ per lot on the page). |
| **VIX (India)** | NSE's volatility index. |
| **VRP** | Volatility risk premium: implied minus subsequently realized volatility. |
| **VWAP** | Volume-weighted average price for the session. |
| **Wall** | A book level with ≥ 3× the median size on its side. |
| **Wing** | The bought, further-OTM leg that caps a spread's risk. |
| **Writer pain** | How far an option has moved against those who sold it at an anchor (09:20 price or VWAP); their stops add fuel. |
