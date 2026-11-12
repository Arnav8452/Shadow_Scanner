"""Version 1 ("Shadow Order Book" Scanner) & Forward-Testing Shadow Ledger.

Implements the complete Human-in-the-Loop Radar & Microstructure Pricing Engine (PDF §5–§8):
1. Maintains an in-memory ``InstrumentMicrostructureState`` for each watched symbol (up to 50
   concurrent ``full_d30`` 30-level depth streams on Upstox V3, or Fyers/Dhan/Sim).
2. Evaluates all 5 institutional setup waves:
   - ``STOP_HUNT_RECLAIM`` (Wyckoff Spring below PDL/ORL/EQL/Put Wall or Upthrust above PDH/ORH)
   - ``WALL_BREAKOUT_SQUEEZE`` (Stop-Run & Short-Squeeze Cascade through overhead resistance)
   - ``INITIATIVE_EXHAUSTION_FADE`` (Failed Auction / Bull or Bear Trap)
   - ``VWAP_DELIVERY_RECLAIM`` (3-5d EOD Relative Delivery Spike stock reclaiming intraday VWAP)
   - ``CAS_3PM_VWAP_REVERSAL`` (3:00 PM – 3:30 PM / 15:10–15:40 Closing VWAP window institutional
     rebalance when intraday trend diverges from Closing Equilibrium ``iep`` / ``atp``).
3. Computes microstructure-anchored **Entry**, **Stop-Loss** (1–2 ticks *behind* the strongest
   L1–L15 defending Bid/Ask Wall or sweep extreme), **Target 1** (≥ 1.5R, 1–2 ticks *in front of*
   the next L10–L30 opposing wall or VWAP), and **Target 2** (≥ 2.5R–2.8R).
4. Forward paper-tests every actionable signal in the **Shadow Ledger**, tracking transitions
   ``PENDING_TRIGGER -> ACTIVE -> HIT_T1 / HIT_T2 / HIT_SL / INVALIDATED_DELTA_FLIP /
   SQUARED_OFF_EOD``. Fills walk the recorded book; costs come from config/rules for the
   configured brokerage plan; each trade also carries net P&L at 2x slippage. Naked option
   shorts and entries after the cut-off time are scored but never paper-traded.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Any

from engine.analytics.microstructure import BookMicrostructureSnapshot, analyze_book_microstructure
from engine.analytics.order_flow import (
    MechanicDetection,
    OrderFlowMechanic,
    RollingOrderFlowTracker,
)
from engine.analytics.positioning import (
    FuturesOIBuildup,
    OIBuildupQuadrant,
    classify_futures_oi_buildup,
)
from engine.analytics.technicals import (
    StopPoolKind,
    StopPoolLevel,
    compute_vwap_bands,
    evaluate_stop_pool_sweep,
)
from engine.backtest.costs import fno_charges
from engine.core.config import REPO_ROOT, ShadowScannerConfig
from engine.core.rules import IST, RuleBook
from engine.data.adapters.base import Depth, Level, Quote
from engine.data.eod.bhavcopy import DeliveryBias
from engine.shadow.models import (
    ConfidenceBreakdown,
    ShadowLedgerOutcome,
    ShadowSignal,
    ShadowSignalStatus,
    SignalGrade,
    SignalWave,
)


@dataclass
class InstrumentMicrostructureState:
    symbol: str
    tick_size: float = 0.05
    lot_size: int = 1
    pdh: float = 0.0
    pdl: float = 0.0
    prev_close: float = 0.0
    orh_15m: float = 0.0
    orl_15m: float = 0.0
    call_oi_wall: float = 0.0
    put_oi_wall: float = 0.0
    delivery_bias: DeliveryBias = DeliveryBias.NEUTRAL
    gex_regime: str = "POSITIVE_GAMMA_PIN"
    breadth_aligned: bool = False  # unknown until constituent breadth is fed (no free points)
    # From the instrument master. Broker keys such as Upstox "NSE_FO|54749" do not show the
    # contract type, so the symbol pattern is only a fallback when this is unknown.
    is_option: bool | None = None

    # Live intraday state
    ltp: float = 0.0
    session_open: float = 0.0
    session_high: float = 0.0
    session_low: float = 0.0
    cum_volume: int = 0
    oi_now: int = 0
    iep: float | None = None
    iiq_total: int | None = None
    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()
    last_recv_ns: int = 0

    def __post_init__(self) -> None:
        self.prices: deque[float] = deque(maxlen=300)
        self.volumes: deque[int] = deque(maxlen=300)
        # (recv_ns, price, oi) recorded only when OI changes: exchanges publish OI less often than
        # prices, so OI build-up compares price at the moments OI actually updated.
        self.oi_history: deque[tuple[int, float, int]] = deque(maxlen=600)
        self.flow_tracker = RollingOrderFlowTracker(window=60)

    def stop_pools(self) -> list[StopPoolLevel]:
        pools: list[StopPoolLevel] = []
        if self.pdl > 0:
            pools.append(StopPoolLevel(StopPoolKind.PDL, self.pdl, is_downside_pool=True))
        if self.pdh > 0:
            pools.append(StopPoolLevel(StopPoolKind.PDH, self.pdh, is_downside_pool=False))
        if self.orl_15m > 0:
            pools.append(StopPoolLevel(StopPoolKind.ORL_15M, self.orl_15m, is_downside_pool=True))
        if self.orh_15m > 0:
            pools.append(StopPoolLevel(StopPoolKind.ORH_15M, self.orh_15m, is_downside_pool=False))
        if self.put_oi_wall > 0:
            pools.append(
                StopPoolLevel(StopPoolKind.PUT_OI_WALL, self.put_oi_wall, is_downside_pool=True)
            )
        if self.call_oi_wall > 0:
            pools.append(
                StopPoolLevel(StopPoolKind.CALL_OI_WALL, self.call_oi_wall, is_downside_pool=False)
            )
        return pools


_OPTION_SYMBOL_RE = re.compile(r"\d(CE|PE)(?:_[A-Z0-9]+)?$")


OI_WINDOW_NS = 180 * 1_000_000_000  # compare OI over ~3 minutes
OI_MIN_SPAN_NS = 120 * 1_000_000_000  # and never over less than 2 minutes


def oi_window(
    history: Sequence[tuple[int, float, int]], window_ns: int, min_span_ns: int
) -> tuple[tuple[int, float, int], tuple[int, float, int]] | None:
    """(earlier, latest) OI updates about ``window_ns`` apart, or None.

    ``history`` holds (recv_ns, price, oi) at each OI *change*. The earlier point is the last
    update at or before ``latest - window_ns``; prices are those seen at the same instants, so a
    fresh price move is never compared against a stale OI print.
    """
    if len(history) < 2:
        return None
    latest = history[-1]
    target = latest[0] - window_ns
    earlier = None
    for h in history:
        if h[0] <= target:
            earlier = h
        else:
            break
    if earlier is None or latest[0] - earlier[0] < min_span_ns:
        return None
    return earlier, latest


def _round_tick(price: float, tick: float) -> float:
    t = max(tick, 0.01)
    return round(round(price / t) * t, 2)


def compute_microstructure_sl_targets(
    *,
    direction: str,
    ltp: float,
    tick_size: float,
    session_low: float,
    session_high: float,
    vwap: float,
    book: BookMicrostructureSnapshot,
    sl_tick_buffer: int = 2,
    min_rr_t1: float = 1.5,
    min_rr_t2: float = 2.5,
) -> tuple[float, float, float, float, float, float, str, str]:
    """Compute entry, SL behind L1–L15 structural wall, and T1/T2 in front of L10–L30 walls."""
    tick = max(tick_size, 0.05)
    buf = sl_tick_buffer * tick
    is_long = direction.upper() == "LONG"

    if is_long:
        entry = _round_tick(ltp + tick, tick)
        # Anchor SL behind strongest L1-L15 bid wall or sweep low
        wall = book.strongest_bid_wall
        if wall is not None and wall.price < entry:
            raw_sl = min(
                wall.price - buf, session_low - buf if session_low > 0 else wall.price - buf
            )
            sl_info = (
                f"Behind L{wall.level_index} Bid Wall "
                f"({wall.price:.2f}, {wall.qty} qty, {wall.multiple_of_avg:.1f}x avg)"
            )
        else:
            anchor = session_low if (0 < session_low < entry) else entry * 0.996
            raw_sl = anchor - buf
            sl_info = f"Below Sweep Low ({anchor:.2f})"

        # Clamp risk between 0.12% and 0.80% of entry price for intraday precision
        min_risk = max(2.0 * tick, entry * 0.0012)
        max_risk = entry * 0.0080
        risk = max(min_risk, min(max_risk, entry - raw_sl))
        sl = _round_tick(entry - risk, tick)
        risk = max(tick, entry - sl)

        # Anchor T1 in front of L10-L30 deep ask wall or VWAP, enforcing >= min_rr_t1
        deep_ask = book.deep_ask_wall_l10_l30
        min_t1 = entry + min_rr_t1 * risk
        if deep_ask is not None and deep_ask.price - tick >= min_t1:
            t1 = _round_tick(deep_ask.price - tick, tick)
            t1_info = (
                f"Front of L{deep_ask.level_index} Ask Wall "
                f"({deep_ask.price:.2f}, {deep_ask.qty} qty)"
            )
        elif vwap > min_t1:
            t1 = _round_tick(vwap, tick)
            t1_info = f"Session VWAP Magnet ({vwap:.2f})"
        else:
            t1 = _round_tick(min_t1, tick)
            t1_info = f"Projected {min_rr_t1:.1f}R Structural Target"

        t2 = _round_tick(max(t1 + risk, entry + min_rr_t2 * risk), tick)
        rr1 = round((t1 - entry) / risk, 2)
        rr2 = round((t2 - entry) / risk, 2)
    else:
        entry = _round_tick(ltp - tick, tick)
        wall = book.strongest_ask_wall
        if wall is not None and wall.price > entry:
            raw_sl = max(
                wall.price + buf, session_high + buf if session_high > entry else wall.price + buf
            )
            sl_info = (
                f"Behind L{wall.level_index} Ask Wall "
                f"({wall.price:.2f}, {wall.qty} qty, {wall.multiple_of_avg:.1f}x avg)"
            )
        else:
            anchor = session_high if session_high > entry else entry * 1.004
            raw_sl = anchor + buf
            sl_info = f"Above Upthrust High ({anchor:.2f})"

        min_risk = max(2.0 * tick, entry * 0.0012)
        max_risk = entry * 0.0080
        risk = max(min_risk, min(max_risk, raw_sl - entry))
        sl = _round_tick(entry + risk, tick)
        risk = max(tick, sl - entry)

        deep_bid = book.deep_bid_wall_l10_l30
        max_t1 = entry - min_rr_t1 * risk
        if deep_bid is not None and 0 < deep_bid.price + tick <= max_t1:
            t1 = _round_tick(deep_bid.price + tick, tick)
            t1_info = (
                f"Front of L{deep_bid.level_index} Bid Wall "
                f"({deep_bid.price:.2f}, {deep_bid.qty} qty)"
            )
        elif 0 < vwap < max_t1:
            t1 = _round_tick(vwap, tick)
            t1_info = f"Session VWAP Magnet ({vwap:.2f})"
        else:
            t1 = _round_tick(max_t1, tick)
            t1_info = f"Projected {min_rr_t1:.1f}R Structural Target"

        t2 = _round_tick(min(t1 - risk, entry - min_rr_t2 * risk), tick)
        rr1 = round((entry - t1) / risk, 2)
        rr2 = round((entry - t2) / risk, 2)

    return entry, sl, t1, t2, rr1, rr2, sl_info, t1_info


class ShadowOrderBookScanner:
    """Version 1 Human-in-the-Loop Shadow Order Book Scanner & Forward-Testing Ledger."""

    def __init__(
        self,
        config: ShadowScannerConfig | None = None,
        *,
        capital_inr: float = 200_000.0,
        max_lots: int = 1,
        book: RuleBook | None = None,
        cost_broker: str | None = None,
    ) -> None:
        self.config = config or ShadowScannerConfig()
        self.capital_inr = capital_inr
        self.max_lots = max(1, max_lots)  # CLAUDE.md rule 5: one lot until 20-30 live trades
        self._book = book
        # Brokerage plan the paper costs assume (config: shadow_scanner.cost_broker).
        self.cost_broker = cost_broker or self.config.cost_broker
        self.states: dict[str, InstrumentMicrostructureState] = {}
        self.active_signals: dict[str, ShadowSignal] = {}
        self.watchlist_signals: dict[str, ShadowSignal] = {}
        self.closed_outcomes: list[ShadowLedgerOutcome] = []
        # Signals that were scored but not paper-traded, by reason (shown in the summary).
        self.not_paper_traded: dict[str, int] = {}
        # (symbol, direction) -> when it first reached a tradeable grade without dropping below.
        self._tradeable_since: dict[tuple[str, str], datetime] = {}
        self._seq = 0

    def register_instrument(self, state: InstrumentMicrostructureState) -> None:
        if (
            state.symbol not in self.states
            and len(self.states) >= self.config.max_watchlist_symbols
        ):
            cap = self.config.max_watchlist_symbols
            raise ValueError(f"Shadow scanner cap ({cap}) reached; cannot add {state.symbol}")
        self.states[state.symbol] = state

    def empirical_quarter_kelly_risk_pct(self) -> float:
        """Compute rolling empirical Quarter-Kelly risk % from closed Shadow trades (PDF §7.1).

        Formula:
            f* = p - (1 - p) / b
            Risk_pct = min(kelly_max_risk_pct, max(0.25, kelly_fraction * f* * 100))
        Uses a conservative 0.50% cold-start default until at least 10 trades are logged.
        """
        window = self.closed_outcomes[-self.config.kelly_rolling_window :]
        if len(window) < 10:
            return 0.50
        wins = [o.net_pnl_inr for o in window if o.net_pnl_inr > 0]
        losses = [abs(o.net_pnl_inr) for o in window if o.net_pnl_inr <= 0]
        if not wins:
            return 0.25
        if not losses:
            return self.config.kelly_max_risk_pct
        p = len(wins) / len(window)
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        b = (avg_win / avg_loss) if avg_loss > 0 else 1.5
        f_star = p - (1.0 - p) / b
        if f_star <= 0:
            return 0.0
        q_kelly_pct = self.config.kelly_fraction * f_star * 100.0
        return round(min(self.config.kelly_max_risk_pct, max(0.25, q_kelly_pct)), 3)

    def on_quote(self, quote: Quote) -> None:
        st = self.states.get(quote.symbol)
        if st is None:
            st = InstrumentMicrostructureState(symbol=quote.symbol)
            self.states[quote.symbol] = st

        if quote.ltp is not None and quote.ltp > 0:
            st.ltp = quote.ltp
            if st.session_open <= 0:
                st.session_open = quote.open or quote.ltp
            st.session_high = max(st.session_high or quote.ltp, quote.high or quote.ltp, quote.ltp)
            st.session_low = (
                min(st.session_low, quote.low or quote.ltp, quote.ltp)
                if st.session_low > 0
                else (quote.low or quote.ltp)
            )
            vol_step = max(
                1,
                (quote.volume - st.cum_volume)
                if (quote.volume and st.cum_volume)
                else (quote.ltq or 1),
            )
            st.prices.append(quote.ltp)
            st.volumes.append(vol_step)
        if quote.volume is not None:
            st.cum_volume = quote.volume
        if quote.prev_close is not None and st.prev_close <= 0:
            st.prev_close = quote.prev_close
        if quote.oi is not None and quote.oi > 0:
            if quote.oi != st.oi_now or not st.oi_history:
                st.oi_history.append((quote.recv_ns, st.ltp, quote.oi))
            st.oi_now = quote.oi
        if quote.iep is not None:
            st.iep = quote.iep
        if quote.iiq_total is not None:
            st.iiq_total = quote.iiq_total
        st.last_recv_ns = quote.recv_ns

    def on_depth(self, depth: Depth, *, now: datetime | None = None) -> ShadowSignal | None:
        st = self.states.get(depth.symbol)
        if st is None:
            st = InstrumentMicrostructureState(symbol=depth.symbol)
            self.states[depth.symbol] = st

        st.bids = depth.bids[: self.config.depth_levels]
        st.asks = depth.asks[: self.config.depth_levels]
        st.last_recv_ns = depth.recv_ns
        if st.ltp <= 0 and st.bids and st.asks:
            st.ltp = round(0.5 * (st.bids[0][0] + st.asks[0][0]), 2)
            st.session_high = max(st.session_high, st.ltp)
            st.session_low = st.ltp if st.session_low <= 0 else min(st.session_low, st.ltp)

        ts = now or datetime.fromtimestamp(depth.recv_ns / 1_000_000_000, tz=IST)
        self._update_active_ledger(st, ts)
        return self.evaluate_instrument(st.symbol, now=ts)

    def evaluate_instrument(
        self, symbol: str, *, now: datetime | None = None
    ) -> ShadowSignal | None:
        """Score an instrument on the 100-Point Confidence Formula and emit ShadowSignal."""
        st = self.states.get(symbol)
        if st is None or st.ltp <= 0 or not st.bids or not st.asks:
            return None
        ts = now or datetime.now(IST)

        book: BookMicrostructureSnapshot = analyze_book_microstructure(st.bids, st.asks)
        vwap_bands = compute_vwap_bands(list(st.prices), list(st.volumes))
        vwap = vwap_bands.vwap or st.ltp

        # Evaluate both LONG and SHORT structural location
        pools = st.stop_pools()
        sweep_long = evaluate_stop_pool_sweep(
            ltp=st.ltp,
            session_low=st.session_low,
            session_high=st.session_high,
            pools=pools,
            direction="LONG",
            min_sweep_pct=self.config.stop_pool_band_min_pct * 0.5,
            max_sweep_pct=self.config.stop_pool_band_max_pct * 1.25,
        )
        sweep_short = evaluate_stop_pool_sweep(
            ltp=st.ltp,
            session_low=st.session_low,
            session_high=st.session_high,
            pools=pools,
            direction="SHORT",
            min_sweep_pct=self.config.stop_pool_band_min_pct * 0.5,
            max_sweep_pct=self.config.stop_pool_band_max_pct * 1.25,
        )

        at_res_break = (st.orh_15m > 0 and st.ltp > st.orh_15m) or (st.pdh > 0 and st.ltp > st.pdh)
        mech: MechanicDetection = st.flow_tracker.update(
            recv_ns=st.last_recv_ns or int(ts.timestamp() * 1_000_000_000),
            ltp=st.ltp,
            cum_volume=st.cum_volume,
            ltq=st.volumes[-1] if st.volumes else 0,
            bids=st.bids,
            asks=st.asks,
            at_resistance_break=at_res_break,
            at_support_sweep_reclaim=sweep_long.reclaimed_level or sweep_long.in_sweep_zone,
            at_upthrust_reject=sweep_short.reclaimed_level or sweep_short.in_sweep_zone,
        )

        # Determine dominant candidate direction & wave
        if mech.direction_bias == "SHORT" or (
            sweep_short.structural_score_pts > sweep_long.structural_score_pts and book.obi_l5 < 0
        ):
            direction = "SHORT"
            struct_eval = sweep_short
        else:
            direction = "LONG"
            struct_eval = sweep_long

        # Check 3:00 PM - 3:40 PM IST Closing Auction Session (CAS) Institutional VWAP Reversal
        in_cas_window = time(15, 0) <= ts.timetz().replace(tzinfo=None) <= time(15, 40)
        cas_bonus_pts = 0.0
        wave = SignalWave.STOP_HUNT_RECLAIM

        if in_cas_window and (st.iep is not None or vwap > 0):
            eq_px = st.iep or vwap
            div_pct = (eq_px - st.ltp) / st.ltp * 100.0
            if (direction == "LONG" and div_pct >= 0.25 and book.obi_l5 >= 0.40) or (
                direction == "SHORT" and div_pct <= -0.25 and book.obi_l5 <= -0.40
            ):
                wave = SignalWave.CAS_3PM_VWAP_REVERSAL
                cas_bonus_pts = 22.0
        elif mech.primary_mechanic is OrderFlowMechanic.STOP_RUN_SHORT_SQUEEZE or at_res_break:
            wave = SignalWave.WALL_BREAKOUT_SQUEEZE
        elif mech.primary_mechanic is OrderFlowMechanic.INITIATIVE_EXHAUSTION:
            wave = SignalWave.INITIATIVE_EXHAUSTION_FADE
        elif (
            st.delivery_bias is DeliveryBias.LONG_ACCUMULATION
            and direction == "LONG"
            and st.ltp >= vwap
            and not struct_eval.reclaimed_level
        ):
            wave = SignalWave.VWAP_DELIVERY_RECLAIM

        # Pillar 1 (0-25 pts): Structural Location
        p1_pts = max(struct_eval.structural_score_pts, cas_bonus_pts)
        if wave is SignalWave.WALL_BREAKOUT_SQUEEZE and at_res_break:
            p1_pts = max(p1_pts, 22.0)
        if wave is SignalWave.VWAP_DELIVERY_RECLAIM:
            p1_pts = max(p1_pts, 21.0)

        # Pillar 2 (0-30 pts): Order-Flow & Anti-Spoofing Verification
        p2_pts = mech.anti_spoof.flow_score_pts

        # Pillar 3 (0-25 pts): Deep-Book OBI & L6-L25 Air-Pocket Vacuum
        p3_pts = mech.obi_air_pocket_score_pts

        # Pillar 4 (0-20 pts): Cross-Asset Derivative OI Buildup + GEX + Delivery + Breadth
        oi_buildup = self._evaluate_futures_oi(
            st, is_stop_hunt=(wave is SignalWave.STOP_HUNT_RECLAIM)
        )
        p4_pts = (
            oi_buildup.confluence_pts_long
            if direction == "LONG"
            else oi_buildup.confluence_pts_short
        )
        if (direction == "LONG" and st.delivery_bias is DeliveryBias.LONG_ACCUMULATION) or (
            direction == "SHORT" and st.delivery_bias is DeliveryBias.RETAIL_CHURN_FADE
        ):
            p4_pts = min(20.0, p4_pts + 4.0)
        if st.breadth_aligned:
            p4_pts = min(20.0, p4_pts + 2.0)

        # If anti-spoofing detected a phantom wall pull (RER < 0.20), hard-zero the flow score
        if mech.anti_spoof.is_spoof_pull:
            p2_pts = 0.0

        breakdown = ConfidenceBreakdown(
            structural_location_pts=round(min(25.0, p1_pts), 2),
            order_flow_anti_spoof_pts=round(min(30.0, p2_pts), 2),
            obi_air_pocket_pts=round(min(25.0, p3_pts), 2),
            cross_asset_derivative_pts=round(min(20.0, p4_pts), 2),
        )
        score = breakdown.total_score

        if mech.anti_spoof.is_spoof_pull:
            grade = SignalGrade.NOISE
            status = ShadowSignalStatus.BLOCKED_SPOOF
        elif score >= self.config.a_plus_confidence_score:
            grade = SignalGrade.A_PLUS
            status = ShadowSignalStatus.PENDING_TRIGGER
        elif score >= self.config.min_confidence_score:
            grade = SignalGrade.ACTIONABLE
            status = ShadowSignalStatus.PENDING_TRIGGER
        elif score >= self.config.watchlist_score:
            grade = SignalGrade.WATCHLIST
            status = ShadowSignalStatus.PENDING_TRIGGER
        else:
            grade = SignalGrade.NOISE
            status = ShadowSignalStatus.PENDING_TRIGGER

        entry, sl, t1, t2, rr1, rr2, sl_info, t1_info = compute_microstructure_sl_targets(
            direction=direction,
            ltp=st.ltp,
            tick_size=st.tick_size,
            session_low=st.session_low,
            session_high=st.session_high,
            vwap=vwap,
            book=book,
            sl_tick_buffer=self.config.sl_tick_buffer,
            min_rr_t1=self.config.min_rr_t1,
            min_rr_t2=self.config.min_rr_t2,
        )

        # Scores are recomputed on every book update and cross thresholds on noise. A grade
        # counts only after it has held, in the same direction, for signal_confirm_s.
        confirmed = True
        if grade in {SignalGrade.A_PLUS, SignalGrade.ACTIONABLE}:
            for key in [k for k in self._tradeable_since if k[0] == symbol and k[1] != direction]:
                del self._tradeable_since[key]
            since = self._tradeable_since.setdefault((symbol, direction), ts)
            confirmed = (ts - since).total_seconds() >= self.config.signal_confirm_s
        else:
            for key in [k for k in self._tradeable_since if k[0] == symbol]:
                del self._tradeable_since[key]

        risk_pct = self.empirical_quarter_kelly_risk_pct()
        risk_inr = self.capital_inr * (risk_pct / 100.0)
        per_unit_risk = max(st.tick_size, abs(entry - sl))
        raw_units = int(risk_inr / per_unit_risk) if per_unit_risk > 0 else st.lot_size
        # A shadow signal is always shown for one lot so it can be forward-tested, but never
        # more than the per-strategy lot cap. The hard per-trade loss cap is checked by the
        # pre-trade gate before any V2 order.
        lots = min(self.max_lots, max(1, raw_units // max(1, st.lot_size)))
        rec_qty = lots * max(1, st.lot_size)

        self._seq += 1
        sig_id = f"SHDW-{ts.strftime('%Y%m%d-%H%M%S')}-{symbol}-{self._seq:04d}"
        air_ratio = (
            book.overhead_air_pocket_ratio
            if direction == "LONG"
            else book.underfoot_air_pocket_ratio
        )
        rationale = (
            f"[{grade.value} {score:.1f}/100] {wave.value} ({direction}): "
            f"{struct_eval.description} | {mech.rationale} | {oi_buildup.interpretation}"
        )

        sig = ShadowSignal(
            signal_id=sig_id,
            created_at=ts,
            symbol=symbol,
            direction=direction,
            wave=wave,
            mechanic=mech.primary_mechanic,
            status=status,
            grade=grade,
            confidence_score=score,
            breakdown=breakdown,
            ltp_at_signal=st.ltp,
            entry_price=entry,
            stop_loss=sl,
            target_1=t1,
            target_2=t2,
            rr_t1=rr1,
            rr_t2=rr2,
            sl_wall_info=sl_info,
            target_wall_info=t1_info,
            real_execution_ratio=mech.anti_spoof.real_execution_ratio,
            obi_l5=book.obi_l5,
            air_pocket_ratio=air_ratio,
            cvd_z_score=mech.cvd_z_score,
            futures_oi_quadrant=oi_buildup.quadrant.value,
            recommended_risk_pct=risk_pct,
            recommended_qty=rec_qty,
            rationale=rationale,
            confirmed=confirmed,
        )

        if grade in {SignalGrade.A_PLUS, SignalGrade.ACTIONABLE} and confirmed:
            # Keep active trade if already triggered, else update with highest-scoring signal
            existing = self.active_signals.get(symbol)
            if existing is None or existing.status is ShadowSignalStatus.PENDING_TRIGGER:
                skip = self._paper_skip_reason(sig, ts)
                if skip is None:
                    self.active_signals[symbol] = sig
                else:
                    self._count_skip(skip)
        elif grade is SignalGrade.WATCHLIST:
            self.watchlist_signals[symbol] = sig

        return sig

    def _evaluate_futures_oi(
        self, st: InstrumentMicrostructureState, *, is_stop_hunt: bool
    ) -> FuturesOIBuildup:
        pair = oi_window(st.oi_history, OI_WINDOW_NS, OI_MIN_SPAN_NS)
        if pair is None:
            # Not enough OI history to classify a build-up: award no points. (Previously this
            # granted 15 of 20 points to both directions from no data.)
            return FuturesOIBuildup(
                OIBuildupQuadrant.LONG_BUILDUP
                if st.ltp >= st.session_open
                else OIBuildupQuadrant.SHORT_BUILDUP,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                "OI history unavailable (no points)",
            )
        oldest, latest = pair
        return classify_futures_oi_buildup(
            price_now=latest[1],
            price_3m_ago=oldest[1],
            oi_now=latest[2],
            oi_3m_ago=oldest[2],
            is_stop_hunt_reclaim=is_stop_hunt,
        )

    def is_option(self, symbol: str) -> bool:
        st = self.states.get(symbol)
        if st is not None and st.is_option is not None:
            return st.is_option
        return bool(_OPTION_SYMBOL_RE.search(symbol.upper()))

    def is_tradable(self, sig: ShadowSignal) -> bool:
        """A SHORT on an option means selling it unhedged (CLAUDE.md rule 1): never tradable."""
        return not (sig.direction == "SHORT" and self.is_option(sig.symbol))

    def _paper_skip_reason(self, sig: ShadowSignal, ts: datetime) -> str | None:
        """Why a 75+ signal is not tracked in the signal ledger (Track A), or None.

        Track A measures signals, not tradability: SHORT signals on options are tracked and
        tagged ``tradable=False`` (see :meth:`is_tradable`) instead of being dropped, so the
        sample is not censored. The paper accounts (Tracks B/C) never sell options.
        """
        if ts.astimezone(IST).time() >= self.config.paper_no_new_entries_after:
            return "after_no_new_entries_time"
        return None

    def _count_skip(self, reason: str) -> None:
        self.not_paper_traded[reason] = self.not_paper_traded.get(reason, 0) + 1

    def _update_active_ledger(self, st: InstrumentMicrostructureState, ts: datetime) -> None:
        sig = self.active_signals.get(st.symbol)
        if sig is None:
            return
        px = st.ltp
        is_long = sig.direction == "LONG"
        clock = ts.astimezone(IST).time()

        if sig.status is ShadowSignalStatus.PENDING_TRIGGER:
            if clock >= self.config.paper_no_new_entries_after:
                del self.active_signals[st.symbol]  # never triggered: no trade to count
                self._count_skip("pending_cancelled_at_cutoff")
                return
            triggered = (is_long and px >= sig.entry_price) or (
                not is_long and px <= sig.entry_price
            )
            if triggered:
                # Fill by walking the book from the touch (ask for longs, bid for shorts) for
                # the whole quantity, never at LTP (CLAUDE.md).
                fill, slip, thin = self._walk_book(
                    st, buy=is_long, qty=sig.recommended_qty, fallback=px
                )
                self.active_signals[st.symbol] = replace(
                    sig,
                    status=ShadowSignalStatus.ACTIVE,
                    entry_price=fill,
                    entry_slippage_inr=slip,
                    entry_thin_book=thin,
                )
            return

        if sig.status is not ShadowSignalStatus.ACTIVE:
            return

        if clock >= self.config.paper_square_off_at:
            self._close(st, sig, ShadowSignalStatus.SQUARED_OFF_EOD, "Square-off time", ts)
            return

        # Check Stop Loss, Target 2, Target 1, or Delta-Flip structural invalidation
        hit_sl = (is_long and px <= sig.stop_loss) or (not is_long and px >= sig.stop_loss)
        hit_t2 = (is_long and px >= sig.target_2) or (not is_long and px <= sig.target_2)
        hit_t1 = (is_long and px >= sig.target_1) or (not is_long and px <= sig.target_1)

        book = analyze_book_microstructure(st.bids, st.asks)
        delta_flipped = (is_long and book.obi_l5 <= -0.65) or (not is_long and book.obi_l5 >= 0.65)

        if hit_sl:
            status = ShadowSignalStatus.HIT_SL
            reason = f"Hit Microstructure Stop-Loss ({sig.stop_loss:.2f})"
        elif hit_t2:
            status = ShadowSignalStatus.HIT_T2
            reason = f"Hit Target 2 ({sig.target_2:.2f}, {sig.rr_t2:.1f}R)"
        elif hit_t1:
            status = ShadowSignalStatus.HIT_T1
            reason = f"Hit Target 1 ({sig.target_1:.2f}, {sig.rr_t1:.1f}R)"
        elif delta_flipped:
            status = ShadowSignalStatus.INVALIDATED_DELTA_FLIP
            reason = f"Structural Invalidation: Order-Book OBI flipped ({book.obi_l5:+.2f})"
        else:
            return
        self._close(st, sig, status, reason, ts)

    def close_open_signals(self, ts: datetime, reason: str = "Session end") -> int:
        """Square off every ACTIVE paper trade at the current book; drop untriggered ones.

        Called at the square-off time and when the session ends, so open trades never go
        missing from the ledger. Returns the number of trades closed.
        """
        closed = 0
        for symbol, sig in list(self.active_signals.items()):
            st = self.states.get(symbol)
            if sig.status is ShadowSignalStatus.ACTIVE and st is not None:
                self._close(st, sig, ShadowSignalStatus.SQUARED_OFF_EOD, reason, ts)
                closed += 1
            elif sig.status is ShadowSignalStatus.PENDING_TRIGGER:
                del self.active_signals[symbol]
                self._count_skip("pending_cancelled_at_cutoff")
        return closed

    def _close(
        self,
        st: InstrumentMicrostructureState,
        sig: ShadowSignal,
        status: ShadowSignalStatus,
        reason: str,
        ts: datetime,
    ) -> None:
        is_long = sig.direction == "LONG"
        qty = sig.recommended_qty
        # Exit by walking the opposite side of the book (bid for longs, ask for shorts) when the
        # level is hit, not at the stop/target price: gaps and thin books make that optimistic.
        exit_px, exit_slip, exit_thin = self._walk_book(
            st, buy=not is_long, qty=qty, fallback=st.ltp
        )
        gross = (exit_px - sig.entry_price) * qty if is_long else (sig.entry_price - exit_px) * qty
        charges = self._round_trip_charges(
            sig.symbol,
            entry_turnover=sig.entry_price * qty,
            exit_turnover=exit_px * qty,
            is_long=is_long,
            on=ts.astimezone(IST).date(),
        )
        net = gross - charges
        slippage = sig.entry_slippage_inr + exit_slip
        unit_risk = max(st.tick_size, abs(sig.entry_price - sig.stop_loss)) * qty
        r_net = net / unit_risk if unit_risk > 0 else 0.0
        self.closed_outcomes.append(
            ShadowLedgerOutcome(
                signal_id=sig.signal_id,
                symbol=sig.symbol,
                direction=sig.direction,
                wave=sig.wave,
                confidence_score=sig.confidence_score,
                status=status,
                entry_price=sig.entry_price,
                exit_price=round(exit_px, 2),
                stop_loss=sig.stop_loss,
                target_1=sig.target_1,
                target_2=sig.target_2,
                qty=qty,
                gross_pnl_inr=round(gross, 2),
                charges_inr=round(charges, 2),
                net_pnl_inr=round(net, 2),
                r_multiple_net=round(r_net, 2),
                opened_at=sig.created_at,
                closed_at=ts,
                exit_reason=reason,
                slippage_inr=round(slippage, 2),
                net_pnl_2x_slippage_inr=round(net - slippage, 2),
                thin_book=sig.entry_thin_book or exit_thin,
                tradable=self.is_tradable(sig),
            )
        )
        del self.active_signals[sig.symbol]

    @staticmethod
    def _walk_book(
        st: InstrumentMicrostructureState, *, buy: bool, qty: int, fallback: float
    ) -> tuple[float, float, bool]:
        """(average fill price, slippage vs mid in ₹ for the whole qty, ran past the book).

        Takes liquidity level by level from the touch. Quantity beyond the visible book is
        priced at the last visible level and flagged, since its true cost is unknown.
        """
        side = st.asks if buy else st.bids
        remaining = max(0, qty)
        cost = 0.0
        last_px = 0.0
        for px, lvl_qty, _ in side:
            if px <= 0 or lvl_qty <= 0:
                continue
            take = min(remaining, lvl_qty)
            cost += take * px
            remaining -= take
            last_px = px
            if remaining == 0:
                break
        if last_px <= 0:  # no usable book at all
            return fallback, 0.0, True
        thin = remaining > 0
        cost += remaining * last_px
        avg = cost / qty if qty > 0 else last_px
        if st.bids and st.asks and st.bids[0][0] > 0 and st.asks[0][0] > 0:
            mid = 0.5 * (st.bids[0][0] + st.asks[0][0])
        else:
            mid = avg
        slip = (avg - mid) * qty if buy else (mid - avg) * qty
        return avg, max(0.0, slip), thin

    def _round_trip_charges(
        self, symbol: str, *, entry_turnover: float, exit_turnover: float, is_long: bool, on: Any
    ) -> float:
        """Full statutory + broker charges from config/rules (no hardcoded rates)."""
        if self._book is None:
            self._book = RuleBook.load(REPO_ROOT / "config" / "rules")
        segment: Any = "options" if self.is_option(symbol) else "futures"
        buy_turnover, sell_turnover = (
            (entry_turnover, exit_turnover) if is_long else (exit_turnover, entry_turnover)
        )
        total = 0.0
        for side, turnover in (("buy", buy_turnover), ("sell", sell_turnover)):
            total += fno_charges(
                self._book,
                on,
                segment=segment,
                side=side,  # type: ignore[arg-type]
                turnover_inr=turnover,
                broker=self.cost_broker,
            ).total
        return total

    def ranked_signals(self) -> list[ShadowSignal]:
        """Return active/pending Shadow Signals ranked highest confidence score first."""
        return sorted(
            self.active_signals.values(),
            key=lambda s: s.confidence_score,
            reverse=True,
        )

    def ranked_watchlist(self) -> list[ShadowSignal]:
        """Return pre-alert watchlist setups (score 65–74.9) ranked highest score first."""
        return sorted(
            (w for sym, w in self.watchlist_signals.items() if sym not in self.active_signals),
            key=lambda s: s.confidence_score,
            reverse=True,
        )

    def ledger_summary(self) -> dict[str, Any]:
        """Return honest net-of-cost Shadow Ledger statistics."""
        n = len(self.closed_outcomes)
        if n == 0:
            return {
                "total_trades": 0,
                "win_rate_pct": 0.0,
                "gross_pnl_inr": 0.0,
                "charges_inr": 0.0,
                "net_pnl_inr": 0.0,
                "slippage_inr": 0.0,
                "net_pnl_2x_slippage_inr": 0.0,
                "avg_r_multiple_net": 0.0,
                "quarter_kelly_risk_pct": self.empirical_quarter_kelly_risk_pct(),
                "cost_broker": self.cost_broker,
                "not_paper_traded": dict(self.not_paper_traded),
            }
        wins = [o for o in self.closed_outcomes if o.net_pnl_inr > 0]
        gross = sum(o.gross_pnl_inr for o in self.closed_outcomes)
        charges = sum(o.charges_inr for o in self.closed_outcomes)
        net = sum(o.net_pnl_inr for o in self.closed_outcomes)
        avg_r = sum(o.r_multiple_net for o in self.closed_outcomes) / n
        return {
            "total_trades": n,
            "win_rate_pct": round(len(wins) / n * 100.0, 2),
            "gross_pnl_inr": round(gross, 2),
            "charges_inr": round(charges, 2),
            "net_pnl_inr": round(net, 2),
            "slippage_inr": round(sum(o.slippage_inr for o in self.closed_outcomes), 2),
            "net_pnl_2x_slippage_inr": round(
                sum(o.net_pnl_2x_slippage_inr for o in self.closed_outcomes), 2
            ),
            "avg_r_multiple_net": round(avg_r, 3),
            "quarter_kelly_risk_pct": self.empirical_quarter_kelly_risk_pct(),
            "cost_broker": self.cost_broker,
            "not_paper_traded": dict(self.not_paper_traded),
        }
