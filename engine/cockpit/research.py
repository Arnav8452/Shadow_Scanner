"""Research tab data (read-only, computed on request in the gateway's thread pool).

The key table is the signal ledger (Track A) split by score band: if higher scores do not earn
more after costs and at 2x slippage, the ranking is not worth trading on.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

BANDS = ((75.0, 80.0, "75–80"), (80.0, 90.0, "80–90"), (90.0, 101.0, "90+"))
ACCOUNTS = ("bot", "score", "debit", "gamma", "condor", "manual")
_REPORT_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,60}\.md$")


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    nets = [float(r.get("net_pnl_inr", 0.0)) for r in rows]
    net2 = [float(r.get("net_pnl_2x_slippage_inr", r.get("net_pnl_inr", 0.0))) for r in rows]
    rs = [float(r.get("r_multiple_net", 0.0)) for r in rows]
    wins = sum(1 for x in nets if x > 0)
    return {
        "n": n,
        "win_pct": round(100.0 * wins / n, 1) if n else None,
        "net": round(sum(nets), 2),
        "net_2x": round(sum(net2), 2),
        "avg_net": round(sum(nets) / n, 2) if n else None,
        "avg_r": round(sum(rs) / n, 3) if n else None,
    }


def signal_ledger_stats(path: Path) -> dict[str, Any]:
    rows = list(_jsonl(path))
    days = sorted({str(r.get("closed_at", ""))[:10] for r in rows if r.get("closed_at")})
    bands = []
    for lo, hi, name in BANDS:
        sel = [r for r in rows if lo <= float(r.get("confidence_score", 0.0)) < hi]
        bands.append({"band": name, **_bucket(sel)})
    by_wave: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_hour: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_status: dict[str, list[dict[str, Any]]] = defaultdict(list)
    tradable: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_wave[str(r.get("wave", "?"))].append(r)
        by_status[str(r.get("status", "?"))].append(r)
        tradable["tradable" if r.get("tradable", True) else "not tradable"].append(r)
        try:
            h = datetime.fromisoformat(str(r["opened_at"])).strftime("%H:00")
        except (KeyError, ValueError):
            h = "?"
        by_hour[h].append(r)
    return {
        "days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "total": _bucket(rows),
        "bands": bands,
        "by_wave": [{"key": k, **_bucket(v)} for k, v in sorted(by_wave.items())],
        "by_hour": [{"key": k, **_bucket(v)} for k, v in sorted(by_hour.items())],
        "by_exit": [{"key": k, **_bucket(v)} for k, v in sorted(by_status.items())],
        "tradable": [{"key": k, **_bucket(v)} for k, v in sorted(tradable.items())],
        "note": (
            "Unproven until 20–30 sessions; every variant tried counts as a trial "
            "(deflated Sharpe). Treat a band as real only with t ≈ 3."
        ),
    }


STUDY_BANDS = (
    (75.0, 80.0, "75–80"),
    (80.0, 90.0, "80–90"),
    (90.0, 101.0, "90+"),
)
STUDY_KEYS = ("15m", "30m", "60m", "close")


def _study_cell(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Share of signals where the underlying moved the signal's way, mean move, its t-stat and
    the ATM-option net after charges, at one horizon."""
    res = [r["results"][key] for r in rows if isinstance(r.get("results", {}).get(key), dict)]
    moves = [float(x["move_pct"]) for x in res if "move_pct" in x]
    opt = [float(x["opt_net_inr"]) for x in res if "opt_net_inr" in x]
    n = len(moves)
    mean = sum(moves) / n if n else None
    t = None
    if n >= 3 and mean is not None:
        var = sum((m - mean) ** 2 for m in moves) / (n - 1)
        t = round(mean / (var**0.5 / n**0.5), 2) if var > 0 else None
    return {
        "n": n,
        "hit_pct": round(100.0 * sum(1 for m in moves if m > 0) / n, 1) if n else None,
        "avg_move_pct": round(mean, 3) if mean is not None else None,
        "t": t,
        "opt_n": len(opt),
        "opt_net": round(sum(opt), 2) if opt else None,
        "opt_avg": round(sum(opt) / len(opt), 2) if opt else None,
    }


