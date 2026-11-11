"""NSE Participant-wise Open Interest (FII, DII, Pro, Client) EOD parser & regime classifier.

Strictly enforces SPEC §3 L3 & §4.9:
- Participant OI is published after market close (~18:30–19:30 IST).
- Every row carries ``known_at = 19:30 IST`` on ``trade_date``, ensuring it can only be queried for
  the *next* trading session's directional/confluence bias — never same-day intraday lookahead.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum

from engine.core.rules import IST


class ParticipantCategory(StrEnum):
    CLIENT = "CLIENT"
    DII = "DII"
    FII = "FII"
    PRO = "PRO"


@dataclass(frozen=True, slots=True)
class ParticipantOIRow:
    trade_date: date
    category: ParticipantCategory
    fut_idx_long: int
    fut_idx_short: int
    fut_stk_long: int
    fut_stk_short: int
    opt_idx_call_long: int
    opt_idx_put_long: int
    opt_idx_call_short: int
    opt_idx_put_short: int
    known_at: datetime

    @property
    def net_fut_idx(self) -> int:
        return self.fut_idx_long - self.fut_idx_short

    @property
    def fii_long_short_ratio(self) -> float:
        total = self.fut_idx_long + self.fut_idx_short
        return (self.fut_idx_long / total) if total > 0 else 0.5

    @property
    def net_opt_idx_delta_proxy(self) -> int:
        """Net bullish option contracts = (Call Long - Call Short) + (Put Short - Put Long)."""
        return (self.opt_idx_call_long - self.opt_idx_call_short) + (
            self.opt_idx_put_short - self.opt_idx_put_long
        )


@dataclass(frozen=True, slots=True)
class ParticipantRegime:
    trade_date: date
    fii_net_fut_idx: int
    fii_ls_ratio: float
    pro_net_opt_idx: int
    client_net_fut_idx: int
    client_net_opt_idx: int
    institutional_bias: str  # BULLISH | BEARISH | NEUTRAL | EXTREME_SHORT_COVER_RISK
    score_pts: float  # [-10.0, +10.0] confluence contribution
    known_at: datetime


def _map_category(raw: str) -> ParticipantCategory | None:
    u = raw.strip().upper()
    if u.startswith("CLIENT"):
        return ParticipantCategory.CLIENT
    if u.startswith("DII"):
        return ParticipantCategory.DII
    if u.startswith("FII") or u.startswith("FPI"):
        return ParticipantCategory.FII
    if u.startswith("PRO"):
        return ParticipantCategory.PRO
    return None


def parse_participant_oi_csv(text: str, *, trade_date: date) -> list[ParticipantOIRow]:
    """Parse NSE ``fao_participant_oi_<ddmmyyyy>.csv`` text."""
    rows: list[ParticipantOIRow] = []
    known = datetime.combine(trade_date, time(19, 30), tzinfo=IST)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # Skip title banner lines until the header containing 'Client Type'
    start_idx = 0
    for idx, ln in enumerate(lines):
        if "CLIENT TYPE" in ln.upper():
            start_idx = idx
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[start_idx:])))
    for raw in reader:
        norm = {k.strip().upper(): (v or "").strip() for k, v in raw.items() if k}
        cat = _map_category(norm.get("CLIENT TYPE", ""))
        if cat is None:
            continue
        rows.append(
            ParticipantOIRow(
                trade_date=trade_date,
                category=cat,
                fut_idx_long=int(float(norm.get("FUTURE INDEX LONG", "0") or 0)),
                fut_idx_short=int(float(norm.get("FUTURE INDEX SHORT", "0") or 0)),
                fut_stk_long=int(float(norm.get("FUTURE STOCK LONG", "0") or 0)),
                fut_stk_short=int(float(norm.get("FUTURE STOCK SHORT", "0") or 0)),
                opt_idx_call_long=int(float(norm.get("OPTION INDEX CALL LONG", "0") or 0)),
                opt_idx_put_long=int(float(norm.get("OPTION INDEX PUT LONG", "0") or 0)),
                opt_idx_call_short=int(float(norm.get("OPTION INDEX CALL SHORT", "0") or 0)),
                opt_idx_put_short=int(float(norm.get("OPTION INDEX PUT SHORT", "0") or 0)),
                known_at=known,
            )
        )
    return rows


def summarize_participant_regime(rows: Sequence[ParticipantOIRow]) -> ParticipantRegime:
    """Summarize FII + Pro vs Client positioning into a next-day directional bias."""
    if not rows:
        raise ValueError("rows must not be empty")
    by_cat = {r.category: r for r in rows}
    t_date = rows[0].trade_date
    known = rows[0].known_at

    fii = by_cat.get(ParticipantCategory.FII)
    pro = by_cat.get(ParticipantCategory.PRO)
    cli = by_cat.get(ParticipantCategory.CLIENT)

    fii_net = fii.net_fut_idx if fii else 0
    fii_ls = fii.fii_long_short_ratio if fii else 0.5
    pro_opt = pro.net_opt_idx_delta_proxy if pro else 0
    cli_fut = cli.net_fut_idx if cli else 0
    cli_opt = cli.net_opt_idx_delta_proxy if cli else 0

    # Institutional alignment: FII index futures + Pro index options vs retail Client opposite side
    inst_signal = 0.0
    if fii_net > 15_000 and pro_opt > 0:
        bias = "BULLISH"
        inst_signal = 7.5
    elif fii_ls < 0.22 and cli_fut > 50_000:
        # Crowded FII shorts below 22% L/S ratio: watch for violent short-covering squeeze on ORH break
        bias = "EXTREME_SHORT_COVER_RISK"
        inst_signal = 2.5
    elif fii_net < -15_000 and pro_opt < 0:
        bias = "BEARISH"
        inst_signal = -7.5
    else:
        bias = "NEUTRAL"
        inst_signal = 0.0

    return ParticipantRegime(
        trade_date=t_date,
        fii_net_fut_idx=fii_net,
        fii_ls_ratio=round(fii_ls, 4),
        pro_net_opt_idx=pro_opt,
        client_net_fut_idx=cli_fut,
        client_net_opt_idx=cli_opt,
        institutional_bias=bias,
        score_pts=inst_signal,
        known_at=known,
    )
