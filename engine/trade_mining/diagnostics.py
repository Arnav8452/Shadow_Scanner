"""SPEC §9.3–9.4 diagnostics for a mined reference log (or my own journal) -> Markdown.

Everything here describes what a trader did; none of it shows that a rule works. Candidate
rules still go through backtest and paper (§8) before use.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from engine.trade_mining.parse_report import MinedTradeRow
from engine.trade_mining.self_review import analyze_trade_journal

_UNDERLYING_RE = re.compile(r"^([A-Z&\-]+?)(?=\d)")


def underlying_of(instrument: str) -> str:
    m = _UNDERLYING_RE.match(instrument)
    return m.group(1) if m else instrument


def kind_of(instrument: str) -> str:
    for k in ("FUT", "CE", "PE"):
        if instrument.endswith(k):
            return k
    return "EQ"


def _secs(t: str | None) -> int | None:
    if not t:
        return None
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def _pct(a: float, b: float) -> float:
    return round(a / b * 100.0, 1) if b else 0.0


def _median(xs: Sequence[float]) -> float | None:
    return round(float(statistics.median(xs)), 2) if xs else None


@dataclass(frozen=True, slots=True)
class Position:
    """Fills that share day, instrument, side and exit time: one idea scaled in over tranches."""

    trade_date: date
    instrument: str
    side: str
    first_in: str
    last_in: str
    exit_time: str
    tranches: int
    qty: int
    avg_entry: float
    exit_price: float
    net_inr: float
    gross_inr: float


def group_positions(rows: Sequence[MinedTradeRow]) -> list[Position]:
    groups: dict[tuple[date, str, str, str], list[MinedTradeRow]] = defaultdict(list)
    for r in rows:
        groups[(r.trade_date, r.instrument, r.side, r.time_out or "")].append(r)
    out: list[Position] = []
    for (d, inst, side, t_out), fills in groups.items():
        qty = sum(f.qty for f in fills)
        ins = sorted(f.time_in or "" for f in fills)
        out.append(
            Position(
                trade_date=d,
                instrument=inst,
                side=side,
                first_in=ins[0],
                last_in=ins[-1],
                exit_time=t_out,
                tranches=len(fills),
                qty=qty,
                avg_entry=round(sum(f.entry_price * f.qty for f in fills) / qty, 2) if qty else 0.0,
                exit_price=round(sum(f.exit_price * f.qty for f in fills) / qty, 2) if qty else 0.0,
                net_inr=round(sum(f.net_pnl_inr for f in fills), 2),
                gross_inr=round(sum(f.gross_pnl_inr for f in fills), 2),
            )
        )
    return sorted(out, key=lambda p: (p.trade_date, p.first_in))


def _max_concurrent(rows: Sequence[MinedTradeRow]) -> int:
    best = 0
    by_day: dict[date, list[tuple[int, int]]] = defaultdict(list)
    for r in rows:
        a, b = _secs(r.time_in), _secs(r.time_out)
        if a is not None and b is not None:
            by_day[r.trade_date].append((a, b))
    for spans in by_day.values():
        events = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans])
        cur = 0
        for _, step in events:
            cur += step
            best = max(best, cur)
    return best


def _max_drawdown(daily: Sequence[float]) -> float:
    peak = equity = dd = 0.0
    for x in daily:
        equity += x
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    return round(dd, 2)


def diagnostics_markdown(
    rows: Sequence[MinedTradeRow],
    *,
    label: str,
    stated_daily_nets: dict[date, float] | None = None,
    tick_size: float = 0.05,
) -> str:
    """Markdown report: style (§9.3) and diagnostics (§9.4). No names or account IDs."""
    if not rows:
        return f"# Trade mining — {label}\n\nNo trades parsed.\n"
    ordered = sorted(rows, key=lambda r: (r.trade_date, r.time_in or ""))
    rev = analyze_trade_journal(ordered, tick_size=tick_size)
    n = len(ordered)
    days = sorted({r.trade_date for r in ordered})
    daily_net: dict[date, float] = defaultdict(float)
    for r in ordered:
        daily_net[r.trade_date] += r.net_pnl_inr
    nets = [r.net_pnl_inr for r in ordered]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    pf = round(sum(wins) / -sum(losses), 2) if losses else float("inf")
    payoff = (
        round((sum(wins) / len(wins)) / (-sum(losses) / len(losses)), 2) if wins and losses else 0
    )
    holds = [h / 60 for r in ordered if (h := r.holding_seconds) is not None]
    gross = sum(r.gross_pnl_inr for r in ordered)
    charges = sum(r.charges_inr for r in ordered)
    net = sum(nets)

    L: list[str] = [f"# Trade mining — {label}", ""]
    L.append(
        "Describes what this trader did. It is **not** evidence that any rule works; candidates "
        "still need a bid/ask backtest with costs and paper trading (SPEC §8)."
    )
    L += ["", "## Reconciliation", ""]
    if stated_daily_nets is not None:
        worst = max(
            (abs(daily_net.get(d, 0.0) - v) for d, v in stated_daily_nets.items()), default=0.0
        )
        L.append(
            f"- {len(stated_daily_nets)} stated days; largest |Σ net − Daily Net| = ₹{worst:,.2f}."
        )
    L.append(f"- {n} fills on {len(days)} days, {days[0]} to {days[-1]}.")

    L += ["", "## Headline", ""]
    L += [
        f"- Gross ₹{gross:,.2f}; charges ₹{charges:,.2f} "
        f"({_pct(charges, abs(gross))}% of |gross|); net ₹{net:,.2f}.",
        f"- Win rate {_pct(len(wins), n)}%; payoff {payoff}; profit factor {pf}.",
        f"- Median hold {_median(holds)} min; {round(n / len(days), 1)} fills/day; "
        f"max concurrent fills {_max_concurrent(ordered)}.",
        f"- Positive days {_pct(sum(1 for d in days if daily_net[d] > 0), len(days))}%; "
        f"max drawdown on daily net ₹{_max_drawdown([daily_net[d] for d in days]):,.2f}.",
    ]

    # Concentration
    by_und: dict[str, list[float]] = defaultdict(list)
    for r in ordered:
        by_und[f"{underlying_of(r.instrument)} {kind_of(r.instrument)}"].append(r.net_pnl_inr)
    L += [
        "",
        "## Instrument concentration",
        "",
        "| Underlying / kind | Fills | Net ₹ |",
        "|---|---:|---:|",
    ]
    for k, xs in sorted(by_und.items(), key=lambda kv: -abs(sum(kv[1]))):
        L.append(f"| {k} | {len(xs)} | {sum(xs):,.2f} |")
    top = sorted(nets, reverse=True)
    for top_n in (1, 5, 10):
        if n > top_n:
            L.append(f"\nNet without the best {top_n} fill(s): ₹{net - sum(top[:top_n]):,.2f}")

    # Time of day
    L += ["", "## Time of day (entry)", "", "| Bucket | Net ₹ |", "|---|---:|"]
    for b, v in sorted(rev.time_bucket_net_pnl.items()):
        L.append(f"| {b} | {v:,.2f} |")
    late = [r.net_pnl_inr for r in ordered if (r.time_in or "") >= "14:00:00"]
    L.append(f"\nEntries at or after 14:00: {len(late)} fills, net ₹{sum(late):,.2f}.")

    # Option premium moves (long options only)
    longs = [r for r in ordered if kind_of(r.instrument) in ("CE", "PE") and r.side == "BUY"]
    if longs:
        moves = [
            (r.exit_price - r.entry_price) / r.entry_price * 100 for r in longs if r.entry_price
        ]
        win_m = [m for m in moves if m > 0]
        loss_m = [m for m in moves if m <= 0]
        mfe = [r.mfe_inr / r.qty / r.entry_price * 100 for r in longs if r.qty and r.entry_price]
        mae = [r.mae_inr / r.qty / r.entry_price * 100 for r in longs if r.qty and r.entry_price]
        n_ce = sum(1 for r in longs if r.instrument.endswith("CE"))
        L += ["", "## Long options: premium move at exit", ""]
        L += [
            f"- {len(longs)} long-option fills; CE {n_ce}, PE {len(longs) - n_ce}.",
            f"- Winners: median {_median(win_m)}% of premium (n={len(win_m)}); "
            f"losers: median {_median(loss_m)}% (n={len(loss_m)}).",
            f"- Median MFE {_median(mfe)}% / MAE {_median(mae)}% of entry premium "
            "(from the report's MFE/MAE columns).",
        ]
    shorts = [r for r in ordered if kind_of(r.instrument) in ("CE", "PE") and r.side == "SELL"]
    if shorts:
        short_net = sum(r.net_pnl_inr for r in shorts)
        L.append(f"- Option sells: {len(shorts)} fills, net ₹{short_net:,.2f}.")

    # Positions / scaling
    pos = group_positions(ordered)
    multi = [p for p in pos if p.tranches > 1]
    per_day = [float(c) for c in Counter(p.trade_date for p in pos).values()]
    L += ["", "## Positions (fills sharing day, instrument, side, exit time)", ""]
    L += [
        f"- {len(pos)} positions; {len(multi)} scaled in over 2+ fills "
        f"(max {max((p.tranches for p in pos), default=0)} fills).",
        f"- Positions per day: median {_median(per_day)}.",
    ]

    # Exits, margin, flags
    ex = Counter(r.exit_order_type or "?" for r in ordered)
    L += ["", "## Exits, margin, flags", ""]
    exits = ", ".join(f"{k} {_pct(v, n)}%" for k, v in ex.most_common())
    L.append(f"- Exit order types: {exits}.")
    margins = [float(r.margin_label.rstrip("%")) for r in ordered if r.margin_label.endswith("%")]
    if margins:
        L.append(f"- Margin used per fill: median {_median(margins)}%, max {max(margins)}%.")
    L.append(
        f"- Flags: {sum(1 for r in ordered if r.breach_inr > 0)} limit breaches, "
        f"{sum(1 for r in ordered if r.qty_mismatch)} quantity mismatches."
    )

    # Behaviour
    rv = rev.revenge
    L += ["", "## Behaviour after losses (entries ≤ 15 min after a losing exit)", ""]
    L += [
        f"- Post-loss fills {rv.post_loss_trade_count}: win rate {rv.post_loss_win_rate_pct}%, "
        f"avg qty {rv.post_loss_avg_qty}, net ₹{rv.post_loss_net_pnl_inr:,.2f}.",
        f"- Other fills {rv.baseline_trade_count}: win rate {rv.baseline_win_rate_pct}%, "
        f"avg qty {rv.baseline_avg_qty}.",
    ]
    L += ["", "## Activity terciles (avg daily net)", ""]
    for k, v in rev.activity_tercile_avg_daily_net.items():
        L.append(f"- {k}: ₹{v:,.2f}")
    L += ["", f"## Slippage stress (extra ticks per side, tick ₹{tick_size})", ""]
    for k, v in rev.slippage_stress_net_pnl.items():
        L.append(f"- {k}: net ₹{v:,.2f}")
    L.append("")
    return "\n".join(L)
