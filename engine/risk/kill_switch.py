"""Emergency Kill Switch — Broker-Position-Only Short-First Flatten (M8, SPEC §6, CLAUDE.md Rule 2).

Enforces three critical safety rules (approved by owner 11 Nov 2026):
1. Never trusts local in-memory position state during an emergency; builds flatten orders strictly
   from ``broker_reported_positions``.
2. **Short-First Ordering**: Buys back all short positions (``qty < 0``) *before* selling any long
   positions (``qty > 0``) so margin never spikes during the emergency flatten sequence.
3. **No Blind Flatten Without Quotes**: When triggered by a feed disconnect
   (``quotes_available=False``), freezes new entries and alerts the owner rather than submitting
   blind flatten orders without a live quote reference price.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmergencyFlattenIntent:
    stage: int  # 1 = close shorts first, 2 = close longs second
    symbol: str
    side: str  # "BUY" to cover short, "SELL" to liquidate long
    qty: int
    order_type: str = "MARKET_PROTECTED"
    reference_price: float | None = None


def build_kill_switch_plan(
    broker_reported_positions: Mapping[str, int],
    *,
    quotes_available: bool = True,
    reference_prices: Mapping[str, float] | None = None,
) -> list[EmergencyFlattenIntent]:
    """Generate short-first emergency flatten intents from broker-reported positions only.

    When ``quotes_available`` is False (e.g. a data-feed disconnect), returns ``[]`` so the engine
    freezes new entries and alerts the owner instead of firing blind orders without quotes.
    """
    if not quotes_available:
        return []

    stage1_cover_shorts: list[EmergencyFlattenIntent] = []
    stage2_sell_longs: list[EmergencyFlattenIntent] = []

    for sym, signed_qty in sorted(broker_reported_positions.items()):
        ref_px = reference_prices.get(sym) if reference_prices is not None else None
        if signed_qty < 0:
            stage1_cover_shorts.append(
                EmergencyFlattenIntent(
                    stage=1,
                    symbol=sym,
                    side="BUY",
                    qty=abs(signed_qty),
                    order_type="MARKET_PROTECTED",
                    reference_price=ref_px,
                )
            )
        elif signed_qty > 0:
            stage2_sell_longs.append(
                EmergencyFlattenIntent(
                    stage=2,
                    symbol=sym,
                    side="SELL",
                    qty=signed_qty,
                    order_type="MARKET_PROTECTED",
                    reference_price=ref_px,
                )
            )

    return stage1_cover_shorts + stage2_sell_longs
