"""Parquet dataset writer and DuckDB point-in-time reader.

Layout: ``<root>/<dataset>/date=YYYY-MM-DD/part-<HHMMSS>-<pid>-<seq>.parquet`` (date in IST).
Parts are written to a temp name and renamed, so a crash never leaves a half-written file
that readers would pick up. Small parts are fine; :func:`compact_day` merges them.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from engine.core.clock import from_ns

US_PER_S = 1_000_000


def ns_to_us(ns: int | None) -> int | None:
    return None if ns is None else ns // 1000


def s_to_us(s: int | float | None) -> int | None:
    return None if s is None else int(s * US_PER_S)


class DatasetWriter:
    """Buffers row dicts and writes date-partitioned Parquet parts. Not thread-safe."""

    def __init__(
        self,
        root: Path,
        dataset: str,
        schema: pa.Schema,
        *,
        compression: str = "zstd",
    ) -> None:
        self.dir = root / dataset
        self.schema = schema
        self.compression = compression
        self._rows: list[dict[str, Any]] = []
        self._seq = 0
        self.rows_written = 0
        self.parts_written = 0

    def __len__(self) -> int:
        return len(self._rows)

    def append(self, row: dict[str, Any]) -> None:
        self._rows.append(row)

    def flush(self) -> int:
        if not self._rows:
            return 0
        by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for r in self._rows:
            by_day[from_ns(r["ts"] * 1000).date()].append(r)
        n = 0
        for day, rows in by_day.items():
            table = pa.Table.from_pylist(rows, schema=self.schema)
            part_dir = self.dir / f"date={day.isoformat()}"
            part_dir.mkdir(parents=True, exist_ok=True)
            self._seq += 1
            stamp = from_ns(rows[0]["ts"] * 1000).strftime("%H%M%S")
            final = part_dir / f"part-{stamp}-{os.getpid()}-{self._seq:06d}.parquet"
            tmp = final.with_suffix(".parquet.tmp")
            pq.write_table(table, tmp, compression=self.compression)
            os.replace(tmp, final)
            n += len(rows)
            self.parts_written += 1
        self._rows.clear()
        self.rows_written += n
        return n


def day_glob(root: Path, dataset: str, day: date | None = None) -> str:
    part = f"date={day.isoformat()}" if day else "date=*"
    return (root / dataset / part / "*.parquet").as_posix()


def compact_day(root: Path, dataset: str, day: date, schema: pa.Schema) -> Path | None:
    """Merge a day's parts into one file sorted by (ts, symbol); originals are removed."""
    part_dir = root / dataset / f"date={day.isoformat()}"
    parts = sorted(part_dir.glob("part-*.parquet"))
    if len(parts) <= 1:
        return parts[0] if parts else None
    table = pa.concat_tables([pq.read_table(p, schema=schema) for p in parts])
    sort_keys = [("ts", "ascending")] + (
        [("symbol", "ascending")] if "symbol" in schema.names else []
    )
    table = table.sort_by(sort_keys)
    final = part_dir / f"day-{day.isoformat()}.parquet"
    tmp = final.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    if pq.read_metadata(tmp).num_rows != table.num_rows:
        tmp.unlink()
        raise OSError(f"compaction check failed for {part_dir}")
    os.replace(tmp, final)
    for p in parts:
        if p != final:
            p.unlink()
    return final


