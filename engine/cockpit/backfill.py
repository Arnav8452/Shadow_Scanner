"""Seed the calendar and compression strategies' history from data we already can get, instead
of waiting 10-20 sessions (owner question, 13 Nov 2026).

- **Term spread (calendar, spec C): not backfilled.** Bhavcopy closes of the far monthly expiry
  are stale (thin trading): on 12 Nov they implied a forward 1.2% above spot and a 27.7% IV
  against ~14% live. A true backfill needs synchronous intraday quotes of both expiries.
- **15-minute band widths (compression, spec D):** each past session's index 1-minute candles
  from Upstox's historical candle API (v3) are replayed through the same ``IndexTape`` the live
  strategy uses, collecting ``bb_width(15)`` the same way.

Point in time: a session's line describes that session only and is used from the next one on,
as live. Days already in a history file are never written twice.
"""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from engine.cockpit.tape import IndexTape
from engine.core.env import get_secret
from engine.core.net import USER_AGENT
from engine.core.rules import IST, RuleBook


def _days_in(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.add(str(json.loads(line)["day"]))
        except (ValueError, KeyError):
            continue
    return out


def _append_sorted(path: Path, recs: list[dict[str, Any]]) -> int:
    """Insert ``recs`` (new days only), keeping the file in day order."""
    have = _days_in(path)
    new = [r for r in recs if r["day"] not in have]
    if not new:
        return 0
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    allrecs = [json.loads(x) for x in lines if x.strip()] + new
    allrecs.sort(key=lambda r: str(r["day"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in allrecs), encoding="utf-8")
    return len(new)


def _http_json(url: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"accept": "application/json", "Authorization": "Bearer " + token,
                 "User-Agent": USER_AGENT},
    )  # fmt: skip
    with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
        return dict(json.loads(r.read()))


def index_minute_candles(key: str, day: date, token: str) -> list[list[Any]]:
    """Upstox v3 1-minute candles for one session, oldest first."""
    k = urllib.parse.quote(key, safe="")
    d = day.isoformat()
    doc = _http_json(f"https://api.upstox.com/v3/historical-candle/{k}/minutes/1/{d}/{d}", token)
    return sorted((doc.get("data") or {}).get("candles") or [], key=lambda c: str(c[0]))


def widths_from_candles(underlying: str, candles: Sequence[Sequence[Any]]) -> list[float]:
    """The live strategy's widths_today: bb_width(15) after each sample, when it changed."""
    t = IndexTape(underlying)
    ws: list[float] = []
    for c in candles:
        start = datetime.fromisoformat(str(c[0])).astimezone(IST)
        o, h, lo, cl = (float(c[1]), float(c[2]), float(c[3]), float(c[4]))
        for i, px in enumerate((o, h, lo, cl)):
            t.on_sample(start + timedelta(seconds=10 + 12 * i), spot=px)
        w = t.bb_width(15)
        if w is not None and (not ws or ws[-1] != w):
            ws.append(w)
    return ws


def backfill(
    root: Path,
    *,
    raw_dir: Path,
    book: RuleBook,
    calendar: Any,
    sessions: Sequence[date],
    index_keys: dict[str, str],
    journal_dir: Path,
    fetch: Callable[[str, date, str], list[list[Any]]] = index_minute_candles,
) -> list[str]:
    lines: list[str] = [
        "term spread: not backfilled. Bhavcopy closes of the far (monthly) expiry are stale "
        "(e.g. 12 Nov: ~2.8k contracts, implied forward 25,600 vs spot 25,290, IV 27.7% vs ~14% "
        "live), so the calendar keeps its own history from the live chain at each close."
    ]
    tok = ""
    for name in ("UPSTOX_ACCESS_TOKEN", "UPSTOX_ANALYTICS_TOKEN"):
        try:
            tok = get_secret(name).reveal()
        except Exception:
            tok = ""
        if tok:
            break
    if not tok:
        lines.append("band widths: no Upstox token in .env; skipped")
        return lines
    recs: list[dict[str, Any]] = []
    for d in sessions:
        rec: dict[str, Any] = {"day": d.isoformat(), "source": "upstox_1m"}
        for und, key in index_keys.items():
            try:
                ws = widths_from_candles(und, fetch(key, d, tok))
            except Exception as exc:  # one bad day must not stop the rest
                lines.append(f"band widths {und} {d}: {type(exc).__name__}")
                continue
            if ws:
                rec[und] = ws
        if len(rec) > 2:
            recs.append(rec)
    n = _append_sorted(journal_dir / "bb_width_history.jsonl", recs)
    lines.append(f"band widths: {n} new session(s) from Upstox 1-minute candles")
    return lines


__all__ = ["backfill", "widths_from_candles"]
