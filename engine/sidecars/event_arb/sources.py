"""Event inputs: the curated events file and exchange announcement candidates.

* ``load_events`` — ``config/event_arb/events.yaml``: events with terms entered from filings.
  ``UNKNOWN`` (or blank) means not yet published; it is never filled in by guessing.
* ``parse_nse_rss`` / ``classify`` — NSE's official "Latest Announcements" RSS feed, filtered
  to buybacks, open offers, rights, OFS and demergers. Candidates are appended to a local
  JSONL store, so the 60-day history builds up from the first day the scan runs (the feed
  itself holds about one day).
"""

from __future__ import annotations

import json
import re
import types
import typing
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from engine.sidecars.event_arb.models import (
    TERMS_FOR,
    Event,
    EventType,
    Filing,
    PriceSnapshot,
)

IST = ZoneInfo("Asia/Kolkata")
NSE_ANNOUNCEMENTS_RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
USER_AGENT = "quant-engine/0.1 (personal research; low frequency)"


class EventFileError(ValueError):
    pass


# ---------------------------------------------------------------- events.yaml


def _is_unknown(v: object) -> bool:
    return v is None or (isinstance(v, str) and v.strip().upper() in {"", "UNKNOWN"})


def _coerce(value: Any, hint: Any, where: str) -> Any:
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin in (typing.Union, types.UnionType):
        if _is_unknown(value) and type(None) in args:
            return None
        inner = [a for a in args if a is not type(None)]
        return _coerce(value, inner[0], where)
    if origin is tuple:
        if _is_unknown(value):
            return ()
        if not isinstance(value, list | tuple):
            raise EventFileError(f"{where}: expected a list")
        return tuple(_coerce(v, args[0], where) for v in value)
    if hint is date:
        if isinstance(value, datetime):
            raise EventFileError(f"{where}: expected a date, got a datetime")
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint(str(value).strip().lower())
    if hint is bool:
        if not isinstance(value, bool):
            raise EventFileError(f"{where}: expected true/false")
        return value
    if hint is int:
        if isinstance(value, float) and not value.is_integer():
            raise EventFileError(f"{where}: expected an integer")
        if isinstance(value, str):
            f = float(value.replace(",", ""))
            if not f.is_integer():
                raise EventFileError(f"{where}: expected an integer, got {value!r}")
            return int(f)
        return int(value)
    if hint is float:
        return float(str(value).replace(",", "")) if isinstance(value, str) else float(value)
    if hint is str:
        return "" if value is None else str(value)
    return value


def _build(cls: type, data: Mapping[str, Any], where: str) -> Any:
    hints = typing.get_type_hints(cls)
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise EventFileError(f"{where}: unknown field(s) {sorted(unknown)} for {cls.__name__}")
    kwargs = {}
    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = _coerce(data[f.name], hints[f.name], f"{where}.{f.name}")
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise EventFileError(f"{where}: {exc}") from exc


def event_from_mapping(rec: Mapping[str, Any], where: str = "event") -> Event:
    try:
        etype = EventType(str(rec["type"]))
    except (KeyError, ValueError) as exc:
        raise EventFileError(f"{where}: bad or missing type") from exc
    filings = tuple(_build(Filing, f, f"{where}.filings") for f in rec.get("filings") or [])
    if not filings or not all(f.url.startswith(("http://", "https://")) for f in filings):
        raise EventFileError(f"{where}: at least one filing with an http(s) url is required")
    terms = _build(TERMS_FOR[etype], rec.get("terms") or {}, f"{where}.terms")
    price = _build(PriceSnapshot, rec["price"], f"{where}.price") if rec.get("price") else None
    base = {k: v for k, v in rec.items() if k not in {"type", "filings", "terms", "price"}}
    hints = typing.get_type_hints(Event)
    kwargs = {k: _coerce(v, hints[k], f"{where}.{k}") for k, v in base.items() if k in hints}
    extra = set(base) - set(hints)
    if extra:
        raise EventFileError(f"{where}: unknown field(s) {sorted(extra)}")
    try:
        return Event(type=etype, filings=filings, terms=terms, price=price, **kwargs)
    except TypeError as exc:
        raise EventFileError(f"{where}: {exc}") from exc


