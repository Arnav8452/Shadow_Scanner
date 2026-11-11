"""Intraday Technicals, Stop-Pool Mapping (PDH/PDL, ORH/ORL, EQH/EQL), VWAP, and Volume Profile.

Implements SPEC §4.8 and PDF §3.2, §4.3, §5.1.1:
1. **Session VWAP + Standard Deviation Bands (±1σ, ±2σ)** and TWAP.
2. **Opening Range (ORH / ORL)** for 1m, 2m, 5m, and 15m windows.
3. **Stop-Pool Cluster Detector (PDF §5.1.1)**:
   - Maps Previous Day High/Low (``PDH``/``PDL``), 15-min Opening Range (``ORH``/``ORL``), Round
     Numbers, and **Equal Highs / Equal Lows (``EQH``/``EQL``)** (2+ swing peaks/troughs within
     ``0.10%`` of each other where retail stop-loss orders cluster).
   - Evaluates whether current price is inside the **Stop-Hunt Sweep Zone** (``0.15%`` to ``0.40%``
     beyond a structural stop pool) or has just reclaimed the level!
4. **Composite Volume Profile (POC, VAH, VAL, HVN, LVN)**:
   - Aggregates volume by price bin to locate the Point of Control (``POC``), 70% Value Area
     (``VAH``, ``VAL``), High-Volume Nodes (``HVN`` = magnets), and Low-Volume Nodes (``LVN`` =
     structural air pockets where price accelerates rapidly).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class StopPoolKind(StrEnum):
    PDL = "PDL"
    PDH = "PDH"
    ORL_15M = "ORL_15M"
    ORH_15M = "ORH_15M"
    EQUAL_LOWS = "EQUAL_LOWS"
    EQUAL_HIGHS = "EQUAL_HIGHS"
    PUT_OI_WALL = "PUT_OI_WALL"
    CALL_OI_WALL = "CALL_OI_WALL"
    VWAP = "VWAP"


@dataclass(frozen=True, slots=True)
class StopPoolLevel:
    kind: StopPoolKind
    price: float
    is_downside_pool: bool  # True for PDL, ORL, EQL, Put OI Wall (sell-stops sit below)
    touches: int = 1


@dataclass(frozen=True, slots=True)
class StopSweepEvaluation:
    nearest_pool: StopPoolLevel | None
    in_sweep_zone: bool
    reclaimed_level: bool
    excursion_pct: float  # % distance beyond the structural level during the sweep
    structural_score_pts: float  # [0, 25] contribution to the 100-Point Confidence Formula
    description: str


@dataclass(frozen=True, slots=True)
class VWAPBands:
    vwap: float
    std_dev: float
    upper_1sd: float
    lower_1sd: float
    upper_2sd: float
    lower_2sd: float


@dataclass(frozen=True, slots=True)
class VolumeProfileResult:
    poc: float
    vah: float
    val: float
    hvns: tuple[float, ...]
    lvns: tuple[float, ...]


def compute_vwap_bands(
    prices: Sequence[float],
    volumes: Sequence[int],
) -> VWAPBands:
    """Compute volume-weighted average price and ±1σ / ±2σ volume-weighted dispersion bands."""
    if not prices or len(prices) != len(volumes):
        return VWAPBands(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    tot_v = sum(max(0, v) for v in volumes)
    if tot_v <= 0:
        last = prices[-1]
        return VWAPBands(last, 0.0, last, last, last, last)

    vwap = sum(p * max(0, v) for p, v in zip(prices, volumes, strict=True)) / tot_v
    var = sum(max(0, v) * ((p - vwap) ** 2) for p, v in zip(prices, volumes, strict=True)) / tot_v
    sd = math.sqrt(max(0.0, var))
    return VWAPBands(
        vwap=round(vwap, 2),
        std_dev=round(sd, 2),
        upper_1sd=round(vwap + sd, 2),
        lower_1sd=round(vwap - sd, 2),
        upper_2sd=round(vwap + 2.0 * sd, 2),
        lower_2sd=round(vwap - 2.0 * sd, 2),
    )


def compute_wilder_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    period: int = 14,
) -> float:
    """Compute Wilder's Average True Range (ATR)."""
    n = min(len(highs), len(lows), len(closes))
    if n == 0:
        return 0.0
    trs: list[float] = [highs[0] - lows[0]]
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) <= period:
        return round(sum(trs) / len(trs), 4)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = ((period - 1) * atr + tr) / period
    return round(atr, 4)


