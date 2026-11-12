"""Data models for Version 1 ("Shadow Order Book" Scanner) and the Shadow Ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from engine.analytics.order_flow import OrderFlowMechanic


class SignalWave(StrEnum):
    STOP_HUNT_RECLAIM = "STOP_HUNT_RECLAIM"  # Wave 1: Wyckoff Spring / Upthrust Reclaim
    WALL_BREAKOUT_SQUEEZE = "WALL_BREAKOUT_SQUEEZE"  # Wave 2: Stop-Run & Short-Squeeze Cascade
    INITIATIVE_EXHAUSTION_FADE = "INITIATIVE_EXHAUSTION_FADE"  # Wave 3: Failed Auction Trap
    VWAP_DELIVERY_RECLAIM = (
        "VWAP_DELIVERY_RECLAIM"  # Multi-day Delivery Spike + Intraday VWAP Reclaim
    )
    CAS_3PM_VWAP_REVERSAL = (
        "CAS_3PM_VWAP_REVERSAL"  # 15:00-15:40 Closing Auction Institutional Reversal
    )
    SPOOF_RUG_PULL = "SPOOF_RUG_PULL"  # a displayed wall pulled without trades near price
    EATEN_WALL_SQUEEZE = "EATEN_WALL_SQUEEZE"  # a wall traded away: the side that ate it wins


class ShadowSignalStatus(StrEnum):
    PENDING_TRIGGER = "PENDING_TRIGGER"
    ACTIVE = "ACTIVE"
    HIT_T1 = "HIT_T1"
    HIT_T2 = "HIT_T2"
    HIT_SL = "HIT_SL"
    INVALIDATED_DELTA_FLIP = "INVALIDATED_DELTA_FLIP"
    EXPIRED_TIME_DECAY = "EXPIRED_TIME_DECAY"
    SQUARED_OFF_EOD = "SQUARED_OFF_EOD"  # still open at the square-off time or session end
    BLOCKED_SPOOF = "BLOCKED_SPOOF"


class SignalGrade(StrEnum):
    A_PLUS = "A_PLUS"  # Score >= 80
    ACTIONABLE = "ACTIONABLE"  # Score 75.0 - 79.9
    WATCHLIST = "WATCHLIST"  # Score 65.0 - 74.9
    NOISE = "NOISE"  # Score < 65.0 or Spoof Blocked


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    """Exact 4-Pillar 100-Point Composite Confidence Formula (PDF §8.1.3)."""

    structural_location_pts: float  # [0, 25]: Stop-pool sweep/reclaim of PDL/PDH/ORL/ORH/OI Wall
    order_flow_anti_spoof_pts: float  # [0, 30]: RER >= 0.75 + Wall Age >= 500ms + |CVD_Z| >= 2.0
    obi_air_pocket_pts: float  # [0, 25]: |OBI_L5| >= 0.50 + L6-L25 Air-Pocket < 0.55
    cross_asset_derivative_pts: float  # [0, 20]: Futures 4-Quad OI + GEX + Breadth + EOD Delivery

    @property
    def total_score(self) -> float:
        return round(
            min(
                100.0,
                max(
                    0.0,
                    self.structural_location_pts
                    + self.order_flow_anti_spoof_pts
                    + self.obi_air_pocket_pts
                    + self.cross_asset_derivative_pts,
                ),
            ),
            2,
        )


@dataclass(frozen=True, slots=True)
class ShadowSignal:
    signal_id: str
    created_at: datetime
    symbol: str
    direction: str  # "LONG" or "SHORT"
    wave: SignalWave
    mechanic: OrderFlowMechanic
    status: ShadowSignalStatus
    grade: SignalGrade
    confidence_score: float
    breakdown: ConfidenceBreakdown
    ltp_at_signal: float
    entry_price: float
    stop_loss: float
    target_1: float
    target_2: float
    rr_t1: float
    rr_t2: float
    sl_wall_info: str
    target_wall_info: str
    real_execution_ratio: float
    obi_l5: float
    air_pocket_ratio: float
    cvd_z_score: float
    futures_oi_quadrant: str
    recommended_risk_pct: float
    recommended_qty: int
    rationale: str
    # False while a tradeable grade has not yet held for ``signal_confirm_s`` (flicker).
    confirmed: bool = True
    entry_slippage_inr: float = 0.0  # paid vs mid when the paper entry filled (whole qty)
    entry_thin_book: bool = False  # the visible book could not fill the whole qty


@dataclass(frozen=True, slots=True)
class ShadowLedgerOutcome:
    signal_id: str
    symbol: str
    direction: str
    wave: SignalWave
    confidence_score: float
    status: ShadowSignalStatus
    entry_price: float
    exit_price: float
    stop_loss: float
    target_1: float
    target_2: float
    qty: int
    gross_pnl_inr: float
    charges_inr: float
    net_pnl_inr: float
    r_multiple_net: float
    opened_at: datetime
    closed_at: datetime
    exit_reason: str
    slippage_inr: float = 0.0  # entry + exit fills vs mid, for the whole quantity
    net_pnl_2x_slippage_inr: float = 0.0  # net with slippage doubled (CLAUDE.md stress)
    thin_book: bool = False  # a fill walked past the visible book
    tradable: bool = True  # False for SHORT signals on options (would be a naked short)
