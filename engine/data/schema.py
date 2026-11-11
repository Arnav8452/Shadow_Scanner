"""Parquet schemas for recorded market data (see docs/DATA_DICTIONARY.md).

Timestamps are UTC microseconds. ``ts`` is the *known-at* time used for point-in-time
queries: the local receive time for ``capture='tick'`` rows and the snapshot time for
``capture='snap'`` rows. Exchange times (``exch_ts``, ``ltt``) are kept separately.
"""

from __future__ import annotations

import pyarrow as pa

DEPTH_LEVELS = 5
TS = pa.timestamp("us", tz="UTC")

_depth_fields: list[pa.Field] = []
for side in ("bid", "ask"):
    for i in range(1, DEPTH_LEVELS + 1):
        _depth_fields += [
            pa.field(f"{side}_px_{i}", pa.float64()),
            pa.field(f"{side}_qty_{i}", pa.int64()),
            pa.field(f"{side}_ord_{i}", pa.int32()),
        ]

QUOTES_SCHEMA = pa.schema(
    [
        pa.field("ts", TS, nullable=False),
        pa.field("capture", pa.string(), nullable=False),  # tick | snap
        pa.field("msg", pa.string(), nullable=False),  # quote | depth | snap
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("underlying", pa.string()),
        pa.field("kind", pa.string()),  # IDX EQ FUT CE PE
        pa.field("expiry", pa.date32()),
        pa.field("strike", pa.float64()),
        pa.field("quote_recv_ts", TS),
        pa.field("depth_recv_ts", TS),
        pa.field("exch_ts", TS),
        pa.field("ltt", TS),
        pa.field("ltp", pa.float64()),
        pa.field("ltq", pa.int64()),
        pa.field("atp", pa.float64()),
        pa.field("volume", pa.int64()),
        pa.field("oi", pa.int64()),
        pa.field("tbq", pa.int64()),
        pa.field("tsq", pa.int64()),
        pa.field("bid", pa.float64()),  # top of book as sent in the quote message
        pa.field("bid_qty", pa.int64()),
        pa.field("ask", pa.float64()),
        pa.field("ask_qty", pa.int64()),
        pa.field("open", pa.float64()),
        pa.field("high", pa.float64()),
        pa.field("low", pa.float64()),
        pa.field("prev_close", pa.float64()),
        pa.field("upper_ckt", pa.float64()),
        pa.field("lower_ckt", pa.float64()),
        *_depth_fields,
        pa.field("qc_flags", pa.int32(), nullable=False),
        pa.field("conn_id", pa.int32(), nullable=False),
        pa.field("seq", pa.int64(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
    ]
)

EVENTS_SCHEMA = pa.schema(
    [
        pa.field("ts", TS, nullable=False),
        pa.field("event", pa.string(), nullable=False),
        # gap | feed_status | subscribe | unsubscribe | dropped | lot_mismatch | session | note
        pa.field("symbol", pa.string()),
        pa.field("detail", pa.string()),
        pa.field("start_ts", TS),
        pa.field("end_ts", TS),
        pa.field("value", pa.float64()),
        pa.field("conn_id", pa.int32()),
        pa.field("source", pa.string(), nullable=False),
    ]
)

# 30-level order book (Upstox Plus ``full_d30``) for the shadow-scanner watchlist (<= 50 keys).
# One row per received depth message; levels best-first; ``ts`` is the receive time (known-at).
DEPTH30_SCHEMA = pa.schema(
    [
        pa.field("ts", TS, nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("underlying", pa.string()),
        pa.field("kind", pa.string()),
        pa.field("expiry", pa.date32()),
        pa.field("strike", pa.float64()),
        pa.field("exch_ts", TS),
        pa.field("levels", pa.int16(), nullable=False),
        pa.field("bid_px", pa.list_(pa.float64())),
        pa.field("bid_qty", pa.list_(pa.int64())),
        pa.field("ask_px", pa.list_(pa.float64())),
        pa.field("ask_qty", pa.list_(pa.int64())),
        pa.field("seq", pa.int64(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
    ]
)
