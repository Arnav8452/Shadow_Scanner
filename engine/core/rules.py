"""Effective-dated regulatory / fee / contract rule tables (config/rules/).

Every row carries ``value, unit, effective_from, effective_to, source_url, verified_on,
status, notes``. Any other column is a *key* column (e.g. ``segment``, ``underlying``).

Semantics
---------
* ``effective_from`` is inclusive, ``effective_to`` is **exclusive** (blank = still in force),
  so consecutive rows chain as ``[a, b) [b, c)``.
* ``status`` is ``primary`` (exchange/SEBI/government document read), ``secondary``
  (broker or other reputable page read) or ``UNVERIFIED``. Verified rows must cite
  ``source_url`` and ``verified_on``.
* Lookups refuse ``UNVERIFIED`` rows unless the caller opts in with
  ``allow_unverified=True`` — strategy and cost code must not silently use unchecked numbers.
* ``lot_sizes`` is keyed by **contract expiry date**, not trade date.
"""

from __future__ import annotations

import csv
import itertools
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

IST = ZoneInfo("Asia/Kolkata")

STANDARD_COLUMNS: tuple[str, ...] = (
    "value",
    "unit",
    "effective_from",
    "effective_to",
    "source_url",
    "verified_on",
    "status",
    "notes",
)


class RuleError(Exception):
    """Base error for rule tables."""


class RuleValidationError(RuleError):
    """A rule table file is malformed or internally inconsistent."""


class RuleNotFoundError(RuleError, LookupError):
    """No row is in force for the given keys and date."""


class UnverifiedRuleError(RuleError):
    """The only row in force is UNVERIFIED and the caller did not allow that."""


