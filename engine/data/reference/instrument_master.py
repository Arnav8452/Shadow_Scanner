"""Instrument master: which contracts exist, with lot size, tick size, strike and expiry.

The broker's daily contract file is the source of truth for *what is listed today* (lot sizes
can change mid-life of a contract — see REVIEW §0 item 11). Rule tables remain the source for
regulatory history; :func:`lot_size_mismatches` cross-checks the two.

Snapshots are stored bitemporally: ``data/raw/instruments/<broker>/<YYYY-MM-DD>/<segment>.csv.gz``
plus a ``.meta.json`` holding ``fetched_at`` (when we learned it) and the URL and SHA-256.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Any

from engine.core.net import http_get_bytes
from engine.core.rules import IST, RuleBook, RuleNotFoundError


class Kind(StrEnum):
    INDEX = "IDX"
    EQUITY = "EQ"
    FUTURE = "FUT"
    CALL = "CE"
    PUT = "PE"

    @property
    def is_option(self) -> bool:
        return self in (Kind.CALL, Kind.PUT)


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str  # broker ticker, e.g. NSE:NIFTY26N1725000CE
    broker: str
    exchange: str  # NSE / BSE
    segment: str  # cm / fo
    token: str  # broker token
    exchange_token: str  # exchange scrip code
    underlying: str  # e.g. NIFTY
    kind: Kind
    expiry: date | None
    expiry_ts: datetime | None  # IST
    strike: float | None
    lot_size: int
    tick_size: float
    isin: str
    description: str


class MasterError(ValueError):
    pass


# --------------------------------------------------------------------------- Fyers format

FYERS_MASTER_URL = "https://public.fyers.in/sym_details/{segment}.csv"
# Column order documented at
# https://fyers.in/api-docs/v3/api/market-information/symbol-master/symbol-master-file/
FYERS_COLUMNS = (
    "fytoken",
    "details",
    "exch_instrument_type",
    "lot_size",
    "tick_size",
    "isin",
    "trading_session",
    "last_update",
    "expiry_epoch",
    "ticker",
    "exchange",
    "segment",
    "scrip_code",
    "underlying",
    "underlying_scrip_code",
    "strike",
    "option_type",
    "underlying_fytoken",
    "reserved_str",
    "reserved_int",
    "reserved_float",
)
_FYERS_EXCHANGE = {"10": "NSE", "12": "BSE", "11": "MCX"}
_FYERS_SEGMENT = {"10": "cm", "11": "fo", "12": "cd", "20": "com"}


def _fyers_kind(ticker: str, segment: str, option_type: str) -> Kind | None:
    if option_type in ("CE", "PE"):
        return Kind(option_type)
    if segment == "fo":
        return Kind.FUTURE if ticker.endswith("FUT") else None
    if segment == "cm":
        if ticker.endswith("-INDEX"):
            return Kind.INDEX
        if ticker.endswith("-EQ") or ticker.endswith("-BE"):
            return Kind.EQUITY
    return None


def parse_fyers_master(text: str, *, kinds: set[Kind] | None = None) -> list[Instrument]:
    """Parse a Fyers ``sym_details`` CSV (no header row). Unknown instrument kinds are skipped."""
    out: list[Instrument] = []
    for lineno, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not row or not row[0].strip():
            continue
        if len(row) < 18:
            raise MasterError(f"line {lineno}: expected >= 18 columns, got {len(row)}")
        r = dict(zip(FYERS_COLUMNS, (c.strip() for c in row), strict=False))
        exchange = _FYERS_EXCHANGE.get(r["exchange"])
        segment = _FYERS_SEGMENT.get(r["segment"])
        if exchange is None or segment is None:
            continue
        kind = _fyers_kind(r["ticker"], segment, r["option_type"])
        if kind is None or (kinds is not None and kind not in kinds):
            continue
        expiry_ts: datetime | None = None
        if r["expiry_epoch"]:
            expiry_ts = datetime.fromtimestamp(int(r["expiry_epoch"]), IST)
        strike = float(r["strike"]) if kind.is_option else None
        try:
            lot = int(r["lot_size"])
            tick = float(r["tick_size"])
        except ValueError as exc:
            raise MasterError(f"line {lineno}: bad lot/tick size: {row[:6]}") from exc
        if segment == "fo" and (expiry_ts is None or lot < 1):
            raise MasterError(f"line {lineno}: derivative without expiry/lot: {r['ticker']}")
        if kind.is_option and (strike is None or strike <= 0):
            raise MasterError(f"line {lineno}: option without strike: {r['ticker']}")
        out.append(
            Instrument(
                symbol=r["ticker"],
                broker="fyers",
                exchange=exchange,
                segment=segment,
                token=r["fytoken"],
                exchange_token=r["scrip_code"],
                underlying=r["underlying"],
                kind=kind,
                expiry=expiry_ts.date() if expiry_ts else None,
                expiry_ts=expiry_ts,
                strike=strike,
                lot_size=lot,
                tick_size=tick,
                isin=r["isin"],
                description=r["details"],
            )
        )
    return out


# --------------------------------------------------------------------------- master


class InstrumentMaster:
    def __init__(self, instruments: Iterable[Instrument], *, as_of: date, source: str) -> None:
        self.as_of = as_of
        self.source = source
        self._by_symbol: dict[str, Instrument] = {}
        self._options: dict[tuple[str, date], list[Instrument]] = defaultdict(list)
        self._futures: dict[str, list[Instrument]] = defaultdict(list)
        for ins in instruments:
            if ins.symbol in self._by_symbol:
                raise MasterError(f"duplicate symbol {ins.symbol}")
            self._by_symbol[ins.symbol] = ins
            if ins.kind.is_option and ins.expiry is not None:
                self._options[(ins.underlying, ins.expiry)].append(ins)
            elif ins.kind is Kind.FUTURE:
                self._futures[ins.underlying].append(ins)

    def __len__(self) -> int:
        return len(self._by_symbol)

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._by_symbol

    def get(self, symbol: str) -> Instrument:
        try:
            return self._by_symbol[symbol]
        except KeyError:
            msg = f"{symbol} not in instrument master ({self.source} {self.as_of})"
            raise KeyError(msg) from None

    def all(self) -> list[Instrument]:
        return list(self._by_symbol.values())

    def option_expiries(self, underlying: str, on: date) -> list[date]:
        return sorted({e for (u, e) in self._options if u == underlying and e >= on})

    def options(self, underlying: str, expiry: date) -> list[Instrument]:
        return sorted(
            self._options.get((underlying, expiry), []), key=lambda i: (i.strike or 0, i.kind)
        )

    def strikes(self, underlying: str, expiry: date) -> list[float]:
        return sorted({i.strike for i in self._options.get((underlying, expiry), []) if i.strike})

    def futures(self, underlying: str, on: date) -> list[Instrument]:
        return sorted(
            (f for f in self._futures.get(underlying, []) if f.expiry and f.expiry >= on),
            key=lambda f: f.expiry or date.max,
        )


EXPIRY_ROLES = ("current_weekly", "next_weekly", "current_monthly", "next_monthly")


def classify_expiries(expiries: Sequence[date], roles: Sequence[str]) -> dict[str, date]:
    """Map roles to listed expiries (already filtered to >= today, any order).

    *weekly* = the n-th nearest listed expiry; *monthly* = the last listed expiry of a calendar
    month (so holiday-shifted monthlies are handled by the exchange's own listing).
    """
    unknown = set(roles) - set(EXPIRY_ROLES)
    if unknown:
        raise ValueError(f"unknown expiry roles {sorted(unknown)}; use {EXPIRY_ROLES}")
    exp = sorted(set(expiries))
    if not exp:
        return {}
    by_month: dict[tuple[int, int], date] = {}
    for e in exp:
        by_month[(e.year, e.month)] = max(e, by_month.get((e.year, e.month), e))
    monthlies = sorted(by_month.values())
    out: dict[str, date] = {}
    for role in roles:
        if role == "current_weekly":
            out[role] = exp[0]
        elif role == "next_weekly" and len(exp) > 1:
            out[role] = exp[1]
        elif role == "current_monthly":
            out[role] = monthlies[0]
        elif role == "next_monthly" and len(monthlies) > 1:
            out[role] = monthlies[1]
    return out


@dataclass(frozen=True)
class LotMismatch:
    symbol: str
    master_lot: int
    rule_lot: int | None
    rule_status: str


def lot_size_mismatches(
    master: InstrumentMaster, book: RuleBook, underlyings: Iterable[str]
) -> list[LotMismatch]:
    """Contracts whose master lot size disagrees with (or is missing from) config/rules."""
    out: list[LotMismatch] = []
    wanted = set(underlyings)
    seen: set[tuple[str, str, date, int]] = set()
    for ins in master.all():
        if ins.segment != "fo" or ins.underlying not in wanted or ins.expiry is None:
            continue
        key = (ins.exchange, ins.underlying, ins.expiry, ins.lot_size)
        if key in seen:
            continue
        seen.add(key)
        try:
            row = book.lookup(
                "lot_sizes",
                ins.expiry,
                allow_unverified=True,
                exchange=ins.exchange,
                underlying=ins.underlying,
            )
        except (RuleNotFoundError, KeyError):
            out.append(LotMismatch(ins.symbol, ins.lot_size, None, "missing"))
            continue
        if row.as_int() != ins.lot_size:
            out.append(LotMismatch(ins.symbol, ins.lot_size, row.as_int(), row.status.value))
    return out


# --------------------------------------------------------------------------- snapshots


_SUFFIX = {"fyers": ".csv.gz", "upstox": ".json.gz"}


def snapshot_path(raw_dir: Path, broker: str, day: date, segment: str) -> Path:
    suffix = _SUFFIX.get(broker, ".csv.gz")
    return raw_dir / "instruments" / broker / day.isoformat() / f"{segment}{suffix}"


def fetch_fyers_master(
    segment: str,
    *,
    raw_dir: Path,
    day: date,
    now: datetime,
    fetcher: Callable[[str], bytes] = http_get_bytes,
    refresh: bool = False,
) -> Path:
    """Download today's Fyers contract file once per day and store it with its metadata."""
    if segment not in {"NSE_FO", "NSE_CM", "BSE_FO", "BSE_CM"}:
        raise ValueError(f"unsupported Fyers master segment {segment!r}")
    path = snapshot_path(raw_dir, "fyers", day, segment)
    if path.exists() and not refresh:
        return path
    url = FYERS_MASTER_URL.format(segment=segment)
    body = fetcher(url)
    parsed = parse_fyers_master(body.decode("utf-8", errors="replace"))
    if not parsed:
        raise MasterError(f"{url}: no instruments parsed")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(body))
    os.replace(tmp, path)
    meta = {
        "url": url,
        "fetched_at": now.isoformat(),
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "instruments_parsed": len(parsed),
    }
    path.with_name(path.name + ".meta.json").write_text(json.dumps(meta, indent=2), "utf-8")
    return path


