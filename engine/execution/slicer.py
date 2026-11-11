"""Freeze-quantity slicing (CLAUDE.md rule 3). Pure functions; limits come from
``config/rules/freeze_qty.csv`` and lot sizes from the instrument master / ``lot_sizes.csv``.

NSE circulars publish a raw "quantity freeze limit". Whether an order exactly AT the limit is
accepted is not stated, so we stay strictly below it — conservative by one lot at most.
"""

from __future__ import annotations


def max_order_qty(freeze_limit: int, lot_size: int) -> int:
    """Largest whole-lot quantity strictly below the freeze limit."""
    if freeze_limit <= 0 or lot_size <= 0:
        raise ValueError("freeze_limit and lot_size must be > 0")
    lots = (freeze_limit - 1) // lot_size
    if lots < 1:
        raise ValueError(f"freeze limit {freeze_limit} is below one lot ({lot_size})")
    return lots * lot_size


def slice_quantity(total_qty: int, freeze_limit: int, lot_size: int) -> list[int]:
    """Split ``total_qty`` (a whole number of lots) into child orders below the freeze limit."""
    if total_qty <= 0:
        raise ValueError("total_qty must be > 0")
    if total_qty % lot_size:
        raise ValueError(f"quantity {total_qty} is not a multiple of lot size {lot_size}")
    cap = max_order_qty(freeze_limit, lot_size)
    full, rest = divmod(total_qty, cap)
    return [cap] * full + ([rest] if rest else [])
