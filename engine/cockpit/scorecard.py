"""Promotion scorecard: the Stage-C gate of the testing plan, applied to every paper account's
whole journal (all sessions so far). Nothing here is a profit claim; it says which strategies
have earned a closer look and which should be pruned.

Per account:

- sessions with a closed trade, trades, net P&L after charges, and net with **charges doubled**
  (fills are already at the recorded bid/ask, so doubling the charges is the cost stress);
- **t-statistic** of the per-trade net (mean / standard error);
- **deflated Sharpe** of the daily net, deflated by the number of strategies being tried at
  once (every account is a trial; ``extra_trials`` adds variants tested elsewhere);
- the **kill rules** (``engine/risk/kill_rules.py``).

Verdicts:
- ``collecting``: under ``min_sessions`` sessions or ``min_trades`` trades;
- ``kill rule``: a kill rule fired;
- ``prune``: enough sessions and net <= 0, or <= 0 with charges doubled;
- ``candidate``: t >= ``t_min``, positive with charges doubled, deflated Sharpe passed: ask the
  owner, write kill rules, 1 lot live only in Mode B;
- ``unproven``: everything else.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.backtest.deflated_sharpe import compute_deflated_sharpe
from engine.cockpit.research import _accounts, _jsonl
from engine.risk.kill_rules import KillRules, evaluate_kill_rules


@dataclass(frozen=True)
class Score:
    account: str
    sessions: int
    trades: int
    net: float
    net_2x_charges: float
    t_stat: float | None
    dsr_pct: float | None
    dsr_passed: bool
    kill: tuple[str, ...]
    verdict: str


def score_account(
    name: str,
    closes: list[dict[str, Any]],
    *,
    capital: float,
    n_trials: int,
    rules: KillRules,
    min_sessions: int = 20,
    min_trades: int = 20,
    t_min: float = 3.0,
) -> Score:
    closes = sorted(closes, key=lambda r: str(r.get("closed_at") or r.get("t") or ""))
    nets = [float(r.get("net_inr") or 0.0) for r in closes]
    charges = [float(r.get("charges_inr") or 0.0) for r in closes]
    by_day: dict[str, float] = defaultdict(float)
    for r, x in zip(closes, nets, strict=True):
        by_day[str(r.get("closed_at") or r.get("t") or "")[:10]] += x
    n = len(nets)
    net = sum(nets)
    net2 = net - sum(charges)
    t = None
    if n >= 3 and statistics.stdev(nets) > 0:
        t = statistics.fmean(nets) / (statistics.stdev(nets) / math.sqrt(n))
    daily = [v / capital for v in by_day.values()] if capital > 0 else []
    dsr = compute_deflated_sharpe(daily, n_trials=n_trials) if len(daily) >= 5 else None
    kill = evaluate_kill_rules(nets, rules)
    if kill.killed:
        verdict = "kill rule"
    elif len(by_day) < min_sessions or n < min_trades:
        verdict = "collecting"
    elif net <= 0 or net2 <= 0:
        verdict = "prune"
    elif t is not None and t >= t_min and dsr is not None and dsr.passed:
        verdict = "candidate"
    else:
        verdict = "unproven"
    return Score(
        name, len(by_day), n, round(net, 2), round(net2, 2),
        round(t, 2) if t is not None else None,
        round(dsr.deflated_sharpe_pct, 1) if dsr is not None else None,
        bool(dsr.passed) if dsr is not None else False,
        kill.reasons, verdict,
    )  # fmt: skip


def _normalise(r: dict[str, Any]) -> dict[str, Any]:
    """The swing sleeve journals ``net``/``charges``/``entry_day``/``exit_day``; the intraday
    accounts ``net_inr``/``charges_inr``/``opened_at``/``closed_at``. One shape for both."""
    out = dict(r)
    if "net_inr" not in out and "net" in out:
        out["net_inr"] = out["net"]
    if "charges_inr" not in out and "charges" in out:
        out["charges_inr"] = out["charges"]
    if not out.get("opened_at") and out.get("entry_day"):
        out["opened_at"] = f"{out['entry_day']}T09:15:00+05:30"
    if not out.get("closed_at") and out.get("exit_day"):
        out["closed_at"] = f"{out['exit_day']}T15:30:00+05:30"
    return out


def account_closes(journal_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Closed trades per paper account (accounts with none are left out), in one shape."""
    accounts = {a: [_normalise(r) for r in _jsonl(journal_dir / f"paper_{a}.jsonl")
                    if r.get("type") == "close"]
                for a in _accounts(journal_dir)}  # fmt: skip
    return {a: c for a, c in accounts.items() if c}


def scorecard(
    journal_dir: Path,
    *,
    capital: float,
    rules: KillRules | None = None,
    extra_trials: int = 0,
    min_sessions: int = 20,
    min_trades: int = 20,
) -> list[Score]:
    rules = rules or KillRules()
    accounts = account_closes(journal_dir)
    trials = max(1, len(accounts) + extra_trials)
    return sorted(
        (
            score_account(
                a,
                c,
                capital=capital,
                n_trials=trials,
                rules=rules,
                min_sessions=min_sessions,
                min_trades=min_trades,
            )
            for a, c in accounts.items()
        ),
        key=lambda s: (s.verdict != "candidate", -s.net),
    )


def render(scores: list[Score], *, now: datetime, trials: int) -> str:
    lines = [
        f"# Promotion scorecard, {now:%d %b %Y %H:%M}",
        "",
        f"Every paper account's whole journal. Deflated Sharpe counts {trials} strategies tried. "
        "A **candidate** still needs the owner's approval, written kill rules and Mode B before "
        "one live lot. Nothing here shows that a strategy is profitable.",
        "",
        "| Account | Verdict | Sessions | Trades | Net ₹ | Net, charges ×2 ₹ | t | DSR % "
        "| Kill rules |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        lines.append(
            f"| {s.account} | **{s.verdict}** | {s.sessions} | {s.trades} | {s.net:,.0f} | "
            f"{s.net_2x_charges:,.0f} | {'—' if s.t_stat is None else f'{s.t_stat:.2f}'} | "
            f"{'—' if s.dsr_pct is None else f'{s.dsr_pct:.0f}'} | "
            f"{'; '.join(s.kill) or '—'} |"
        )
    return "\n".join(lines) + "\n"


__all__ = ["Score", "account_closes", "render", "score_account", "scorecard"]