def detect_equal_highs_lows(
    swing_highs: Sequence[float],
    swing_lows: Sequence[float],
    *,
    tolerance_pct: float = 0.10,
) -> list[StopPoolLevel]:
    """Identify Equal Highs (EQH) and Equal Lows (EQL) where retail stop-loss orders cluster."""
    pools: list[StopPoolLevel] = []

    def _cluster(vals: Sequence[float], kind: StopPoolKind, is_down: bool) -> None:
        used = [False] * len(vals)
        for i, v1 in enumerate(vals):
            if used[i] or v1 <= 0:
                continue
            cluster = [v1]
            used[i] = True
            for j in range(i + 1, len(vals)):
                if not used[j] and abs(vals[j] - v1) / v1 * 100.0 <= tolerance_pct:
                    cluster.append(vals[j])
                    used[j] = True
            if len(cluster) >= 2:
                avg_px = sum(cluster) / len(cluster)
                pools.append(
                    StopPoolLevel(
                        kind=kind,
                        price=round(avg_px, 2),
                        is_downside_pool=is_down,
                        touches=len(cluster),
                    )
                )

    _cluster(swing_lows, StopPoolKind.EQUAL_LOWS, True)
    _cluster(swing_highs, StopPoolKind.EQUAL_HIGHS, False)
    return pools


def evaluate_stop_pool_sweep(
    *,
    ltp: float,
    session_low: float,
    session_high: float,
    pools: Sequence[StopPoolLevel],
    direction: str,  # "LONG" or "SHORT"
    min_sweep_pct: float = 0.08,
    max_sweep_pct: float = 0.45,
) -> StopSweepEvaluation:
    """Evaluate 25-point Structural Location & Stop-Pool Sweep status (PDF §5.1 & §8.1.3)."""
    if not pools or ltp <= 0:
        return StopSweepEvaluation(None, False, False, 0.0, 0.0, "No structural stop pools")

    want_downside = direction.upper() == "LONG"
    relevant = [p for p in pools if p.is_downside_pool == want_downside and p.price > 0]
    if not relevant:
        relevant = [p for p in pools if p.price > 0]

    best_eval = StopSweepEvaluation(None, False, False, 0.0, 0.0, "Away from structural levels")

    for pool in relevant:
        lvl = pool.price
        if want_downside:
            # Downside sweep below PDL / ORL / EQL / Put OI Wall:
            # session_low dipped below lvl, and ltp has either reclaimed lvl or is inside sweep band
            sweep_depth_pct = ((lvl - session_low) / lvl * 100.0) if session_low < lvl else 0.0
            dist_now_pct = abs(ltp - lvl) / lvl * 100.0
            reclaimed = session_low < lvl <= ltp and sweep_depth_pct <= max_sweep_pct * 1.5
            in_zone = min_sweep_pct <= sweep_depth_pct <= max_sweep_pct and dist_now_pct <= 0.35

            pts = 0.0
            desc = f"Near {pool.kind.value} ({lvl:.2f})"
            if reclaimed and sweep_depth_pct >= min_sweep_pct:
                pts = 25.0
                desc = (
                    f"Stop-Hunt Sweep & Reclaim of {pool.kind.value} ({lvl:.2f}, "
                    f"swept -{sweep_depth_pct:.2f}% to {session_low:.2f} and reclaimed)"
                )
            elif in_zone:
                pts = 20.0
                desc = (
                    f"Inside Stop-Pool Sweep Zone below {pool.kind.value} "
                    f"({lvl:.2f}, -{sweep_depth_pct:.2f}%)"
                )
            elif dist_now_pct <= 0.25:
                pts = 15.0
                desc = f"Testing structural support {pool.kind.value} ({lvl:.2f})"
        else:
            # Upside sweep above PDH / ORH / EQH / Call OI Wall (or breakout test):
            sweep_height_pct = ((session_high - lvl) / lvl * 100.0) if session_high > lvl else 0.0
            dist_now_pct = abs(ltp - lvl) / lvl * 100.0
            reclaimed = session_high > lvl >= ltp and sweep_height_pct <= max_sweep_pct * 1.5
            in_zone = min_sweep_pct <= sweep_height_pct <= max_sweep_pct and dist_now_pct <= 0.35

            pts = 0.0
            desc = f"Near {pool.kind.value} ({lvl:.2f})"
            if reclaimed and sweep_height_pct >= min_sweep_pct:
                pts = 25.0
                desc = (
                    f"Upthrust Sweep & Reject of {pool.kind.value} ({lvl:.2f}, "
                    f"swept +{sweep_height_pct:.2f}% to {session_high:.2f} and rejected)"
                )
            elif in_zone:
                pts = 20.0
                desc = (
                    f"Inside Upside Stop-Pool Zone above {pool.kind.value} "
                    f"({lvl:.2f}, +{sweep_height_pct:.2f}%)"
                )
            elif dist_now_pct <= 0.25:
                pts = 15.0
                desc = f"Testing structural resistance {pool.kind.value} ({lvl:.2f})"

        if pts > best_eval.structural_score_pts:
            best_eval = StopSweepEvaluation(
                nearest_pool=pool,
                in_sweep_zone=in_zone,
                reclaimed_level=reclaimed,
                excursion_pct=round(sweep_depth_pct if want_downside else sweep_height_pct, 3),
                structural_score_pts=pts,
                description=desc,
            )

    return best_eval