def latest_snapshot(raw_dir: Path, broker: str, segment: str, on_or_before: date) -> Path | None:
    base = raw_dir / "instruments" / broker
    if not base.is_dir():
        return None
    days = sorted(
        (d for d in base.iterdir() if d.is_dir() and d.name <= on_or_before.isoformat()),
        reverse=True,
    )
    suffix = _SUFFIX.get(broker, ".csv.gz")
    for d in days:
        p = d / f"{segment}{suffix}"
        if p.exists():
            return p
    return None


def load_fyers_master(paths: Sequence[Path], *, as_of: date) -> InstrumentMaster:
    instruments: list[Instrument] = []
    for p in paths:
        instruments.extend(parse_fyers_master(gzip.decompress(p.read_bytes()).decode("utf-8")))
    return InstrumentMaster(instruments, as_of=as_of, source="fyers")


# --------------------------------------------------------------------------- Upstox format

UPSTOX_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/{exchange}.json.gz"
# Instrument keys: options/futures "NSE_FO|<exchange token>", indices "NSE_INDEX|Nifty 50",
# equities "NSE_EQ|<ISIN>". Tick sizes in the file are in paise; expiry is epoch ms at 23:59:59.


def parse_upstox_master(
    rows: Iterable[Mapping[str, Any]], *, close_time: time | None = None
) -> list[Instrument]:
    """Parse rows of an Upstox ``<EXCHANGE>.json.gz`` instrument file.

    Index futures/options carry ``underlying_key`` (e.g. ``NSE_INDEX|Nifty 50``) and
    ``underlying_symbol`` (``NIFTY``). Index rows get that same underlying name so spot and chain
    line up. An index that derivatives refer to but the file does not list is added.
    ``close_time`` sets each expiry to that IST time on the expiry date (the file says 23:59:59).
    """
    items = list(rows)
    und_name: dict[str, str] = {}
    und_exchange: dict[str, str] = {}
    for r in items:
        key, sym = r.get("underlying_key"), r.get("underlying_symbol")
        if str(r.get("segment", "")).endswith("_FO") and key and sym:
            und_name.setdefault(str(key), str(sym))
            und_exchange.setdefault(str(key), str(r.get("exchange", "")))
    out: list[Instrument] = []
    seen: set[str] = set()
    for r in items:
        seg = str(r.get("segment", ""))
        itype = str(r.get("instrument_type", ""))
        key = str(r.get("instrument_key", ""))
        if not key or key in seen:
            continue
        kind: Kind | None = None
        if seg in ("NSE_FO", "BSE_FO"):
            kind = {"CE": Kind.CALL, "PE": Kind.PUT, "FUT": Kind.FUTURE}.get(itype)
        elif seg in ("NSE_INDEX", "BSE_INDEX"):
            kind = Kind.INDEX
        elif seg == "NSE_EQ" and itype in ("EQ", "BE"):
            kind = Kind.EQUITY
        if kind is None:
            continue
        expiry_ts: datetime | None = None
        if r.get("expiry"):
            raw_ts = datetime.fromtimestamp(int(r["expiry"]) / 1000, IST)
            expiry_ts = datetime.combine(raw_ts.date(), close_time, IST) if close_time else raw_ts
        strike = float(r.get("strike_price") or 0) if kind.is_option else None
        if kind.is_option and not strike:
            raise MasterError(f"{key}: option without strike")
        if kind in (Kind.CALL, Kind.PUT, Kind.FUTURE) and expiry_ts is None:
            raise MasterError(f"{key}: derivative without expiry")
        if kind is Kind.INDEX:
            underlying = und_name.get(key) or str(r.get("trading_symbol", "")).replace(" ", "")
        else:
            underlying = str(r.get("underlying_symbol") or r.get("trading_symbol") or "")
        seen.add(key)
        out.append(
            Instrument(
                symbol=key,
                broker="upstox",
                exchange=str(r.get("exchange") or seg[:3]),
                segment="fo" if seg.endswith("_FO") else "cm",
                token=key,
                exchange_token=str(r.get("exchange_token", "")),
                underlying=underlying,
                kind=kind,
                expiry=expiry_ts.date() if expiry_ts else None,
                expiry_ts=expiry_ts,
                strike=strike,
                lot_size=int(r.get("lot_size") or 0) if kind is not Kind.INDEX else 0,
                tick_size=float(r.get("tick_size") or 0) / 100.0,
                isin=str(r.get("isin", "")),
                description=str(r.get("trading_symbol") or r.get("name") or key),
            )
        )
    for key, name in und_name.items():
        if key not in seen and "_INDEX|" in key:
            seen.add(key)
            out.append(
                Instrument(
                    symbol=key,
                    broker="upstox",
                    exchange=und_exchange.get(key) or key[:3],
                    segment="cm",
                    token=key,
                    exchange_token="",
                    underlying=name,
                    kind=Kind.INDEX,
                    expiry=None,
                    expiry_ts=None,
                    strike=None,
                    lot_size=0,
                    tick_size=0.0,
                    isin="",
                    description=f"{key} (from derivatives' underlying_key)",
                )
            )
    return out


