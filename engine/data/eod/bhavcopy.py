"""NSE Cash & F&O EOD Bhavcopy parser and Institutional Relative Delivery % Scanner.

Implements:
1. UDiFF / standard NSE Cash & F&O bhavcopy CSV parsing with strict point-in-time ``known_at``
   timestamps (EOD data from trading date ``T`` is stamped as known after 19:30 IST on ``T``,
   so it can only inform session ``T+1`` and beyond — zero lookahead).
2. **Institutional Relative Delivery Spike Scanner** (PDF §3 & §6):
   - Compares each stock's daily ``delivery_qty`` and ``delivery_pct`` against its rolling 20-day
     baseline (because baseline delivery % differs structurally between high-beta names and
     defensive heavyweights).
   - ``LONG_ACCUMULATION``: ``delivery_qty >= 2.0 * avg_20d_delivery_qty`` AND
     ``delivery_pct >= avg_20d_delivery_pct`` AND ``close_return_pct >= +1.0%``
     -> 3-5 day institutional accumulation watchlist (bias for VWAP reclaim / stop-hunt longs).
   - ``RETAIL_CHURN_FADE``: ``close_return_pct >= +5.0%`` (or intraday high spike >= 8%) AND
     ``delivery_pct < 0.75 * avg_20d_delivery_pct`` (e.g., < 20% delivery on a parabolic day)
     -> purely speculative intraday churn; high-probability mean-reversion short watchlist.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum

from engine.core.rules import IST


class DeliveryBias(StrEnum):
    LONG_ACCUMULATION = "LONG_ACCUMULATION"
    RETAIL_CHURN_FADE = "RETAIL_CHURN_FADE"
    DISTRIBUTION_SHORT = "DISTRIBUTION_SHORT"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True, slots=True)
class CashBhavcopyRow:
    trade_date: date
    symbol: str
    series: str
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    traded_qty: int
    turnover_inr: float
    delivery_qty: int
    delivery_pct: float
    known_at: datetime

    @property
    def close_return_pct(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return (self.close - self.prev_close) / self.prev_close * 100.0

    @property
    def vwap_approx(self) -> float:
        if self.traded_qty <= 0:
            return self.close
        return self.turnover_inr / self.traded_qty


@dataclass(frozen=True, slots=True)
class FnOBhavcopyRow:
    trade_date: date
    symbol: str
    instrument: str  # FUTIDX, FUTSTK, OPTIDX, OPTSTK
    expiry: date
    strike: float
    option_type: str  # XX, CE, PE
    open: float
    high: float
    low: float
    close: float
    settle_price: float
    contracts: int
    oi: int
    change_in_oi: int
    known_at: datetime
    underlying_price: float = 0.0  # UDiFF UndrlygPric (spot used by NSE for the day)
    lot_size: int = 0  # UDiFF NewBrdLotQty


@dataclass(frozen=True, slots=True)
class DeliveryWatchlistEntry:
    trade_date: date
    symbol: str
    bias: DeliveryBias
    delivery_qty: int
    avg_20d_delivery_qty: float
    delivery_qty_multiple: float
    delivery_pct: float
    avg_20d_delivery_pct: float
    close_return_pct: float
    anchor_vwap: float
    rationale: str


def _eod_known_at(d: date) -> datetime:
    """EOD bhavcopy is published ~18:30–19:30 IST; stamp 19:30 IST to forbid intraday lookahead."""
    return datetime.combine(d, time(19, 30), tzinfo=IST)


def _parse_date(raw: str) -> date:
    s = raw.strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format in bhavcopy: {raw!r}")


def parse_cash_bhavcopy_csv(text: str) -> list[CashBhavcopyRow]:
    """Parse NSE Cash Bhavcopy + Delivery CSV (supports both legacy and UDiFF column names)."""
    rows: list[CashBhavcopyRow] = []
    reader = csv.DictReader(io.StringIO(text.strip()))
    for raw in reader:
        norm = {k.strip().upper(): (v or "").strip() for k, v in raw.items() if k}
        series = norm.get("SERIES") or norm.get("SCTYSRS") or "EQ"
        if series not in {"EQ", "BE"}:
            continue
        sym = norm.get("SYMBOL") or norm.get("TCKRSYMB") or ""
        if not sym:
            continue
        dt_str = (
            norm.get("DATE1")
            or norm.get("TIMESTAMP")
            or norm.get("TRADDT")
            or norm.get("TRADE_DATE")
            or ""
        )
        t_date = _parse_date(dt_str)
        open_px = float(norm.get("OPEN_PRICE") or norm.get("OPEN") or norm.get("OPNPRIC") or 0.0)
        high_px = float(norm.get("HIGH_PRICE") or norm.get("HIGH") or norm.get("HGHPRIC") or 0.0)
        low_px = float(norm.get("LOW_PRICE") or norm.get("LOW") or norm.get("LWPRIC") or 0.0)
        close_px = float(norm.get("CLOSE_PRICE") or norm.get("CLOSE") or norm.get("CLSPRIC") or 0.0)
        prev_px = float(
            norm.get("PREV_CLOSE") or norm.get("PREVCLOSE") or norm.get("PRVSCLSGPRIC") or close_px
        )
        trd_qty = int(
            float(norm.get("TTL_TRD_QNTY") or norm.get("TOTTRDQTY") or norm.get("TTLTRADGVOL") or 0)
        )
        turnover = float(
            norm.get("TURNOVER_LACS")
            and float(norm["TURNOVER_LACS"]) * 100_000.0
            or norm.get("TOTTRDVAL")
            or norm.get("TTLTRFVAL")
            or (close_px * trd_qty)
        )
        deliv_str = norm.get("DELIV_QTY") or norm.get("DELIVERY_QTY") or "0"
        deliv_qty = 0 if deliv_str in {"-", ""} else int(float(deliv_str))
        deliv_pct_str = norm.get("DELIV_PER") or norm.get("DELIVERY_PCT") or ""
        if deliv_pct_str and deliv_pct_str != "-":
            deliv_pct = float(deliv_pct_str)
        else:
            deliv_pct = (deliv_qty / trd_qty * 100.0) if trd_qty > 0 else 0.0

        rows.append(
            CashBhavcopyRow(
                trade_date=t_date,
                symbol=sym,
                series=series,
                open=open_px,
                high=high_px,
                low=low_px,
                close=close_px,
                prev_close=prev_px,
                traded_qty=trd_qty,
                turnover_inr=turnover,
                delivery_qty=deliv_qty,
                delivery_pct=deliv_pct,
                known_at=_eod_known_at(t_date),
            )
        )
    return rows


def parse_fno_bhavcopy_csv(text: str) -> list[FnOBhavcopyRow]:
    """Parse NSE F&O Bhavcopy CSV (supports legacy and UDiFF column names)."""
    rows: list[FnOBhavcopyRow] = []
    reader = csv.DictReader(io.StringIO(text.strip()))
    for raw in reader:
        norm = {k.strip().upper(): (v or "").strip() for k, v in raw.items() if k}
        sym = norm.get("SYMBOL") or norm.get("TCKRSYMB") or ""
        if not sym:
            continue
        inst = norm.get("INSTRUMENT") or norm.get("FININSTRMTP") or "FUTIDX"
        t_date = _parse_date(
            norm.get("TIMESTAMP") or norm.get("TRADDT") or norm.get("TRADE_DATE") or ""
        )
        exp_date = _parse_date(
            norm.get("EXPIRY_DT") or norm.get("XPRYDT") or norm.get("EXPIRY") or ""
        )
        strike = float(norm.get("STRIKE_PR") or norm.get("STRKPRIC") or 0.0)
        opt_type = norm.get("OPTION_TYP") or norm.get("OPTNTP") or "XX"
        open_px = float(norm.get("OPEN") or norm.get("OPNPRIC") or 0.0)
        high_px = float(norm.get("HIGH") or norm.get("HGHPRIC") or 0.0)
        low_px = float(norm.get("LOW") or norm.get("LWPRIC") or 0.0)
        close_px = float(norm.get("CLOSE") or norm.get("CLSPRIC") or 0.0)
        settle_px = float(norm.get("SETTLE_PR") or norm.get("STTLMPRIC") or close_px)
        contracts = int(float(norm.get("CONTRACTS") or norm.get("TTLTRADGVOL") or 0))
        oi = int(float(norm.get("OPEN_INT") or norm.get("OPNINTRST") or 0))
        chg_oi = int(float(norm.get("CHG_IN_OI") or norm.get("CHGINOPNINTRST") or 0))
        und_px = float(norm.get("UNDRLYGPRIC") or 0.0)
        lot = int(float(norm.get("NEWBRDLOTQTY") or norm.get("MARKET_LOT") or 0))

        rows.append(
            FnOBhavcopyRow(
                trade_date=t_date,
                symbol=sym,
                instrument=inst,
                expiry=exp_date,
                strike=strike,
                option_type=opt_type,
                open=open_px,
                high=high_px,
                low=low_px,
                close=close_px,
                settle_price=settle_px,
                contracts=contracts,
                oi=oi,
                change_in_oi=chg_oi,
                known_at=_eod_known_at(t_date),
                underlying_price=und_px,
                lot_size=lot,
            )
        )
    return rows


def scan_relative_delivery_spikes(
    history_by_symbol: dict[str, Sequence[CashBhavcopyRow]],
    *,
    lookback_days: int = 20,
    min_delivery_qty_mult: float = 2.0,
    min_accumulation_return_pct: float = 1.0,
    min_churn_spike_pct: float = 5.0,
    max_churn_rel_delivery_ratio: float = 0.75,
) -> list[DeliveryWatchlistEntry]:
    """Classify each symbol's latest EOD session into institutional accumulation or retail churn.

    Requires at least 5 historical sessions (up to ``lookback_days``) prior to the latest session.
    """
    results: list[DeliveryWatchlistEntry] = []
    for sym, series in sorted(history_by_symbol.items()):
        ordered = sorted(series, key=lambda r: r.trade_date)
        if len(ordered) < 2:
            continue
        latest = ordered[-1]
        prior = ordered[max(0, len(ordered) - 1 - lookback_days) : -1]
        if not prior:
            continue

        avg_qty = sum(r.delivery_qty for r in prior) / len(prior)
        avg_pct = sum(r.delivery_pct for r in prior) / len(prior)
        if avg_qty <= 0 or avg_pct <= 0:
            continue

        qty_mult = latest.delivery_qty / avg_qty
        ret = latest.close_return_pct
        intraday_range_pct = (
            (latest.high - latest.prev_close) / latest.prev_close * 100.0
            if latest.prev_close > 0
            else 0.0
        )

        bias = DeliveryBias.NEUTRAL
        rationale = "Within normal 20-day delivery profile"

        if (
            qty_mult >= min_delivery_qty_mult
            and latest.delivery_pct >= avg_pct
            and ret >= min_accumulation_return_pct
        ):
            bias = DeliveryBias.LONG_ACCUMULATION
            rationale = (
                f"Institutional accumulation: Delivery Qty {qty_mult:.2f}x 20d avg "
                f"({latest.delivery_pct:.1f}% vs {avg_pct:.1f}% avg) on +{ret:.2f}% close"
            )
        elif (
            ret >= min_churn_spike_pct or intraday_range_pct >= min_churn_spike_pct * 1.4
        ) and latest.delivery_pct < avg_pct * max_churn_rel_delivery_ratio:
            bias = DeliveryBias.RETAIL_CHURN_FADE
            rationale = (
                f"Speculative retail churn: +{max(ret, intraday_range_pct):.2f}% spike on low "
                f"delivery ({latest.delivery_pct:.1f}% vs {avg_pct:.1f}% 20d avg)"
            )
        elif (
            qty_mult >= min_delivery_qty_mult
            and latest.delivery_pct >= avg_pct
            and ret <= -min_accumulation_return_pct
        ):
            bias = DeliveryBias.DISTRIBUTION_SHORT
            rationale = (
                f"Institutional distribution: Delivery Qty {qty_mult:.2f}x 20d avg "
                f"({latest.delivery_pct:.1f}% vs {avg_pct:.1f}% avg) on {ret:.2f}% selloff"
            )

        if bias is not DeliveryBias.NEUTRAL:
            results.append(
                DeliveryWatchlistEntry(
                    trade_date=latest.trade_date,
                    symbol=sym,
                    bias=bias,
                    delivery_qty=latest.delivery_qty,
                    avg_20d_delivery_qty=round(avg_qty, 2),
                    delivery_qty_multiple=round(qty_mult, 2),
                    delivery_pct=round(latest.delivery_pct, 2),
                    avg_20d_delivery_pct=round(avg_pct, 2),
                    close_return_pct=round(ret, 2),
                    anchor_vwap=round(latest.vwap_approx, 2),
                    rationale=rationale,
                )
            )
    results.sort(key=lambda x: x.delivery_qty_multiple, reverse=True)
    return results
