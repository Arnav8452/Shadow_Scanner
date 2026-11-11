"""Minimal ``.env`` reader and a ``Secret`` wrapper that never prints its value."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path


class Secret:
    """Holds a credential. ``str``/``repr`` are masked; call ``reveal()`` at the point of use."""

    __slots__ = ("_name", "_value")

    def __init__(self, name: str, value: str) -> None:
        self._name = name
        self._value = value

    def reveal(self) -> str:
        return self._value

    @property
    def name(self) -> str:
        return self._name

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return f"Secret({self._name}=****)"

    __str__ = __repr__


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; ``#`` comments, blank lines, optional quotes, ``export``."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


def load_dotenv(path: Path, environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Load ``path`` into ``environ`` (default ``os.environ``) without overriding existing keys."""
    target = os.environ if environ is None else environ
    if not path.is_file():
        return {}
    values = parse_dotenv(path.read_text(encoding="utf-8"))
    for k, v in values.items():
        target.setdefault(k, v)
    return values


def update_dotenv(path: Path, updates: Mapping[str, str]) -> None:
    """Set keys in ``.env`` in place (other lines untouched), atomically. Values are never
    echoed; callers must not log them either."""
    for k, v in updates.items():
        if not k.isidentifier() or "\n" in v or "\r" in v:
            raise ValueError(f"invalid .env entry for {k!r}")
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pending = dict(updates)
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        body = stripped[len("export ") :].lstrip() if stripped.startswith("export ") else stripped
        key = body.partition("=")[0].strip()
        if body and not body.startswith("#") and key in pending:
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(raw)
    out += [f"{k}={v}" for k, v in pending.items()]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def get_secret(name: str, environ: Mapping[str, str] | None = None) -> Secret:
    source = os.environ if environ is None else environ
    return Secret(name, source.get(name, ""))
