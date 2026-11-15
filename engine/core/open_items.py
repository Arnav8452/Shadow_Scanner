"""Open-items checklist: everything the docs and rules say is unfinished, in one place.

Sources, scanned fresh each run so the list can't drift from them:
- ``config/rules/*.csv``: every row whose status is ``UNVERIFIED``;
- ``config/settings.yaml``: every line marked ``PROPOSED`` (a value awaiting the owner's OK);
- ``docs/DECISIONS.md``: every "Open" block (``**Open:**``, ``- **Open (owner):**``,
  ``**Open questions for the owner**`` …) with its bullets, under its section heading.

Each item gets a short stable id (a hash of its source and text). An item is closed by adding
its id to ``config/open_items_resolved.csv`` (``id,resolved_on,how``) once it is done or
answered; the old text stays in DECISIONS as history.

Owners: ``owner`` (a decision or input only the owner can give: approvals, contract notes),
``claude`` (verification and engine work). Argon's research items stay in the private
``argon/`` inbox and are never written here.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

_OPEN = re.compile(
    r"^(?:- )?\*\*Open(?P<owner> \(owner\))?(?: questions(?: for the owner)?)?"
    r"[:.]?\*\*[:.]?\s*(?P<text>.*)$"
)
_BULLET = re.compile(r"^(?P<indent>\s*)(?:-|\d+\.)\s+(?P<text>.*)$")
_OWNER_WORDS = re.compile(
    r"\bowner\b|\byour (?:ok|call|approval)\b|\bapprov|\bcontract notes?\b|\bshare\b", re.I
)
# Rule files whose unverified values only a real contract note can settle.
_NEEDS_CONTRACT_NOTE = {"brokerage.csv", "exchange_charges.csv", "stt.csv"}


@dataclass(frozen=True)
class OpenItem:
    id: str
    owner: str  # owner | claude
    source: str  # file and where in it
    text: str


def _id(source: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha1(f"{source}|{norm}".encode()).hexdigest()[:8]


def _owner(text: str, forced: bool) -> str:
    return "owner" if forced or _OWNER_WORDS.search(text) else "claude"


def scan_decisions(text: str, source: str = "docs/DECISIONS.md") -> list[OpenItem]:
    lines = text.splitlines()
    out: list[OpenItem] = []
    heading = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            heading = line[3:].strip()
        m = _OPEN.match(line.strip())
        if not m:
            i += 1
            continue
        forced = bool(m.group("owner")) or "for the owner" in line
        where = f"{source} › {heading}" if heading else source
        inline = m.group("text").strip()
        items: list[str] = [inline] if inline else []
        # A bulleted header ("- **Open:** …") owns only bullets indented under it; its
        # siblings at the same indent are other points of the section. A bold paragraph
        # header ("**Open:**") owns the list that follows it.
        head_indent = len(line) - len(line.lstrip()) if line.lstrip().startswith("- ") else -1
        base: int | None = None
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.startswith(("## ", "---")):
                break
            if not nxt.strip():
                j += 1
                continue
            b = _BULLET.match(nxt)
            if b:
                indent = len(b.group("indent"))
                if indent <= head_indent:
                    break
                if inline:  # sub-points of an inline item belong to it
                    items[-1] += " " + b.group("text").strip()
                    j += 1
                    continue
                if base is None:
                    base = indent
                if indent <= base:
                    items.append(b.group("text").strip())
                elif items:
                    items[-1] += " " + b.group("text").strip()
            elif nxt.startswith(" ") and items and base is not None:
                items[-1] += " " + nxt.strip()  # wrapped continuation of a bullet
            else:
                break
            j += 1
        for t in items:
            out.append(OpenItem(_id(where, t), _owner(t, forced), where, t))
        i = j if j > i + 1 else i + 1
    return out


def scan_rules(rules_dir: Path) -> list[OpenItem]:
    out: list[OpenItem] = []
    for path in sorted(rules_dir.glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as fh:
            for n, row in enumerate(csv.DictReader(fh), start=2):
                if (row.get("status") or "").strip().upper() != "UNVERIFIED":
                    continue
                key = ", ".join(
                    f"{k}={v}"
                    for k, v in row.items()
                    if k and v and k not in ("source_url", "verified_on", "status", "notes")
                )
                note = (row.get("notes") or "").strip()
                text = f"{key}" + (f" — {note}" if note else "")
                where = f"config/rules/{path.name}:{n}"
                owner = "owner" if path.name in _NEEDS_CONTRACT_NOTE else "claude"
                out.append(OpenItem(_id(where, text), owner, where, text))
    return out


def scan_settings(path: Path) -> list[OpenItem]:
    out: list[OpenItem] = []
    if not path.exists():
        return out
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if "PROPOSED" in line:
            where = f"config/{path.name}:{n}"
            text = line.strip()
            out.append(OpenItem(_id(where, text), "owner", where, text))
    return out


def load_resolved(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8", newline="") as fh:
        return {(r.get("id") or "").strip() for r in csv.DictReader(fh) if r.get("id")}


def collect(root: Path) -> tuple[list[OpenItem], int]:
    """(open items, number already resolved) for a repository root."""
    items = [
        *scan_rules(root / "config" / "rules"),
        *scan_settings(root / "config" / "settings.yaml"),
        *scan_decisions((root / "docs" / "DECISIONS.md").read_text(encoding="utf-8")),
    ]
    done = load_resolved(root / "config" / "open_items_resolved.csv")
    return [x for x in items if x.id not in done], sum(1 for x in items if x.id in done)


def render(items: Sequence[OpenItem], resolved: int, *, generated: str) -> str:
    def block(owner: str, title: str, hint: str) -> Iterable[str]:
        mine = [x for x in items if x.owner == owner]
        yield f"## {title} ({len(mine)})"
        yield ""
        yield hint
        yield ""
        groups: dict[str, list[OpenItem]] = {}
        for x in mine:
            groups.setdefault(x.source.split(":")[0].split(" › ")[0], []).append(x)
        for src, xs in groups.items():
            yield f"### {src} ({len(xs)})"
            yield ""
            for x in xs:
                where = x.source.split(" › ", 1)[1] if " › " in x.source else x.source
                yield f"- `{x.id}` {x.text}  \n  _{where}_"
            yield ""

    lines = [
        "# Open items",
        "",
        f"Generated {generated} by `tasks.py open-items` from `config/rules/`, "
        "`config/settings.yaml` and `docs/DECISIONS.md`. Close an item by adding its id to "
        f"`config/open_items_resolved.csv` (`id,resolved_on,how`). {resolved} resolved so far.",
        "",
        *block("owner", "Needs the owner", "Decisions, approvals or inputs only you can give "
               "(real contract notes settle every brokerage, STT and exchange-charge row)."),
        *block("claude", "Needs Claude", "Verification against primary sources and engine work."),
    ]  # fmt: skip
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "OpenItem",
    "collect",
    "load_resolved",
    "render",
    "scan_decisions",
    "scan_rules",
    "scan_settings",
]
