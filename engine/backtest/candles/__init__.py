"""Candle-based backtest runner (owner checklist, 13 Nov 2026).

Recorded bid/ask history exists only since early Nov 2026. Strategies whose triggers use only
price, 1-minute volume, IV and OI can be tested on Upstox 1-minute candles (index since 2022,
expired Nifty options with OI since Oct 2024) if every fill pays a punitive half-spread. This
is a CANDLE APPROXIMATION of SPEC §8, not the event replay over recorded depth:

- fills are never at a candle's close: a decision on a completed bar fills at the NEXT bar's
  open, plus or minus ``max(₹1.50, 1.5% of price)`` per leg and side (``candle_backtest`` in
  settings); every run is repeated at 2x that haircut with doubled charges, and at the owner's
  plan haircut ``max(₹1, 0.75%)``;
- order-book strategies (score, breakthrough, spoof_fade, zone_rider) and the futures-CVD ones
  (oi_ignition, oi_credit) are refused: no depth or trade side exists historically;
- IV is Black-76 on the variance clock from a synthetic forward of last trades (not mids).

Modules: ``data`` (bars, Upstox source with a disk cache), ``market`` (prices, forward, IV,
delta at a minute), ``fills`` (haircut, costs, margin), ``strategies`` (simulators and the
registry), ``runner`` (statistics, trial count, report).
"""

from engine.backtest.candles.data import Bar, CandleSource, MemorySource, UpstoxCandleSource
from engine.backtest.candles.fills import Haircut
from engine.backtest.candles.runner import RunResult, render_report, run_candle_backtest
from engine.backtest.candles.strategies import (
    NOT_BUILT,
    REFUSED,
    STRATEGIES,
    CandleTrade,
    StrategyRefused,
)

__all__ = [
    "NOT_BUILT",
    "REFUSED",
    "STRATEGIES",
    "Bar",
    "CandleSource",
    "CandleTrade",
    "Haircut",
    "MemorySource",
    "RunResult",
    "StrategyRefused",
    "UpstoxCandleSource",
    "render_report",
    "run_candle_backtest",
]
