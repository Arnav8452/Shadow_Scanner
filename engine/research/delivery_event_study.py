"""Event study: do EOD delivery spikes predict the next 1/3/5 sessions? (owner request, 12 Nov 2026)

Question: after a stock's delivery spike on day ``T`` (classified as in
:func:`engine.data.eod.bhavcopy.scan_relative_delivery_spikes`), is the return from the open of
``T+1`` to the close of ``T+h`` better, in the bias's direction, than on non-spike days — after
costs?

Point in time and survivorship:
- the classification uses only ``T`` and the 20 sessions before it (the file is known at 19:30
  on ``T``); entry is the next session's open, never ``T``'s close;
- the universe on ``T`` is the stocks with stock futures in ``T``'s F&O bhavcopy (F&O
  membership as of that day, not today's list).

Costs: a delivery round trip (``equity_delivery_charges``, buy + sell, the configured broker) on
a notional, plus a slippage assumption per side (HYPOTHESIS), rerun at 2x slippage.
``RETAIL_CHURN_FADE`` and ``DISTRIBUTION_SHORT`` are short-direction: retail cannot hold cash
shorts overnight, so their numbers only say whether the *direction* would be worth expressing
with options, and the cash-cost figure is a proxy.

Every threshold combination is a trial; the deflated Sharpe ratio uses the total count. Trades
entered the same day are averaged into one daily return (one observation per entry day); with
h > 1 consecutive observations still overlap, so treat t-stats as optimistic.
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from engine.backtest.costs import equity_delivery_charges
from engine.backtest.deflated_sharpe import compute_deflated_sharpe
from engine.core.rules import RuleBook
from engine.data.eod.bhavcopy import CashBhavcopyRow, parse_cash_bhavcopy_csv
from engine.data.eod.delivery_watch import STOCK_FUTURES
from engine.data.eod.download import eod_path, read_eod_text, weekdays

LONG_BIAS = "LONG_ACCUMULATION"
CHURN = "RETAIL_CHURN_FADE"
DISTRIBUTION = "DISTRIBUTION_SHORT"
DIRECTION = {LONG_BIAS: 1, CHURN: -1, DISTRIBUTION: -1}


@dataclass(frozen=True, slots=True)
class DayFeatures:
    """What the scanner looks at on ``T`` for one stock, and what happened next."""

    symbol: str
    day: date
    qty_mult: float  # delivery qty / 20-session average
    pct_vs_avg: float  # delivery % / its 20-session average
    ret_pct: float  # close-to-close on T
    high_pct: float  # high vs previous close on T
    fwd: dict[int, float]  # h -> open(T+1) to close(T+h), %


@dataclass(frozen=True)
class Trial:
    kind: str  # accumulation (LONG + DISTRIBUTION) | churn
    min_qty_mult: float = 2.0
    min_ret_pct: float = 1.0
    min_spike_pct: float = 5.0
    max_rel_delivery: float = 0.75

    def classify(self, f: DayFeatures) -> str | None:
        """Same rules as ``scan_relative_delivery_spikes`` with this trial's thresholds."""
        if self.kind == "accumulation":
            if f.qty_mult >= self.min_qty_mult and f.pct_vs_avg >= 1.0:
                if f.ret_pct >= self.min_ret_pct:
                    return LONG_BIAS
                if f.ret_pct <= -self.min_ret_pct:
                    return DISTRIBUTION
            return None
        spike = f.ret_pct >= self.min_spike_pct or f.high_pct >= self.min_spike_pct * 1.4
        return CHURN if spike and f.pct_vs_avg < self.max_rel_delivery else None

    def label(self) -> str:
        if self.kind == "accumulation":
            return f"accum qty>={self.min_qty_mult:g}x ret>={self.min_ret_pct:g}%"
        return f"churn up>={self.min_spike_pct:g}% deliv<{self.max_rel_delivery:g}x avg"


DEFAULT_GRID: tuple[Trial, ...] = tuple(
    [
        Trial("accumulation", min_qty_mult=q, min_ret_pct=r)
        for q, r in itertools.product((1.5, 2.0, 3.0), (0.5, 1.0, 2.0))
    ]
    + [
        Trial("churn", min_spike_pct=s, max_rel_delivery=d)
        for s, d in itertools.product((3.0, 5.0, 7.0), (0.6, 0.75))
    ]
)


