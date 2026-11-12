"""Parse reference "Trade History" PDFs from word positions (M1, SPEC §9.2).

The reports are rendered tables: every column starts at a fixed x on the page, and each trade is a
block with the entry time on top and the exit time below. Other cells of the trade sit anywhere
between (or just outside) those two lines, so text-mode extraction (``pdftotext``) jumbles them.
This parser reads words with coordinates instead, assigns each word to the column whose header
starts at or left of it, and assigns it to the trade whose time block it falls in.

Names and full account IDs are never kept: the only identifier taken from a trade row is the
4-digit account suffix, which is hashed. Day headers with several accounts are summed.

``pdfplumber`` is only needed by :func:`read_pdf_words` (``uv sync --extra mining``); the parser
itself works on plain :class:`Word` lists so tests use synthetic pages.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from engine.trade_mining.parse_report import MinedTradeRow, ParsedTradeReport, anonymize_identifier

_DATE_RE = re.compile(r"^20\d\d-\d\d-\d\d$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_ACCT_LABEL_RE = re.compile(r"^[A-Z]{2}\d{6,}:$")
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# Header words -> column names. "MFE" starts the MFE / MAE column; "NET" is the NET P&L header.
_HEADER_COLUMNS = {
    "TIME": "time",
    "ACCOUNT": "account",
    "INST": "inst",
    "TYPE": "side",
    "QTY": "qty",
    "MARGIN": "margin",
    "MODALITY": "modality",
    "REQUESTED": "requested",
    "ENTRY": "entry",
    "EXIT": "exit",
    "MFE": "mfe_mae",
    "GROSS": "gross",
    "CHARGES": "charges",
    "NET": "net",
    "FLAGS": "flags",
}
_REQUIRED = ("time", "inst", "side", "qty", "entry", "exit", "gross", "net")
_X_SLACK = 4.0  # a value may start a few points left of its header


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    x0: float
    top: float


@dataclass(frozen=True, slots=True)
class ParseIssue:
    page: int
    trade_date: date | None
    message: str


@dataclass(frozen=True, slots=True)
class PdfParseResult:
    report: ParsedTradeReport
    issues: tuple[ParseIssue, ...]
    accounts_per_day: dict[date, int]


def read_pdf_words(path: Path) -> list[list[Word]]:
    """Words with coordinates for every page (needs the ``mining`` extra)."""
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise RuntimeError("pdfplumber missing: run `uv sync --extra mining`") from exc
    pages: list[list[Word]] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            pages.append(
                [
                    Word(str(w["text"]), float(w["x0"]), float(w["top"]))
                    for w in page.extract_words(keep_blank_chars=False, use_text_flow=False)
                ]
            )
    return pages


def parse_amount(text: str) -> float | None:
    """'₹-3,140.00', '-₹47.18', '+5,215.0', '0.0' -> float; None when there is no number."""
    s = text.replace("₹", "").replace(" ", "")
    m = _NUM_RE.search(s)
    if not m:
        return None
    value = float(m.group(0).replace(",", ""))
    if s.startswith("-") and value > 0:
        value = -value
    return value


def _rows_of(words: Sequence[Word], tol: float = 2.5) -> list[list[Word]]:
    """Group words into visual lines (same top within ``tol``), each sorted by x."""
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        if lines and abs(lines[-1][0].top - w.top) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def _day_net(header: Sequence[Word]) -> tuple[float | None, int]:
    """Stated Daily Net and number of accounts from a day-header line (sorted by x)."""
    words = sorted(header, key=lambda w: w.x0)
    texts = [w.text for w in words]
    try:
        i = next(
            k for k, t in enumerate(texts) if t == "Net:" and k > 0 and texts[k - 1] == "Daily"
        )
    except StopIteration:
        return None, 0
    seg: list[str] = []
    for t in texts[i + 1 :]:
        if t in ("Payout:", "True"):
            break
        seg.append(t)
    labels = [k for k, t in enumerate(seg) if _ACCT_LABEL_RE.match(t)]
    if labels:  # "ACC: ₹a | ACC: ₹b" -> sum of the per-account values
        total = 0.0
        for k in labels:
            v = parse_amount(seg[k + 1]) if k + 1 < len(seg) else None
            if v is None:
                return None, len(labels)
            total += v
        return round(total, 2), len(labels)
    for t in seg:  # "₹Y ₹Z (Legal)" -> first value is the net
        v = parse_amount(t)
        if v is not None and "(" not in t:
            return v, 1
    return None, 0


def _columns(header_words: Sequence[Word]) -> dict[str, float]:
    cols: dict[str, float] = {}
    for w in header_words:
        name = _HEADER_COLUMNS.get(w.text)
        if name and name not in cols:
            cols[name] = w.x0
    return cols


def _column_of(x0: float, starts: Sequence[tuple[float, str]]) -> str | None:
    found: str | None = None
    for start, name in starts:
        if start <= x0 + _X_SLACK:
            found = name
        else:
            break
    return found


def _trade_from_cells(
    cells: dict[str, list[Word]], day: date
) -> tuple[MinedTradeRow | None, str | None]:
    def texts(col: str) -> list[str]:
        return [w.text for w in sorted(cells.get(col, []), key=lambda w: (w.top, w.x0))]

    times = [t for t in texts("time") if _TIME_RE.match(t)]
    inst = [t for t in texts("inst") if re.match(r"^[A-Z][A-Z0-9&\-]*$", t)]
    side = [t for t in texts("side") if t in ("BUY", "SELL")]
    qty = [t for t in texts("qty") if t.isdigit()]
    if len(times) != 2 or len(inst) != 1 or len(side) != 1 or len(qty) != 1:
        return None, (f"incomplete trade block: times={times} inst={inst} side={side} qty={qty}")
    entry = [v for t in texts("entry") if (v := parse_amount(t)) is not None]
    exit_ = [v for t in texts("exit") if (v := parse_amount(t)) is not None]
    gross = [v for t in texts("gross") if (v := parse_amount(t)) is not None]
    net = [v for t in texts("net") if (v := parse_amount(t)) is not None]
    if len(entry) != 1 or len(exit_) != 1 or len(gross) != 1 or len(net) != 1:
        return None, (
            f"{inst[0]} {times[0]}: price/amount cells entry={entry} exit={exit_} "
            f"gross={gross} net={net}"
        )
    charge_text = "".join(texts("charges"))  # may wrap as "-" then "₹1,150.00"
    charges = parse_amount(charge_text)
    if charges is not None and charge_text.startswith("-") and charges > 0:
        charges = -charges
    mfe_mae = [v for t in texts("mfe_mae") if (v := parse_amount(t)) is not None]
    acct_words = texts("account")
    acct = next((t for t in acct_words if t.isdigit()), None)
    status = next((t for t in acct_words if t.isalpha()), "")
    modality = texts("modality")
    requested = texts("requested")
    flags = " ".join(t for t in texts("flags") if t != "-")
    breach = re.search(r"\(\+?₹?([\d,]+(?:\.\d+)?)\)", flags.replace(" ", ""))
    margin = texts("margin")
    q = int(qty[0])
    mfe_inr = mfe_mae[0] if mfe_mae else 0.0
    mae_inr = mfe_mae[1] if len(mfe_mae) > 1 else 0.0
    row = MinedTradeRow(
        trade_date=day,
        time_in=times[0],
        time_out=times[1],
        account_anon=anonymize_identifier(acct),
        instrument=inst[0],
        side=side[0],
        qty=q,
        margin_label=margin[0] if margin else "",
        entry_price=entry[0],
        exit_price=exit_[0],
        mfe_pts=round(mfe_inr / q, 4) if q else 0.0,
        mae_pts=round(mae_inr / q, 4) if q else 0.0,
        gross_pnl_inr=gross[0],
        net_pnl_inr=net[0],
        charges_inr=round(gross[0] - net[0], 2),
        qty_mismatch="QTY_MISMATCH" in flags,
        breach_inr=float(breach.group(1).replace(",", "")) if breach else 0.0,
        is_live=status == "LIVE",
        exit_order_type=modality[1] if len(modality) > 1 else (modality[0] if modality else ""),
        entry_order_type=modality[0] if modality else "",
        requested_in=requested[0] if requested else "",
        requested_out=requested[1] if len(requested) > 1 else "",
        mfe_inr=mfe_inr,
        mae_inr=mae_inr,
        stated_charges_inr=charges,
        flags=flags,
    )
    return row, None


def parse_trade_history_pages(pages: Sequence[Sequence[Word]]) -> PdfParseResult:
    """Parse all pages of one report. Never keeps names or full account IDs."""
    rows: list[MinedTradeRow] = []
    stated: dict[date, float] = {}
    accounts: dict[date, int] = {}
    issues: list[ParseIssue] = []
    day: date | None = None
    cols: dict[str, float] | None = None

    for page_no, words in enumerate(pages):
        lines = _rows_of(words)
        # Section markers on this page: day headers and table headers, in order of y.
        segments: list[tuple[float, str, list[Word]]] = []
        for line in lines:
            first = line[0]
            if _DATE_RE.match(first.text) and first.x0 < 60:
                # The amounts can sit a point above or below the date's baseline.
                band = [w for w in words if abs(w.top - first.top) <= 3 and w is not first]
                segments.append((first.top, "day", [first, *band]))
            elif first.text == "Week" and first.x0 < 60:
                segments.append((first.top, "stop", line))
            elif first.text == "TIME" and first.x0 < 60:
                hdr = [w for w in words if first.top - 2 <= w.top <= first.top + 12]
                segments.append((first.top, "table", hdr))
        segments.sort(key=lambda s: s[0])

        # A table can run over a page break without repeating its header: the top of this page
        # continues the previous page's table (same columns, same day) until the first marker.
        body_from: float | None = float("-inf") if cols is not None else None
        bounds: list[tuple[float, float, dict[str, float], date | None]] = []
        for top, kind, ws in segments:
            if body_from is not None and cols is not None:
                bounds.append((body_from, top - 1, cols, day))
            body_from = None
            if kind == "stop":
                continue
            if kind == "day":
                day = date.fromisoformat(ws[0].text)
                net, n_acc = _day_net(ws[1:])
                if net is None:
                    issues.append(ParseIssue(page_no, day, "day header without a Daily Net"))
                elif day in stated and abs(stated[day] - net) > 0.005:
                    issues.append(ParseIssue(page_no, day, "day header repeated with another net"))
                else:
                    stated[day] = net
                    accounts[day] = n_acc
            else:
                cols = _columns(ws)
                missing = [c for c in _REQUIRED if c not in cols]
                if missing:
                    issues.append(ParseIssue(page_no, day, f"table header missing {missing}"))
                    cols, body_from = None, None
                    continue
                body_from = max(w.top for w in ws) + 1
        if body_from is not None and cols is not None:
            bounds.append((body_from, float("inf"), cols, day))
        else:
            cols = None  # the page ended outside a table (after a day or week line)

        for y0, y1, colmap, bday in bounds:
            if bday is None:
                issues.append(ParseIssue(page_no, None, "trade table before any day header"))
                continue
            body = [w for w in words if y0 <= w.top <= y1]
            starts = sorted((x, name) for name, x in colmap.items())
            time_x1 = min(x for x, n in starts if n != "time")
            times = sorted(
                (w for w in body if _TIME_RE.match(w.text) and w.x0 < time_x1), key=lambda w: w.top
            )
            if len(times) % 2:
                issues.append(ParseIssue(page_no, bday, f"odd number of time cells ({len(times)})"))
                continue
            pairs = [(times[i], times[i + 1]) for i in range(0, len(times), 2)]
            for i, (t_in, t_out) in enumerate(pairs):
                lo = y0 if i == 0 else (pairs[i - 1][1].top + t_in.top) / 2
                hi = y1 if i == len(pairs) - 1 else (t_out.top + pairs[i + 1][0].top) / 2
                cells: dict[str, list[Word]] = {}
                for w in body:
                    if lo <= w.top < hi or (i == len(pairs) - 1 and w.top == hi):
                        col = _column_of(w.x0, starts)
                        if col is not None:
                            cells.setdefault(col, []).append(w)
                row, err = _trade_from_cells(cells, bday)
                if row is None:
                    issues.append(ParseIssue(page_no, bday, err or "unparsed trade"))
                else:
                    rows.append(row)

    report = ParsedTradeReport(
        rows=tuple(rows), stated_daily_nets=stated, skipped_lines=len(issues)
    )
    return PdfParseResult(report=report, issues=tuple(issues), accounts_per_day=accounts)


def parse_trade_history_pdf(path: Path) -> PdfParseResult:
    return parse_trade_history_pages(read_pdf_words(path))
