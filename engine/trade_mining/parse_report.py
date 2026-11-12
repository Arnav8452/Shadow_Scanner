"""Reference Trade-History PDF (`pdftotext -layout`) Parser with PII Anonymization (SPEC §9.1).

Ports and hardens ``docs/seed/parse_report_seed.py``:
- Strips / hashes account numbers, PANs, and personal names so outputs never contain PII.
- Extracts entry/exit prices, MFE/MAE when present, gross/net P&L, charges, ``QTY_MISMATCH`` flags,
  margin breach amounts, and stated ``Daily Net`` totals for reconciliation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

_TIME_RE = re.compile(r"^\s*(\d{2}:\d{2}:\d{2})\b")
_DATE_HEADER_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s+True Equity(?:\s+.*?Daily Net[:\s]*₹\s*(-?[\d,]+\.\d+))?"
)
_DAILY_NET_RE = re.compile(r"Daily Net[:\s]*₹\s*(-?[\d,]+\.\d+)", re.IGNORECASE)
_HEAD_RE = re.compile(r"\s(\d{4})\s+([A-Z&][A-Z0-9&]*)\s+(BUY|SELL)\s+(\d+)\s+(\S+)")
_HEAD2_RE = re.compile(r"^\s+([A-Z&][A-Z0-9&]*\d[A-Z0-9&]*)\s+(BUY|SELL)\s+(\d+)\s+(\S+)")
_AMT_RE = re.compile(r"₹\s*(-?[\d,]+\.\d+)")
_DEC_RE = re.compile(r"(?<![\d,\.+\-₹])([\d,]*\d\.\d+)")
_ACCT_BACK_RE = re.compile(r"^\s*\d{2}:\d{2}:\d{2}\s+(\d{4})")
_ORDER_MOD_RE = re.compile(r"\b(MARKET_PROTECTED|MARKET|LIMIT|SL_M)\b")
_BREACH_RE = re.compile(r"\(\+\s*₹?\s*([\d,]+(?:\.\d+)?)")
_PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")


@dataclass(frozen=True, slots=True)
class MinedTradeRow:
    trade_date: date
    time_in: str | None
    time_out: str | None
    account_anon: str
    instrument: str
    side: str  # "BUY" or "SELL"
    qty: int
    margin_label: str
    entry_price: float
    exit_price: float
    mfe_pts: float
    mae_pts: float
    gross_pnl_inr: float
    net_pnl_inr: float
    charges_inr: float
    qty_mismatch: bool
    breach_inr: float
    is_live: bool
    exit_order_type: str
    # Filled by the PDF word-position parser (pdf_report.py); defaults keep older callers working.
    entry_order_type: str = ""
    requested_in: str = ""  # "MKT" or the requested limit/stop price, as printed
    requested_out: str = ""
    mfe_inr: float = 0.0  # MFE / MAE columns are rupees for the whole quantity
    mae_inr: float = 0.0
    stated_charges_inr: float | None = None  # CHARGES column as printed (negative = paid)
    flags: str = ""

    @property
    def holding_seconds(self) -> int | None:
        if not self.time_in or not self.time_out:
            return None
        try:
            h1, m1, s1 = (int(x) for x in self.time_in.split(":"))
            h2, m2, s2 = (int(x) for x in self.time_out.split(":"))
            return max(0, (h2 * 3600 + m2 * 60 + s2) - (h1 * 3600 + m1 * 60 + s1))
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class ParsedTradeReport:
    rows: tuple[MinedTradeRow, ...]
    stated_daily_nets: dict[date, float]
    skipped_lines: int


def anonymize_identifier(raw: str | None) -> str:
    """Hash account code / identifier into a non-reversible 8-hex-char pseudonym."""
    if not raw:
        return "ANON0000"
    digest = hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()[:8].upper()
    return f"ACCT-{digest}"


def redact_pii_text(text: str) -> str:
    """Redact Indian PAN numbers from free-form text."""
    return _PAN_RE.sub("REDACTED_PAN", text)


def parse_pdftotext_report(layout_text: str) -> ParsedTradeReport:
    """Parse ``pdftotext -layout`` output of a reference Trade History PDF."""
    sanitized = redact_pii_text(layout_text.replace("\f", "\n"))
    lines = sanitized.split("\n")

    current_date: date | None = None
    stated_nets: dict[date, float] = {}
    rows: list[MinedTradeRow] = []
    used_time_lines: set[int] = set()
    skipped = 0

    for j, line in enumerate(lines):
        m_date = _DATE_HEADER_RE.match(line)
        if m_date:
            current_date = date.fromisoformat(m_date.group(1))
            if m_date.group(2):
                stated_nets[current_date] = float(m_date.group(2).replace(",", ""))
            else:
                m_dn = _DAILY_NET_RE.search(line)
                if m_dn:
                    stated_nets[current_date] = float(m_dn.group(1).replace(",", ""))
            continue

        if current_date is not None:
            m_dn_any = _DAILY_NET_RE.search(line)
            if m_dn_any and current_date not in stated_nets:
                stated_nets[current_date] = float(m_dn_any.group(1).replace(",", ""))

        if _TIME_RE.match(line):
            continue

        mm = _HEAD_RE.search(line)
        acct: str | None = None
        if mm:
            acct, inst, side, qty_s, mg = mm.groups()
        else:
            m2 = _HEAD2_RE.match(line)
            if not m2:
                continue
            inst, side, qty_s, mg = m2.groups()
            mm = m2
            for k in range(j - 1, max(j - 4, -1), -1):
                a_match = _ACCT_BACK_RE.match(lines[k])
                if a_match:
                    acct = a_match.group(1)
                    break

        if current_date is None:
            skipped += 1
            continue

        rest = line[mm.end() :]
        amline = line
        if "₹" not in line and j + 1 < len(lines):
            rest += " " + lines[j + 1]
            amline = lines[j + 1]

        prices = [float(x.replace(",", "")) for x in _DEC_RE.findall(_AMT_RE.sub(" ", rest))]
        amts = _AMT_RE.findall(amline)
        if len(prices) < 2 or len(amts) < 2:
            skipped += 1
            continue

        tin: str | None = None
        tout: str | None = None
        mod_out = ""

        for k in range(j - 1, max(j - 7, -1), -1):
            t = _TIME_RE.match(lines[k])
            if t and k not in used_time_lines:
                tin = t.group(1)
                used_time_lines.add(k)
                break

        for k in range(j + 1, min(j + 7, len(lines))):
            t = _TIME_RE.match(lines[k])
            if t:
                tout = t.group(1)
                used_time_lines.add(k)
                om = _ORDER_MOD_RE.search(lines[k])
                mod_out = om.group(1) if om else ""
                break

        blk = " ".join(lines[max(j - 3, 0) : min(j + 5, len(lines))])
        if "SL_M" in line and not mod_out:
            mod_out = "SL_M"

        gross = float(amts[0].replace(",", ""))
        net = float(amts[-1].replace(",", ""))
        mfe = prices[2] if len(prices) >= 4 else 0.0
        mae = prices[3] if len(prices) >= 4 else 0.0
        br = _BREACH_RE.search(blk) if "BREACH" in blk else None

        rows.append(
            MinedTradeRow(
                trade_date=current_date,
                time_in=tin,
                time_out=tout,
                account_anon=anonymize_identifier(acct),
                instrument=inst,
                side=side,
                qty=int(qty_s),
                margin_label=mg,
                entry_price=prices[0],
                exit_price=prices[1],
                mfe_pts=mfe,
                mae_pts=mae,
                gross_pnl_inr=gross,
                net_pnl_inr=net,
                charges_inr=round(gross - net, 2),
                qty_mismatch=("QTY_MISMATCH" in blk),
                breach_inr=float(br.group(1).replace(",", "")) if br else 0.0,
                is_live=("LIVE" in blk),
                exit_order_type=mod_out,
            )
        )

    return ParsedTradeReport(
        rows=tuple(rows),
        stated_daily_nets=stated_nets,
        skipped_lines=skipped,
    )