def load_history(
    raw_dir: Path, start: date, end: date
) -> tuple[dict[str, list[CashBhavcopyRow]], dict[date, set[str]]]:
    """Delivery rows by symbol (sorted) and the F&O stock universe per day (from that day's
    F&O bhavcopy; days without one are skipped)."""
    from engine.data.eod.bhavcopy import parse_fno_bhavcopy_csv

    rows: dict[str, list[CashBhavcopyRow]] = {}
    fno: dict[date, set[str]] = {}
    for d in weekdays(start, end):
        p = eod_path(raw_dir, "delivery", d)
        if not p.exists():
            continue
        for r in parse_cash_bhavcopy_csv(read_eod_text(p)):
            if r.trade_date == d and r.series == "EQ":
                rows.setdefault(r.symbol, []).append(r)
        fo = eod_path(raw_dir, "fo", d)
        if fo.exists():
            fno[d] = {
                r.symbol
                for r in parse_fno_bhavcopy_csv(read_eod_text(fo))
                if r.instrument in STOCK_FUTURES
            }
    for v in rows.values():
        v.sort(key=lambda r: r.trade_date)
    return rows, fno


def features(
    rows: dict[str, list[CashBhavcopyRow]],
    fno: dict[date, set[str]],
    *,
    lookback: int = 20,
    horizons: Sequence[int] = (1, 3, 5),
) -> list[DayFeatures]:
    out: list[DayFeatures] = []
    hmax = max(horizons)
    for sym, series in rows.items():
        for i in range(lookback, len(series) - hmax):
            t = series[i]
            if sym not in fno.get(t.trade_date, set()):
                continue
            prior = series[i - lookback : i]
            avg_q = sum(r.delivery_qty for r in prior) / lookback
            avg_p = sum(r.delivery_pct for r in prior) / lookback
            if avg_q <= 0 or avg_p <= 0 or t.prev_close <= 0:
                continue
            entry = series[i + 1].open
            if entry <= 0:
                continue
            fwd = {h: (series[i + h].close / entry - 1.0) * 100.0 for h in horizons}
            out.append(
                DayFeatures(
                    symbol=sym,
                    day=t.trade_date,
                    qty_mult=t.delivery_qty / avg_q,
                    pct_vs_avg=t.delivery_pct / avg_p,
                    ret_pct=t.close_return_pct,
                    high_pct=(t.high - t.prev_close) / t.prev_close * 100.0,
                    fwd=fwd,
                )
            )
    return out


def round_trip_cost_pct(book: RuleBook, on: date, *, notional: float, broker: str) -> float:
    buy = equity_delivery_charges(
        book, on, side="buy", value_inr=notional, broker=broker, allow_unverified=True
    )
    sell = equity_delivery_charges(
        book, on, side="sell", value_inr=notional, broker=broker, allow_unverified=True
    )
    return (buy.total + sell.total) / notional * 100.0


@dataclass
class CellResult:
    trial: str
    bias: str
    horizon: int
    n_trades: int
    n_days: int
    mean_gross_pct: float
    mean_net_pct: float
    mean_net_2x_pct: float
    baseline_pct: float  # non-spike stock-days, same direction, gross
    excess_gross_pct: float
    t_stat_excess: float
    win_pct: float
    sharpe_annual: float
    deflated_sharpe_pct: float
    passed: bool
    notes: list[str] = field(default_factory=list)


def trial_count(grid: Sequence[Trial], horizons: Sequence[int]) -> int:
    return sum(2 if t.kind == "accumulation" else 1 for t in grid) * len(horizons)


def _t(x: Sequence[float], mu0: float) -> float:
    if len(x) < 3:
        return 0.0
    sd = statistics.stdev(x)
    return 0.0 if sd <= 0 else (statistics.fmean(x) - mu0) / (sd / math.sqrt(len(x)))


