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

from engine.analytics.expiry_clock import (
    NON_TRADING_DAYS_PER_YEAR,
    TRADING_DAYS_PER_YEAR,
    variance_weights,
)
from engine.analytics.greeks import compute_black76_greeks
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


# Natenberg N4: risk is two-dimensional. Spot moves (%) x parallel IV shifts (vol points),
# each leg repriced at its own strike IV plus the shift (sticky strike). A view, not a rule:
# the carry rule above still uses the four coupled gap scenarios.
GRID_SPOT_PCT: tuple[float, ...] = (-8, -5, -2, 0, 2, 5, 8)
GRID_IV_PTS: tuple[float, ...] = (-5, -2, 0, 5, 10)
MIN_IV = 0.03


def one_session_t_var() -> float:
    """Variance-clock years in one trading session plus the night after it (weekends and
    holidays not included), so the next-session slice matches the clock the IVs were made on."""
    w_on, w_nt = variance_weights()
    return (1.0 + w_on) / (TRADING_DAYS_PER_YEAR * (1.0 + w_on) + NON_TRADING_DAYS_PER_YEAR * w_nt)


def price_vol_grid(
    legs: Sequence[Any],
    detail: Mapping[str, Mapping[str, Any]],
    spot: float | None,
    *,
    spot_pcts: Sequence[float] = GRID_SPOT_PCT,
    iv_pts: Sequence[float] = GRID_IV_PTS,
    t_shift: float = 0.0,
) -> dict[str, Any] | None:
    """P&L in rupees of the open ``legs`` for every (IV shift, spot move) pair, against their
    model value now; ``t_shift`` (variance-clock years) lets time pass first. Rows are IV
    shifts, columns spot moves. None when an open option leg has no IV detail (or a futures leg
    has no spot)."""
    pnl = [[0.0 for _ in spot_pcts] for _ in iv_pts]
    for lg in legs:
        if not lg.open:
            continue
        sign = 1.0 if lg.side == "BUY" else -1.0
        if lg.kind == "FUT":
            if not spot:
                return None
            for row in pnl:
                for j, g in enumerate(spot_pcts):
                    row[j] += sign * spot * g / 100 * lg.qty
            continue
        d = detail.get(lg.symbol)
        if not d or d.get("iv") is None or not d.get("t_var"):
            return None
        f, k, t = float(d["forward"]), float(d["strike"]), float(d["t_var"])
        iv = float(d["iv"]) / 100
        call = lg.kind == "CE"
        base = black76_price(f, k, t, iv, is_call=call)
        for i, dv in enumerate(iv_pts):
            sig = max(MIN_IV, iv + dv / 100)
            for j, g in enumerate(spot_pcts):
                v = black76_price(f * (1 + g / 100), k, t - t_shift, sig, is_call=call)
                pnl[i][j] += sign * (v - base) * lg.qty
    return {
        "spot": list(spot_pcts),
        "iv": list(iv_pts),
        "t_shift": t_shift,
        "pnl": [[round(v, 0) for v in row] for row in pnl],
    }


def book_greeks(
    legs: Sequence[Any], detail: Mapping[str, Mapping[str, Any]], spot: float | None
) -> dict[str, Any] | None:
    """Open book greeks in rupees: P&L per +1% spot (delta), the extra P&L from gamma on a 1%
    move, P&L per +1 vol point (vega) in total and by days to expiry. None when an open option
    leg has no IV detail (or a futures leg has no spot)."""
    delta = gamma = vega = 0.0
    by_exp: dict[str, float] = {}
    for lg in legs:
        if not lg.open:
            continue
        sign = 1.0 if lg.side == "BUY" else -1.0
        if lg.kind == "FUT":
            if not spot:
                return None
            delta += sign * spot * 0.01 * lg.qty
            continue
        d = detail.get(lg.symbol)
        if not d or d.get("iv") is None or not d.get("t_var"):
            return None
        f, k, t = float(d["forward"]), float(d["strike"]), float(d["t_var"])
        g = compute_black76_greeks(f, k, t, float(d["iv"]) / 100, is_call=lg.kind == "CE")
        delta += sign * g.delta * f * 0.01 * lg.qty
        gamma += sign * 0.5 * g.gamma * (f * 0.01) ** 2 * lg.qty
        v = sign * g.vega_per_1pct * lg.qty
        vega += v
        label = f"{d.get('days', '?')}d"
        by_exp[label] = by_exp.get(label, 0.0) + v
    return {
        "delta_inr_per_1pct": round(delta, 0),
        "gamma_inr_per_1pct": round(gamma, 0),
        "vega_inr_per_pt": round(vega, 0),
        "vega_by_expiry": {k: round(v, 0) for k, v in by_exp.items()},
    }


def risk_view(legs: Sequence[Any], analytics: Any, underlying: str) -> dict[str, Any] | None:
    """The N4 view of open legs from ``LiveAnalytics``: the grid now, the grid after one
    session, and book greeks."""
    if analytics is None:
        return None
    spot = (getattr(analytics, "strip", {}).get(underlying) or {}).get("spot")
    detail = getattr(analytics, "detail", {}) or {}
    grid = price_vol_grid(legs, detail, spot)
    nxt = price_vol_grid(legs, detail, spot, t_shift=one_session_t_var())
    greeks = book_greeks(legs, detail, spot)
    if grid is None or nxt is None or greeks is None:
        return None
    return {"grid": grid, "next_session": nxt, "greeks": greeks}


__all__ = [
    "DEFAULT_SCENARIOS",
    "GRID_IV_PTS",
    "GRID_SPOT_PCT",
    "book_greeks",
    "gap_pnl",
    "one_session_t_var",
    "price_vol_grid",
    "risk_view",
    "scenario_label",
    "worst",
]
