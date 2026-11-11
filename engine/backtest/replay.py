"""Stress evaluator for a list of already-simulated trades (2x slippage, latency grid).

STATUS: this is **not** an event replay backtester. It takes trades whose entry and exit prices
are given (``SimulatedTrade``) and re-prices them with extra slippage and a crude latency drift.
It does not replay recorded quotes/depth, generate signals, or fill at bid/ask; that is what
SPEC §13 M6 requires ("replay of recorded days reproduces PaperBroker results") and it is not
built yet. Sharpe here is per-trade P&L scaled by sqrt(252), which is only meaningful for one
trade per day.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    symbol: str
    direction: str  # "LONG" or "SHORT"
    qty: int
    entry_price: float
    exit_price: float
    base_slippage_pts: float
    charges_inr: float
    latency_adverse_drift_pts_per_25ms: float = 0.05

    def net_pnl_at(self, *, slippage_mult: float = 1.0, latency_ms: int = 0) -> float:
        sign = 1.0 if self.direction.upper() == "LONG" else -1.0
        raw_gross = (self.exit_price - self.entry_price) * self.qty * sign
        extra_slip = (max(1.0, slippage_mult) - 1.0) * self.base_slippage_pts * 2.0 * self.qty
        lat_cost = (latency_ms / 25.0) * self.latency_adverse_drift_pts_per_25ms * self.qty
        return raw_gross - extra_slip - lat_cost - self.charges_inr


@dataclass(frozen=True, slots=True)
class BacktestEvaluation:
    total_trades: int
    win_rate_pct: float
    net_pnl_1x_inr: float
    net_pnl_2x_slippage_inr: float
    sharpe_1x: float
    sharpe_2x_slippage: float
    max_drawdown_inr: float
    passes_2x_slippage_gate: bool
    latency_sweep_net_pnl: dict[int, float]  # {0: ..., 5: ..., 25: ..., 50: ..., 100: ...}
    survives_50ms_retail_latency: bool


def _compute_sharpe_and_mdd(pnls: Sequence[float]) -> tuple[float, float]:
    if not pnls:
        return 0.0, 0.0
    if len(pnls) < 2:
        return (1.0 if pnls[0] > 0 else -1.0), max(0.0, -pnls[0])
    mean_p = sum(pnls) / len(pnls)
    sd_p = statistics.stdev(pnls)
    sharpe = (mean_p / sd_p * math.sqrt(252.0)) if sd_p > 1e-6 else 0.0

    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return round(sharpe, 4), round(mdd, 2)


def evaluate_backtest_with_stress(
    trades: Sequence[SimulatedTrade],
    *,
    latency_grid_ms: Sequence[int] = (0, 5, 25, 50, 100),
) -> BacktestEvaluation:
    """Evaluate trade series at 1× slippage, mandatory 2× slippage, and across latency grid."""
    if not trades:
        return BacktestEvaluation(
            0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, False, {ms: 0.0 for ms in latency_grid_ms}, False
        )

    pnls_1x = [t.net_pnl_at(slippage_mult=1.0, latency_ms=0) for t in trades]
    pnls_2x = [t.net_pnl_at(slippage_mult=2.0, latency_ms=0) for t in trades]

    wins_1x = sum(1 for p in pnls_1x if p > 0)
    net_1x = sum(pnls_1x)
    net_2x = sum(pnls_2x)
    sh_1x, mdd_1x = _compute_sharpe_and_mdd(pnls_1x)
    sh_2x, _ = _compute_sharpe_and_mdd(pnls_2x)

    lat_sweep: dict[int, float] = {}
    for ms in latency_grid_ms:
        lat_sweep[int(ms)] = round(
            sum(t.net_pnl_at(slippage_mult=1.0, latency_ms=int(ms)) for t in trades), 2
        )

    passes_2x = net_2x > 0 and sh_2x > 0
    survives_50ms = lat_sweep.get(50, net_1x) > 0

    return BacktestEvaluation(
        total_trades=len(trades),
        win_rate_pct=round(wins_1x / len(trades) * 100.0, 2),
        net_pnl_1x_inr=round(net_1x, 2),
        net_pnl_2x_slippage_inr=round(net_2x, 2),
        sharpe_1x=sh_1x,
        sharpe_2x_slippage=sh_2x,
        max_drawdown_inr=mdd_1x,
        passes_2x_slippage_gate=passes_2x,
        latency_sweep_net_pnl=lat_sweep,
        survives_50ms_retail_latency=survives_50ms,
    )
