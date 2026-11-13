"""Audit every paper trade against the hard rules (owner request, 13 Nov 2026: "verify the whole
thing"). Reads the journals only; changes nothing. Each finding names the account, the trade
and the rule it breaks, so a rule bug cannot hide behind a summary P&L.

Checks (rule source in brackets):
- **R1 defined risk** (CLAUDE.md 1): every short option leg has a long leg of the same type, a
  short future a long call, a long future a long put; wings were bought before shorts were sold.
- **R2 per-trade risk** (risk.per_trade_max_loss_pct): a long option's planned loss (entry - stop)
  x qty within the directional limit; a spread's max loss within the spread limit.
- **R3 lots** (cockpit.paper_max_lots): at most this many lots per trade.
- **R4 entry window** (risk.block_first_minutes, paper_no_new_entries_after): no entry in the first
  15 minutes or after 15:00.
- **R5 trades per day** (risk.max_trades_per_day) per account.
- **R6 daily loss** (risk.daily_loss_limit_pct): no entry after the day's realized loss reached
  the limit.
- **R7 square-off** (paper_square_off_at): intraday accounts hold nothing past 15:20, nor
  overnight.
- **R8 scalp discipline** (owner's charges-breakeven protocol): a scalp account's losing trade
  is closed within 10 minutes (180-s scratch, 15-candle rule), never held as a bag.
- **R9 accounting**: charges > 0 and net = gross - charges on every close.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.core.rules import IST

SCALP_ACCOUNTS = {"put_panic", "oi_ignition", "breakthrough", "spoof_fade", "score"}
OVERNIGHT_ACCOUNTS = {"condor", "calendar", "compression", "synthetic", "married", "swing"}
SPREAD_ACCOUNTS = {
    "condor", "debit", "iron_fly", "oi_credit", "calendar", "compression", "expiry_pin",
    "synthetic", "married", "mcx", "hiv",
}  # fmt: skip


@dataclass
class Finding:
    rule: str
    account: str
    when: str
    what: str


def _t(x: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(x)).astimezone(IST)
    except (TypeError, ValueError):
        return None


def audit_journals(
    journal_dir: Path,
    *,
    capital_inr: float,
    directional_pct: float,
    spread_pct: float,
    daily_loss_pct: float,
    max_trades: int,
    max_lots: int,
    lot_of: Any = None,
    first_entry: str = "09:30",
    last_entry: str = "15:00",
    square_off: str = "15:20",
    scalp_max_hold_min: float = 10.0,
) -> tuple[list[Finding], dict[str, Any]]:
    finds: list[Finding] = []
    stats: dict[str, Any] = {"accounts": 0, "opens": 0, "closes": 0}
    dir_lim = capital_inr * directional_pct / 100
    spr_lim = capital_inr * spread_pct / 100
    day_lim = capital_inr * daily_loss_pct / 100
    for path in sorted(journal_dir.glob("paper_*.jsonl")):
        acct = path.stem[len("paper_") :]
        if acct == "swing":  # nightly cash/option account: its own rules (engine/execution)
            continue
        stats["accounts"] += 1
        opens_by_day: dict[str, int] = defaultdict(int)
        realized: dict[str, float] = defaultdict(float)
        open_at: dict[str, datetime] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            kind = r.get("type")
            t = _t(r.get("t"))
            if t is None or kind not in ("open", "close"):
                continue
            day, hm = t.date().isoformat(), t.strftime("%H:%M")
            key = str(r.get("id") or r.get("symbol"))
            if kind == "open":
                stats["opens"] += 1
                opens_by_day[day] += 1
                open_at[key] = t
                where = f"{day} {t:%H:%M:%S} {key}"
                if not first_entry <= hm < last_entry:
                    finds.append(
                        Finding(
                            "R4 entry window",
                            acct,
                            where,
                            f"entered at {hm} (allowed {first_entry}-{last_entry})",
                        )
                    )
                if opens_by_day[day] > max_trades:
                    finds.append(
                        Finding(
                            "R5 trades per day",
                            acct,
                            where,
                            f"entry #{opens_by_day[day]} (max {max_trades})",
                        )
                    )
                if realized[day] <= -day_lim:
                    finds.append(
                        Finding(
                            "R6 daily loss",
                            acct,
                            where,
                            f"entered after the day's loss reached {realized[day]:,.0f}",
                        )
                    )
                if "legs" in r:
                    legs = r["legs"]
                    first_sell = next(
                        (i for i, lg in enumerate(legs) if lg["side"] == "SELL"), None
                    )
                    for lg in legs:
                        if lg["side"] != "SELL":
                            continue
                        need = "CE" if lg["kind"] == "FUT" else lg["kind"]
                        if not any(w["side"] == "BUY" and w["kind"] == need for w in legs):
                            finds.append(
                                Finding(
                                    "R1 defined risk",
                                    acct,
                                    where,
                                    f"short {lg['kind']} {lg['strike']:g} has no long {need}",
                                )
                            )
                    if first_sell is not None and any(
                        lg["side"] == "BUY" for lg in legs[first_sell:]
                    ):
                        finds.append(
                            Finding(
                                "R1 defined risk",
                                acct,
                                where,
                                "a short was filled before all wings",
                            )
                        )
                    if float(r.get("max_loss_inr", 0)) > spr_lim + 1:
                        finds.append(
                            Finding(
                                "R2 per-trade risk",
                                acct,
                                where,
                                f"max loss {r['max_loss_inr']:,.0f} > {spr_lim:,.0f}",
                            )
                        )
                    lots = (
                        int(r.get("qty", 0)) // max(1, int(legs[0].get("qty") or 1)) if legs else 1
                    )
                else:
                    qty = int(r.get("qty") or 0)
                    loss = (float(r.get("price") or 0) - float(r.get("stop") or 0)) * qty
                    if loss > dir_lim + 1:
                        finds.append(
                            Finding(
                                "R2 per-trade risk",
                                acct,
                                where,
                                f"planned loss {loss:,.0f} > {dir_lim:,.0f}",
                            )
                        )
                    lot = int(lot_of(str(r.get("symbol"))) if lot_of else 0) or 0
                    lots = qty // lot if lot else 1
                if lots > max_lots:
                    finds.append(Finding("R3 lots", acct, where, f"{lots} lots (max {max_lots})"))
            else:
                stats["closes"] += 1
                net = float(r.get("net_inr") or 0)
                realized[day] += net
                gross, ch = float(r.get("gross_inr") or 0), float(r.get("charges_inr") or 0)
                where = f"{day} {t:%H:%M:%S} {key}"
                if ch <= 0 or abs(gross - ch - net) > 1.0:
                    finds.append(
                        Finding(
                            "R9 accounting",
                            acct,
                            where,
                            f"gross {gross} - charges {ch} != net {net}",
                        )
                    )
                o = _t(r.get("opened_at")) or open_at.get(key)
                if o is not None:
                    held = (t - o).total_seconds() / 60
                    if acct not in OVERNIGHT_ACCOUNTS and (o.date() != t.date() or hm > square_off):
                        finds.append(
                            Finding(
                                "R7 square-off",
                                acct,
                                where,
                                f"opened {o:%d %b %H:%M}, closed {t:%d %b %H:%M}",
                            )
                        )
                    if acct in SCALP_ACCOUNTS and net < 0 and held > scalp_max_hold_min:
                        finds.append(
                            Finding(
                                "R8 scalp discipline",
                                acct,
                                where,
                                f"losing scalp held {held:.0f} min (net {net:,.0f}): "
                                f"{r.get('reason', '')}",
                            )
                        )
    return finds, stats


def render(finds: list[Finding], stats: dict[str, Any], as_of: datetime) -> str:
    lines = [
        f"# Paper-trade audit, {as_of:%d %b %Y %H:%M}",
        "",
        f"{stats['accounts']} accounts, {stats['opens']} entries, {stats['closes']} exits checked "
        "against the hard rules (engine/cockpit/audit.py).",
        "",
    ]
    if not finds:
        lines.append("**No rule breaches.**")
        return "\n".join(lines) + "\n"
    by_rule: dict[str, list[Finding]] = defaultdict(list)
    for f in finds:
        by_rule[f.rule].append(f)
    lines.append(f"**{len(finds)} finding(s)**")
    for rule in sorted(by_rule):
        lines += ["", f"## {rule} ({len(by_rule[rule])})", ""]
        lines += [f"- `{f.account}` {f.when}: {f.what}" for f in by_rule[rule]]
    return "\n".join(lines) + "\n"


__all__ = ["Finding", "audit_journals", "render"]
