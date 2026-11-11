"""Own-Journal & Reference-Log Behavioral Self-Review Engine (SPEC §9.2).

Quantifies:
1. **Gross vs Net Friction Drag**: Total charges as % of gross profit and per-trade cost drag.
2. **Revenge-Trading Detector**: Compares trade frequency, lot size, and win rate on trades entered
   within ``revenge_window_min`` (default 15 min) after a realized loss vs normal baseline trades.
3. **Activity Terciles (Overtrading Proof)**: Splits trading days into Low, Medium, and High trade-
   count terciles and compares mean Daily Net P&L across terciles.
4. **Time-of-Day Expectancy Buckets**: Morning Open (09:15–09:30), Morning Trend (09:30–11:00),
   Midday Chop (11:00–13:30), Afternoon (13:30–15:00), and Closing CAS Window (15:00–15:40).
5. **Slippage Stress Test (+1, +2, +4 Ticks)**: Recomputes total net P&L assuming 1, 2, or 4 extra
   ticks of adverse fill slippage per round-trip.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from engine.trade_mining.parse_report import MinedTradeRow


@dataclass(frozen=True, slots=True)
class RevengeTradingMetrics:
    post_loss_trade_count: int
    baseline_trade_count: int
    post_loss_win_rate_pct: float
    baseline_win_rate_pct: float
    post_loss_avg_qty: float
    baseline_avg_qty: float
    post_loss_net_pnl_inr: float


@dataclass(frozen=True, slots=True)
class SelfReviewSummary:
    total_trades: int
    trading_days: int
    win_rate_pct: float
    gross_pnl_inr: float
    total_charges_inr: float
    net_pnl_inr: float
    charges_pct_of_gross_abs: float
    revenge: RevengeTradingMetrics
    time_bucket_net_pnl: dict[str, float]
    activity_tercile_avg_daily_net: dict[str, float]
    slippage_stress_net_pnl: dict[str, float]  # "+1_tick", "+2_ticks", "+4_ticks"


def _time_bucket(t_str: str | None) -> str:
    if not t_str:
        return "UNKNOWN"
    if t_str < "09:30:00":
        return "09:15-09:30_OPEN"
    if t_str < "11:00:00":
        return "09:30-11:00_MORNING"
    if t_str < "13:30:00":
        return "11:00-13:30_MIDDAY"
    if t_str < "15:00:00":
        return "13:30-15:00_AFTERNOON"
    return "15:00-15:40_CLOSE_CAS"


def _minutes_between(t_prev_out: str | None, t_curr_in: str | None) -> float | None:
    if not t_prev_out or not t_curr_in:
        return None
    try:
        h1, m1, s1 = (int(x) for x in t_prev_out.split(":"))
        h2, m2, s2 = (int(x) for x in t_curr_in.split(":"))
        diff = ((h2 * 3600 + m2 * 60 + s2) - (h1 * 3600 + m1 * 60 + s1)) / 60.0
        return diff if diff >= 0 else None
    except ValueError:
        return None


def analyze_trade_journal(
    rows: Sequence[MinedTradeRow],
    *,
    revenge_window_min: float = 15.0,
    tick_size: float = 0.05,
) -> SelfReviewSummary:
    """Compute comprehensive behavioral, time-of-day, and slippage diagnostics on trade rows."""
    if not rows:
        empty_rev = RevengeTradingMetrics(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
        return SelfReviewSummary(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, empty_rev, {}, {}, {})

    ordered = sorted(rows, key=lambda r: (r.trade_date, r.time_in or "00:00:00"))
    wins = [r for r in ordered if r.net_pnl_inr > 0]
    gross = sum(r.gross_pnl_inr for r in ordered)
    charges = sum(r.charges_inr for r in ordered)
    net = sum(r.net_pnl_inr for r in ordered)
    charges_pct = (charges / abs(gross) * 100.0) if abs(gross) > 1e-6 else 100.0

    # 1. Revenge-trading analysis (trades entered <= revenge_window_min after a losing trade)
    post_loss: list[MinedTradeRow] = []
    baseline: list[MinedTradeRow] = []
    for i, r in enumerate(ordered):
        if i == 0 or ordered[i - 1].trade_date != r.trade_date:
            baseline.append(r)
            continue
        prev = ordered[i - 1]
        gap_m = _minutes_between(prev.time_out, r.time_in)
        if prev.net_pnl_inr < 0 and gap_m is not None and gap_m <= revenge_window_min:
            post_loss.append(r)
        else:
            baseline.append(r)

    pl_wins = sum(1 for r in post_loss if r.net_pnl_inr > 0)
    bl_wins = sum(1 for r in baseline if r.net_pnl_inr > 0)
    revenge = RevengeTradingMetrics(
        post_loss_trade_count=len(post_loss),
        baseline_trade_count=len(baseline),
        post_loss_win_rate_pct=round(pl_wins / len(post_loss) * 100.0, 2) if post_loss else 0.0,
        baseline_win_rate_pct=round(bl_wins / len(baseline) * 100.0, 2) if baseline else 0.0,
        post_loss_avg_qty=round(sum(r.qty for r in post_loss) / len(post_loss), 1)
        if post_loss
        else 0.0,
        baseline_avg_qty=round(sum(r.qty for r in baseline) / len(baseline), 1)
        if baseline
        else 0.0,
        post_loss_net_pnl_inr=round(sum(r.net_pnl_inr for r in post_loss), 2),
    )

    # 2. Time-of-day expectancy buckets
    buckets: dict[str, float] = {}
    for r in ordered:
        b = _time_bucket(r.time_in)
        buckets[b] = round(buckets.get(b, 0.0) + r.net_pnl_inr, 2)

    # 3. Activity terciles by daily trade count
    daily_counts: dict[object, int] = {}
    daily_nets: dict[object, float] = {}
    for r in ordered:
        daily_counts[r.trade_date] = daily_counts.get(r.trade_date, 0) + 1
        daily_nets[r.trade_date] = daily_nets.get(r.trade_date, 0.0) + r.net_pnl_inr

    days_sorted = sorted(daily_counts, key=lambda d: daily_counts[d])
    n_days = len(days_sorted)
    t1_idx = max(1, n_days // 3)
    t2_idx = max(t1_idx + 1, (2 * n_days) // 3)
    low_days = days_sorted[:t1_idx]
    mid_days = days_sorted[t1_idx:t2_idx] or low_days
    high_days = days_sorted[t2_idx:] or mid_days

    terciles = {
        "LOW_ACTIVITY_DAYS": round(sum(daily_nets[d] for d in low_days) / len(low_days), 2),
        "MID_ACTIVITY_DAYS": round(sum(daily_nets[d] for d in mid_days) / len(mid_days), 2),
        "HIGH_ACTIVITY_DAYS": round(sum(daily_nets[d] for d in high_days) / len(high_days), 2),
    }

    # 4. Slippage sensitivity (+1, +2, +4 ticks per trade)
    total_qty = sum(r.qty for r in ordered)
    slippage_stress = {
        "+1_tick": round(net - 1.0 * tick_size * total_qty, 2),
        "+2_ticks": round(net - 2.0 * tick_size * total_qty, 2),
        "+4_ticks": round(net - 4.0 * tick_size * total_qty, 2),
    }

    return SelfReviewSummary(
        total_trades=len(ordered),
        trading_days=n_days,
        win_rate_pct=round(len(wins) / len(ordered) * 100.0, 2),
        gross_pnl_inr=round(gross, 2),
        total_charges_inr=round(charges, 2),
        net_pnl_inr=round(net, 2),
        charges_pct_of_gross_abs=round(charges_pct, 2),
        revenge=revenge,
        time_bucket_net_pnl=buckets,
        activity_tercile_avg_daily_net=terciles,
        slippage_stress_net_pnl=slippage_stress,
    )
