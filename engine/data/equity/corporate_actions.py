"""NSE corporate actions (bonus, split, consolidation, rights, dividend, demerger) for the panel.

NSE's bhavcopy previous close is **not** adjusted on an ex-date: Infosys's 1:1 bonus on
15 Jun 2015 shows PREVCLOSE 1975.05 and CLOSE 990.45 (checked in the downloaded file,
14 Nov 2026). The adjustment therefore comes from NSE's corporate-actions list:

``https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date=DD-MM-YYYY
&to_date=DD-MM-YYYY`` — the endpoint NSE's own corporate-actions page calls (plain GET, no
cookies, checked 14 Nov 2026 for June 2015: 90 actions, Infosys "Dividend - Rs 29.50/- Per
Share And Bonus 1:1" ex 15-Jun-2015). Undocumented, so it may change; downloads are kept raw
under ``data/raw/reference/corporate_actions/<YYYY-MM>.json`` and parsed on read.

Point in time: actions are announced well before the ex-date, so applying a factor on its
ex-date uses nothing unknown at that day's close.
"""

from __future__ import annotations

import hashlib
import json
import re
import time as _time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from engine.core.net import http_get_bytes

NSE_API = "https://www.nseindia.com/api"


@dataclass(frozen=True, slots=True)
class CorpAction:
    symbol: str
    series: str
    isin: str
    ex_date: date
    subject: str
    face_value: float


_RS = r"R[se]\.?\s*([\d]+(?:\.\d+)?)"
_DIVIDEND = re.compile(r"Dividend\s*-?\s*" + _RS, re.I)
_BONUS = re.compile(r"Bonus\s*-?\s*(\d+)\s*:\s*(\d+)", re.I)  # also "Bonus- 1:2"
# "From" is optional ("Face Value Split Rs 10 To Rs 1", Granules 2015) and NSE abbreviates
# ("Fv Splt Frm Rs 10 To Re 1", JSW Steel 2017).
_SPLIT = re.compile(
    r"(?:Split|Splt|Sub-?\s*Division|Consolidation).*?(?:Fro?m\s*)?" + _RS + r".*?To\s*" + _RS,
    re.I,
)
# "Rights 1:2 @ Premium Rs 40", "@Prem Rs 40", "@ Prm Rs 102/-", "Rights Issue 4:17@ Premium",
# "Rights : 24:10 At Par", "@ Premium Of Rs 2.50", "@ Premium 91".
_RIGHTS = re.compile(
    r"Rights(?:\s*Issue)?\s*[:\-]?\s*(\d+)\s*:\s*(\d+)\s*(?:@|At)\s*"
    r"(?:(?:Premium|Prem|Prm)\s*(?:Of\s*)?(?:R[se]\.?\s*)?([\d]+(?:\.\d+)?)|(Par))",
    re.I,
)
_DEMERGER = re.compile(r"Demerger|Scheme of Arrangement", re.I)


def parse_subject(subject: str) -> list[tuple[str, Any]]:
    """Actions in a subject line, in order: ("dividend", ₹ per share, summed), ("bonus" or
    "split", price factor), ("rights", (new, held, premium ₹)), ("demerger", None).
    A bonus a:b gives a new shares for every b held, so the price factor is b / (a + b)."""
    found: list[tuple[int, tuple[str, Any]]] = []
    divs = list(_DIVIDEND.finditer(subject))
    if divs:
        found.append((divs[0].start(), ("dividend", sum(float(m.group(1)) for m in divs))))
    for m in _BONUS.finditer(subject):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            found.append((m.start(), ("bonus", b / (a + b))))
    for m in _SPLIT.finditer(subject):
        old, new = float(m.group(1)), float(m.group(2))
        if old > 0 and new > 0:
            found.append((m.start(), ("split", new / old)))
    for m in _RIGHTS.finditer(subject):
        premium = 0.0 if m.group(4) else float(m.group(3))
        found.append((m.start(), ("rights", (int(m.group(1)), int(m.group(2)), premium))))
    m2 = _DEMERGER.search(subject)
    if m2:
        found.append((m2.start(), ("demerger", None)))
    return [item for _, item in sorted(found, key=lambda x: x[0])]


def rights_factor(
    new: int, held: int, *, premium: float, face_value: float, cum_price: float
) -> float | None:
    """Price factor from the theoretical ex-rights price: (held·P + new·S) / ((held+new)·P),
    S = face value + premium. Rights priced at or above the market change nothing (1.0)."""
    if cum_price <= 0 or new <= 0 or held <= 0:
        return None
    sub = face_value + premium
    terp = (held * cum_price + new * sub) / (held + new)
    return min(1.0, terp / cum_price)


def _day(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip().title(), "%d-%b-%Y").date()
    except ValueError:
        return None


def parse_corporate_actions(text: str) -> list[CorpAction]:
    payload = json.loads(text)
    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    out: list[CorpAction] = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        ex = _day(r.get("exDate"))
        sym = str(r.get("symbol") or "").strip()
        if ex is None or not sym:
            continue
        try:
            fv = float(r.get("faceVal") or 0)
        except ValueError:
            fv = 0.0
        out.append(
            CorpAction(
                symbol=sym,
                series=str(r.get("series") or "").strip(),
                isin=str(r.get("isin") or "").strip(),
                ex_date=ex,
                subject=str(r.get("subject") or "").strip(),
                face_value=fv,
            )
        )
    return out


def corporate_actions_url(start: date, end: date) -> str:
    q = urllib.parse.urlencode(
        {
            "index": "equities",
            "from_date": start.strftime("%d-%m-%Y"),
            "to_date": end.strftime("%d-%m-%Y"),
        }
    )
    return f"{NSE_API}/corporates-corporateActions?{q}"


def _months(start: date, end: date) -> list[tuple[date, date]]:
    out = []
    d = date(start.year, start.month, 1)
    while d <= end:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        out.append((d, nxt - timedelta(days=1)))
        d = nxt
    return out


def fetch_corporate_actions(
    raw_dir: Path,
    start: date,
    end: date,
    *,
    today: date,
    fetcher: Callable[[str], bytes] = http_get_bytes,
    sleep: Callable[[float], None] = _time.sleep,
    pace_s: float = 1.0,
) -> dict[str, str]:
    """One JSON per month. Past months are fetched once; months that reach today or later
    (future ex-dates keep being announced) are always refreshed. Returns month → status."""
    out_dir = raw_dir / "reference" / "corporate_actions"
    out_dir.mkdir(parents=True, exist_ok=True)
    status: dict[str, str] = {}
    for m0, m1 in _months(start, end):
        key = m0.strftime("%Y-%m")
        path = out_dir / f"{key}.json"
        if path.exists() and m1 < today - timedelta(days=7):
            status[key] = "cached"
            continue
        url = corporate_actions_url(m0, m1)
        try:
            body = fetcher(url)
            parse_corporate_actions(body.decode("utf-8"))  # must be valid JSON
        except (OSError, ValueError) as exc:
            status[key] = f"error {type(exc).__name__}"
            sleep(pace_s)
            continue
        path.write_bytes(body)
        meta = {
            "url": url,
            "fetched_at": datetime.now().astimezone().isoformat(),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2), "utf-8")
        status[key] = "ok"
        sleep(pace_s)
    return status


def load_corporate_actions(raw_dir: Path) -> list[CorpAction]:
    out: list[CorpAction] = []
    for p in sorted((raw_dir / "reference" / "corporate_actions").glob("????-??.json")):
        out.extend(parse_corporate_actions(p.read_text("utf-8")))
    return out
