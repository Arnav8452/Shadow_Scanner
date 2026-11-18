"""Avoid-filter tags for paper entries. Recorded only; they block nothing yet.

Two research results (private log, 18 Nov 2026) say when *not* to buy a stock:

- **call-heavy stock-option volume:** F&O stocks whose recent option volume is mostly calls
  under-performed put-heavy ones over the next 5 sessions, confirmed on unseen 2022–2023 data
  (t 3.1). Tag: the put share of stock-option contracts over the last ``pcv_sessions``
  sessions (PCV) and its percentile rank among F&O stocks with at least ``pcv_min_contracts``
  contracts. ``call_heavy`` = rank at or below ``call_heavy_rank``;
- **just after an ex-rights date:** stocks trailed Nifty by about 4% over the 10 sessions after
  going ex-rights, in 8 of 8 years 2016–2023. Tag: the latest rights ex-date and the entry's
  offset from it in sessions. ``in_ex_rights_window`` = from ``ex_rights_before`` sessions
  before to ``ex_rights_after`` sessions after.

Every swing and buyback paper entry carries these tags into its fill and close records, and
``tag_report`` splits closed trades by them. Owner's rule: judge only after
``review_after_trades`` tagged closes, before any tag ever blocks an entry.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from engine.data.eod.bhavcopy import FnOBhavcopyRow
from engine.data.equity.corporate_actions import CorpAction, parse_subject

STOCK_OPTIONS = ("OPTSTK", "STO")


class Calendar(Protocol):
    def previous_trading_day(self, d: date) -> date: ...
    def next_trading_day(self, d: date) -> date: ...


@dataclass(frozen=True)
class AvoidTagConfig:
    pcv_sessions: int = 5
    pcv_min_contracts: int = 500
    call_heavy_rank: float = 0.20
    ex_rights_before: int = 1
    ex_rights_after: int = 10
    review_after_trades: int = 30


def avoid_tag_config(raw: Mapping[str, Any] | None) -> AvoidTagConfig:
    known = AvoidTagConfig.__dataclass_fields__
    return AvoidTagConfig(**{k: v for k, v in dict(raw or {}).items() if k in known})


def pcv_ranks(
    rows: Iterable[FnOBhavcopyRow], *, min_contracts: int, days: set[date]
) -> dict[str, tuple[float, float]]:
    """``symbol -> (PCV, percentile rank 0..1)`` from stock-option rows on ``days``: the put
    share of contracts traded, ranked among symbols with at least ``min_contracts``."""
    calls: dict[str, int] = {}
    puts: dict[str, int] = {}
    for r in rows:
        if r.trade_date not in days or r.instrument not in STOCK_OPTIONS:
            continue
        if r.option_type == "CE":
            calls[r.symbol] = calls.get(r.symbol, 0) + r.contracts
        elif r.option_type == "PE":
            puts[r.symbol] = puts.get(r.symbol, 0) + r.contracts
    pcv = {
        s: puts.get(s, 0) / tot
        for s in set(calls) | set(puts)
        if (tot := calls.get(s, 0) + puts.get(s, 0)) >= min_contracts
    }
    order = sorted(pcv, key=lambda s: (pcv[s], s))
    n = len(order)
    return {s: (pcv[s], i / (n - 1) if n > 1 else 0.5) for i, s in enumerate(order)}


def load_pcv(
    raw_dir: Path, day: date, cal: Calendar, cfg: AvoidTagConfig
) -> dict[str, tuple[float, float]]:
    """PCV ranks over the ``pcv_sessions`` sessions ending ``day``, from the F&O bhavcopies
    on disk (a missing file just narrows the window)."""
    from engine.data.eod.bhavcopy import parse_fno_bhavcopy_csv
    from engine.data.eod.download import eod_path, read_eod_text

    days, d = [], day
    for _ in range(cfg.pcv_sessions):
        days.append(d)
        d = cal.previous_trading_day(d)
    rows: list[FnOBhavcopyRow] = []
    for d in days:
        p = eod_path(raw_dir, "fo", d)
        if p.exists():
            rows.extend(parse_fno_bhavcopy_csv(read_eod_text(p)))
    return pcv_ranks(rows, min_contracts=cfg.pcv_min_contracts, days=set(days))


def rights_ex_dates(actions: Iterable[CorpAction]) -> dict[str, list[date]]:
    """``symbol -> sorted rights ex-dates`` from NSE's corporate-action list."""
    out: dict[str, set[date]] = {}
    for a in actions:
        if any(k == "rights" for k, _ in parse_subject(a.subject)) or (
            "rights" in a.subject.lower() and "bonus" not in a.subject.lower()
        ):
            out.setdefault(a.symbol, set()).add(a.ex_date)
    return {s: sorted(v) for s, v in out.items()}