class Status(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class RuleRow:
    keys: Mapping[str, str]
    value: str
    unit: str
    effective_from: date
    effective_to: date | None
    source_url: str
    verified_on: date | None
    status: Status
    notes: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status is not Status.UNVERIFIED

    def in_force(self, on: date) -> bool:
        return self.effective_from <= on and (self.effective_to is None or on < self.effective_to)

    def as_float(self) -> float:
        return float(self.value)

    def as_int(self) -> int:
        f = float(self.value)
        if not f.is_integer():
            raise RuleError(f"value {self.value!r} is not an integer")
        return int(f)


def to_ist_date(as_of: date | datetime) -> date:
    """Normalise an ``as_of`` to an IST calendar date. Naive datetimes are rejected."""
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None:
            raise ValueError("as_of datetime must be timezone-aware")
        return as_of.astimezone(IST).date()
    return as_of


def _parse_date(raw: object, *, field_name: str, where: str) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        raise RuleValidationError(f"{where}: {field_name} must be a date, not a datetime")
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise RuleValidationError(f"{where}: bad {field_name} {text!r} (want YYYY-MM-DD)") from exc


def _make_row(rec: Mapping[str, Any], key_columns: tuple[str, ...], where: str) -> RuleRow:
    status_raw = str(rec.get("status") or "").strip()
    try:
        status = Status(status_raw)
    except ValueError as exc:
        allowed = ", ".join(s.value for s in Status)
        raise RuleValidationError(f"{where}: status {status_raw!r} not in {{{allowed}}}") from exc

    eff_from = _parse_date(rec.get("effective_from"), field_name="effective_from", where=where)
    if eff_from is None:
        raise RuleValidationError(f"{where}: effective_from is required")
    eff_to = _parse_date(rec.get("effective_to"), field_name="effective_to", where=where)
    if eff_to is not None and eff_to <= eff_from:
        raise RuleValidationError(f"{where}: effective_to {eff_to} <= effective_from {eff_from}")
    verified_on = _parse_date(rec.get("verified_on"), field_name="verified_on", where=where)
    source_url = str(rec.get("source_url") or "").strip()

    if status is not Status.UNVERIFIED:
        if not source_url.startswith(("http://", "https://")):
            raise RuleValidationError(f"{where}: {status} row needs an http(s) source_url")
        if verified_on is None:
            raise RuleValidationError(f"{where}: {status} row needs verified_on")

    keys = {k: str(rec.get(k) or "").strip() for k in key_columns}
    missing = [k for k, v in keys.items() if not v]
    if missing:
        raise RuleValidationError(f"{where}: empty key column(s) {missing}")

    params = rec.get("params") or {}
    if not isinstance(params, Mapping):
        raise RuleValidationError(f"{where}: params must be a mapping")

    return RuleRow(
        keys=keys,
        value=str(rec.get("value") if rec.get("value") is not None else "").strip(),
        unit=str(rec.get("unit") or "").strip(),
        effective_from=eff_from,
        effective_to=eff_to,
        source_url=source_url,
        verified_on=verified_on,
        status=status,
        notes=str(rec.get("notes") or "").strip(),
        params=dict(params),
    )


def _overlaps(a: RuleRow, b: RuleRow) -> bool:
    a_end = a.effective_to or date.max
    b_end = b.effective_to or date.max
    return a.effective_from < b_end and b.effective_from < a_end


@dataclass(frozen=True)
class RuleTable:
    name: str
    key_columns: tuple[str, ...]
    rows: tuple[RuleRow, ...]

    def __post_init__(self) -> None:
        groups: dict[tuple[str, ...], list[RuleRow]] = {}
        for row in self.rows:
            groups.setdefault(self._key_tuple(row.keys), []).append(row)
        for key, group in groups.items():
            ordered = sorted(group, key=lambda r: r.effective_from)
            # Sorted by start, any overlap implies an overlap between neighbours.
            for prev, nxt in itertools.pairwise(ordered):
                if _overlaps(prev, nxt):
                    where = dict(zip(self.key_columns, key, strict=True))
                    raise RuleValidationError(
                        f"{self.name}: overlapping rows for {where}: "
                        f"[{prev.effective_from}, {prev.effective_to}) and "
                        f"[{nxt.effective_from}, {nxt.effective_to})"
                    )

    def _key_tuple(self, keys: Mapping[str, str]) -> tuple[str, ...]:
        unknown = set(keys) - set(self.key_columns)
        if unknown:
            raise KeyError(
                f"{self.name}: unknown key column(s) {sorted(unknown)}; "
                f"expected {list(self.key_columns)}"
            )
        missing = [k for k in self.key_columns if k not in keys]
        if missing:
            raise KeyError(f"{self.name}: missing key column(s) {missing}")
        return tuple(keys[k] for k in self.key_columns)

    def history(self, **keys: str) -> list[RuleRow]:
        """All rows for these keys, oldest first."""
        want = self._key_tuple(keys)
        rows = [r for r in self.rows if self._key_tuple(r.keys) == want]
        return sorted(rows, key=lambda r: r.effective_from)

    def lookup(
        self, as_of: date | datetime, *, allow_unverified: bool = False, **keys: str
    ) -> RuleRow:
        """The single row in force on ``as_of`` (IST date) for the exact ``keys``."""
        on = to_ist_date(as_of)
        hits = [r for r in self.history(**keys) if r.in_force(on)]
        if not hits:
            raise RuleNotFoundError(f"{self.name}: no rule for {keys} on {on}")
        row = hits[0]  # overlaps are rejected at load, so at most one
        if not row.verified and not allow_unverified:
            raise UnverifiedRuleError(
                f"{self.name}: rule for {keys} on {on} is UNVERIFIED ({row.notes or 'no notes'}); "
                "verify it against a primary source or pass allow_unverified=True"
            )
        return row

    def lookup_all(
        self, as_of: date | datetime, *, allow_unverified: bool = False, **partial_keys: str
    ) -> list[RuleRow]:
        """Every row in force on ``as_of`` whose keys match ``partial_keys`` (e.g. all
        charge components for one exchange+segment). Raises if none, or if any is UNVERIFIED
        and not allowed — a partial sum must never silently drop a component."""
        on = to_ist_date(as_of)
        unknown = set(partial_keys) - set(self.key_columns)
        if unknown:
            raise KeyError(f"{self.name}: unknown key column(s) {sorted(unknown)}")
        hits = [
            r
            for r in self.rows
            if r.in_force(on) and all(r.keys[k] == v for k, v in partial_keys.items())
        ]
        if not hits:
            raise RuleNotFoundError(f"{self.name}: no rules for {partial_keys} on {on}")
        bad = [r for r in hits if not r.verified]
        if bad and not allow_unverified:
            raise UnverifiedRuleError(
                f"{self.name}: {len(bad)} UNVERIFIED row(s) for {partial_keys} on {on}: "
                + "; ".join(str(dict(r.keys)) for r in bad)
            )
        return sorted(hits, key=lambda r: tuple(r.keys.values()))

    def unverified(self) -> list[RuleRow]:
        return [r for r in self.rows if not r.verified]

    def __iter__(self) -> Iterator[RuleRow]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)


