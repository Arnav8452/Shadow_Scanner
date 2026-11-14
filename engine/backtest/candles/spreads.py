"""Measured option spreads from our own recorder, to calibrate the candle haircut.

``measure_spreads`` reads the recorded quotes (``data/parquet/quotes``, best bid and ask) and
returns the half-spread ``(ask - bid) / 2`` by last-price bucket, between ``start`` and ``end``
IST: median and 90th percentile in rupees and as % of price. By default it uses the periodic
``snap`` captures (every recorded contract at a fixed cadence, i.e. the spread at an arbitrary
decision time; ``tick`` captures over-weight busy moments) of the NEAREST expiry on each day,
the contracts the candle backtest trades: the snapshots also hold far expiries and deep strikes
whose spreads are many times wider. The candle report prints this table next to its haircut.

``SPREADS=p90`` (or ``median``) in ``tasks.py candle-backtest`` turns the table into the haircut
(each bucket's relative half-spread). Recorded spreads exist only from Nov 2026; using them for
2025 assumes spreads were no wider then, which is why the default stays at about 2-3x the
median, at or above the 90th percentile.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

BUCKET_EDGES: tuple[float, ...] = (0.0, 10.0, 25.0, 50.0, 100.0, 200.0)


@dataclass(frozen=True)
class SpreadBucket:
    lo: float
    hi: float | None
    n: int
    median_inr: float
    p90_inr: float
    median_pct: float
    p90_pct: float


def measure_spreads(
    quotes_dir: Path,
    *,
    underlying: str = "NIFTY",
    start: str = "10:00",
    end: str = "15:10",
    capture: str | None = "snap",
    nearest_expiry_only: bool = True,
) -> tuple[list[SpreadBucket], list[str]]:
    """(buckets, recorded dates) from every ``date=*`` folder under ``quotes_dir``."""
    import duckdb

    days = sorted(p.name.removeprefix("date=") for p in quotes_dir.glob("date=*") if p.is_dir())
    if not days:
        return [], []
    glob = (quotes_dir / "date=*" / "*.parquet").as_posix()
    cases = " ".join(f"WHEN ltp < {hi} THEN {lo}" for lo, hi in pairwise(BUCKET_EDGES))
    sql = f"""
        SELECT b, count(*), median(h), quantile_cont(h, 0.9),
               median(h / ltp) * 100, quantile_cont(h / ltp, 0.9) * 100
        FROM (
            SELECT ltp, (ask - bid) / 2 AS h, CASE {cases} ELSE {BUCKET_EDGES[-1]} END AS b
            FROM (
                SELECT *, min(expiry) OVER (PARTITION BY date) AS near
                FROM read_parquet('{glob}', hive_partitioning = true)
                WHERE underlying = ? AND kind IN ('CE', 'PE') AND expiry >= date
            )
            WHERE bid > 0 AND ask > bid AND ltp > 0
              AND strftime(ts AT TIME ZONE 'Asia/Kolkata', '%H:%M') BETWEEN ? AND ?
              {"AND expiry = near" if nearest_expiry_only else ""}
              {"AND capture = ?" if capture else ""}
        ) GROUP BY b ORDER BY b
    """
    con = duckdb.connect()
    try:
        args: list[str] = [underlying, start, end] + ([capture] if capture else [])
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    edges = dict(zip(BUCKET_EDGES, (*BUCKET_EDGES[1:], None), strict=True))
    out = [
        SpreadBucket(
            float(b), edges.get(float(b)), int(n), float(m), float(p), float(mp), float(pp)
        )
        for b, n, m, p, mp, pp in rows
    ]
    return out, days


def haircut_table(
    buckets: list[SpreadBucket], stat: str = "p90"
) -> tuple[tuple[float, float], ...]:
    """``((lower edge, half-spread % of price), ...)`` for ``Haircut.table``."""
    if stat not in ("p90", "median"):
        raise ValueError("SPREADS must be p90 or median")
    return tuple((b.lo, b.p90_pct if stat == "p90" else b.median_pct) for b in buckets)


def render_spreads(buckets: list[SpreadBucket], days: list[str], underlying: str = "NIFTY") -> str:
    if not buckets:
        return "No recorded quotes found to measure spreads."
    lines = [
        f"Measured half-spread (ask - bid) / 2 of {underlying} options in our recorder: "
        f"nearest weekly expiry, periodic snapshots, 10:00-15:10 IST, sessions "
        f"{', '.join(days)}:",
        "",
        "| Last price | Quotes | Median ₹ | 90th pct ₹ | Median % | 90th pct % |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for b in buckets:
        rng = f"₹{b.lo:g}–{b.hi:g}" if b.hi is not None else f"≥ ₹{b.lo:g}"
        lines.append(
            f"| {rng} | {b.n:,} | {b.median_inr:.3f} | {b.p90_inr:.3f} | "
            f"{b.median_pct:.2f}% | {b.p90_pct:.2f}% |"
        )
    return "\n".join(lines)


__all__ = ["BUCKET_EDGES", "SpreadBucket", "haircut_table", "measure_spreads", "render_spreads"]
