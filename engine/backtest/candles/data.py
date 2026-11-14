"""1-minute candles for the candle backtest: bars, a source protocol, an in-memory source for
tests, and the Upstox source with a disk cache under ``data/`` (gitignored).

Upstox (Plus) endpoints, as probed on 13 Nov 2026:

- ``GET /v2/expired-instruments/expiries`` -> 111 Nifty expiries back to 3 Oct 2024;
- ``GET /v2/expired-instruments/option/contract`` -> contracts with ``lot_size``;
- ``GET /v2/expired-instruments/historical-candle/{key}/1minute/{to}/{from}`` -> one call
  returns a contract's whole window (5 weeks = 9,000 bars), ``[ts, o, h, l, c, volume, oi]``;
- ``GET /v3/historical-candle/{key}/minutes/1/{to}/{from}`` -> live contracts and the index
  (one month per call; the index has volume 0).

A request budget (default 400 a minute, 1,500 per 30 minutes) keeps under Upstox's limits;
HTTP 429 backs off (up to 10 times, 5 s doubling to 5 min). The token is passed in and
never printed or written.
"""

from __future__ import annotations

import gzip
import json
import time as _time
import urllib.error
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from engine.cockpit.term_backfill import API, UpstoxOptions
from engine.core.rules import IST


@dataclass(frozen=True, slots=True)
class Bar:
    """One 1-minute candle; ``start`` is the minute's start (IST). The bar is complete, and
    usable for a decision, from ``start + 1 min``."""

    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: float = 0.0

    @property
    def day(self) -> date:
        return self.start.astimezone(IST).date()


@dataclass(frozen=True, slots=True)
class Contract:
    key: str
    expiry: date
    strike: float
    kind: str  # "CE" | "PE"
    lot: int


def parse_candles(raw: Iterable[Sequence[Any]]) -> list[Bar]:
    """Upstox candle rows -> bars, oldest first (Upstox returns newest first)."""
    out = []
    for c in raw:
        out.append(
            Bar(
                datetime.fromisoformat(str(c[0])).astimezone(IST),
                float(c[1]),
                float(c[2]),
                float(c[3]),
                float(c[4]),
                float(c[5] or 0) if len(c) > 5 else 0.0,
                float(c[6] or 0) if len(c) > 6 else 0.0,
            )
        )
    out.sort(key=lambda b: b.start)
    return out


class CandleSource(Protocol):
    underlying: str

    def expiries(self) -> list[date]: ...

    def contracts(self, expiry: date) -> Mapping[tuple[float, str], Contract]: ...

    def option_bars(self, contract: Contract) -> list[Bar]: ...

    def index_bars(self, day: date) -> list[Bar]: ...

    def sessions(self, start: date, end: date) -> list[date]: ...


@dataclass
class MemorySource:
    """Bars held in memory (synthetic tests)."""

    index: dict[date, list[Bar]]
    chains: dict[date, dict[tuple[float, str], Contract]]
    bars: dict[str, list[Bar]]
    underlying: str = "NIFTY"
    fetched: list[str] = field(default_factory=list)

    def expiries(self) -> list[date]:
        return sorted(self.chains)

    def contracts(self, expiry: date) -> Mapping[tuple[float, str], Contract]:
        return self.chains.get(expiry, {})

    def option_bars(self, contract: Contract) -> list[Bar]:
        self.fetched.append(contract.key)
        return self.bars.get(contract.key, [])

    def index_bars(self, day: date) -> list[Bar]:
        return self.index.get(day, [])

    def sessions(self, start: date, end: date) -> list[date]:
        return sorted(d for d in self.index if start <= d <= end and self.index[d])