def compute_volume_profile(
    prices: Sequence[float],
    volumes: Sequence[int],
    *,
    n_bins: int = 24,
    value_area_pct: float = 0.70,
) -> VolumeProfileResult:
    """Compute Composite Volume Profile: POC, 70% Value Area (VAH/VAL), HVNs, and LVNs."""
    if not prices or len(prices) != len(volumes):
        return VolumeProfileResult(0.0, 0.0, 0.0, (), ())
    lo = min(prices)
    hi = max(prices)
    if hi <= lo:
        return VolumeProfileResult(lo, lo, lo, (lo,), ())

    bin_w = (hi - lo) / max(2, n_bins)
    bins = [0] * n_bins
    centers = [lo + (i + 0.5) * bin_w for i in range(n_bins)]

    for p, v in zip(prices, volumes, strict=True):
        idx = min(n_bins - 1, max(0, int((p - lo) / bin_w)))
        bins[idx] += max(0, v)

    tot_vol = sum(bins)
    if tot_vol <= 0:
        return VolumeProfileResult(lo, hi, lo, (), ())

    poc_idx = max(range(n_bins), key=lambda i: bins[i])
    poc = centers[poc_idx]

    # Expand outward from POC until 70% of total volume is captured
    captured = bins[poc_idx]
    left = poc_idx
    right = poc_idx
    target = tot_vol * value_area_pct
    while captured < target and (left > 0 or right < n_bins - 1):
        v_left = bins[left - 1] if left > 0 else -1
        v_right = bins[right + 1] if right < n_bins - 1 else -1
        if v_right >= v_left:
            right += 1
            captured += bins[right]
        else:
            left -= 1
            captured += bins[left]

    val = centers[left]
    vah = centers[right]

    avg_bin = tot_vol / n_bins
    hvns = tuple(round(centers[i], 2) for i in range(n_bins) if bins[i] >= 1.5 * avg_bin)
    lvns = tuple(
        round(centers[i], 2) for i in range(1, n_bins - 1) if 0 < bins[i] <= 0.45 * avg_bin
    )

    return VolumeProfileResult(
        poc=round(poc, 2),
        vah=round(vah, 2),
        val=round(val, 2),
        hvns=hvns,
        lvns=lvns,
    )