def load_events(path: Path) -> list[Event]:
    if not path.is_file():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records = doc.get("events") or []
    events = [event_from_mapping(r, f"{path.name}[{i}]") for i, r in enumerate(records)]
    ids = [e.id for e in events]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise EventFileError(f"{path.name}: duplicate event id(s) {sorted(dupes)}")
    return events


# ---------------------------------------------------------------- announcements feed


@dataclass(frozen=True)
class Announcement:
    company: str
    url: str
    text: str
    subject: str
    published: str  # ISO datetime, IST
    kind: str  # EventType value
    note: str = ""


_RULES: tuple[tuple[re.Pattern[str], EventType | None, str], ...] = (
    (
        re.compile(r"buy[\s-]?back.*open market|open market.*buy[\s-]?back", re.I | re.S),
        EventType.OPEN_MARKET_BUYBACK,
        "",
    ),
    (
        re.compile(r"buy[\s-]?back.*tender|tender.*buy[\s-]?back", re.I | re.S),
        EventType.TENDER_BUYBACK,
        "",
    ),
    (
        re.compile(r"buy[\s-]?back", re.I),
        EventType.TENDER_BUYBACK,
        "route not stated — confirm tender vs open market",
    ),
    (
        re.compile(r"open offer|detailed public statement|offer opening public announcement", re.I),
        EventType.OPEN_OFFER,
        "",
    ),
    (re.compile(r"rights? issue|rights entitlement", re.I), EventType.RIGHTS, ""),
    (re.compile(r"offer for sale|\bOFS\b", re.I), EventType.OFS, ""),
    (re.compile(r"de-?merger", re.I), EventType.DEMERGER, ""),
)


def classify(text: str, subject: str = "") -> tuple[EventType | None, str]:
    blob = f"{text} {subject}"
    for pattern, kind, note in _RULES:
        if pattern.search(blob):
            return kind, note
    return None, ""


def parse_nse_rss(xml_text: str) -> list[Announcement]:
    """Relevant items from the NSE announcements RSS (others are dropped)."""
    root = ET.fromstring(xml_text)
    out: list[Announcement] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        text, _, subject = desc.partition("|SUBJECT:")
        kind, note = classify(text, subject)
        if kind is None:
            continue
        raw_date = (item.findtext("pubDate") or "").strip()
        try:
            published = datetime.strptime(raw_date, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        except ValueError:
            continue
        out.append(
            Announcement(
                title, link, text.strip(), subject.strip(), published.isoformat(), kind.value, note
            )
        )
    return out


Fetcher = Callable[[str], str]


def http_get(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body: bytes = resp.read()
    return body.decode("utf-8", errors="replace")


class CandidateStore:
    """Append-only JSONL of classified announcements, de-duplicated by URL."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[Announcement]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(Announcement(**json.loads(line)))
        return out

    def add(self, items: Iterable[Announcement]) -> list[Announcement]:
        seen = {a.url for a in self.load()}
        new = []
        for a in items:
            if a.url not in seen:
                seen.add(a.url)
                new.append(a)
        if new:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                for a in new:
                    fh.write(json.dumps(asdict(a), ensure_ascii=False) + "\n")
        return new

    def recent(self, as_of: date, days: int = 60) -> list[Announcement]:
        """Candidates published in (as_of − days, as_of]; point-in-time (nothing after as_of)."""
        start = as_of - timedelta(days=days)
        items = [
            a for a in self.load() if start < datetime.fromisoformat(a.published).date() <= as_of
        ]
        return sorted(items, key=lambda a: a.published, reverse=True)


def to_jsonable(obj: Any) -> Any:  # for debugging / state files
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, list | tuple):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj
