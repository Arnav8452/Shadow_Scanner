"""Run a candle backtest in three scenarios, then score it honestly.

- **base:** the conservative haircut, ``max(₹1.50, 1.5%)`` per leg per side, charges as is;
- **stress:** 2x that haircut AND every order's charges doubled (SPEC 8 2x slippage, plus a
  margin for charges the rule tables miss);
- **plan:** the owner's plan haircut ``max(₹1, 0.75%)``, charges as is (for comparison only).

- **Daily P&L** over every session in the range (0 on days without a trade). A structure held
  overnight is marked to market at each session's last bar (closing at the haircut, exit
  charges excluded), so drawdowns include open losses.
- **Walk-forward** (``evaluate_walk_forward``): rolling 60-session in-sample / 20-session
  out-of-sample folds. Parameters are fixed from settings, not fitted per fold, so this
  measures stability across time, not a fitting step.
- **Monte Carlo** (``run_block_bootstrap_mc``): 5-day blocks of daily P&L, drawdown percentiles
  and the chance of a 20%-of-capital drawdown.
- **Deflated Sharpe** (``compute_deflated_sharpe``) with ``n_trials`` = every distinct parameter
  set ever run for this strategy, from the trial registry (``data/backtest/candle_trials.jsonl``).
  Date ranges and the haircut stress are not trials; a changed rule or number is.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from engine.backtest.candles.strategies import (
    COMMON_DEVIATIONS,
    CandleTrade,
    Ctx,
    SimOut,
    StrategySpec,
)
from engine.backtest.deflated_sharpe import DeflatedSharpeResult, compute_deflated_sharpe
from engine.backtest.montecarlo import MonteCarloSummary, run_block_bootstrap_mc
from engine.backtest.walkforward import WalkForwardSummary, evaluate_walk_forward
from engine.core.rules import IST


def params_hash(strategy: str, params: Any) -> tuple[str, dict[str, Any]]:
    body = asdict(params) if is_dataclass(params) and not isinstance(params, type) else dict(params)
    blob = json.dumps({"strategy": strategy, "params": body}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12], body


def register_trial(path: Path, strategy: str, params: Any, now: datetime) -> tuple[str, int]:
    """Record this parameter set; return (its hash, distinct sets ever run for ``strategy``)."""
    h, body = params_hash(strategy, params)
    rows: list[dict[str, Any]] = []
    if path.exists():
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not any(r.get("strategy") == strategy and r.get("hash") == h for r in rows):
        rows.append({"strategy": strategy, "hash": h, "first_run": now.isoformat(), "params": body})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")
    n = len({r["hash"] for r in rows if r.get("strategy") == strategy})
    return h, n


def daily_pnl(trades: Sequence[CandleTrade], sessions: Sequence[date]) -> list[float]:
    """Net P&L per session: MTM changes while open, the rest on the exit day."""
    pnl = dict.fromkeys(sessions, 0.0)
    for tr in trades:
        prev = 0.0
        exit_day = tr.exit_day
        for d in sorted(tr.marks):
            if d >= exit_day:
                break
            pnl[d] = pnl.get(d, 0.0) + tr.marks[d] - prev
            prev = tr.marks[d]
        pnl[exit_day] = pnl.get(exit_day, 0.0) + tr.net - prev
    return [pnl[d] for d in sorted(pnl)]


def _max_dd(xs: Sequence[float]) -> float:
    cum = peak = mdd = 0.0
    for x in xs:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


@dataclass
class Summary:
    trades: int
    wins: int
    win_rate_pct: float
    avg_win: float
    avg_loss: float
    profit_factor: float | None
    gross: float
    charges: float
    haircut_paid: float
    net: float
    max_dd: float
    sharpe: float
    t_stat: float
    best: float
    worst: float
    avg_margin: float | None
    flagged_trades: int
    stale_marks: int
    breakeven_win_pct: float | None = None  # mean of each trade's planned break-even win rate
    tp_on_margin_pct: float | None = None  # mean net take-profit / margin
    realized_breakeven_pct: float | None = None  # |avg loss| / (avg win + |avg loss|)


def _mean(xs: Sequence[float | None]) -> float | None:
    got = [x for x in xs if x is not None]
    return statistics.fmean(got) if got else None


def summarize(trades: Sequence[CandleTrade], daily: Sequence[float]) -> Summary:
    nets = [t.net for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    sd = statistics.stdev(daily) if len(daily) > 1 else 0.0
    mean = statistics.fmean(daily) if daily else 0.0
    margins = [t.margin_inr for t in trades if t.margin_inr]
    eco = [t.context.get("economics") or {} for t in trades]
    aw = statistics.fmean(wins) if wins else 0.0
    al = statistics.fmean(losses) if losses else 0.0
    return Summary(
        trades=len(trades),
        wins=len(wins),
        win_rate_pct=100.0 * len(wins) / len(trades) if trades else 0.0,
        avg_win=statistics.fmean(wins) if wins else 0.0,
        avg_loss=statistics.fmean(losses) if losses else 0.0,
        profit_factor=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        gross=sum(t.gross for t in trades),
        charges=sum(t.charges for t in trades),
        haircut_paid=sum(t.haircut_inr for t in trades),
        net=sum(nets),
        max_dd=_max_dd(daily),
        sharpe=(mean / sd * math.sqrt(252.0)) if sd > 1e-9 else 0.0,
        t_stat=(mean / sd * math.sqrt(len(daily))) if sd > 1e-9 else 0.0,
        best=max(nets, default=0.0),
        worst=min(nets, default=0.0),
        avg_margin=statistics.fmean(margins) if margins else None,
        flagged_trades=sum(1 for t in trades if t.flags),
        stale_marks=sum(int(t.context.get("stale_marks", 0)) for t in trades),
        breakeven_win_pct=_mean([e.get("breakeven_win_pct") for e in eco]),
        tp_on_margin_pct=_mean([e.get("tp_on_margin_pct") for e in eco]),
        realized_breakeven_pct=(-al / (aw - al) * 100) if wins and losses and aw - al > 0 else None,
    )


@dataclass
class RunResult:
    spec: StrategySpec
    start: date
    end: date
    params: dict[str, Any]
    params_hash: str
    n_trials: int
    capital_inr: float
    haircut_desc: str
    stress_mult: float
    costs_desc: str
    base: SimOut
    stress: SimOut
    daily: list[float]
    daily_stress: list[float]
    s1: Summary
    s2: Summary
    plan: SimOut | None
    s3: Summary | None
    stress_desc: str
    plan_desc: str
    label: str
    wf: WalkForwardSummary
    mc: MonteCarloSummary
    dsr: DeflatedSharpeResult
    ruin_pct: float
    notes: list[str] = field(default_factory=list)
    generated: datetime = field(default_factory=lambda: datetime.now(IST))


def run_candle_backtest(
    spec: StrategySpec,
    params: Any,
    make_ctx: Callable[[str], Ctx],
    sessions: Sequence[date],
    *,
    trials_path: Path,
    also_trials: Sequence[Any] = (),
    label: str = "",
    with_plan: bool = True,
    wf_train: int = 60,
    wf_test: int = 20,
    mc_paths: int = 1000,
    mc_block: int = 5,
    ruin_pct: float = 20.0,
    now: datetime | None = None,
) -> RunResult:
    now = now or datetime.now(IST)
    if not sessions:
        raise ValueError("no sessions with index candles in the range")
    for other in also_trials:  # every variant compared in this run counts, before scoring
        register_trial(trials_path, spec.key, other, now)
    h, n_trials = register_trial(trials_path, spec.key, params, now)
    _, body = params_hash(spec.key, params)
    ctx1 = make_ctx("base")
    base = spec.run(ctx1, sessions, params)
    ctx2 = make_ctx("stress")
    stress = spec.run(ctx2, sessions, params)
    ctx3 = make_ctx("plan") if with_plan else None
    plan = spec.run(ctx3, sessions, params) if ctx3 is not None else None
    d1 = daily_pnl(base.trades, sessions)
    d2 = daily_pnl(stress.trades, sessions)
    d3 = daily_pnl(plan.trades, sessions) if plan is not None else []
    cap = ctx1.capital_inr
    rets = [x / cap for x in d1]
    hc = ctx1.haircut
    costs_on = (
        f"the run date {ctx1.run_date}" if ctx1.costs.costs_on == "run_date" else "each trade date"
    )
    notes = []
    errs = list(getattr(ctx1.market.src, "errors", []))
    if errs:
        notes.append(f"{len(errs)} contract download(s) failed and were treated as no data")
    if ctx1.costs.unverified:
        notes.append(
            "UNVERIFIED rule rows used in costs: " + ", ".join(sorted(ctx1.costs.unverified))
        )
    if ctx1.costs.fallbacks:
        notes.append(f"{ctx1.costs.fallbacks} order(s) priced with the fallback broker's brokerage")
    return RunResult(
        spec=spec,
        start=sessions[0],
        end=sessions[-1],
        params=body,
        params_hash=h,
        n_trials=n_trials,
        capital_inr=cap,
        haircut_desc=f"max(₹{hc.min_inr:g}, {hc.pct:g}% of price) per leg per side",
        stress_mult=ctx2.haircut.mult,
        stress_desc=(f"{ctx2.haircut.mult:g}x haircut, charges x{ctx2.costs.charges_mult:g}"),
        plan_desc=(
            f"plan haircut max(₹{ctx3.haircut.min_inr:g}, {ctx3.haircut.pct:g}%)"
            if ctx3 is not None
            else ""
        ),
        plan=plan,
        s3=summarize(plan.trades, d3) if plan is not None else None,
        label=label,
        costs_desc=(f"config/rules as of {costs_on}, brokerage {ctx1.costs.broker}"),
        base=base,
        stress=stress,
        daily=d1,
        daily_stress=d2,
        s1=summarize(base.trades, d1),
        s2=summarize(stress.trades, d2),
        wf=evaluate_walk_forward(rets, train_size=wf_train, test_size=wf_test),
        mc=run_block_bootstrap_mc(
            d1, n_paths=mc_paths, block_size=mc_block, ruin_threshold_inr=cap * ruin_pct / 100
        ),
        dsr=compute_deflated_sharpe(rets, n_trials=n_trials),
        ruin_pct=ruin_pct,
        notes=notes,
        generated=now,
    )


# ------------------------------------------------------------------------------- report


def _inr(x: float | None) -> str:
    if x is None:
        return "—"
    return f"₹{x:,.0f}" if x >= 0 else f"−₹{-x:,.0f}"


def trades_csv(trades: Sequence[CandleTrade]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(
        ["entry", "exit", "structure", "expiry", "legs", "credit_pts", "gross", "charges",
         "haircut_paid", "net", "reason_in", "reason_out", "flags"]
    )  # fmt: skip
    for t in trades:
        legs = " ".join(
            f"{lg.side[0]}{lg.label}@{lg.entry_px:.2f}->{lg.exit_px:.2f}x{lg.qty}" for lg in t.legs
        )
        w.writerow(
            [
                t.entry_at.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
                (t.exit_at or t.entry_at).astimezone(IST).strftime("%Y-%m-%d %H:%M"),
                t.structure,
                t.expiry.isoformat(),
                legs,
                f"{t.credit_pts:.2f}",
                f"{t.gross:.2f}",
                f"{t.charges:.2f}",
                f"{t.haircut_inr:.2f}",
                f"{t.net:.2f}",
                t.reason_in,
                t.reason_out,
                "; ".join(t.flags),
            ]
        )
    return buf.getvalue()


def _row(name: str, *cells: str) -> str:
    return f"| {name} | " + " | ".join(cells) + " |"


def _pct(x: float | None, digits: int = 0) -> str:
    return f"{x:.{digits}f}%" if x is not None else "—"


def _pf(x: float | None) -> str:
    return f"{x:.2f}" if x is not None else "—"


BANNER = (
    "> **CANDLE APPROXIMATION, not a recorded-depth replay.** Built from Upstox 1-minute "
    "candles (last trades). No bid/ask history exists before Nov 2026, so every fill is the "
    "next bar's open with a punitive half-spread ({haircut}), and the whole run is repeated with "
    "{stress}. Results are an upper bound on what the rule's prices allow, not evidence of "
    "profit; assume live is about half of this."
)


def render_variants(results: Sequence[RunResult]) -> str:
    """One row per variant: the comparison the deflated Sharpe's trial count is about."""
    r0 = results[0]
    L = [
        f"# Candle backtest — {r0.spec.title}, {len(results)} variants: {r0.start} to {r0.end}",
        "",
        BANNER.format(haircut=r0.haircut_desc, stress=r0.stress_desc),
        "",
        f"Every variant below is a trial: the deflated Sharpe uses **{r0.n_trials}** trials "
        f"for `{r0.spec.key}`. Net is after every order's charges. Break-even win rate = net "
        "stop / (net take-profit + net stop) on each trade's own credit and charges (mean over "
        "trades); take-profit on margin uses the `margin_rules.yaml` table.",
        "",
        "| Variant | Trades | Win rate | Break-even win rate | TP on margin | Net (1x) "
        f"| Net ({r0.stress_desc}) | Net ({r0.plan_desc or 'plan haircut'}) | t | DSR |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        s3 = r.s3
        L.append(
            _row(
                r.label or r.params_hash,
                str(r.s1.trades),
                _pct(r.s1.win_rate_pct),
                _pct(r.s1.breakeven_win_pct),
                _pct(r.s1.tp_on_margin_pct, 2),
                _inr(r.s1.net),
                f"{_inr(r.s2.net)} ({r.s2.trades} trades)",
                f"{_inr(s3.net)} ({s3.trades} trades)" if s3 is not None else "—",
                f"{r.s1.t_stat:.2f}",
                _pct(r.dsr.deflated_sharpe_pct, 1),
            )
        )
    L.append("")
    L.append(
        "A variant earns more than its costs only if its win rate is above its break-even win "
        "rate by a margin the sample can resolve; with t below about 3 none of these is proven. "
        "The stress and plan columns re-run the whole rule, so their trade counts differ: a "
        "wider haircut fails the credit filter more often, and a handful of trades proves nothing."
    )
    L.append("")
    return "\n".join(L)


def render_report(res: RunResult, csv_name: str = "", max_trades: int = 40) -> str:
    s1, s2, s3 = res.s1, res.s2, res.s3
    head = [f"1x ({res.haircut_desc.split(' per')[0]})", res.stress_desc]
    sums = [s1, s2]
    if s3 is not None:
        head.append(res.plan_desc)
        sums.append(s3)
    L: list[str] = []
    title = f" ({res.label})" if res.label else ""
    L.append(f"# Candle backtest — {res.spec.title}{title}: {res.start} to {res.end}")
    L.append("")
    L.append(BANNER.format(haircut=res.haircut_desc, stress=res.stress_desc))
    L.append("")
    L.append("## Run")
    L.append("")
    L.append(f"- Strategy: `{res.spec.key}` ({res.spec.ref}); parameters hash `{res.params_hash}`")
    L.append(f"- Sessions: {len(res.base.sessions)} ({res.start} to {res.end})")
    L.append(f"- Capital: {_inr(res.capital_inr)}; one lot per trade")
    L.append(f"- Costs: {res.costs_desc}; margin from `margin_rules.yaml` (13 Nov 2026 quotes)")
    L.append(
        f"- Trials counted for the deflated Sharpe: **{res.n_trials}** distinct parameter set(s) "
        "run for this strategy so far"
    )
    if res.base.iv_days:
        L.append(
            f"- IV-percentile warm-up: {res.base.iv_days} sessions of closing ATM IV "
            "before the range"
        )
    L.append(f"- Generated {res.generated:%Y-%m-%d %H:%M} IST")
    L.append("")
    L.append("## Results")
    L.append("")
    L.append("| | " + " | ".join(head) + " |")
    L.append("|---|" + "---:|" * len(head))
    L.append(_row("Trades", *(str(s.trades) for s in sums)))
    L.append(_row("Win rate", *(_pct(s.win_rate_pct) for s in sums)))
    L.append(_row("Break-even win rate (planned)", *(_pct(s.breakeven_win_pct) for s in sums)))
    L.append(
        _row("Break-even win rate (realized)", *(_pct(s.realized_breakeven_pct) for s in sums))
    )
    L.append(_row("Take-profit on margin", *(_pct(s.tp_on_margin_pct, 2) for s in sums)))
    L.append(_row("Average win / loss", *(f"{_inr(s.avg_win)} / {_inr(s.avg_loss)}" for s in sums)))
    L.append(_row("Profit factor", *(_pf(s.profit_factor) for s in sums)))
    L.append(_row("Gross P&L", *(_inr(s.gross) for s in sums)))
    L.append(_row("Charges", *(_inr(s.charges) for s in sums)))
    L.append(_row("Haircut paid (in gross)", *(_inr(s.haircut_paid) for s in sums)))
    L.append(
        _row(
            "Gross with the haircut added back (same trades)",
            *(_inr(s.gross + s.haircut_paid) for s in sums),
        )
    )
    L.append(_row("**Net P&L**", *(f"**{_inr(s.net)}**" for s in sums)))
    L.append(_row("Max drawdown (daily, MTM)", *(_inr(s.max_dd) for s in sums)))
    L.append(_row("Sharpe (daily, annualised)", *(f"{s.sharpe:.2f}" for s in sums)))
    L.append(_row("t-stat of mean daily P&L", *(f"{s.t_stat:.2f}" for s in sums)))
    L.append(_row("Best / worst trade", *(f"{_inr(s.best)} / {_inr(s.worst)}" for s in sums)))
    if s1.avg_margin:
        L.append(_row("Margin per trade (estimate)", *(_inr(s.avg_margin) for s in sums)))
    L.append("")
    L.append(f"Live is assumed to be about half of the 1x net: {_inr(s1.net / 2)}. ")
    L.append(
        "A new signal is treated as unproven unless t is about 3; "
        f"here t = {s1.t_stat:.2f} (1x), {s2.t_stat:.2f} ({res.stress_desc})."
    )
    L.append("")
    L.append("## Validation (1x haircut, daily returns on capital)")
    L.append("")
    wf = res.wf
    L.append(
        f"- Walk-forward: {len(wf.folds)} folds; mean in-sample Sharpe {wf.mean_is_sharpe:.2f}, "
        f"out-of-sample {wf.mean_oos_sharpe:.2f}, decay {wf.sharpe_decay_pct:.0f}% -> "
        f"{'passes' if wf.passed else 'fails'} the 40% decay gate. Parameters are fixed from "
        "settings, not refitted per fold."
    )
    mc = res.mc
    L.append(
        f"- Monte Carlo ({mc.n_paths} paths, 5-day blocks): max drawdown median "
        f"{_inr(mc.median_max_drawdown_inr)}, 95th {_inr(mc.p95_max_drawdown_inr)}, 99th "
        f"{_inr(mc.p99_max_drawdown_inr)}; P(drawdown >= {res.ruin_pct:g}% of capital) = "
        f"{mc.prob_ruin_pct:.1f}%"
    )
    ds = res.dsr
    L.append(
        f"- Deflated Sharpe: observed {ds.observed_sharpe_annual:.2f}, benchmark for "
        f"{ds.n_trials} trial(s) {ds.benchmark_sharpe_annual:.2f}; "
        f"PSR {ds.probabilistic_sharpe_pct:.1f}%, "
        f"DSR {ds.deflated_sharpe_pct:.1f}% over {ds.n_observations} sessions "
        f"(skew {ds.skewness:.2f}, kurtosis {ds.kurtosis:.1f})"
    )
    L.append("")
    L.append("## How this differs from the live rule")
    L.append("")
    for x in (*res.spec.deviations, *COMMON_DEVIATIONS):
        L.append(f"- {x}")
    L.append("")
    L.append("## Signals not traded (sessions, 1x run)")
    L.append("")
    sk: Counter[str] = res.base.skips
    if sk:
        for why, n in sk.most_common():
            L.append(f"- {why}: {n}")
    else:
        L.append("- none")
    L.append("")
    L.append("## Exits (1x run)")
    L.append("")
    exits = Counter(t.reason_out.split(" (")[0] for t in res.base.trades)
    for why, n in exits.most_common():
        L.append(f"- {why}: {n}")
    L.append("")
    L.append("## Data quality")
    L.append("")
    L.append(
        f"- Trades with flags: {s1.flagged_trades}; "
        f"stale leg marks (> 15 min old): {s1.stale_marks}"
    )
    for note in res.notes:
        L.append(f"- {note}")
    flags = Counter(
        f.split(":")[0] if "no bar after" in f else f for t in res.base.trades for f in t.flags
    )
    for f, n in flags.most_common(8):
        L.append(f"- {f}: {n}")
    L.append("")
    L.append("## Trades (1x)")
    L.append("")
    if csv_name:
        L.append(f"All trades: `{csv_name}`.")
        L.append("")
    L.append("| Entry | Exit | Structure | Net | Exit reason |")
    L.append("|---|---|---|---:|---|")
    for t in res.base.trades[-max_trades:]:
        L.append(
            f"| {t.entry_at.astimezone(IST):%Y-%m-%d %H:%M} | "
            f"{(t.exit_at or t.entry_at).astimezone(IST):%Y-%m-%d %H:%M} | {t.structure} | "
            f"{_inr(t.net)} | {t.reason_out} |"
        )
    if len(res.base.trades) > max_trades:
        L.append("")
        L.append(f"(last {max_trades} of {len(res.base.trades)} shown)")
    L.append("")
    return "\n".join(L)


__all__ = [
    "RunResult",
    "Summary",
    "daily_pnl",
    "params_hash",
    "register_trial",
    "render_report",
    "render_variants",
    "run_candle_backtest",
    "summarize",
    "trades_csv",
]