def _table_from_records(
    name: str, records: Iterable[Mapping[str, Any]], columns: Iterable[str], source: Path
) -> RuleTable:
    cols = [c.strip() for c in columns]
    missing = [c for c in STANDARD_COLUMNS if c not in cols]
    if missing:
        raise RuleValidationError(f"{source.name}: missing column(s) {missing}")
    key_columns = tuple(c for c in cols if c not in STANDARD_COLUMNS and c != "params")
    rows = tuple(
        _make_row(rec, key_columns, f"{source.name}:{i}") for i, rec in enumerate(records, start=2)
    )
    return RuleTable(name=name, key_columns=key_columns, rows=rows)


def load_csv_table(path: Path) -> RuleTable:
    with path.open(newline="", encoding="utf-8-sig") as fh:  # tolerate Excel BOM
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise RuleValidationError(f"{path.name}: empty file (header required)")
        columns = list(reader.fieldnames)
        records = list(reader)
    return _table_from_records(path.stem, records, columns, path)


def load_yaml_table(path: Path) -> RuleTable:
    """YAML rule file: ``rules: [{name, params, <standard columns>}]``; keyed by ``name``."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records = doc.get("rules") if isinstance(doc, dict) else None
    if not isinstance(records, list):
        raise RuleValidationError(f"{path.name}: top-level 'rules:' list required")
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise RuleValidationError(f"{path.name}: rule #{i} is not a mapping")
        rec.setdefault("value", "")
        rec.setdefault("unit", "")
    columns = ["name", *STANDARD_COLUMNS]
    return _table_from_records(path.stem, records, columns, path)


class RuleBook:
    """All rule tables in a directory, by file stem (``stt``, ``lot_sizes``...)."""

    def __init__(self, tables: Mapping[str, RuleTable]) -> None:
        self._tables = dict(tables)

    @classmethod
    def load(cls, directory: Path) -> RuleBook:
        if not directory.is_dir():
            raise RuleValidationError(f"rules directory not found: {directory}")
        tables: dict[str, RuleTable] = {}
        for path in sorted(directory.iterdir()):
            if path.suffix == ".csv":
                tables[path.stem] = load_csv_table(path)
            elif path.suffix in {".yaml", ".yml"}:
                tables[path.stem] = load_yaml_table(path)
        return cls(tables)

    def __getitem__(self, name: str) -> RuleTable:
        try:
            return self._tables[name]
        except KeyError:
            raise KeyError(f"no rule table {name!r}; have {sorted(self._tables)}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._tables

    @property
    def names(self) -> list[str]:
        return sorted(self._tables)

    def lookup(
        self, table: str, as_of: date | datetime, *, allow_unverified: bool = False, **keys: str
    ) -> RuleRow:
        return self[table].lookup(as_of, allow_unverified=allow_unverified, **keys)

    def unverified_report(self) -> dict[str, list[RuleRow]]:
        return {n: t.unverified() for n, t in self._tables.items() if t.unverified()}