def _sessions_between(cal: Calendar, a: date, b: date) -> int:
    """Signed number of sessions from ``a`` to ``b`` (0 when equal)."""
    if a == b:
        return 0
    sign, lo, hi = (1, a, b) if a < b else (-1, b, a)
    n, d = 0, lo
    while d < hi:
        d = cal.next_trading_day(d)
        n += 1
    return sign * n


def entry_tags(
    symbol: str,
    *,
    entry_day: date,
    pcv: Mapping[str, tuple[float, float]],
    rights: Mapping[str, Sequence[date]],
    cal: Calendar,
    cfg: AvoidTagConfig,
) -> dict[str, Any]:
    """The avoid-filter tags for buying ``symbol`` on ``entry_day``. ``None`` = unknown (no
    stock-option volume, or no rights issue on record)."""
    p = pcv.get(symbol)
    tags: dict[str, Any] = {
        "pcv": round(p[0], 4) if p else None,
        "pcv_rank": round(p[1], 4) if p else None,
        "call_heavy": (p[1] <= cfg.call_heavy_rank) if p else None,
        "ex_rights_date": None,
        "ex_rights_offset": None,
        "in_ex_rights_window": False,
    }
    exs = [d for d in rights.get(symbol, ()) if abs((entry_day - d).days) <= 40]
    if exs:
        ex = min(exs, key=lambda d: abs((entry_day - d).days))
        off = _sessions_between(cal, ex, entry_day)
        tags.update(
            ex_rights_date=ex.isoformat(),
            ex_rights_offset=off,
            in_ex_rights_window=-cfg.ex_rights_before <= off <= cfg.ex_rights_after,
        )
    return tags


def _split(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for label, want in (("yes", True), ("no", False)):
        nets = [float(r.get("net", r.get("net_inr", 0.0)) or 0.0) for r in rows
                if r["tags"].get(key) is want]  # fmt: skip
        out[label] = {
            "n": len(nets),
            "avg_net": round(sum(nets) / len(nets), 2) if nets else None,
            "total_net": round(sum(nets), 2),
            "win_pct": round(100 * sum(x > 0 for x in nets) / len(nets), 1) if nets else None,
        }
    return out


def tag_report(
    journal_dir: Path, *, review_after: int = 30, accounts: Sequence[str] = ("swing", "buyback")
) -> dict[str, Any]:
    """Closed paper trades split by each avoid tag, per account; ``ready`` once
    ``review_after`` tagged closes exist across the accounts."""
    out: dict[str, Any] = {"accounts": {}, "review_after": review_after}
    total = 0
    for acct in accounts:
        path = journal_dir / f"paper_{acct}.jsonl"
        rows: list[dict[str, Any]] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "close" and isinstance(r.get("tags"), dict) and r["tags"]:
                    rows.append(r)
        total += len(rows)
        out["accounts"][acct] = {
            "tagged": len(rows),
            "call_heavy": _split(rows, "call_heavy"),
            "in_ex_rights_window": _split(rows, "in_ex_rights_window"),
        }
    out["tagged_total"] = total
    out["ready"] = total >= review_after
    return out


__all__ = [
    "AvoidTagConfig",
    "avoid_tag_config",
    "entry_tags",
    "load_pcv",
    "pcv_ranks",
    "rights_ex_dates",
    "tag_report",
]
