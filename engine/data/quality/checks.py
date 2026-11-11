"""Data-quality checks (SPEC §3): stale, crossed, zero bid, missing strikes, gaps, clock skew.

Two levels:

* :func:`row_flags` — a bitmask computed when each row is recorded; it travels with the
  data in the ``qc_flags`` column.
* :func:`qc_day` — a day report over the stored data, used for M2 acceptance (no feed gap
  over 5 s inside the session, complete 1-min snapshots, ±N strike coverage) and by
  ``tasks.py qc DATE=...``.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from enum import IntFlag
from pathlib import Path
from typing import Any

from engine.core.clock import NS_PER_S, MarketSession
from engine.core.rules import IST
from engine.data.adapters.base import Depth, Quote
from engine.data.store import Store


class Flag(IntFlag):
    CROSSED = 1  # best bid > best ask
    LOCKED = 2  # best bid == best ask
    ZERO_BID = 4  # no bid (or bid <= 0)
    ZERO_ASK = 8
    NO_QUOTE = 16  # subscribed but no quote received yet
    STALE = 32  # last quote older than the stale threshold at row time
    NO_DEPTH = 64  # depth subscribed but none received yet
    EXCH_TS_AHEAD = 128  # exchange time after local receive time (local clock behind)
    LAG_HIGH = 256  # receive time minus exchange time above threshold
    DEPTH_DISORDER = 512  # bid levels not descending or ask levels not ascending
    LTP_OUTSIDE_BAND = 1024  # LTP outside the broker-sent circuit band


@dataclass(frozen=True)
class FlagThresholds:
    stale_s: float = 120.0
    lag_s: float = 5.0
    ahead_s: float = 1.0


def _ordered(prices: list[float], descending: bool) -> bool:
    pairs = itertools.pairwise(prices)
    return all(a >= b for a, b in pairs) if descending else all(a <= b for a, b in pairs)


def best_bid_ask(quote: Quote | None, depth: Depth | None) -> tuple[float | None, float | None]:
    if depth is not None and (depth.bids or depth.asks):
        bid = depth.bids[0][0] if depth.bids else None
        ask = depth.asks[0][0] if depth.asks else None
        return bid, ask
    if quote is not None:
        return quote.bid, quote.ask
    return None, None


def row_flags(
    kind: str,
    quote: Quote | None,
    depth: Depth | None,
    now_ns: int,
    *,
    depth_expected: bool,
    th: FlagThresholds,
) -> int:
    f = Flag(0)
    if quote is None:
        f |= Flag.NO_QUOTE
    else:
        if now_ns - quote.recv_ns > th.stale_s * NS_PER_S:
            f |= Flag.STALE
        if quote.exch_ts is not None:
            exch_ns = quote.exch_ts * NS_PER_S
            if exch_ns > quote.recv_ns + th.ahead_s * NS_PER_S:
                f |= Flag.EXCH_TS_AHEAD
            elif quote.recv_ns - exch_ns > th.lag_s * NS_PER_S:
                f |= Flag.LAG_HIGH
        lo, hi, ltp = quote.lower_ckt, quote.upper_ckt, quote.ltp
        if lo and hi and ltp is not None and lo > 0 and hi > 0 and not lo <= ltp <= hi:
            f |= Flag.LTP_OUTSIDE_BAND
    if kind == "IDX":
        return int(f)  # indices have no book
    if depth_expected and depth is None:
        f |= Flag.NO_DEPTH
    bid, ask = best_bid_ask(quote, depth)
    if bid is None or bid <= 0:
        f |= Flag.ZERO_BID
    if ask is None or ask <= 0:
        f |= Flag.ZERO_ASK
    if bid and ask and bid > 0 and ask > 0:
        if bid > ask:
            f |= Flag.CROSSED
        elif bid == ask:
            f |= Flag.LOCKED
    if depth is not None:
        bids = [p for p, q, _ in depth.bids if p > 0 and q > 0]
        asks = [p for p, q, _ in depth.asks if p > 0 and q > 0]
        if not (_ordered(bids, descending=True) and _ordered(asks, descending=False)):
            f |= Flag.DEPTH_DISORDER
    return int(f)


def describe_flags(mask: int) -> list[str]:
    return [m.name or str(m.value) for m in Flag if mask & m.value]


# --------------------------------------------------------------------------- day report


@dataclass
class Gap:
    start: str
    end: str
    seconds: float
    source: str  # ticks | open | close | recorder


@dataclass
class DayQC:
    day: str
    rows: int = 0
    tick_rows: int = 0
    snap_rows: int = 0
    symbols: int = 0
    first_ts: str | None = None
    last_ts: str | None = None
    session_open: str = ""
    session_close: str = ""
    gap_threshold_s: float = 5.0
    gaps: list[Gap] = field(default_factory=list)
    max_gap_s: float = 0.0
    snapshot_minutes_expected: int = 0
    snapshot_minutes_missing: list[str] = field(default_factory=list)
    flag_counts: dict[str, dict[str, int]] = field(default_factory=dict)  # kind -> flag -> n
    lag_s: dict[str, float | None] = field(default_factory=dict)  # quantiles of recv - exch
    strike_coverage: dict[str, dict[str, int]] = field(default_factory=dict)
    required_strikes_each_side: int = 25
    dropped_for_budget: int = 0
    oi_present: bool = False
    feed_status: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    passed: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


def _iso(ts: Any) -> str:
    """IST ISO string for aware datetimes (duckdb returns UTC)."""
    if isinstance(ts, datetime) and ts.tzinfo is not None:
        return ts.astimezone(IST).isoformat(timespec="seconds")
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def _dedupe(gaps: list[Gap]) -> list[Gap]:
    """The recorder logs gaps live and the tick scan finds them again; keep one of each."""
    seen: set[tuple[str, str]] = set()
    out: list[Gap] = []
    for g in sorted(gaps, key=lambda g: (g.start, g.source != "ticks")):
        key = (g.start[:19], g.end[:19])
        if key not in seen:
            seen.add(key)
            out.append(g)
    return out


def qc_day(
    store: Store,
    day: date,
    session: MarketSession,
    *,
    gap_threshold_s: float = 5.0,
    required_strikes_each_side: int = 25,
    snapshot_interval_s: int = 60,
) -> DayQC:
    q = DayQC(
        day=day.isoformat(),
        session_open=session.open_dt.isoformat(),
        session_close=session.close_dt.isoformat(),
        gap_threshold_s=gap_threshold_s,
        required_strikes_each_side=required_strikes_each_side,
    )
    if not store.has("quotes", day):
        q.problems.append("no quotes recorded")
        return q
    src = store.source_sql("quotes", day)
    o, c = session.open_dt, session.close_dt

    row = store.rows(
        f"SELECT count(*), count(*) FILTER (capture='tick'), count(*) FILTER (capture='snap'), "
        f"count(DISTINCT symbol), min(ts), max(ts), count(oi) FROM {src}"
    )[0]
    assert row is not None
    q.rows, q.tick_rows, q.snap_rows, q.symbols = int(row[0]), int(row[1]), int(row[2]), int(row[3])
    q.first_ts, q.last_ts, q.oi_present = _iso(row[4]), _iso(row[5]), bool(row[6])

    # --- feed gaps (all tick messages merged), plus late start / early stop
    gaps = store.rows(
        f"""
        WITH t AS (SELECT DISTINCT ts FROM {src} WHERE capture = 'tick' AND ts >= ? AND ts < ?),
             g AS (SELECT lag(ts) OVER (ORDER BY ts) AS prev, ts FROM t)
        SELECT prev, ts, epoch(ts) - epoch(prev) AS secs FROM g
        WHERE prev IS NOT NULL AND epoch(ts) - epoch(prev) > ? ORDER BY prev
        """,
        [o, c, gap_threshold_s],
    )
    q.gaps = [Gap(_iso(a), _iso(b), round(float(s), 3), "ticks") for a, b, s in gaps]
    bounds = store.rows(
        f"SELECT min(ts), max(ts) FROM {src} WHERE capture='tick' AND ts >= ? AND ts < ?", [o, c]
    )[0]
    if bounds is None or bounds[0] is None:
        q.gaps.append(Gap(_iso(o), _iso(c), (c - o).total_seconds(), "ticks"))
    else:
        first, last = bounds
        if (first - o).total_seconds() > gap_threshold_s:
            q.gaps.insert(0, Gap(_iso(o), _iso(first), (first - o).total_seconds(), "open"))
        if (c - last).total_seconds() > gap_threshold_s:
            q.gaps.append(Gap(_iso(last), _iso(c), (c - last).total_seconds(), "close"))
    ev = store.events(day)
    if ev.num_rows:
        for r in ev.to_pylist():
            if r["event"] == "gap" and (r["value"] or 0) > gap_threshold_s:
                q.gaps.append(
                    Gap(_iso(r["start_ts"]), _iso(r["end_ts"]), float(r["value"]), "recorder")
                )
            elif r["event"] == "dropped":
                q.dropped_for_budget = max(q.dropped_for_budget, int(r["value"] or 0))
            elif r["event"] == "feed_status":
                key = (r["detail"] or "").split(":", 1)[0]
                q.feed_status[key] = q.feed_status.get(key, 0) + 1
    q.gaps = _dedupe(q.gaps)
    q.max_gap_s = max((g.seconds for g in q.gaps), default=0.0)

    # --- snapshot completeness
    minutes = store.rows(
        f"SELECT DISTINCT date_trunc('minute', ts) FROM {src} WHERE capture='snap'"
    )
    have = {m[0].replace(second=0, microsecond=0) for m in minutes}
    expected: list[datetime] = []
    t = o.replace(second=0, microsecond=0)
    if t < o:
        t += timedelta(minutes=1)
    while t < c:
        expected.append(t)
        t += timedelta(seconds=snapshot_interval_s)
    q.snapshot_minutes_expected = len(expected)
    have_utc = {h.timestamp() for h in have}
    q.snapshot_minutes_missing = [
        e.strftime("%H:%M") for e in expected if e.timestamp() not in have_utc
    ]

    # --- flag counts on snapshot rows (one per symbol per minute, so rates are comparable)
    for kind, mask, n in store.rows(
        f"SELECT kind, qc_flags, count(*) FROM {src} WHERE capture='snap' AND ts >= ? AND ts < ? "
        "GROUP BY kind, qc_flags",
        [o, c],
    ):
        bucket = q.flag_counts.setdefault(str(kind), {"rows": 0})
        bucket["rows"] += int(n)
        for name in describe_flags(int(mask)):
            bucket[name] = bucket.get(name, 0) + int(n)

    # --- clock skew / latency from quote messages carrying an exchange timestamp
    lag = store.rows(
        f"""SELECT quantile_cont(d, [0.01, 0.5, 0.95, 0.99]), min(d), max(d), count(*) FROM (
              SELECT epoch(quote_recv_ts) - epoch(exch_ts) AS d FROM {src}
              WHERE capture='tick' AND msg='quote' AND exch_ts IS NOT NULL AND ts >= ? AND ts < ?)
        """,
        [o, c],
    )[0]
    if lag is not None and lag[3]:
        p01, p50, p95, p99 = lag[0]
        q.lag_s = {
            "p01": round(p01, 3),
            "p50": round(p50, 3),
            "p95": round(p95, 3),
            "p99": round(p99, 3),
            "min": round(lag[1], 3),
            "max": round(lag[2], 3),
            "n": float(lag[3]),
        }
        if p50 < -1.0:
            q.problems.append(
                f"local clock behind exchange (median recv-exch {p50:.2f}s); check NTP"
            )
        elif p50 > 3.0:
            q.problems.append(f"median feed lag {p50:.2f}s; check NTP and network")

    # --- strike coverage per (underlying, expiry): min strikes each side of ATM over snapshots
    cov = store.rows(
        f"""
        WITH s AS (SELECT ts, underlying, expiry, strike FROM {src}
                   WHERE capture='snap' AND kind IN ('CE','PE') AND ts >= ? AND ts < ?),
             spot AS (SELECT ts, underlying, ltp FROM {src}
                   WHERE capture='snap' AND kind='IDX' AND ltp IS NOT NULL AND ts >= ? AND ts < ?),
             j AS (SELECT s.ts, s.underlying, s.expiry,
                          count(DISTINCT s.strike) FILTER (s.strike < spot.ltp) AS below,
                          count(DISTINCT s.strike) FILTER (s.strike >= spot.ltp) AS above
                   FROM s JOIN spot USING (ts, underlying) GROUP BY ALL)
        SELECT underlying, expiry, min(below), min(above), count(*) FROM j GROUP BY ALL ORDER BY ALL
        """,
        [o, c, o, c],
    )
    for und, exp, below, above, n in cov:
        q.strike_coverage[f"{und} {exp}"] = {
            "min_below": int(below),
            "min_above": int(above),
            "snapshots": int(n),
        }

    # --- verdict
    if q.max_gap_s > gap_threshold_s:
        q.problems.append(f"feed gap {q.max_gap_s:.1f}s > {gap_threshold_s}s")
    if q.snapshot_minutes_missing:
        q.problems.append(f"{len(q.snapshot_minutes_missing)} snapshot minutes missing")
    if not q.strike_coverage:
        q.problems.append("no option chain coverage (no spot or no option snapshots)")
    for key, cv in q.strike_coverage.items():
        if min(cv["min_below"], cv["min_above"]) < required_strikes_each_side:
            q.problems.append(
                f"{key}: only {cv['min_below']}/{cv['min_above']} strikes below/above ATM "
                f"(need {required_strikes_each_side})"
            )
    if not q.oi_present:
        q.problems.append("no open interest in the feed")
    q.passed = not q.problems
    return q


def render_markdown(q: DayQC) -> str:
    lines = [
        f"# Recorder QC — {q.day}",
        "",
        f"**Verdict: {'PASS' if q.passed else 'FAIL'}** "
        f"(M2 acceptance: no gap > {q.gap_threshold_s:g}s, complete 1-min snapshots, "
        f"±{q.required_strikes_each_side} strikes, OI present)",
        "",
        f"- Session {q.session_open[11:16]}–{q.session_close[11:16]} IST; rows {q.rows:,} "
        f"(tick {q.tick_rows:,}, snap {q.snap_rows:,}); symbols {q.symbols}",
        f"- First/last row: {q.first_ts} / {q.last_ts}",
        f"- Max gap: {q.max_gap_s:.1f}s; gaps over threshold: {len(q.gaps)}",
        f"- Snapshot minutes missing: {len(q.snapshot_minutes_missing)} of "
        f"{q.snapshot_minutes_expected}",
        f"- Contracts wanted but not subscribed (symbol cap): {q.dropped_for_budget}",
        f"- Latency recv−exchange (s): {q.lag_s or 'n/a'}",
        f"- Feed status events: {q.feed_status or 'none'}",
        "",
    ]
    if q.problems:
        lines += ["## Problems", *[f"- {p}" for p in q.problems], ""]
    if q.gaps:
        lines += ["## Gaps", "| start | end | seconds | source |", "|---|---|---|---|"]
        lines += [f"| {g.start} | {g.end} | {g.seconds:.1f} | {g.source} |" for g in q.gaps[:50]]
        lines.append("")
    if q.strike_coverage:
        lines += ["## Strike coverage (min over snapshots)", "| chain | below ATM | above ATM |"]
        lines.append("|---|---|---|")
        for k, cv in q.strike_coverage.items():
            lines.append(f"| {k} | {cv['min_below']} | {cv['min_above']} |")
        lines.append("")
    if q.flag_counts:
        lines += ["## Row flags on snapshot rows", "| kind | rows | flags |", "|---|---|---|"]
        for kind, counts in sorted(q.flag_counts.items()):
            rows = counts.get("rows", 0)
            rest = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()) if k != "rows")
            lines.append(f"| {kind} | {rows} | {rest or '—'} |")
    return "\n".join(lines) + "\n"


def write_qc(q: DayQC, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    j = out_dir / f"{q.day}.json"
    m = out_dir / f"{q.day}.md"
    j.write_text(q.to_json(), encoding="utf-8")
    m.write_text(render_markdown(q), encoding="utf-8")
    return j, m
