"""F&O screener from NSE's end-of-day files (all ~220 F&O underlyings, indices and stocks).

Point in time: a bhavcopy for day T is known at 19:30 on T, so during T+1 the screener shows T's
close. Everything is computed from the exchange file, never from a broker's analytics:

- futures price and change, futures OI change, and the price/OI quadrant (long build-up, short
  build-up, short covering, long unwinding);
- ATM IV: our Black-76 on the synthetic forward from closes (``atm_iv_for_day``, tier C), its
  change and its percentile over up to 252 earlier sessions (needs >= 60);
- volume: contracts traded vs the average of the previous 20 sessions;
- near-expiry PCR (OI) and max pain;
- results dates (calendar rows) and the F&O ban list.

Per-session summaries are cached in ``data/state/screener/<date>.json`` (one parse of each
1.4-s bhavcopy, ever). Opportunity flags are rule-of-thumb HYPOTHESES for the owner's
attention, not signals; they are counted as trials if ever traded.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from engine.cockpit.chain import max_pain
from engine.core.calendar import TradingCalendar
from engine.core.rules import IST, RuleBook
from engine.data.eod.bhavcopy import FnOBhavcopyRow, parse_fno_bhavcopy_csv
from engine.data.eod.download import eod_path, read_eod_text
from engine.data.eod.iv_history import atm_iv_for_day, percentile_rank

FUT = ("IDF", "STF", "FUTIDX", "FUTSTK")
OPT = ("IDO", "STO", "OPTIDX", "OPTSTK")
INDEX_INSTR = ("IDF", "IDO", "FUTIDX", "OPTIDX")
SUMMARY_VERSION = 1


def summarize_day(
    rows: Sequence[FnOBhavcopyRow], day: date, *, book: RuleBook, calendar: TradingCalendar | None
) -> dict[str, dict[str, Any]]:
    """Per underlying: futures closes and OI, contracts, near-expiry PCR / max pain, ATM IV."""
    by_sym: dict[str, list[FnOBhavcopyRow]] = defaultdict(list)
    for r in rows:
        if r.trade_date == day:
            by_sym[r.symbol].append(r)
    try:
        rate = book.lookup("risk_free_rate", day, allow_unverified=True, tenor="91d_tbill")
        r_frac = rate.as_float() / 100.0
    except Exception:
        r_frac = 0.065
    out: dict[str, dict[str, Any]] = {}
    for sym, rs in by_sym.items():
        futs = sorted((x for x in rs if x.instrument in FUT), key=lambda x: x.expiry)
        opts = [x for x in rs if x.instrument in OPT and x.option_type in ("CE", "PE")]
        is_index = any(x.instrument in INDEX_INSTR for x in rs)
        rec: dict[str, Any] = {
            "kind": "index" if is_index else "stock",
            "spot": next((x.underlying_price for x in rs if x.underlying_price > 0), None),
            "contracts": sum(x.contracts for x in rs),
            "fut": {x.expiry.isoformat(): x.close for x in futs if x.close > 0},
            "fut_oi": sum(x.oi for x in futs),
            "lot": next((x.lot_size for x in rs if x.lot_size), None),
        }
        if opts:
            near = (
                min(x.expiry for x in opts if x.expiry >= day)
                if any(x.expiry >= day for x in opts)
                else None
            )
            if near is not None:
                chain = [x for x in opts if x.expiry == near]
                ce = sum(x.oi for x in chain if x.option_type == "CE")
                pe = sum(x.oi for x in chain if x.option_type == "PE")
                legs: dict[float, dict[str, dict[str, float]]] = {}
                for x in chain:
                    legs.setdefault(float(x.strike), {})[x.option_type] = {"oi": float(x.oi)}
                rec.update(
                    {
                        "opt_expiry": near.isoformat(),
                        "pcr": round(pe / ce, 2) if ce else None,
                        "max_pain": max_pain(legs),
                    }
                )
            # main's atm_iv_for_day reads index option codes only: present stock options as such.
            as_idx = [x if x.instrument in ("IDO", "OPTIDX") else replace(x, instrument="IDO")
                      for x in opts]  # fmt: skip
            try:
                iv = atm_iv_for_day(as_idx, underlying=sym, book=book, calendar=calendar, r=r_frac)
            except Exception:
                iv = None
            if iv is not None:
                rec["atm_iv"] = round(iv.atm_iv * 100, 2)
                rec["iv_expiry"] = iv.expiry.isoformat()
        out[sym] = rec
    return out


class ScreenerCache:
    def __init__(
        self,
        raw_dir: Path,
        cache_dir: Path,
        *,
        book: RuleBook,
        calendar: TradingCalendar | None,
    ) -> None:
        self.raw_dir = raw_dir
        self.cache_dir = cache_dir
        self.book = book
        self.calendar = calendar

    def available_days(self, until: date, back_days: int = 400) -> list[date]:
        out = []
        for i in range(back_days, -1, -1):
            d = until - timedelta(days=i)
            if eod_path(self.raw_dir, "fo", d).exists():
                out.append(d)
        return out

    def summary(self, day: date) -> dict[str, dict[str, Any]] | None:
        path = self.cache_dir / f"{day.isoformat()}.json"
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                if doc.get("v") == SUMMARY_VERSION:
                    return dict(doc["symbols"])
            except (ValueError, KeyError):
                pass
        fo = eod_path(self.raw_dir, "fo", day)
        if not fo.exists():
            return None
        rows = parse_fno_bhavcopy_csv(read_eod_text(fo))
        sums = summarize_day(rows, day, book=self.book, calendar=self.calendar)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": SUMMARY_VERSION, "symbols": sums}), encoding="utf-8")
        tmp.replace(path)
        return sums

    def build(self, days: Iterable[date], progress: Callable[[date], None] | None = None) -> int:
        n = 0
        for d in days:
            if self.summary(d) is not None:
                n += 1
                if progress is not None:
                    progress(d)
        return n

    def cached_days(self) -> list[date]:
        if not self.cache_dir.exists():
            return []
        out = []
        for p in self.cache_dir.glob("*.json"):
            try:
                out.append(date.fromisoformat(p.stem))
            except ValueError:
                continue
        return sorted(out)


def _quadrant(price_chg: float | None, oi_chg: float | None) -> str | None:
    if price_chg is None or oi_chg is None or price_chg == 0 or oi_chg == 0:
        return None
    if price_chg > 0:
        return "long build-up" if oi_chg > 0 else "short covering"
    return "short build-up" if oi_chg > 0 else "long unwinding"


def screen(
    cache: ScreenerCache,
    *,
    now: datetime,
    events: Sequence[Any] = (),
    ban: Sequence[str] = (),
    vol_window: int = 20,
    ivp_lookback: int = 252,
    ivp_min_obs: int = 60,
) -> dict[str, Any]:
    """Rows for the newest session whose file was public by ``now`` (19:30 rule)."""
    local = now.astimezone(IST)
    last_known = (
        local.date()
        if local.hour * 60 + local.minute >= 19 * 60 + 30
        else (local.date() - timedelta(days=1))
    )
    days = cache.available_days(last_known)
    if len(days) < 2:
        return {"day": None, "rows": [], "note": "needs at least two F&O bhavcopies (tasks.py eod)"}
    t, t1 = days[-1], days[-2]
    cur, prev = cache.summary(t), cache.summary(t1)
    if cur is None or prev is None:
        return {"day": None, "rows": [], "note": "bhavcopy unreadable"}
    vol_days = days[-1 - vol_window : -1]
    vol_hist: dict[str, list[int]] = defaultdict(list)
    for d in vol_days:
        s = cache.summary(d) or {}
        for sym, rec in s.items():
            vol_hist[sym].append(int(rec.get("contracts") or 0))
    # IV history only from summaries already cached (tasks.py screener-history builds a year).
    iv_hist: dict[str, list[float]] = defaultdict(list)
    cached = [d for d in cache.cached_days() if d < t][-ivp_lookback:]
    for d in cached:
        s = cache.summary(d) or {}
        for sym, rec in s.items():
            if rec.get("atm_iv"):
                iv_hist[sym].append(float(rec["atm_iv"]))
    results: dict[str, datetime] = {}
    for ce in events:
        ev = getattr(ce, "event", ce)
        if (
            str(getattr(ev.category, "value", ev.category)) != "CONSTITUENT_EARNINGS"
            or not ev.symbol
        ):
            continue
        if ev.start_ts >= now and (ev.symbol not in results or ev.start_ts < results[ev.symbol]):
            results[ev.symbol] = ev.start_ts
    banned = set(ban)
    rows = []
    for sym, rec in cur.items():
        p = prev.get(sym, {})
        futs = rec.get("fut") or {}
        near = min(futs) if futs else None
        fut = futs.get(near) if near else None
        p_fut = (p.get("fut") or {}).get(near) if near else None
        fut_chg = round((fut / p_fut - 1) * 100, 2) if fut and p_fut else None
        oi_chg = (
            round((rec["fut_oi"] / p["fut_oi"] - 1) * 100, 2)
            if rec.get("fut_oi") and p.get("fut_oi")
            else None
        )
        iv, p_iv = rec.get("atm_iv"), p.get("atm_iv")
        hist = iv_hist.get(sym, [])
        ivp = round(percentile_rank(hist, iv)) if iv and len(hist) >= ivp_min_obs else None
        vh = vol_hist.get(sym, [])
        avg = statistics.fmean(vh) if len(vh) >= 5 else None
        vol_x = round(rec["contracts"] / avg, 2) if avg else None
        res = results.get(sym)
        res_days = (res.astimezone(IST).date() - local.date()).days if res else None
        spot = rec.get("spot")
        mp = rec.get("max_pain")
        flags = []
        if sym in banned:
            flags.append("F&O ban: no new positions")
        if ivp is not None and ivp >= 70 and (res_days is None or res_days > 7):
            flags.append("rich premium: credit spread / condor candidate (5.1)")
        if ivp is not None and ivp <= 25:
            flags.append("cheap options: buying candidate (5.2 / 5.8)")
        if res_days is not None and res_days <= 10:
            flags.append(f"results in {res_days} d: IV crush risk (5.5)")
        if vol_x is not None and vol_x >= 2:
            flags.append(f"unusual activity: {vol_x:g}x volume")
        rows.append(
            {
                "symbol": sym,
                "kind": rec.get("kind"),
                "spot": spot,
                "fut": fut,
                "fut_expiry": near,
                "fut_chg_pct": fut_chg,
                "atm_iv": iv,
                "iv_chg": round(iv - p_iv, 2) if iv and p_iv else None,
                "ivp": ivp,
                "ivp_days": len(hist),
                "vol_x": vol_x,
                "fut_oi_chg_pct": oi_chg,
                "quadrant": _quadrant(fut_chg, oi_chg),
                "pcr": rec.get("pcr"),
                "max_pain": mp,
                "max_pain_dist_pct": round((mp / spot - 1) * 100, 2) if mp and spot else None,
                "results": res.astimezone(IST).strftime("%d %b") if res else None,
                "results_days": res_days,
                "ban": sym in banned,
                "lot": rec.get("lot"),
                "flags": flags,
            }
        )
    rows.sort(key=lambda r: (r["kind"] != "index", r["symbol"]))
    return {
        "day": t.isoformat(),
        "prev_day": t1.isoformat(),
        "iv_days_cached": len(cached),
        "rows": rows,
        "note": (
            f"End of day {t.isoformat()} (NSE F&O bhavcopy, public 19:30). IV is ours (Black-76 "
            "on closes, tier C). IV percentile needs ≥ 60 earlier sessions in the cache "
            f"({len(cached)} cached; tasks.py screener-history builds a year). Flags are "
            "HYPOTHESES for attention, not signals."
        ),
    }
