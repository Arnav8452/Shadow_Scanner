# Data dictionary

## Rule tables — `config/rules/*.csv` and `margin_rules.yaml`

Loaded by `engine.core.rules.RuleBook`. Validated by `tasks.py rules` and the test suite.

| Column | Meaning |
|---|---|
| *(key columns)* | Any column not listed below identifies the rule (e.g. `segment`, `leg`, `exchange`, `underlying`). Lookups must give every key. |
| `value` | The number/code. Percentages are in **percent** (`0.15` = 0.15%), not fractions. |
| `unit` | e.g. `pct_of_premium`, `pct_of_price`, `inr_per_crore_turnover`, `inr_per_executed_order`, `units_per_lot`, `units_per_order`, `weekday`. |
| `effective_from` | First day in force (inclusive), `YYYY-MM-DD`. |
| `effective_to` | First day **no longer** in force (exclusive). Blank = still in force. Rows chain as `[a,b) [b,c)`. Overlaps for the same key are rejected. |
| `source_url` | Where the value was checked. Required unless `UNVERIFIED`. |
| `verified_on` | Date the value was checked. Required unless `UNVERIFIED`. |
| `status` | `primary` (exchange/SEBI/government document), `secondary` (broker or other reputable page), `UNVERIFIED`. |
| `notes` | Caveats, circular numbers, what still needs checking. |

**Lookups refuse `UNVERIFIED` rows** unless `allow_unverified=True` is passed. Cost and strategy code must not pass that flag in live or paper paths.

`as_of` may be a `date` or a timezone-aware `datetime`; datetimes are converted to the IST calendar date. Naive datetimes are rejected.

### Table-specific notes

- `lot_sizes`: look up by **contract expiry date**, not trade date. The broker contract file (instrument master, M2) is authoritative per contract; this table is the fallback and cross-check (`tasks.py instruments` prints mismatches).
- `freeze_qty`: the **raw limit** from the NSE circular (Nifty 1,800 from 2 Nov 2026). The largest order is computed by `engine.execution.slicer.max_order_qty` (whole lots, strictly below the limit: 1,755 = 27 × 65). NSE revises these often (monthly in 2026) — refresh before each series.
- `expiry_calendar`: weekday rules (`TUE`, `LAST_TUE`...). Listed expiries come from the contract file; an expiry on a holiday moves to the previous trading day (e.g. Nifty monthly 23 Nov 2026).
- `holidays`: 2026 cash-market holidays (NSE/CMTR/71775, 72260) plus `special_session` rows (Muhurat). The calendar refuses years that are not loaded.
- `market_sessions`: keys `exchange, segment, session`; value `HH:MM` IST. Sessions: `pre_open_start`, `pre_open_end`, `normal_open`, `normal_close`, `trade_mod_end`, `close_vwap_start`. **NSE F&O closes at 15:40 from 3 Aug 2026** (NSE/FAOP/74467); 15:30 before. Use this table for expiry time-to-maturity, never a constant.
- `margin_rules.yaml`: list under `rules:`; keyed by `name`; rule body in `params`.

## Recorded market data (M2) — `data/parquet/<dataset>/date=YYYY-MM-DD/*.parquet`

Written by `engine.data.recorder` (schemas in `engine/data/schema.py`); read with
`engine.data.store.Store`, whose queries all take `as_of` and never return rows known later.
Dry runs (`SIM=1`) write the same layout under `data/sim/parquet/` and are tagged `source='sim'`.
Parts are compacted into `day-YYYY-MM-DD.parquet` after the session. All timestamps are UTC
microseconds; the partition date is the IST date.

### `quotes`

| Column | Meaning |
|---|---|
| `ts` | **Known-at** time for point-in-time queries: receive time for `capture='tick'`, snapshot time for `capture='snap'`. |
| `capture` | `tick` = one row per received message (09:00–09:35 for every contract; all day for spots, VIX, futures and ±10 strikes of the current weekly). `snap` = every subscribed contract once a minute from the last state received. |
| `msg` | `quote`, `depth` (which message produced a tick row) or `snap`. |
| `symbol`, `underlying`, `kind`, `expiry`, `strike` | Broker ticker and contract terms from the instrument master. `kind` ∈ IDX, EQ, FUT, CE, PE. |
| `quote_recv_ts`, `depth_recv_ts` | Local receive time of the latest quote / depth message included in the row. |
| `exch_ts`, `ltt` | Exchange feed time and last-trade time as sent by the broker (whole seconds on Fyers). |
| `ltp`, `ltq`, `atp`, `volume`, `oi`, `tbq`, `tsq` | Last price/qty, average traded price, cumulative volume, open interest, total buy/sell qty. `oi` is **null on Fyers** (its SDK drops it). |
| `bid`, `bid_qty`, `ask`, `ask_qty` | Top of book as sent in the quote message. |
| `bid_px_1..5`, `bid_qty_1..5`, `bid_ord_1..5`, `ask_*` | Five-level depth (price, quantity, number of orders), best first. |
| `open`, `high`, `low`, `prev_close`, `upper_ckt`, `lower_ckt` | Day OHLC, previous close, circuit band (null when the broker sends none). |
| `qc_flags` | Bitmask from `engine.data.quality.checks.Flag`: 1 CROSSED, 2 LOCKED, 4 ZERO_BID, 8 ZERO_ASK, 16 NO_QUOTE, 32 STALE, 64 NO_DEPTH, 128 EXCH_TS_AHEAD, 256 LAG_HIGH, 512 DEPTH_DISORDER, 1024 LTP_OUTSIDE_BAND. |
| `conn_id` | Feed connection number for the day (increments on reconnect). |
| `seq`, `source` | Recorder sequence number; adapter name (`fyers`, `sim`, ...). |

### `recorder_events`

`ts, event, symbol, detail, start_ts, end_ts, value, conn_id, source`. Events: `gap` (feed
silence over the threshold inside the session; `value` = seconds), `feed_status`
(connected / disconnected / limit / auth_failed / error), `subscribe`, `unsubscribe`,
`dropped` (contracts wanted but over the broker symbol cap; `value` = count),
`lot_mismatch`, `note`, `session`.

### `depth30` (live shadow scanner, Upstox Plus `full_d30`)

One row per received 30-level depth message for the scanner watchlist (at most 50 keys).
Columns:
- `ts` (receive time = known-at), `symbol`, `underlying`, `kind`, `expiry`, `strike`;
- `exch_ts` (Upstox server time), `levels`;
- `bid_px`, `bid_qty`, `ask_px`, `ask_qty` (lists, best level first; no order counts on Upstox);
- `seq`, `source`.

Written by `engine/shadow/live.py` and compacted after the session.

### Other M2 files

- `data/raw/instruments/<broker>/<YYYY-MM-DD>/<segment>.csv.gz` + `.meta.json` — the broker contract file as downloaded, with `fetched_at` (known-at), URL and SHA-256.
- `data/state/recorder_sticky.json` — contracts that entered the strike band, kept until expiry.
- `reports/qc/<YYYY-MM-DD>.json|.md` — day QC report (`tasks.py qc`); `reports/qc/sim/` for dry runs.
