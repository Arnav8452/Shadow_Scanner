"""Deterministic Order Finite State Machine (M5, SPEC §7, CLAUDE.md Rule 2).

Enforces:
- Plain ``MARKET`` orders are strictly forbidden; only ``LIMIT`` and ``MARKET_PROTECTED`` are
  permitted.
- Legal state transitions:
  ``NEW -> SENT -> PARTIAL -> FILLED / REJECTED / CANCELLED / TIMED_OUT``
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from engine.core.config import ALLOWED_ORDER_TYPES


class OrderState(StrEnum):
    NEW = "NEW"
    SENT = "SENT"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.TIMED_OUT,
        }


_LEGAL_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.NEW: frozenset({OrderState.SENT, OrderState.REJECTED, OrderState.CANCELLED}),
    OrderState.SENT: frozenset(
        {
            OrderState.PARTIAL,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.TIMED_OUT,
        }
    ),
    OrderState.PARTIAL: frozenset(
        {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED, OrderState.TIMED_OUT}
    ),
    OrderState.FILLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.TIMED_OUT: frozenset(),
}


@dataclass(frozen=True, slots=True)
class OrderRecord:
    order_id: str
    symbol: str
    side: str  # "BUY" or "SELL"
    order_type: str  # "LIMIT" or "MARKET_PROTECTED"
    qty: int
    limit_price: float
    protection_pct: float = 0.5
    state: OrderState = OrderState.NEW
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        ot = self.order_type.upper()
        if ot == "MARKET" or ot not in ALLOWED_ORDER_TYPES:
            allowed = sorted(ALLOWED_ORDER_TYPES)
            raise ValueError(f"Forbidden order_type={self.order_type!r}; must be in {allowed}")
        if self.qty <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        if self.limit_price <= 0:
            raise ValueError(f"limit_price must be > 0, got {self.limit_price}")

    @property
    def remaining_qty(self) -> int:
        return max(0, self.qty - self.filled_qty)


def transition_order(
    order: OrderRecord,
    target: OrderState,
    *,
    now: datetime,
    fill_qty_delta: int = 0,
    fill_price: float = 0.0,
    reason: str = "",
) -> OrderRecord:
    """Validate and apply an FSM transition to ``order``."""
    allowed = _LEGAL_TRANSITIONS.get(order.state, frozenset())
    if target not in allowed:
        raise ValueError(f"Illegal order transition: {order.state.value} -> {target.value}")

    new_filled = order.filled_qty + max(0, fill_qty_delta)
    if new_filled > order.qty:
        raise ValueError(f"Filled quantity {new_filled} exceeds order qty {order.qty}")

    if fill_qty_delta > 0 and fill_price > 0:
        prev_val = order.filled_qty * order.avg_fill_price
        new_avg = (prev_val + fill_qty_delta * fill_price) / new_filled
    else:
        new_avg = order.avg_fill_price

    if target is OrderState.PARTIAL and new_filled == order.qty:
        target = OrderState.FILLED

    return replace(
        order,
        state=target,
        filled_qty=new_filled,
        avg_fill_price=round(new_avg, 4),
        updated_at=now,
        reason=reason or order.reason,
    )
