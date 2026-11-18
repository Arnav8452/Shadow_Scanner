"""Capital-time scoreboard for paper accounts: is a strategy worth its capital, next to Nifty?

Nifty's long-run ~11% a year ties up all the capital all year. A strategy that uses capital
only part of the time frees it for others, so the fair comparison is per unit of capital and
time (the owner's desk view, 15 Nov 2026):

- **capital used** per trade: the margin blocked for a spread (``margin.final``, else
  ``required``), the premium for a long option, the net debit when no margin is known, the
  cost of a cash position;
- **capital-sessions** = capital × trading sessions held (an intraday trade uses one session);
- **utilisation** = capital-sessions / (account capital × sessions in the period);
- **deployed return p.a.** = net ÷ capital-sessions × sessions a year (what the money earns
  while working);
- **account return p.a.** = net ÷ capital, annualised over the period, plus idle cash at the
  T-bill rate for the unused share: what the account compounds at with this strategy alone;
- **max drawdown** of the cumulative net, and **Nifty over the same dates** (return, drawdown).

Annualised figures are shown only after ``min_sessions_annualise`` sessions: a few days scaled
to a year mean nothing. Returns are simple annualised rates. No tax is deducted: the owner's
gains sit inside the basic exemption (``owner_effective_tax_on_trading_gains`` in
``config/rules/tax_rules.csv``). Nothing here is a profit claim.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class CapitalTime:
    account: str
    period_sessions: int
    trades_with_capital: int
    trades_without_capital: int
    avg_capital: float | None
    capital_sessions: float
    utilisation: float | None
    net: float
    deployed_return_pa: float | None
    account_return_pa: float | None
    max_drawdown_inr: float
    nifty_return_pa: float | None
    nifty_drawdown: float | None
    # Other holdings over the same dates: (name, return p.a., max drawdown), e.g. the owner's
    # idle-money candidates Nifty Midcap 150 and a 50/50 Nifty / Midcap 150 mix.
    benchmarks: tuple[tuple[str, float | None, float | None], ...] = ()


def capital_used(r: Mapping[str, Any]) -> float | None:
    """₹ tied up by one closed trade, from what the journal recorded."""
    m = r.get("margin")
    if isinstance(m, Mapping):
        for k in ("final", "required"):
            v = m.get(k)
            if isinstance(v, int | float) and v > 0:
                return float(v)
    legs = r.get("legs")
    if isinstance(legs, list) and legs:
        debit = 0.0
        for leg in legs:
            px = float(leg.get("entry_px") or 0.0) * float(leg.get("qty") or 0)
            debit += px if str(leg.get("side", "")).upper() == "BUY" else -px
        return debit if debit > 0 else None
    qty = r.get("qty")
    px = r.get("entry_price", r.get("price"))
    if isinstance(qty, int | float) and isinstance(px, int | float) and qty > 0 and px > 0:
        return float(qty) * float(px)
    return None


def _day(raw: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(raw)).date()
    except ValueError:
        return None


def sessions_between(start: date, end: date) -> int:
    """Weekday sessions from ``start`` to ``end`` inclusive (holidays are rare enough to ignore
    for a utilisation figure)."""
    n, d = 0, start
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return max(n, 1)


def index_window(
    closes: Sequence[tuple[date, float]], start: date, end: date
) -> tuple[float | None, float | None]:
    """(return, max drawdown) of an index from its last close on or before ``start`` to its
    last close on or before ``end``. None when there are no closes near the window."""
    pts = sorted(closes)
    if not any(start - timedelta(days=7) <= d <= end for d, _ in pts):
        return None, None
    before = [c for c in pts if c[0] <= start]
    first = before[-1] if before else next((c for c in pts if c[0] >= start), None)
    window = [c for c in pts if first is not None and first[0] <= c[0] <= end]
    if first is None or not window:
        return None, None
    peak, dd = first[1], 0.0
    for _, px in window:
        peak = max(peak, px)
        dd = min(dd, px / peak - 1)
    return window[-1][1] / first[1] - 1, dd


def capital_time(
    account: str,
    closes: Sequence[Mapping[str, Any]],
    *,
    capital: float,
    start: date,
    end: date,
    risk_free_pa: float,
    index_closes: Sequence[tuple[date, float]],
    sessions_per_year: int = 248,
    min_sessions_annualise: int = 20,
    benchmarks: Mapping[str, Sequence[tuple[date, float]]] | None = None,
) -> CapitalTime:
    rows = sorted(closes, key=lambda r: str(r.get("closed_at") or r.get("t") or ""))
    period = sessions_between(start, end)
    cap_sessions, caps, missing = 0.0, [], 0
    cum = peak = dd = 0.0
    for r in rows:
        cum += float(r.get("net_inr") or 0.0)
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
        c = capital_used(r)
        d0 = _day(r.get("opened_at")) or _day(r.get("closed_at") or r.get("t"))
        d1 = _day(r.get("closed_at") or r.get("t")) or d0
        if c is None or d0 is None or d1 is None:
            missing += 1
            continue
        caps.append(c)
        cap_sessions += c * sessions_between(d0, d1)
    net = sum(float(r.get("net_inr") or 0.0) for r in rows)
    util = cap_sessions / (capital * period) if capital > 0 else None
    ann = period >= min_sessions_annualise
    deployed = net / cap_sessions * sessions_per_year if ann and cap_sessions > 0 else None
    account_pa = None
    if ann and capital > 0 and util is not None:
        account_pa = net / capital * sessions_per_year / period + risk_free_pa * max(0.0, 1 - util)
    n_ret, n_dd = index_window(index_closes, start, end)
    bench = []
    for name, series in (benchmarks or {}).items():
        b_ret, b_dd = index_window(series, start, end)
        b_pa = b_ret * sessions_per_year / period if ann and b_ret is not None else None
        bench.append((name, b_pa, b_dd))
    return CapitalTime(
        account=account,
        period_sessions=period,
        trades_with_capital=len(caps),
        trades_without_capital=missing,
        avg_capital=sum(caps) / len(caps) if caps else None,
        capital_sessions=cap_sessions,
        utilisation=util,
        net=net,
        deployed_return_pa=deployed,
        account_return_pa=account_pa,
        max_drawdown_inr=dd,
        nifty_return_pa=n_ret * sessions_per_year / period if ann and n_ret is not None else None,
        nifty_drawdown=n_dd,
        benchmarks=tuple(bench),
    )


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f}%"


def render_capital_time(
    rows: Sequence[CapitalTime], *, risk_free_pa: float, min_sessions_annualise: int
) -> str:
    lines = [
        "## Capital and time (vs Nifty over the same dates)",
        "",
        f"Return per unit of capital and time. Idle capital is credited the T-bill rate "
        f"({risk_free_pa * 100:.2f}% a year). Annualised figures appear after "
        f"{min_sessions_annualise} sessions. No tax: gains are within the owner's basic exemption.",
        "",
    ]
    names = [b[0] for b in rows[0].benchmarks] if rows else []
    extra_h = "".join(f" {n} p.a. |" for n in names)
    lines += [
        "| Account | Sessions | Avg capital ₹ | Utilisation | Deployed return p.a. "
        f"| Account return p.a. | Nifty p.a. |{extra_h} Max drawdown ₹ | Nifty drawdown |",
        "|---|---|---|---|---|---|---|" + "---|" * len(names) + "---|---|",
    ]
    for c in rows:
        avg = "—" if c.avg_capital is None else f"{c.avg_capital:,.0f}"
        note = (
            f" ({c.trades_without_capital} trades with no capital recorded)"
            if (c.trades_without_capital)
            else ""
        )
        lines.append(
            f"| {c.account}{note} | {c.period_sessions} | {avg} | {_pct(c.utilisation)} | "
            f"{_pct(c.deployed_return_pa)} | {_pct(c.account_return_pa)} | "
            f"{_pct(c.nifty_return_pa)} |"
            + "".join(f" {_pct(b[1])} |" for b in c.benchmarks)
            + f" {c.max_drawdown_inr:,.0f} | {_pct(c.nifty_drawdown)} |"
        )
    if rows and rows[-1].benchmarks:
        last = rows[-1]
        dds = "; ".join(f"{n} {_pct(dd)}" for n, _, dd in last.benchmarks)
        lines += [
            "",
            f"Worst fall over the last row's dates: Nifty {_pct(last.nifty_drawdown)}; {dds}. "
            "Benchmarks are index levels (no costs, no dividends); the 50/50 mix is rebalanced "
            "on the first session of each year.",
        ]
    return "\n".join(lines) + "\n"


def index_closes_from_eod(
    texts: Iterable[str], names: Sequence[str]
) -> dict[str, list[tuple[date, float]]]:
    """``name -> [(date, close)]`` from NSE's daily ``ind_close_all`` files (one text each;
    the same index can appear twice in a file)."""
    want = {n.lower(): n for n in names}
    out: dict[str, dict[date, float]] = {n: {} for n in names}
    for text in texts:
        for line in text.splitlines():
            cells = line.split(",")
            if len(cells) < 6 or cells[0].strip().lower() not in want:
                continue
            try:
                d = datetime.strptime(cells[1].strip(), "%d-%m-%Y").date()  # noqa: DTZ007
                out[want[cells[0].strip().lower()]][d] = float(cells[5])
            except ValueError:
                continue
    return {n: sorted(v.items()) for n, v in out.items()}


def blend_closes(
    a: Sequence[tuple[date, float]], b: Sequence[tuple[date, float]], *, weight_a: float = 0.5
) -> list[tuple[date, float]]:
    """A level series for ``weight_a`` of ``a`` and the rest in ``b``, starting at 100 on
    their first common date and rebalanced on the first common date of each calendar year."""
    da, db = dict(a), dict(b)
    out: list[tuple[date, float]] = []
    units_a = units_b = 0.0
    year: int | None = None
    value = 100.0
    for d in sorted(set(da) & set(db)):
        if out:
            value = units_a * da[d] + units_b * db[d]
        if year != d.year:  # the series' first session, or a new year's
            year = d.year
            units_a = weight_a * value / da[d]
            units_b = (1 - weight_a) * value / db[d]
        out.append((d, value))
    return out


__all__ = [
    "CapitalTime",
    "blend_closes",
    "capital_time",
    "capital_used",
    "index_closes_from_eod",
    "index_window",
    "render_capital_time",
    "sessions_between",
]
