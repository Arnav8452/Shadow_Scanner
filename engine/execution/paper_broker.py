"""Depth-Aware L1–L30 PaperBroker with Pre-Trade Liquidity Gate (M5, SPEC §5, §7).

Never mid-price fills!
- ``BUY`` orders fill against the Ask book (walking L1..L30 levels when ``qty > ask_qty_1``).
- ``SELL`` orders fill against the Bid book (walking L1..L30 levels when ``qty > bid_qty_1``).
- Enforces a Pre-Trade Liquidity Gate: rejects entry orders if bid-ask spread exceeds
  ``max_spread_pct`` or total L1–L5 depth on the fill side is ``< min_depth_multiple * qty``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from engine.backtest.costs import ChargeBreakdown, fno_charges
from engine.core.rules import RuleBook
from engine.data.adapters.base import Level
from engine.execution.order_fsm import OrderRecord, OrderState, transition_order


@dataclass(frozen=True, slots=True)
class PaperFillResult:
    order: OrderRecord
    charges: ChargeBreakdown
    slippage_pts: float


class PaperBroker:
    """Realistic depth-walking PaperBroker used in Mode A and Mode B paper testing."""

    def __init__(
        self,
        book: RuleBook,
        *,
        broker_name: str = "paper",
        max_spread_pct: float = 5.0,
        min_depth_multiple: float = 3.0,
    ) -> None:
        self.book = book
        self.broker_name = broker_name
        self.max_spread_pct = max_spread_pct
        self.min_depth_multiple = min_depth_multiple
        self.positions: dict[str, int] = {}
        self.avg_prices: dict[str, float] = {}
        self.realized_gross_inr: float = 0.0
        self.total_charges_inr: float = 0.0
        self.order_history: list[OrderRecord] = []

    def execute_against_depth(
        self,
        order: OrderRecord,
        *,
        bids: Sequence[Level],
        asks: Sequence[Level],
        now: datetime,
        trade_date: date,
        segment: Literal["options", "futures"] = "options",
        exchange: str = "NSE",
        is_exit: bool = False,
    ) -> PaperFillResult:
        """Walk L1–L30 depth to fill ``order`` honestly, applying pre-trade liquidity checks."""
        sent = transition_order(order, OrderState.SENT, now=now)

        if not bids or not asks:
            rej = transition_order(sent, OrderState.REJECTED, now=now, reason="empty_order_book")
            self.order_history.append(rej)
            zero_c = fno_charges(
                self.book,
                trade_date,
                segment=segment,
                side="buy",
                turnover_inr=0.0,
                broker=self.broker_name,
            )
            return PaperFillResult(rej, zero_c, 0.0)

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = 0.5 * (best_bid + best_ask)
        spread_pct = ((best_ask - best_bid) / mid * 100.0) if mid > 0 else 999.0

        is_buy = sent.side.upper() == "BUY"
        ladder = asks if is_buy else bids
        top5_qty = sum(max(0, lv[1]) for lv in ladder[:5])

        # Pre-trade liquidity gate (applied on fresh entries; exits may cross wider books)
        if not is_exit:
            if spread_pct > self.max_spread_pct:
                rej = transition_order(
                    sent,
                    OrderState.REJECTED,
                    now=now,
                    reason=f"spread_pct {spread_pct:.2f}% > max {self.max_spread_pct:.2f}%",
                )
                self.order_history.append(rej)
                zero_c = fno_charges(
                    self.book,
                    trade_date,
                    segment=segment,
                    side="buy",
                    turnover_inr=0.0,
                    broker=self.broker_name,
                )
                return PaperFillResult(rej, zero_c, 0.0)
            if top5_qty < self.min_depth_multiple * sent.qty:
                rej = transition_order(
                    sent,
                    OrderState.REJECTED,
                    now=now,
                    reason=f"thin_top5_depth ({top5_qty} < {self.min_depth_multiple}x {sent.qty})",
                )
                self.order_history.append(rej)
                zero_c = fno_charges(
                    self.book,
                    trade_date,
                    segment=segment,
                    side="buy",
                    turnover_inr=0.0,
                    broker=self.broker_name,
                )
                return PaperFillResult(rej, zero_c, 0.0)

        # Price bound for LIMIT vs MARKET_PROTECTED
        max_px = (
            sent.limit_price * (1.0 + sent.protection_pct / 100.0)
            if sent.order_type.upper() == "MARKET_PROTECTED" and is_buy
            else sent.limit_price
        )
        min_px = (
            sent.limit_price * (1.0 - sent.protection_pct / 100.0)
            if sent.order_type.upper() == "MARKET_PROTECTED" and not is_buy
            else sent.limit_price
        )

        rem = sent.qty
        filled_q = 0
        notional = 0.0
        for px, avail_q, _ in ladder:
            if avail_q <= 0:
                continue
            if is_buy and px > max_px:
                break
            if not is_buy and px < min_px:
                break
            take = min(rem, avail_q)
            filled_q += take
            notional += take * px
            rem -= take
            if rem == 0:
                break

        if filled_q == 0:
            timed_out = transition_order(
                sent, OrderState.TIMED_OUT, now=now, reason="no_fill_at_limit_price"
            )
            self.order_history.append(timed_out)
            zero_c = fno_charges(
                self.book,
                trade_date,
                segment=segment,
                side="buy",
                turnover_inr=0.0,
                broker=self.broker_name,
            )
            return PaperFillResult(timed_out, zero_c, 0.0)

        avg_px = notional / filled_q
        target_state = OrderState.FILLED if filled_q == sent.qty else OrderState.PARTIAL
        final_order = transition_order(
            sent,
            target_state,
            now=now,
            fill_qty_delta=filled_q,
            fill_price=avg_px,
        )
        self.order_history.append(final_order)

        side_lit: Literal["buy", "sell"] = "buy" if is_buy else "sell"
        charges = fno_charges(
            self.book,
            trade_date,
            segment=segment,
            side=side_lit,
            turnover_inr=notional,
            broker=self.broker_name,
            exchange=exchange,
        )
        self.total_charges_inr += charges.total
        self._update_position(sent.symbol, filled_q if is_buy else -filled_q, avg_px)

        ref_px = best_ask if is_buy else best_bid
        slip = abs(avg_px - ref_px)
        return PaperFillResult(final_order, charges, round(slip, 4))

    def _update_position(self, symbol: str, signed_qty: int, fill_px: float) -> None:
        old_q = self.positions.get(symbol, 0)
        old_avg = self.avg_prices.get(symbol, fill_px)

        if old_q == 0 or (old_q > 0 and signed_qty > 0) or (old_q < 0 and signed_qty < 0):
            new_q = old_q + signed_qty
            new_avg = (
                (abs(old_q) * old_avg + abs(signed_qty) * fill_px) / abs(new_q)
                if new_q != 0
                else 0.0
            )
            self.positions[symbol] = new_q
            self.avg_prices[symbol] = new_avg
        else:
            closed_q = min(abs(old_q), abs(signed_qty))
            if old_q > 0:
                self.realized_gross_inr += closed_q * (fill_px - old_avg)
            else:
                self.realized_gross_inr += closed_q * (old_avg - fill_px)
            new_q = old_q + signed_qty
            self.positions[symbol] = new_q
            if new_q == 0:
                self.avg_prices.pop(symbol, None)
            elif (old_q > 0 and new_q < 0) or (old_q < 0 and new_q > 0):
                self.avg_prices[symbol] = fill_px

    @property
    def net_realized_pnl_inr(self) -> float:
        return round(self.realized_gross_inr - self.total_charges_inr, 2)