def run_study(
    feats: Sequence[DayFeatures],
    *,
    cost_pct: float,
    slippage_pct_per_side: float,
    grid: Sequence[Trial] = DEFAULT_GRID,
    horizons: Sequence[int] = (1, 3, 5),
) -> list[CellResult]:
    # Every cell examined is a trial: accumulation settings yield two biases (long and
    # distribution), churn settings one, each at every horizon.
    n_trials = trial_count(grid, horizons)
    results: list[CellResult] = []
    span_days = (max(f.day for f in feats) - min(f.day for f in feats)).days if feats else 0
    years = max(span_days / 365.25, 1e-9)
    for trial in grid:
        labels = {id(f): trial.classify(f) for f in feats}
        for h in horizons:
            base_long = [f.fwd[h] for f in feats if labels[id(f)] is None]
            base = statistics.fmean(base_long) if base_long else 0.0
            for bias in (LONG_BIAS, DISTRIBUTION, CHURN):
                sel = [f for f in feats if labels[id(f)] == bias]
                if not sel:
                    continue
                sign = DIRECTION[bias]
                gross = [sign * f.fwd[h] for f in sel]
                slip = 2 * slippage_pct_per_side
                net = [g - cost_pct - slip for g in gross]
                net2 = [g - cost_pct - 2 * slip for g in gross]
                by_day: dict[date, list[float]] = {}
                for f, x in zip(sel, net, strict=True):
                    by_day.setdefault(f.day, []).append(x)
                daily = [statistics.fmean(v) for _, v in sorted(by_day.items())]
                dsr = compute_deflated_sharpe(
                    daily, n_trials=n_trials, periods_per_year=max(1.0, len(daily) / years)
                )
                results.append(
                    CellResult(
                        trial=trial.label(),
                        bias=bias,
                        horizon=h,
                        n_trades=len(sel),
                        n_days=len(daily),
                        mean_gross_pct=round(statistics.fmean(gross), 3),
                        mean_net_pct=round(statistics.fmean(net), 3),
                        mean_net_2x_pct=round(statistics.fmean(net2), 3),
                        baseline_pct=round(sign * base, 3),
                        excess_gross_pct=round(statistics.fmean(gross) - sign * base, 3),
                        t_stat_excess=round(_t(gross, sign * base), 2),
                        win_pct=round(100.0 * sum(x > 0 for x in net) / len(net), 1),
                        sharpe_annual=dsr.observed_sharpe_annual,
                        deflated_sharpe_pct=dsr.deflated_sharpe_pct,
                        passed=dsr.passed and abs(_t(gross, sign * base)) >= 3.0,
                        notes=["short direction: options only"] if sign < 0 else [],
                    )
                )
    return results


def render_markdown(results: Iterable[CellResult], *, meta: dict[str, Any], top: int = 40) -> str:
    res = sorted(results, key=lambda r: (-r.deflated_sharpe_pct, -r.mean_net_pct))
    lines = [
        "# Delivery-spike event study",
        "",
        "HYPOTHESIS test, not a strategy. " + str(meta.get("summary", "")),
        "",
        "| Setting | Value |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in meta.items() if k != "summary"],
        "",
        "Pass = deflated Sharpe >= 90% with positive Sharpe AND |t| of the excess >= 3 "
        "(CLAUDE.md). Short-direction rows are for option expression only.",
        "",
        "| Trial | Bias | h | Trades | Days | Gross % | Net % | Net 2x slip % | Baseline % "
        "| Excess % | t | Win % | Sharpe | DSR % | Pass |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in res[:top]:
        lines.append(
            f"| {r.trial} | {r.bias} | {r.horizon} | {r.n_trades} | {r.n_days} | "
            f"{r.mean_gross_pct} | {r.mean_net_pct} | {r.mean_net_2x_pct} | {r.baseline_pct} | "
            f"{r.excess_gross_pct} | {r.t_stat_excess} | {r.win_pct} | {r.sharpe_annual} | "
            f"{r.deflated_sharpe_pct} | {'yes' if r.passed else 'no'} |"
        )
    return "\n".join(lines) + "\n"


def write_outputs(out_dir: Path, results: list[CellResult], meta: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "delivery_event_study.md"
    md.write_text(render_markdown(results, meta=meta), encoding="utf-8")
    (out_dir / "delivery_event_study.json").write_text(
        json.dumps({"meta": meta, "results": [asdict(r) for r in results]}, indent=1, default=str),
        encoding="utf-8",
    )
    return md
