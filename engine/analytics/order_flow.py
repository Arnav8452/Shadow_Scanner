"""Deep Order-Flow Mechanics, Anti-Spoofing Filter (RER), Lee-Ready CVD, VPIN, and 6 Mechanics.

Implements every rule and formula from the Order-Flow & Stop-Hunt specification PDF:
1. **Anti-Spoofing Real Execution Ratio (RER, PDF §2.6, §4.2, §8.1.3)**:
   ``RER = V_executed_at_level / Delta_V_book_reduction_at_level``
   - ``RER < 0.20``: **Spoof / Liquidity Pull Detected** (wall was cancelled before/upon touch
     without real trades printing in ``ltq`` / ``volume``) -> **blocks entry**.
   - ``RER >= 0.75``: **Genuine Institutional Execution / Absorption Confirmed**.
   - ``RER > 1.25``: **Iceberg Order Reload Detected** (more contracts traded at the wall price
     than were visibly resting on the order book!).
2. **Lee-Ready Tick Classification & Rolling Cumulative Volume Delta (CVD) Z-Score**:
   - Classifies each trade print as buyer-initiated (``+volume``) if ``ltp >= ask`` (or tick uptick
     when inside spread) and seller-initiated (``-volume``) if ``ltp <= bid`` (or tick downtick).
   - Computes rolling ``cvd_z`` to detect 2σ–3σ aggressive institutional bursts.
3. **Order Flow Toxicity (VPIN)**:
   - Volume-Synchronized Probability of Informed Trading across equal-volume buckets:
     ``VPIN = sum(|V_buy - V_sell|) / (N * V_bucket)`` in ``[0, 1]``.
4. **The 6 Core Order-Flow & Market Depth Mechanics**:
   - ``STOP_RUN_SHORT_SQUEEZE`` (Breakout Momentum Cascade)
   - ``LIQUIDITY_ABSORPTION`` (Passive Wall Defense / Wyckoff Spring Reversal)
   - ``INITIATIVE_EXHAUSTION`` (Failed Auction / Bull or Bear Trap)
   - ``LIQUIDITY_VACUUM`` (L6–L25 Air-Pocket Thin-Book Acceleration)
   - ``ORDER_BOOK_FLIP`` (Broken Wall Role Reversal: Resistance <-> Support)
   - ``SPOOF_LIQUIDITY_PULL`` (Phantom Wall Cancellation Alert & Entry Block)
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from engine.analytics.microstructure import BookMicrostructureSnapshot, analyze_book_microstructure
from engine.data.adapters.base import Level


class OrderFlowMechanic(StrEnum):
    STOP_RUN_SHORT_SQUEEZE = "STOP_RUN_SHORT_SQUEEZE"
    LIQUIDITY_ABSORPTION = "LIQUIDITY_ABSORPTION"
    INITIATIVE_EXHAUSTION = "INITIATIVE_EXHAUSTION"
    LIQUIDITY_VACUUM = "LIQUIDITY_VACUUM"
    ORDER_BOOK_FLIP = "ORDER_BOOK_FLIP"
    SPOOF_LIQUIDITY_PULL = "SPOOF_LIQUIDITY_PULL"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True, slots=True)
class AntiSpoofEvaluation:
    real_execution_ratio: float
    is_spoof_pull: bool
    is_genuine_execution: bool
    is_iceberg_reload: bool
    wall_age_ms: int
    wall_persisted: bool
    flow_score_pts: float  # [0, 30] contribution to the 100-Point Confidence Formula
    status_label: str


@dataclass(frozen=True, slots=True)
class MechanicDetection:
    primary_mechanic: OrderFlowMechanic
    active_mechanics: tuple[OrderFlowMechanic, ...]
    direction_bias: str  # "LONG", "SHORT", or "BLOCKED" / "NEUTRAL"
    cvd_delta: float
    cvd_z_score: float
    vpin_toxicity: float
    anti_spoof: AntiSpoofEvaluation
    obi_air_pocket_score_pts: float  # [0, 25] contribution to 100-Point Confidence Formula
    rationale: str


def classify_lee_ready_side(
    ltp: float,
    prev_ltp: float,
    bid: float,
    ask: float,
) -> int:
    """Classify trade initiator via Lee-Ready quote + tick rule (+1 buyer, -1 seller)."""
    mid = 0.5 * (bid + ask) if (bid > 0 and ask > 0) else prev_ltp
    if ask > 0 and ltp >= ask:
        return 1
    if bid > 0 and ltp <= bid:
        return -1
    if abs(ltp - mid) > 1e-6:
        return 1 if ltp > mid else -1
    if ltp > prev_ltp:
        return 1
    if ltp < prev_ltp:
        return -1
    return 1


def compute_real_execution_ratio(
    *,
    traded_volume_delta: int,
    book_wall_reduction: int,
) -> float:
    """Compute Real Execution Ratio ``RER = V_executed / Delta_V_book_reduction`` (PDF §4.2).

    - When ``book_wall_reduction <= 0`` (wall held firm or grew via iceberg reload while trades
      printed against it), returns ``1.35`` if ``traded_volume_delta > 0`` (iceberg absorption)
      else ``1.0``.
    """
    exec_v = max(0, traded_volume_delta)
    red_v = max(0, book_wall_reduction)
    if red_v == 0:
        return 1.35 if exec_v > 0 else 1.0
    rer = exec_v / red_v
    return round(min(2.0, max(0.0, rer)), 4)


def evaluate_anti_spoofing(
    *,
    traded_volume_delta: int,
    book_wall_reduction: int,
    cvd_z_score: float,
    wall_age_ms: int = 1000,
    min_wall_age_ms: int = 500,
    rer_confirm_min: float = 0.75,
    rer_spoof_max: float = 0.20,
) -> AntiSpoofEvaluation:
    """Evaluate 30-point Order-Flow & Anti-Spoofing Verification block (PDF §4.2 & §8.1.3)."""
    rer = compute_real_execution_ratio(
        traded_volume_delta=traded_volume_delta,
        book_wall_reduction=book_wall_reduction,
    )
    persisted = wall_age_ms >= min_wall_age_ms
    # Spoof pull occurs when a sizable wall reduction happens with < 20% real traded volume
    is_spoof = book_wall_reduction > 0 and rer < rer_spoof_max
    is_genuine = rer >= rer_confirm_min and persisted and not is_spoof
    is_iceberg = rer > 1.25 and traded_volume_delta > 0 and persisted

    if is_spoof:
        return AntiSpoofEvaluation(
            real_execution_ratio=rer,
            is_spoof_pull=True,
            is_genuine_execution=False,
            is_iceberg_reload=False,
            wall_age_ms=wall_age_ms,
            wall_persisted=persisted,
            flow_score_pts=0.0,
            status_label=f"SPOOF_PULL_BLOCKED (RER={rer:.2f} < {rer_spoof_max:.2f})",
        )

    pts = 0.0
    if is_genuine:
        pts += 15.0
    elif rer >= 0.50 and persisted:
        pts += 8.0

    abs_z = abs(cvd_z_score)
    if abs_z >= 2.0:
        pts += 15.0
    elif abs_z >= 1.2:
        pts += 9.0
    elif abs_z >= 0.6:
        pts += 4.0

    label = (
        f"ICEBERG_ABSORPTION (RER={rer:.2f}, CVD_Z={cvd_z_score:+.2f})"
        if is_iceberg
        else (
            f"CONFIRMED_FLOW (RER={rer:.2f}, CVD_Z={cvd_z_score:+.2f})"
            if is_genuine
            else f"MODERATE_FLOW (RER={rer:.2f}, CVD_Z={cvd_z_score:+.2f})"
        )
    )
    return AntiSpoofEvaluation(
        real_execution_ratio=rer,
        is_spoof_pull=False,
        is_genuine_execution=is_genuine,
        is_iceberg_reload=is_iceberg,
        wall_age_ms=wall_age_ms,
        wall_persisted=persisted,
        flow_score_pts=round(min(30.0, pts), 2),
        status_label=label,
    )


def compute_vpin(
    buy_volumes: Sequence[int],
    sell_volumes: Sequence[int],
) -> float:
    """Compute Volume-Synchronized Probability of Informed Trading (VPIN) in ``[0, 1]``."""
    if not buy_volumes or len(buy_volumes) != len(sell_volumes):
        return 0.0
    imbalances = [abs(b - s) for b, s in zip(buy_volumes, sell_volumes, strict=True)]
    totals = [max(1, b + s) for b, s in zip(buy_volumes, sell_volumes, strict=True)]
    tot_v = sum(totals)
    return round(sum(imbalances) / tot_v, 4) if tot_v > 0 else 0.0


def detect_order_book_flip(
    *,
    prev_bids: Sequence[Level],
    prev_asks: Sequence[Level],
    curr_bids: Sequence[Level],
    curr_asks: Sequence[Level],
    wall_mult_threshold: float = 2.0,
) -> tuple[bool, str, float]:
    """Detect if a broken Ask Wall flipped into a dominant Bid Wall (or vice versa)."""
    if not prev_bids or not prev_asks or not curr_bids or not curr_asks:
        return False, "NONE", 0.0

    prev_ask_avg = sum(lv[1] for lv in prev_asks) / len(prev_asks)
    curr_bid_avg = sum(lv[1] for lv in curr_bids) / len(curr_bids)
    prev_bid_avg = sum(lv[1] for lv in prev_bids) / len(prev_bids)
    curr_ask_avg = sum(lv[1] for lv in curr_asks) / len(curr_asks)

    prev_ask_walls = {
        round(lv[0], 2): lv[1]
        for lv in prev_asks[:5]
        if lv[1] >= wall_mult_threshold * max(1.0, prev_ask_avg)
    }
    for lv in curr_bids[:5]:
        px = round(lv[0], 2)
        if px in prev_ask_walls and lv[1] >= wall_mult_threshold * max(1.0, curr_bid_avg):
            return True, "BULLISH_SUPPORT_FLIP", px

    prev_bid_walls = {
        round(lv[0], 2): lv[1]
        for lv in prev_bids[:5]
        if lv[1] >= wall_mult_threshold * max(1.0, prev_bid_avg)
    }
    for lv in curr_asks[:5]:
        px = round(lv[0], 2)
        if px in prev_bid_walls and lv[1] >= wall_mult_threshold * max(1.0, curr_ask_avg):
            return True, "BEARISH_RESISTANCE_FLIP", px

    return False, "NONE", 0.0


class RollingOrderFlowTracker:
    """Per-instrument rolling tracker for CVD Z-score, VPIN buckets, wall age, and book flips."""

    def __init__(self, *, window: int = 60, vpin_buckets: int = 20) -> None:
        self.window = max(10, window)
        self.deltas: deque[float] = deque(maxlen=self.window)
        self.buy_buckets: deque[int] = deque(maxlen=vpin_buckets)
        self.sell_buckets: deque[int] = deque(maxlen=vpin_buckets)
        self.prev_ltp: float = 0.0
        self.prev_cum_volume: int | None = None
        self.prev_bids: tuple[Level, ...] = ()
        self.prev_asks: tuple[Level, ...] = ()
        self.wall_first_seen_ns: dict[tuple[str, float], int] = {}

    def update(
        self,
        *,
        recv_ns: int,
        ltp: float,
        cum_volume: int | None,
        ltq: int | None,
        bids: Sequence[Level],
        asks: Sequence[Level],
        at_resistance_break: bool = False,
        at_support_sweep_reclaim: bool = False,
        at_upthrust_reject: bool = False,
    ) -> MechanicDetection:
        bid_px = bids[0][0] if bids else ltp
        ask_px = asks[0][0] if asks else ltp
        prev_px = self.prev_ltp or ltp

        # Volume delta calculation
        if cum_volume is not None and self.prev_cum_volume is not None:
            vol_delta = max(0, cum_volume - self.prev_cum_volume)
        else:
            vol_delta = max(0, ltq or 0)
        self.prev_cum_volume = cum_volume if cum_volume is not None else self.prev_cum_volume

        side = classify_lee_ready_side(ltp, prev_px, bid_px, ask_px)
        signed_delta = float(side * vol_delta)
        self.deltas.append(signed_delta)
        if side > 0:
            self.buy_buckets.append(vol_delta)
            self.sell_buckets.append(0)
        else:
            self.buy_buckets.append(0)
            self.sell_buckets.append(vol_delta)

        # Rolling CVD Z-score
        if len(self.deltas) >= 5:
            mean_d = sum(self.deltas) / len(self.deltas)
            sd_d = statistics.pstdev(self.deltas)
            cvd_z = (
                ((signed_delta - mean_d) / sd_d)
                if sd_d > 1e-6
                else (2.5 if abs(signed_delta) > 0 else 0.0)
            )
        else:
            cvd_z = 2.2 if vol_delta > 0 and side > 0 else (-2.2 if vol_delta > 0 else 0.0)

        # Track resting wall age & top-of-book reduction for RER
        book_reduction = 0
        if (
            self.prev_asks
            and asks
            and abs(self.prev_asks[0][0] - asks[0][0]) < 1e-4
            and self.prev_asks[0][1] > asks[0][1]
        ):
            book_reduction = self.prev_asks[0][1] - asks[0][1]
        elif (
            self.prev_bids
            and bids
            and abs(self.prev_bids[0][0] - bids[0][0]) < 1e-4
            and self.prev_bids[0][1] > bids[0][1]
        ):
            book_reduction = self.prev_bids[0][1] - bids[0][1]

        book_snap: BookMicrostructureSnapshot = analyze_book_microstructure(bids, asks)

        # Compute wall age (ms) for strongest near wall
        wall_age_ms = 1000
        active_wall = (
            book_snap.strongest_bid_wall if book_snap.obi_l5 >= 0 else book_snap.strongest_ask_wall
        )
        if active_wall is not None:
            key = (active_wall.side, round(active_wall.price, 2))
            if key not in self.wall_first_seen_ns:
                self.wall_first_seen_ns[key] = recv_ns
                wall_age_ms = 600  # Initial snapshot credit unless spoof-pulled on next tick
            else:
                wall_age_ms = max(600, int((recv_ns - self.wall_first_seen_ns[key]) / 1_000_000))

        anti_spoof = evaluate_anti_spoofing(
            traded_volume_delta=vol_delta,
            book_wall_reduction=book_reduction,
            cvd_z_score=cvd_z,
            wall_age_ms=wall_age_ms,
        )

        # Evaluate 25-point OBI & Air-Pocket score
        obi_abs = abs(book_snap.obi_l5)
        air_ratio = (
            book_snap.overhead_air_pocket_ratio
            if book_snap.obi_l5 >= 0
            else book_snap.underfoot_air_pocket_ratio
        )
        obi_air_pts = 0.0
        if obi_abs >= 0.50:
            obi_air_pts += 15.0
        elif obi_abs >= 0.30:
            obi_air_pts += 9.0
        if air_ratio < 0.55:
            obi_air_pts += 10.0
        elif air_ratio < 0.75:
            obi_air_pts += 5.0

        # Check all 6 core mechanics
        mechanics: list[OrderFlowMechanic] = []
        flipped, flip_dir, flip_px = detect_order_book_flip(
            prev_bids=self.prev_bids,
            prev_asks=self.prev_asks,
            curr_bids=bids,
            curr_asks=asks,
        )

        if anti_spoof.is_spoof_pull:
            mechanics.append(OrderFlowMechanic.SPOOF_LIQUIDITY_PULL)

        if (
            at_support_sweep_reclaim
            and not anti_spoof.is_spoof_pull
            and book_snap.obi_l5 >= 0.35
            and (anti_spoof.is_genuine_execution or anti_spoof.is_iceberg_reload)
        ):
            mechanics.append(OrderFlowMechanic.LIQUIDITY_ABSORPTION)

        if (
            at_upthrust_reject
            and not anti_spoof.is_spoof_pull
            and book_snap.obi_l5 <= -0.35
            and (anti_spoof.is_genuine_execution or anti_spoof.is_iceberg_reload)
        ):
            mechanics.append(OrderFlowMechanic.LIQUIDITY_ABSORPTION)

        if (
            at_resistance_break
            and not anti_spoof.is_spoof_pull
            and book_snap.obi_l5 >= 0.50
            and cvd_z >= 1.8
            and anti_spoof.is_genuine_execution
        ):
            mechanics.append(OrderFlowMechanic.STOP_RUN_SHORT_SQUEEZE)

        if air_ratio < 0.55 and obi_abs >= 0.45 and not anti_spoof.is_spoof_pull:
            mechanics.append(OrderFlowMechanic.LIQUIDITY_VACUUM)

        if flipped and not anti_spoof.is_spoof_pull:
            mechanics.append(OrderFlowMechanic.ORDER_BOOK_FLIP)

        if (at_resistance_break and cvd_z < 0.5 and book_snap.obi_l5 < 0.0) or (
            at_support_sweep_reclaim and cvd_z < -1.5 and book_snap.obi_l5 < -0.30
        ):
            mechanics.append(OrderFlowMechanic.INITIATIVE_EXHAUSTION)

        primary = mechanics[0] if mechanics else OrderFlowMechanic.NEUTRAL
        if primary is OrderFlowMechanic.SPOOF_LIQUIDITY_PULL:
            bias = "BLOCKED"
            rationale = f"Blocked by Anti-Spoofing Filter: {anti_spoof.status_label}"
        elif primary is OrderFlowMechanic.LIQUIDITY_ABSORPTION:
            bias = "LONG" if book_snap.obi_l5 >= 0 else "SHORT"
            rationale = (
                f"Passive Wall Absorption & Reclaim ({bias}): {anti_spoof.status_label}, "
                f"OBI_L5={book_snap.obi_l5:+.2f}, AirPocket={air_ratio:.2f}"
            )
        elif primary is OrderFlowMechanic.STOP_RUN_SHORT_SQUEEZE:
            bias = "LONG"
            rationale = (
                f"Stop-Run & Short-Squeeze Cascade: {anti_spoof.status_label}, "
                f"OBI_L5={book_snap.obi_l5:+.2f}, Overhead AirPocket={air_ratio:.2f}"
            )
        elif primary is OrderFlowMechanic.ORDER_BOOK_FLIP:
            bias = "LONG" if flip_dir == "BULLISH_SUPPORT_FLIP" else "SHORT"
            rationale = f"Order Book Wall Flip ({flip_dir} at {flip_px:.2f})"
        elif primary is OrderFlowMechanic.LIQUIDITY_VACUUM:
            bias = "LONG" if book_snap.obi_l5 >= 0 else "SHORT"
            rationale = f"L6-L25 Liquidity Vacuum ({bias}, AirPocket={air_ratio:.2f})"
        elif primary is OrderFlowMechanic.INITIATIVE_EXHAUSTION:
            bias = "SHORT" if at_resistance_break else "NEUTRAL"
            rationale = f"Initiative Exhaustion / Failed Auction (CVD_Z={cvd_z:+.2f})"
        else:
            bias = (
                "LONG"
                if book_snap.obi_l5 >= 0.25
                else ("SHORT" if book_snap.obi_l5 <= -0.25 else "NEUTRAL")
            )
            rationale = f"Standard flow ({anti_spoof.status_label}, OBI_L5={book_snap.obi_l5:+.2f})"

        self.prev_ltp = ltp
        self.prev_bids = tuple(bids)
        self.prev_asks = tuple(asks)

        return MechanicDetection(
            primary_mechanic=primary,
            active_mechanics=tuple(mechanics),
            direction_bias=bias,
            cvd_delta=round(signed_delta, 2),
            cvd_z_score=round(math.copysign(min(10.0, abs(cvd_z)), cvd_z), 3),
            vpin_toxicity=compute_vpin(list(self.buy_buckets), list(self.sell_buckets)),
            anti_spoof=anti_spoof,
            obi_air_pocket_score_pts=obi_air_pts,
            rationale=rationale,
        )