class RequestBudget:
    """Sliding-window request limits; ``clock``/``sleep`` are injectable for tests."""

    def __init__(
        self,
        limits: Sequence[tuple[int, float]] = ((400, 60.0), (1500, 1800.0)),
        min_gap_s: float = 0.12,
        clock: Callable[[], float] = _time.monotonic,
        sleep: Callable[[float], None] = _time.sleep,
    ) -> None:
        self.limits, self.min_gap = list(limits), min_gap_s
        self.clock, self.sleep = clock, sleep
        self.sent: deque[float] = deque()
        self.count = 0

    def wait(self) -> None:
        while True:
            now = self.clock()
            longest = max((w for _, w in self.limits), default=0.0)
            while self.sent and now - self.sent[0] > longest:
                self.sent.popleft()
            delay = 0.0
            if self.sent:
                delay = max(delay, self.min_gap - (now - self.sent[-1]))
            for n, window in self.limits:
                recent = [t for t in self.sent if now - t < window]
                if len(recent) >= n:
                    delay = max(delay, window - (now - recent[0]) + 0.01)
            if delay <= 0:
                self.sent.append(now)
                self.count += 1
                return
            self.sleep(delay)


def _read_gz(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _write_gz(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f)
    tmp.replace(path)


def _safe(key: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)


class UpstoxCandleSource:
    """Nifty index, expired and live option candles from Upstox, cached on disk.

    ``live`` maps an unexpired expiry to ``{(strike, kind): (instrument_key, lot)}`` from the
    instrument master. Option bars are fetched for ``[expiry - window_days, expiry]`` once per
    contract (only days before ``today``, so the cache never holds a partial session)."""

    def __init__(
        self,
        token: str,
        http: Callable[[str, str], dict[str, Any]],
        cache_dir: Path,
        *,
        today: date,
        live: Mapping[date, Mapping[tuple[float, str], tuple[str, int]]] | None = None,
        underlying: str = "NIFTY",
        index_key: str = "NSE_INDEX|Nifty 50",
        window_days: int = 14,
        budget: RequestBudget | None = None,
        retries: int = 10,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._token = token
        self.underlying = underlying
        self.index_key = index_key
        self.cache = cache_dir
        self.today = today
        self.window_days = window_days
        self.live = dict(live or {})
        self.budget = budget or RequestBudget()
        self.retries = retries
        self.log = log or (lambda _s: None)
        self._raw_http = http
        self._up = UpstoxOptions(token, self._get, {}, index_key=index_key)
        self._contracts: dict[date, dict[tuple[float, str], Contract]] = {}
        self._bars: dict[str, list[Bar]] = {}
        self._months: dict[tuple[int, int], dict[date, list[Bar]]] = {}
        self._expiries: list[date] | None = None
        self.errors: list[str] = []

    # ------------------------------------------------------------------ http
    def _get(self, url: str, token: str) -> dict[str, Any]:
        delay = 5.0
        for attempt in range(self.retries + 1):
            self.budget.wait()
            try:
                return self._raw_http(url, token)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < self.retries:
                    self.log(f"Upstox rate limit; waiting {delay:.0f} s")
                    self.budget.sleep(delay)
                    delay = min(delay * 2, 300.0)
                    continue
                raise
        raise RuntimeError("unreachable")

    # ------------------------------------------------------------- expiries
    def expiries(self) -> list[date]:
        if self._expiries is not None:
            return self._expiries
        path = self.cache / "expiries" / f"{_safe(self.index_key)}.json"
        expired: list[date] | None = None
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("fetched") == self.today.isoformat():
                expired = [date.fromisoformat(x) for x in doc["expiries"]]
        if expired is None:
            expired = self._up.expired_expiries()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "fetched": self.today.isoformat(),
                        "expiries": [e.isoformat() for e in expired],
                    }
                ),
                encoding="utf-8",
            )
        self._expiries = sorted(set(expired) | set(self.live))
        return self._expiries

    def contracts(self, expiry: date) -> Mapping[tuple[float, str], Contract]:
        if expiry in self._contracts:
            return self._contracts[expiry]
        if expiry in self.live:
            got = {
                k: Contract(key, expiry, k[0], k[1], lot)
                for k, (key, lot) in self.live[expiry].items()
            }
        else:
            path = self.cache / "contracts" / f"{_safe(self.index_key)}_{expiry.isoformat()}.json"
            if path.exists():
                rows = json.loads(path.read_text(encoding="utf-8"))
            else:
                key = urllib.parse.quote(self.index_key, safe="")
                doc = self._get(
                    f"{API}/v2/expired-instruments/option/contract?instrument_key={key}"
                    f"&expiry_date={expiry.isoformat()}",
                    self._token,
                )
                rows = [
                    {
                        "key": str(c["instrument_key"]),
                        "strike": float(c["strike_price"]),
                        "kind": str(c["instrument_type"]),
                        "lot": int(c.get("lot_size") or c.get("minimum_lot") or 0),
                    }
                    for c in doc.get("data") or []
                ]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(rows), encoding="utf-8")
            got = {
                (float(r["strike"]), str(r["kind"])): Contract(
                    str(r["key"]), expiry, float(r["strike"]), str(r["kind"]), int(r["lot"])
                )
                for r in rows
                if r["kind"] in ("CE", "PE")
            }
        self._contracts[expiry] = got
        return got

    # --------------------------------------------------------------- options
    def option_bars(self, contract: Contract) -> list[Bar]:
        if contract.key in self._bars:
            return self._bars[contract.key]
        last_done = self.today - timedelta(days=1)
        frm = contract.expiry - timedelta(days=self.window_days)
        to = min(contract.expiry, last_done)
        path = (
            self.cache / "options" / contract.expiry.isoformat() / f"{_safe(contract.key)}.json.gz"
        )
        raw: list[Any] | None = None
        if path.exists():
            doc = _read_gz(path)
            if date.fromisoformat(doc["to"]) >= to and date.fromisoformat(doc["from"]) <= frm:
                raw = doc["candles"]
        if raw is None and frm <= to:
            k = urllib.parse.quote(contract.key, safe="")
            if contract.expiry in self.live:
                url = f"{API}/v3/historical-candle/{k}/minutes/1/{to.isoformat()}/{frm.isoformat()}"
            else:
                url = (
                    f"{API}/v2/expired-instruments/historical-candle/{k}/1minute/"
                    f"{to.isoformat()}/{frm.isoformat()}"
                )
            try:
                doc = self._get(url, self._token)
                raw = list((doc.get("data") or {}).get("candles") or [])
                _write_gz(path, {"from": frm.isoformat(), "to": to.isoformat(), "candles": raw})
            except Exception as exc:  # one contract must not stop a run; counted in the report
                self.errors.append(f"{contract.key}: {type(exc).__name__}")
                raw = []
        bars = parse_candles(raw or [])
        self._bars[contract.key] = bars
        return bars

    # ----------------------------------------------------------------- index
    def _month(self, y: int, m: int) -> dict[date, list[Bar]]:
        if (y, m) in self._months:
            return self._months[(y, m)]
        first = date(y, m, 1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        last = min(nxt - timedelta(days=1), self.today - timedelta(days=1))
        out: dict[date, list[Bar]] = {}
        if first <= last:
            path = self.cache / "index" / _safe(self.index_key) / f"{y:04d}-{m:02d}.json.gz"
            raw: list[Any] | None = None
            if path.exists():
                doc = _read_gz(path)
                if date.fromisoformat(doc["to"]) >= last:
                    raw = doc["candles"]
            if raw is None:
                k = urllib.parse.quote(self.index_key, safe="")
                doc = self._get(
                    f"{API}/v3/historical-candle/{k}/minutes/1/{last.isoformat()}/"
                    f"{first.isoformat()}",
                    self._token,
                )
                raw = list((doc.get("data") or {}).get("candles") or [])
                _write_gz(path, {"from": first.isoformat(), "to": last.isoformat(), "candles": raw})
            for b in parse_candles(raw):
                out.setdefault(b.day, []).append(b)
        self._months[(y, m)] = out
        return out

    def index_bars(self, day: date) -> list[Bar]:
        return self._month(day.year, day.month).get(day, [])

    def sessions(self, start: date, end: date) -> list[date]:
        days: list[date] = []
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            days += [d for d in self._month(y, m) if start <= d <= end]
            y, m = y + (m == 12), m % 12 + 1
        return sorted(days)


__all__ = [
    "Bar",
    "CandleSource",
    "Contract",
    "MemorySource",
    "RequestBudget",
    "UpstoxCandleSource",
    "parse_candles",
]