def study_stats(path: Path) -> dict[str, Any]:
    """Signal study (``signal_study.jsonl``) by score band, setup and hour, at each horizon."""
    rows = [r for r in _jsonl(path) if isinstance(r.get("results"), dict)]
    days = sorted({str(r.get("t0", ""))[:10] for r in rows if r.get("t0")})

    def table(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        return [{"key": k, **{h: _study_cell(v, h) for h in STUDY_KEYS}} for k, v in groups.items()]

    bands: dict[str, list[dict[str, Any]]] = {name: [] for _, _, name in STUDY_BANDS}
    waves: dict[str, list[dict[str, Any]]] = defaultdict(list)
    hours: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unds: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        sc = float(r.get("score", 0.0))
        for lo, hi, name in STUDY_BANDS:
            if lo <= sc < hi:
                bands[name].append(r)
        waves[str(r.get("wave", "?"))].append(r)
        hours[str(r.get("time", "?"))[:2] + ":00"].append(r)
        unds[f"{r.get('underlying', '?')} {r.get('direction', '?')}"].append(r)
    return {
        "days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "horizons": list(STUDY_KEYS),
        "total": table({"all": rows}),
        "bands": table(bands),
        "by_wave": table(dict(sorted(waves.items()))),
        "by_hour": table(dict(sorted(hours.items()))),
        "by_underlying": table(dict(sorted(unds.items()))),
        "note": (
            "Hit % = the underlying moved the signal's way; t = mean move / standard error. "
            "Treat an edge as real only with t ≈ 3 over 20–30 sessions; every band, setup or "
            "horizon looked at counts as a trial."
        ),
    }


def screener(root: Path, journal_dir: Path, events_file: Path) -> dict[str, Any]:
    """EOD F&O screener (engine/cockpit/screener.py); first call parses ~21 files (~40 s)."""
    from engine.cockpit.screener import ScreenerCache, screen
    from engine.core.calendar import TradingCalendar
    from engine.core.rules import IST, RuleBook
    from engine.data.eod.flows import latest_file, parse_ban_list
    from engine.data.events.calendar_file import load_calendar

    book = RuleBook.load(root / "config" / "rules")
    raw = root / "data" / "raw"
    cache = ScreenerCache(
        raw, journal_dir / "screener", book=book, calendar=TradingCalendar.from_rulebook(book)
    )
    now = datetime.now(IST)
    ban: list[str] = []
    bp = latest_file(raw, "fo_ban", now.date())
    if bp is not None:
        ban_day, syms = parse_ban_list(bp.read_text(encoding="utf-8", errors="replace"))
        if ban_day is not None and ban_day >= now.date():
            ban = syms
    try:
        events = load_calendar(events_file)
    except Exception:
        events = []
    return screen(cache, now=now, events=events, ban=ban)


CONTEXT_FEATURES: tuple[tuple[str, str, Any], ...] = (
    # (key, label, bucketing: tuple of (upper bound, name) for numbers, or None for categories)
    ("hour", "Entry hour", None),
    ("ivp", "IV percentile", ((25, "<25"), (50, "25–50"), (75, "50–75"), (101, "75+"))),
    ("vix", "India VIX", ((12, "<12"), (15, "12–15"), (18, "15–18"), (99, "18+"))),
    ("gex", "Dealer gamma (HYP)", None),
    ("pcr", "PCR (OI)", ((0.7, "<0.7"), (1.0, "0.7–1.0"), (1.3, "1.0–1.3"), (99, "1.3+"))),
    ("vs_prev_day", "Vs previous day range", None),
    ("vs_opening_range", "Vs opening range", None),
    ("breadth_pct", "Heavyweight breadth %", ((-25, "< −25"), (25, "−25–25"), (101, "> 25"))),
    ("corr", "Heavyweight correlation", ((0.3, "<0.3"), (0.6, "0.3–0.6"), (2, "0.6+"))),
    ("event_window", "Inside event window", None),
)


def _bucket_of(value: Any, spec: Any) -> str | None:
    if value is None:
        return None
    if spec is None:
        return str(value)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    for hi, name in spec:
        if v < hi:
            return str(name)
    return str(spec[-1][1])


def _aligned(ctx: dict[str, Any], up: bool) -> str | None:
    """Does heavyweight breadth agree with the trade's direction?"""
    b = ctx.get("breadth_pct")
    if b is None or abs(float(b)) < 25:
        return "breadth neutral" if b is not None else None
    return "with breadth" if (float(b) > 0) == up else "against breadth"


def context_stats(journal_dir: Path, horizon: str = "30m") -> dict[str, Any]:
    """Which market conditions did results come from? Closed paper trades (net ₹ after all
    costs) and signal-study rows (index moved the signal's way at ``horizon``), split by each
    context feature recorded at entry. Many cells are looked at: treat a gap as a lead to test,
    not an edge, until t ≈ 3 over 20–30 sessions."""
    trades: list[dict[str, Any]] = []
    for name in ACCOUNTS:
        for r in _jsonl(journal_dir / f"paper_{name}.jsonl"):
            if r.get("type") != "close" or not isinstance(r.get("context"), dict):
                continue
            trades.append({"acct": name, "net": float(r.get("net_inr", 0.0)), "ctx": r["context"],
                           "up": "PE" not in str(r.get("symbol", ""))})  # fmt: skip
    study: list[dict[str, Any]] = []
    for r in _jsonl(journal_dir / "signal_study.jsonl"):
        res = (r.get("results") or {}).get(horizon) or {}
        if "move_pct" not in res or not isinstance(r.get("context"), dict):
            continue
        study.append({"hit": bool(res.get("hit")), "move": float(res["move_pct"]),
                      "ctx": r["context"], "up": r.get("direction") == "UP",
                      "setup": r.get("wave") == "SETUP_CONFIRMED"})  # fmt: skip

    def split(rows: list[dict[str, Any]], key: str, spec: Any) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for x in rows:
            b = (
                _aligned(x["ctx"], x["up"])
                if key == "_breadth_align"
                else _bucket_of(x["ctx"].get(key), spec)
            )
            if b is not None:
                out[b].append(x)
        return dict(sorted(out.items()))

    features = [*CONTEXT_FEATURES, ("_breadth_align", "Breadth vs direction", None)]
    out_features = []
    for key, label, spec in features:
        t_groups = split(trades, key, spec)
        s_groups = split(study, key, spec)
        buckets = sorted(set(t_groups) | set(s_groups))
        if not buckets:
            continue
        rows = []
        for b in buckets:
            tg, sg = t_groups.get(b, []), s_groups.get(b, [])
            nets = [x["net"] for x in tg]
            moves = [x["move"] for x in sg]
            t_stat = None
            if len(moves) >= 3:
                m = sum(moves) / len(moves)
                var = sum((v - m) ** 2 for v in moves) / (len(moves) - 1)
                t_stat = round(m / (var**0.5 / len(moves) ** 0.5), 2) if var > 0 else None
            rows.append(
                {
                    "bucket": b,
                    "trades": len(tg),
                    "trade_win_pct": round(100 * sum(1 for v in nets if v > 0) / len(nets), 1)
                    if nets
                    else None,
                    "trade_net": round(sum(nets), 2) if nets else None,
                    "trade_avg": round(sum(nets) / len(nets), 2) if nets else None,
                    "signals": len(sg),
                    "signal_hit_pct": round(100 * sum(1 for x in sg if x["hit"]) / len(sg), 1)
                    if sg
                    else None,
                    "signal_avg_move_pct": round(sum(moves) / len(moves), 3) if moves else None,
                    "t": t_stat,
                }
            )
        out_features.append({"key": key, "label": label, "rows": rows})
    return {
        "horizon": horizon,
        "trades": len(trades),
        "signals": len(study),
        "features": out_features,
        "note": (
            "Each trade and signal carries the market state recorded at entry. Every bucket "
            "here is a comparison you are making: with this many cells some will look good by "
            "chance. Treat a gap as a hypothesis to test forward, not an edge, until t ≈ 3 over "
            "20–30 sessions (deflated Sharpe)."
        ),
    }


def paper_history(journal_dir: Path) -> dict[str, Any]:
    days: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    for name in ACCOUNTS:
        for r in _jsonl(journal_dir / f"paper_{name}.jsonl"):
            if r.get("type") != "close":
                continue
            d = str(r.get("closed_at", r.get("t", "")))[:10]
            slot = days[d].setdefault(name, {"trades": 0, "net": 0.0, "wins": 0})
            slot["trades"] += 1
            slot["net"] = round(slot["net"] + float(r.get("net_inr", 0.0)), 2)
            slot["wins"] += 1 if float(r.get("net_inr", 0.0)) > 0 else 0
    return {"days": [{"day": d, **v} for d, v in sorted(days.items(), reverse=True)]}


def iv_series(path: Path, keep: int = 400) -> dict[str, Any]:
    if not path.exists():
        return {"rows": []}
    import pyarrow.parquet as pq

    t = pq.read_table(path, columns=["trade_date", "underlying", "atm_iv", "vix_close"])
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in t.to_pylist():
        u = str(r["underlying"])
        out[u].append(
            {
                "d": r["trade_date"].isoformat(),
                "iv": round(float(r["atm_iv"]) * 100, 2) if r["atm_iv"] is not None else None,
                "vix": r["vix_close"],
            }
        )
    return {u: sorted(v, key=lambda x: x["d"])[-keep:] for u, v in out.items()}


def qc_reports(qc_dir: Path, keep: int = 30) -> dict[str, Any]:
    rows = []
    for p in sorted(qc_dir.glob("*.json"), reverse=True)[:keep]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        rows.append(
            {
                "day": d.get("day", p.stem),
                "passed": d.get("passed"),
                "problems": d.get("problems", [])[:6],
                "rows": d.get("rows"),
                "max_gap_s": d.get("max_gap_s"),
                "oi_present": d.get("oi_present"),
            }
        )
    return {"reports": rows}


def mining_reports(mined_dir: Path) -> dict[str, Any]:
    names = sorted(p.name for p in mined_dir.glob("*.md") if _REPORT_NAME.match(p.name))
    return {"reports": names}


def mining_report(mined_dir: Path, name: str) -> dict[str, Any]:
    if not _REPORT_NAME.match(name):
        raise ValueError("bad report name")
    p = mined_dir / name
    if not p.exists():
        raise FileNotFoundError(name)
    return {"name": name, "text": p.read_text(encoding="utf-8")}


def flows(raw_dir: Path) -> dict[str, Any]:
    """Latest participant positioning (OI and volume), F&O ban list and bulk/block deals."""
    from engine.data.eod.flows import latest_file, parse_ban_list, parse_deals
    from engine.data.eod.participant_oi import parse_participant_oi_csv

    out: dict[str, Any] = {}
    for ds in ("participant_oi", "participant_vol"):
        base = raw_dir / "eod" / ds
        files = sorted(base.glob("*/*.csv"))
        if not files:
            continue
        p = files[-1]
        day = date.fromisoformat(p.stem)
        rows = parse_participant_oi_csv(
            p.read_text(encoding="utf-8", errors="replace"), trade_date=day
        )
        out[ds] = {
            "day": day.isoformat(),
            "rows": [
                {
                    "who": r.category.value,
                    "fut_idx_net": r.net_fut_idx,
                    "fut_idx_long": r.fut_idx_long,
                    "fut_idx_short": r.fut_idx_short,
                    "call_net": r.opt_idx_call_long - r.opt_idx_call_short,
                    "put_net": r.opt_idx_put_long - r.opt_idx_put_short,
                    "fut_stk_net": r.fut_stk_long - r.fut_stk_short,
                }
                for r in rows
            ],
        }
    ban = latest_file(raw_dir, "fo_ban")
    if ban is not None:
        d, syms = parse_ban_list(ban.read_text(encoding="utf-8", errors="replace"))
        out["fo_ban"] = {"trade_date": d.isoformat() if d else ban.stem, "symbols": syms}
    deals: list[dict[str, Any]] = []
    for kind in ("block", "bulk"):
        f = latest_file(raw_dir, kind)
        if f is None:
            continue
        for x in parse_deals(f.read_text(encoding="utf-8", errors="replace"), kind):
            deals.append(
                {
                    "day": x.trade_date.isoformat(),
                    "kind": x.kind,
                    "symbol": x.symbol,
                    "client": x.client,
                    "side": x.side,
                    "qty": x.qty,
                    "price": x.price,
                    "value_cr": round(x.value_cr, 2),
                }
            )
    deals.sort(key=lambda d: -d["value_cr"])
    out["deals"] = deals[:25]
    out["note"] = (
        "EOD files, known after ~19:30 IST. Participant figures are contracts; "
        "ban list applies to the trade date shown."
    )
    return out
