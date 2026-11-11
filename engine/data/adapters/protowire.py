"""Minimal protobuf wire-format reader (decode only; no ``protobuf`` dependency).

Enough to decode the Upstox V3 market-data messages from an explicit schema table. Wire types:
0 varint, 1 fixed64, 2 length-delimited, 5 fixed32 (3/4 groups are not supported).
"""

from __future__ import annotations

import struct
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any


class ProtoDecodeError(ValueError):
    pass


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoDecodeError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtoDecodeError("varint too long")


def iter_fields(buf: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield ``(field_number, wire_type, raw)``; raw is int for varint/fixed, bytes for len."""
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            val, pos = read_varint(buf, pos)
            yield number, wire, val
        elif wire == 1:
            if pos + 8 > n:
                raise ProtoDecodeError("truncated fixed64")
            yield number, wire, buf[pos : pos + 8]
            pos += 8
        elif wire == 2:
            ln, pos = read_varint(buf, pos)
            if pos + ln > n:
                raise ProtoDecodeError("truncated length-delimited field")
            yield number, wire, buf[pos : pos + ln]
            pos += ln
        elif wire == 5:
            if pos + 4 > n:
                raise ProtoDecodeError("truncated fixed32")
            yield number, wire, buf[pos : pos + 4]
            pos += 4
        else:
            raise ProtoDecodeError(f"unsupported wire type {wire}")


def _signed64(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


@dataclass(frozen=True)
class F:
    """Schema field: name, scalar kind or nested message schema, repeated?"""

    name: str
    kind: str | Mapping[int, F]  # "double","float","int64","int32","bool","string","enum", or msg
    repeated: bool = False
    map_value: Mapping[int, F] | None = None  # for map<string, Message>: value schema


def _scalar(kind: str, wire: int, raw: Any) -> Any:
    if kind == "double":
        return struct.unpack("<d", raw)[0] if wire == 1 else None
    if kind == "float":
        return struct.unpack("<f", raw)[0] if wire == 5 else None
    if kind in ("int64", "int32", "enum"):
        return _signed64(raw) if wire == 0 else None
    if kind == "bool":
        return bool(raw) if wire == 0 else None
    if kind == "string":
        return raw.decode("utf-8", errors="replace") if wire == 2 else None
    raise ProtoDecodeError(f"unknown scalar kind {kind}")


def decode(buf: bytes, schema: Mapping[int, F]) -> dict[str, Any]:
    """Decode ``buf`` into a dict using ``schema``; unknown fields are skipped."""
    out: dict[str, Any] = {}
    for number, wire, raw in iter_fields(buf):
        f = schema.get(number)
        if f is None:
            continue
        if f.map_value is not None:
            entry = dict(iter_entry(raw))
            key = entry.get(1)
            val = entry.get(2)
            if isinstance(key, bytes) and isinstance(val, bytes):
                out.setdefault(f.name, {})[key.decode("utf-8", errors="replace")] = decode(
                    val, f.map_value
                )
            continue
        if isinstance(f.kind, str):
            value: Any = _scalar(f.kind, wire, raw)
        else:
            if wire != 2:
                continue
            value = decode(raw, f.kind)
        if f.repeated:
            out.setdefault(f.name, []).append(value)
        else:
            out[f.name] = value
    return out


def iter_entry(buf: bytes) -> Iterator[tuple[int, Any]]:
    for number, _wire, raw in iter_fields(buf):
        yield number, raw
