"""SPEC 6 gap stress: what an overnight gap of +-5% / +-8% would do to an open structure.

Each open option leg is repriced with Black-76 on the engine's own inputs (forward, strike,
variance-clock time and strike IV from ``LiveAnalytics.detail``) at the gapped forward and a
shifted IV (down gaps lift IV, up gaps ease it); futures legs move one for one with the index.
Time does not pass (the night's decay is ignored, which is conservative for short premium), and
exit spreads after a gap are not modelled (they widen: real fills would be worse).

June 4 2024 (election results) closed about 6% down: +-5% and +-8% bracket that day.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from engine.analytics.iv import black76_price

# (index gap %, IV shift in vol points): crashes lift IV, rallies ease it.
DEFAULT_SCENARIOS: tuple[tuple[float, float], ...] = ((-8, 8), (-5, 5), (5, -2), (8, -3))


def scenario_label(gap: float) -> str:
    return f"{gap:+g}%"


def gap_pnl(
    legs: Sequence[Any],
    detail: Mapping[str, Mapping[str, Any]],
    spot: float | None,
    scenarios: Sequence[tuple[float, float]] = DEFAULT_SCENARIOS,
) -> dict[str, float] | None:
    """P&L in rupees per scenario for the open ``legs`` (``symbol, kind, side, qty, open``);
    None when any open option leg has no IV detail (or a futures leg has no spot)."""
    out = {g: 0.0 for g, _ in scenarios}
    for lg in legs:
        if not lg.open:
            continue
        sign = 1.0 if lg.side == "BUY" else -1.0
        if lg.kind == "FUT":
            if not spot:
                return None
            for g, _ in scenarios:
                out[g] += sign * spot * g / 100 * lg.qty
            continue
        d = detail.get(lg.symbol)
        if not d or d.get("iv") is None or not d.get("t_var"):
            return None
        f, k, t = float(d["forward"]), float(d["strike"]), float(d["t_var"])
        iv = float(d["iv"]) / 100
        call = lg.kind == "CE"
        base = black76_price(f, k, t, iv, is_call=call)
        for g, dv in scenarios:
            v = black76_price(f * (1 + g / 100), k, t, max(0.03, iv + dv / 100), is_call=call)
            out[g] += sign * (v - base) * lg.qty
    return {scenario_label(g): round(v, 0) for g, v in out.items()}


def worst(stress: Mapping[str, float] | None) -> tuple[str, float] | None:
    if not stress:
        return None
    k = min(stress, key=lambda x: stress[x])
    return k, stress[k]


__all__ = ["DEFAULT_SCENARIOS", "gap_pnl", "scenario_label", "worst"]