class Store:
    """Read side. Every query takes ``as_of`` and never returns rows known after it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.con = duckdb.connect()
        self.con.execute("SET TimeZone = 'UTC'")

    def has(self, dataset: str, day: date | None) -> bool:
        d = self.root / dataset
        if day is not None:
            d = d / f"date={day.isoformat()}"
            return any(d.glob("*.parquet"))
        return any(d.glob("date=*/*.parquet"))

    def source_sql(self, dataset: str, day: date | None) -> str:
        glob = day_glob(self.root, dataset, day).replace("'", "''")
        return f"read_parquet('{glob}', hive_partitioning = false, union_by_name = true)"

    def rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run ``sql`` and fetch every row (duckdb >= 2 refuses new work on a half-read result)."""
        return self.con.execute(sql, list(params)).fetchall()

    def query(self, sql: str, params: Sequence[Any] = ()) -> pa.Table:
        return self.con.sql(sql, params=list(params)).to_arrow_table()

    @staticmethod
    def _filters(
        as_of: datetime,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        symbols: Iterable[str] | None = None,
        capture: str | None = None,
    ) -> tuple[str, list[Any]]:
        """WHERE clause for a point-in-time read: ``start <= ts < end`` and ``ts <= as_of``."""
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        where = ["ts <= ?"]
        params: list[Any] = [as_of]
        if start is not None:
            where.append("ts >= ?")
            params.append(start)
        if end is not None:
            where.append("ts < ?")
            params.append(end)
        if capture is not None:
            where.append("capture = ?")
            params.append(capture)
        if symbols is not None:
            syms = list(symbols)
            where.append(f"symbol IN ({', '.join('?' for _ in syms)})")
            params += syms
        return " AND ".join(where), params

    def quotes(
        self,
        day: date,
        *,
        as_of: datetime,
        start: datetime | None = None,
        end: datetime | None = None,
        symbols: Iterable[str] | None = None,
        capture: str | None = None,
        columns: str = "*",
    ) -> pa.Table:
        """Recorded quote rows with ``start <= ts < end`` known at ``as_of``, by (ts, seq)."""
        where, params = self._filters(as_of, start=start, end=end, symbols=symbols, capture=capture)
        if not self.has("quotes", day):
            return pa.table({})
        sql = (
            f"SELECT {columns} FROM {self.source_sql('quotes', day)} WHERE {where} ORDER BY ts, seq"
        )
        return self.query(sql, params)

    def ts_span(
        self,
        dataset: str,
        day: date,
        *,
        as_of: datetime,
        start: datetime | None = None,
        symbols: Iterable[str] | None = None,
        capture: str | None = None,
    ) -> tuple[datetime | None, datetime | None]:
        """(min ts, max ts) of the rows a filtered point-in-time read would return."""
        where, params = self._filters(as_of, start=start, symbols=symbols, capture=capture)
        if not self.has(dataset, day):
            return None, None
        (lo, hi), *_ = self.rows(
            f"SELECT min(ts), max(ts) FROM {self.source_sql(dataset, day)} WHERE {where}", params
        )
        return lo, hi

    def latest(
        self,
        day: date,
        *,
        as_of: datetime,
        underlying: str | None = None,
        expiry: date | None = None,
    ) -> pa.Table:
        """Last known row per symbol at ``as_of`` (a point-in-time chain snapshot)."""
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if not self.has("quotes", day):
            return pa.table({})
        where = ["ts <= ?"]
        params: list[Any] = [as_of]
        if underlying is not None:
            where.append("underlying = ?")
            params.append(underlying)
        if expiry is not None:
            where.append("expiry = ?")
            params.append(expiry)
        sql = (
            f"SELECT * FROM {self.source_sql('quotes', day)} WHERE {' AND '.join(where)} "
            "QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ts DESC, seq DESC) = 1 "
            "ORDER BY symbol"
        )
        return self.query(sql, params)

    def events(self, day: date) -> pa.Table:
        if not self.has("recorder_events", day):
            return pa.table({})
        return self.query(f"SELECT * FROM {self.source_sql('recorder_events', day)} ORDER BY ts")

    def depth30(
        self,
        day: date,
        *,
        as_of: datetime,
        start: datetime | None = None,
        end: datetime | None = None,
        symbols: Iterable[str] | None = None,
        columns: str = "*",
    ) -> pa.Table:
        """30-level order-book snapshots recorded from Upstox ``full_d30``, ordered by (ts, seq)."""
        where, params = self._filters(as_of, start=start, end=end, symbols=symbols)
        if not self.has("depth30", day):
            return pa.table({})
        sql = (
            f"SELECT {columns} FROM {self.source_sql('depth30', day)} WHERE {where} "
            "ORDER BY ts, seq"
        )
        return self.query(sql, params)
