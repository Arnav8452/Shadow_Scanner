"""Reconciliation & Validation of Mined Trade Logs (SPEC §9.1).

Verifies two mathematical invariants on every parsed report:
1. **Row-Level Gross Invariant**:
   ``(exit_price - entry_price) * qty * (+1 if BUY else -1) == gross_pnl_inr``
   (flagged when difference exceeds ``row_tolerance_inr``, except on rows explicitly marked
   ``qty_mismatch=True`` in the broker report).
2. **Daily Net Invariant**:
   Sum of ``net_pnl_inr`` across all trades on ``trade_date`` must match the PDF header's stated
   ``Daily Net`` within ``daily_tolerance_inr``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from engine.trade_mining.parse_report import ParsedTradeReport


@dataclass(frozen=True, slots=True)
class RowMismatch:
    row_index: int
    trade_date: date
    instrument: str
    expected_gross_inr: float
    reported_gross_inr: float
    diff_inr: float


@dataclass(frozen=True, slots=True)
class DailyNetMismatch:
    trade_date: date
    summed_net_inr: float
    stated_net_inr: float
    diff_inr: float


@dataclass(frozen=True, slots=True)
class ValidationReport:
    total_rows: int
    valid_rows: int
    row_mismatches: tuple[RowMismatch, ...]
    daily_net_mismatches: tuple[DailyNetMismatch, ...]

    @property
    def passed(self) -> bool:
        """A report with no parsed trades never passes (it usually means the layout changed)."""
        return self.total_rows > 0 and not self.row_mismatches and not self.daily_net_mismatches


def validate_parsed_report(
    report: ParsedTradeReport,
    *,
    row_tolerance_inr: float = 1.5,
    daily_tolerance_inr: float = 2.0,
) -> ValidationReport:
    """Validate row-level gross P&L and per-day net P&L reconciliation."""
    row_errors: list[RowMismatch] = []
    by_day_net: dict[date, float] = {}

    for idx, r in enumerate(report.rows):
        by_day_net[r.trade_date] = by_day_net.get(r.trade_date, 0.0) + r.net_pnl_inr
        if r.qty_mismatch:
            continue
        sign = 1.0 if r.side.upper() == "BUY" else -1.0
        expected_gross = (r.exit_price - r.entry_price) * r.qty * sign
        diff = abs(expected_gross - r.gross_pnl_inr)
        if diff > row_tolerance_inr:
            row_errors.append(
                RowMismatch(
                    row_index=idx,
                    trade_date=r.trade_date,
                    instrument=r.instrument,
                    expected_gross_inr=round(expected_gross, 2),
                    reported_gross_inr=r.gross_pnl_inr,
                    diff_inr=round(diff, 2),
                )
            )

    day_errors: list[DailyNetMismatch] = []
    for d, stated in sorted(report.stated_daily_nets.items()):
        summed = by_day_net.get(d, 0.0)
        diff = abs(summed - stated)
        if diff > daily_tolerance_inr:
            day_errors.append(
                DailyNetMismatch(
                    trade_date=d,
                    summed_net_inr=round(summed, 2),
                    stated_net_inr=round(stated, 2),
                    diff_inr=round(diff, 2),
                )
            )

    return ValidationReport(
        total_rows=len(report.rows),
        valid_rows=len(report.rows) - len(row_errors),
        row_mismatches=tuple(row_errors),
        daily_net_mismatches=tuple(day_errors),
    )