def fetch_upstox_master(
    exchange: str,
    *,
    raw_dir: Path,
    day: date,
    now: datetime,
    fetcher: Callable[[str], bytes] = http_get_bytes,
    refresh: bool = False,
) -> Path:
    """Download today's Upstox instrument file (public, no login) once per day."""
    if exchange not in {"NSE", "BSE"}:
        raise ValueError(f"unsupported Upstox master exchange {exchange!r}")
    path = snapshot_path(raw_dir, "upstox", day, exchange)
    if path.exists() and not refresh:
        return path
    url = UPSTOX_MASTER_URL.format(exchange=exchange)
    body = fetcher(url)  # already gzip-compressed JSON
    parsed = parse_upstox_master(json.loads(gzip.decompress(body)))
    if not parsed:
        raise MasterError(f"{url}: no instruments parsed")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    meta = {
        "url": url,
        "fetched_at": now.isoformat(),
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "instruments_parsed": len(parsed),
    }
    path.with_name(path.name + ".meta.json").write_text(json.dumps(meta, indent=2), "utf-8")
    return path


def load_upstox_master(
    paths: Sequence[Path], *, as_of: date, close_time: time | None = None
) -> InstrumentMaster:
    instruments: list[Instrument] = []
    for p in paths:
        rows = json.loads(gzip.decompress(p.read_bytes()))
        instruments.extend(parse_upstox_master(rows, close_time=close_time))
    return InstrumentMaster(instruments, as_of=as_of, source="upstox")
