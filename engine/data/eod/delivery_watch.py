"""Evening delivery-spike watchlist for the live shadow scanner (optional watchlist group).

After NSE's evening files are out (~19:30 IST on trade date ``T``):

1. read the security-wise delivery files (``sec_bhavdata_full``) for ``T`` and up to 20 sessions
   before it;
2. keep stocks with F&O contracts (stock futures in the ``T`` F&O bhavcopy);
3. run :func:`engine.data.eod.bhavcopy.scan_relative_delivery_spikes`;
4. drop names in the F&O ban period for the next session (no new positions allowed) and names
   whose near-ATM options did not trade enough (:class:`OptionLiquidity`; a stock that cannot be
   expressed cheaply through its options is not worth a feed slot);
5. keep the top ``max_symbols`` by bias priority, then delivery multiple, and write
   ``<dir>/<T>.json``.

All thresholds (:class:`SpikeThresholds`, :class:`OptionLiquidity`) are HYPOTHESES from config,
untested: spikes choose what to watch, not what to trade, until the event study in
``engine/research/delivery_event_study.py`` says otherwise.

The live engine (:func:`load_delivery_watchlist`) reads the newest file dated *before* its
session day, so a file is never used on the day it describes. Items carry their delivery bias
(feeds ``VWAP_DELIVERY_RECLAIM`` and the delivery points of the score) and ``T``'s high, low and
close as previous-day levels.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from engine.core.rules import IST
from engine.data.eod.bhavcopy import (
    CashBhavcopyRow,
    DeliveryBias,
    FnOBhavcopyRow,
    parse_cash_bhavcopy_csv,
    parse_fno_bhavcopy_csv,
    scan_relative_delivery_spikes,
)
from engine.data.eod.download import eod_path, eod_url, read_eod_text
from engine.data.eod.flows import latest_file, parse_ban_list

STOCK_FUTURES = {"STF", "FUTSTK"}
STOCK_OPTIONS = {"STO", "OPTSTK"}
DEFAULT_BIASES = (DeliveryBias.LONG_ACCUMULATION, DeliveryBias.RETAIL_CHURN_FADE)


@dataclass(frozen=True)
class SpikeThresholds:
    """HYPOTHESIS thresholds for :func:`scan_relative_delivery_spikes` (config)."""

    min_delivery_qty_mult: float = 2.0
    min_accumulation_return_pct: float = 1.0
    min_churn_spike_pct: float = 5.0
    max_churn_rel_delivery_ratio: float = 0.75


@dataclass(frozen=True)
class OptionLiquidity:
    """HYPOTHESIS: near-ATM options of the nearest expiry at least ``min_days_to_expiry`` days
    out (the one the bot would buy), ``strikes_each_side`` strikes around the underlying, calls
    and puts summed, must have traded ``min_contracts`` with open interest ``min_oi_contracts``
    on the trade date."""

    strikes_each_side: int = 2
    min_contracts: int = 1000
    min_oi_contracts: int = 500
    min_days_to_expiry: int = 5


@dataclass(frozen=True, slots=True)
class OptionStat:
    expiry: date
    contracts: int
    oi_contracts: int


def near_atm_option_stats(
    fo_rows: Sequence[FnOBhavcopyRow], trade_date: date, cfg: OptionLiquidity
) -> dict[str, OptionStat]:
    """Per stock: traded contracts and OI (contracts; the file gives OI in shares) of the
    near-ATM options of the expiry the bot would trade."""
    by_sym: dict[str, list[FnOBhavcopyRow]] = {}
    for r in fo_rows:
        if r.instrument in STOCK_OPTIONS and r.trade_date == trade_date:
            by_sym.setdefault(r.symbol, []).append(r)
    out: dict[str, OptionStat] = {}
    for sym, rows in by_sym.items():
        exps = sorted(
            {r.expiry for r in rows if (r.expiry - trade_date).days >= cfg.min_days_to_expiry}
        )
        if not exps:
            continue
        near = [r for r in rows if r.expiry == exps[0]]
        und = next((r.underlying_price for r in near if r.underlying_price > 0), 0.0)
        strikes = sorted({r.strike for r in near})
        if und <= 0 or not strikes:
            continue
        atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - und))
        lo, hi = max(0, atm - cfg.strikes_each_side), atm + cfg.strikes_each_side
        band = set(strikes[lo : hi + 1])
        sel = [r for r in near if r.strike in band]
        out[sym] = OptionStat(
            expiry=exps[0],
            contracts=sum(r.contracts for r in sel),
            oi_contracts=sum(r.oi // r.lot_size if r.lot_size > 0 else 0 for r in sel),
        )
    return out


def is_liquid(stat: OptionStat | None, cfg: OptionLiquidity) -> bool:
    return (
        stat is not None
        and stat.contracts >= cfg.min_contracts
        and stat.oi_contracts >= cfg.min_oi_contracts
    )


@dataclass(frozen=True, slots=True)
class DeliveryWatchItem:
    symbol: str
    bias: str
    trade_date: date
    delivery_qty_multiple: float
    delivery_pct: float
    avg_20d_delivery_pct: float
    close_return_pct: float
    anchor_vwap: float
    prev_high: float
    prev_low: float
    prev_close: float
    rationale: str
    option_expiry: str = ""
    option_contracts: int = 0
    option_oi_contracts: int = 0


def fno_stock_symbols(fo_text: str) -> set[str]:
    return {r.symbol for r in parse_fno_bhavcopy_csv(fo_text) if r.instrument in STOCK_FUTURES}


def delivery_history(
    raw_dir: Path, trade_date: date, *, sessions: int = 21, max_calendar_days: int = 45
) -> dict[str, list[CashBhavcopyRow]]:
    """``symbol -> rows`` from the delivery files on or before ``trade_date`` (newest
    ``sessions`` files that exist; holidays simply have no file)."""
    out: dict[str, list[CashBhavcopyRow]] = {}
    found = 0
    for back in range(max_calendar_days + 1):
        d = trade_date - timedelta(days=back)
        p = eod_path(raw_dir, "delivery", d)
        if not p.exists():
            continue
        n = 0
        for r in parse_cash_bhavcopy_csv(read_eod_text(p)):
            if r.trade_date == d:
                out.setdefault(r.symbol, []).append(r)
                n += 1
        found += n > 0  # a file whose rows carry another date (e.g. a holiday) does not count
        if found >= sessions:
            break
    return out


def ban_for_next_session(raw_dir: Path, trade_date: date) -> tuple[date | None, list[str]]:
    """The newest saved ban list that applies after ``trade_date`` (NSE publishes it the evening
    before). Returns (its trade date, symbols) or (None, []) when not saved yet."""
    f = latest_file(raw_dir, "fo_ban")
    if f is None:
        return None, []
    day, syms = parse_ban_list(f.read_text(encoding="utf-8", errors="replace"))
    if day is None or day <= trade_date:
        return day, []
    return day, syms


def select_watchlist(
    history: dict[str, list[CashBhavcopyRow]],
    *,
    fno_symbols: Iterable[str],
    banned: Iterable[str],
    max_symbols: int,
    biases: Sequence[DeliveryBias] = DEFAULT_BIASES,
    lookback_days: int = 20,
    spike: SpikeThresholds | None = None,
    options: Mapping[str, OptionStat] | None = None,
    liquidity: OptionLiquidity | None = None,
) -> tuple[list[DeliveryWatchItem], list[str], list[str]]:
    """(chosen items, spike symbols dropped for the ban list, dropped for thin options).
    ``options=None`` skips the option-liquidity filter."""
    fno = set(fno_symbols)
    ban = set(banned)
    th = spike or SpikeThresholds()
    liq = liquidity or OptionLiquidity()
    hist: dict[str, Sequence[CashBhavcopyRow]] = {s: r for s, r in history.items() if s in fno}
    spikes = scan_relative_delivery_spikes(
        hist,
        lookback_days=lookback_days,
        min_delivery_qty_mult=th.min_delivery_qty_mult,
        min_accumulation_return_pct=th.min_accumulation_return_pct,
        min_churn_spike_pct=th.min_churn_spike_pct,
        max_churn_rel_delivery_ratio=th.max_churn_rel_delivery_ratio,
    )
    order = {b: i for i, b in enumerate(biases)}
    spikes = [s for s in spikes if s.bias in order]
    dropped = sorted(s.symbol for s in spikes if s.symbol in ban)
    thin = sorted(
        s.symbol
        for s in spikes
        if s.symbol not in ban and options is not None and not is_liquid(options.get(s.symbol), liq)
    )
    chosen = [s for s in spikes if s.symbol not in ban and s.symbol not in thin]
    chosen.sort(key=lambda s: (order[s.bias], -s.delivery_qty_multiple, s.symbol))
    items: list[DeliveryWatchItem] = []
    for s in chosen[:max_symbols]:
        last = max(hist[s.symbol], key=lambda r: r.trade_date)
        stat = options.get(s.symbol) if options is not None else None
        items.append(
            DeliveryWatchItem(
                symbol=s.symbol,
                bias=s.bias.value,
                trade_date=s.trade_date,
                delivery_qty_multiple=s.delivery_qty_multiple,
                delivery_pct=s.delivery_pct,
                avg_20d_delivery_pct=s.avg_20d_delivery_pct,
                close_return_pct=s.close_return_pct,
                anchor_vwap=s.anchor_vwap,
                prev_high=last.high,
                prev_low=last.low,
                prev_close=last.close,
                rationale=s.rationale,
                option_expiry=stat.expiry.isoformat() if stat else "",
                option_contracts=stat.contracts if stat else 0,
                option_oi_contracts=stat.oi_contracts if stat else 0,
            )
        )
    return items, dropped, thin


def watchlist_path(out_dir: Path, trade_date: date) -> Path:
    return out_dir / f"{trade_date.isoformat()}.json"


def build_delivery_watchlist(
    raw_dir: Path,
    out_dir: Path,
    trade_date: date,
    *,
    now: datetime,
    max_symbols: int = 5,
    biases: Sequence[DeliveryBias] = DEFAULT_BIASES,
    lookback_days: int = 20,
    min_sessions: int = 6,
    spike: SpikeThresholds | None = None,
    liquidity: OptionLiquidity | None = None,
) -> dict[str, Any]:
    """Build and save ``<out_dir>/<trade_date>.json``; returns the payload written. With fewer
    than ``min_sessions`` delivery files (``T`` plus 5 prior) the list is written empty."""
    fo = eod_path(raw_dir, "fo", trade_date)
    if not fo.exists():
        raise FileNotFoundError(f"F&O bhavcopy for {trade_date} not downloaded ({fo})")
    if not eod_path(raw_dir, "delivery", trade_date).exists():
        raise FileNotFoundError(f"delivery file for {trade_date} not downloaded")
    fo_rows = parse_fno_bhavcopy_csv(read_eod_text(fo))
    fno = {r.symbol for r in fo_rows if r.instrument in STOCK_FUTURES}
    liq = liquidity or OptionLiquidity()
    options = near_atm_option_stats(fo_rows, trade_date, liq)
    history = delivery_history(raw_dir, trade_date, sessions=lookback_days + 1)
    sessions = len({r.trade_date for rows in history.values() for r in rows})
    ban_day, banned = ban_for_next_session(raw_dir, trade_date)
    items, dropped, thin = select_watchlist(
        history,
        fno_symbols=fno,
        banned=banned,
        max_symbols=max_symbols,
        biases=biases,
        lookback_days=lookback_days,
        spike=spike,
        options=options,
        liquidity=liq,
    )
    if sessions < min_sessions:
        items, dropped, thin = [], [], []
    payload: dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        "known_at": datetime.combine(trade_date, time(19, 30), IST).isoformat(),
        "generated_at": now.isoformat(),
        "sessions_used": sessions,
        "fno_stocks": len(fno),
        "ban_trade_date": ban_day.isoformat() if ban_day else None,
        "ban_list_saved": ban_day is not None and ban_day > trade_date,
        "excluded_for_ban": dropped,
        "excluded_thin_options": thin,
        "thresholds_hypothesis": {
            "spike": asdict(spike or SpikeThresholds()),
            "option_liquidity": asdict(liq),
        },
        "biases": [b.value for b in biases],
        "items": [{**asdict(i), "trade_date": i.trade_date.isoformat()} for i in items],
        "sources": [eod_url("delivery", trade_date), eod_url("fo", trade_date)],
        "note": (
            "EOD delivery spikes pick what to watch, not what to trade (HYPOTHESIS thresholds, "
            "untested)."
            + (
                f" Empty: only {sessions} delivery file(s), {min_sessions} needed."
                if sessions < min_sessions
                else ""
            )
        ),
    }
    out = watchlist_path(out_dir, trade_date)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, out)
    return payload


def load_delivery_watchlist(
    out_dir: Path, session_day: date, *, max_age_days: int = 5
) -> tuple[date | None, list[DeliveryWatchItem]]:
    """Newest watchlist dated before ``session_day`` and at most ``max_age_days`` old."""
    if not out_dir.exists():
        return None, []
    files = sorted(
        p for p in out_dir.glob("*.json") if p.stem < session_day.isoformat() and len(p.stem) == 10
    )
    if not files:
        return None, []
    p = files[-1]
    day = date.fromisoformat(p.stem)
    if (session_day - day).days > max_age_days:
        return day, []
    data = json.loads(p.read_text(encoding="utf-8"))
    items = []
    for r in data.get("items", []):
        try:
            items.append(
                DeliveryWatchItem(**{**r, "trade_date": date.fromisoformat(r["trade_date"])})
            )
        except (TypeError, KeyError, ValueError):
            continue
    return day, items


def missing_delivery_days(
    raw_dir: Path, trade_date: date, *, weekdays_back: int, is_trading_day: Callable[[date], bool]
) -> list[date]:
    """Trading days in the lookback whose delivery file is not saved yet."""
    out: list[date] = []
    d = trade_date
    seen = 0
    while seen < weekdays_back:
        if is_trading_day(d):
            seen += 1
            if not eod_path(raw_dir, "delivery", d).exists():
                out.append(d)
        d -= timedelta(days=1)
    return out
