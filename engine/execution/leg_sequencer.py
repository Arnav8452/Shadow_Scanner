"""Multi-leg option sequencer: hedge-first entry and short-first exit (CLAUDE.md rule 1).

Invariants enforced here:

1. **Defined risk only.** Every ``SELL`` (short) leg must be covered by ``BUY`` legs of the same
   option type (CE covers CE, PE covers PE) for at least its quantity; otherwise
   :func:`sequence_entry_legs` refuses the whole structure. A ``SELL`` leg can never be marked as a
   protective hedge.
2. **Hedge-first entry.** All ``BUY`` legs go in stage 1; ``SELL`` legs go in stage 2 and are sent
   only after the hedges fill. On partial hedge fills, :func:`cap_short_legs_to_filled_hedges`
   caps each short to the *filled* quantity of hedges of its own option type, in whole lots — so a
   filled put wing can never "cover" a short call.
3. **Short-first exit.** Shorts are bought back first; long wings are sold last.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace

from engine.core.config import ALLOWED_ORDER_TYPES

# An option symbol ends in <digits>CE / <digits>PE, optionally followed by a tag like "_FAR".
_OPTION_TYPE_RE = re.compile(r"\d(CE|PE)(?:_[A-Z0-9]+)?$")


@dataclass(frozen=True, slots=True)
class SpreadLegIntent:
    symbol: str
    side: str  # "BUY" or "SELL"
    qty: int
    limit_price: float
    is_protective_hedge: bool
    order_type: str = "LIMIT"  # "LIMIT" or "MARKET_PROTECTED" (plain "MARKET" is forbidden)
    option_type: str | None = None  # "CE" / "PE"; inferred from the symbol when omitted
    # A futures leg. A short future is defined-risk only when a long CALL of at least its
    # quantity is already held (the call caps the loss on a rise): it needs "CE" cover.
    is_future: bool = False

    def __post_init__(self) -> None:
        ot = self.order_type.upper()
        if ot == "MARKET" or ot not in ALLOWED_ORDER_TYPES:
            allowed = sorted(ALLOWED_ORDER_TYPES)
            raise ValueError(f"Forbidden order_type={self.order_type!r}; must be in {allowed}")
        side = self.side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {self.side!r}")
        if side == "SELL" and self.is_protective_hedge:
            raise ValueError(f"{self.symbol}: a SELL leg can never be a protective hedge")
        if self.qty <= 0:
            raise ValueError(f"{self.symbol}: qty must be > 0, got {self.qty}")
        if self.option_type is not None and self.option_type.upper() not in ("CE", "PE"):
            raise ValueError(f"option_type must be CE or PE, got {self.option_type!r}")

    @property
    def cover_type(self) -> str | None:
        """The option type that must already be bought to cover this leg when it is short."""
        return "CE" if self.is_future else self.opt_type

    @property
    def opt_type(self) -> str | None:
        if self.option_type is not None:
            return self.option_type.upper()
        m = _OPTION_TYPE_RE.search(self.symbol.upper())
        return m.group(1) if m else None


class NakedShortError(ValueError):
    """A structure would leave a short option without a filled hedge of the same type."""


def _coverage(legs: Sequence[SpreadLegIntent]) -> dict[str | None, int]:
    cover: dict[str | None, int] = defaultdict(int)
    for lg in legs:
        if lg.side.upper() == "BUY" and lg.opt_type is not None and not lg.is_future:
            cover[lg.opt_type] += lg.qty
    return cover


def sequence_entry_legs(
    legs: Sequence[SpreadLegIntent],
) -> tuple[list[SpreadLegIntent], list[SpreadLegIntent]]:
    """Split entry legs into ``(stage_1_buys, stage_2_shorts)``; refuse uncovered shorts."""
    buys = [lg for lg in legs if lg.side.upper() == "BUY"]
    shorts = [lg for lg in legs if lg.side.upper() == "SELL"]
    cover = _coverage(buys)
    need: dict[str | None, int] = defaultdict(int)
    for sh in shorts:
        need[sh.cover_type] += sh.qty
    for typ, qty in need.items():
        if typ is None or cover.get(typ, 0) < qty:
            raise NakedShortError(
                f"short {typ or 'non-option'} quantity {qty} exceeds same-type hedge "
                f"quantity {cover.get(typ, 0)}; defined risk only (CLAUDE.md rule 1)"
            )
    return buys, shorts


def cap_short_legs_to_filled_hedges(
    shorts: Sequence[SpreadLegIntent],
    filled_hedges: Sequence[SpreadLegIntent],
    *,
    lot_size: int = 1,
) -> list[SpreadLegIntent]:
    """Cap each short to the filled quantity of hedges of its own option type (whole lots).

    ``filled_hedges`` are the stage-1 legs with ``qty`` set to the quantity actually filled.
    Shorts that cannot be covered (other type, non-option, nothing filled) are dropped.
    """
    lot = max(1, lot_size)
    available = _coverage(filled_hedges)
    capped: list[SpreadLegIntent] = []
    for sh in shorts:
        typ = sh.cover_type  # a short future needs long-call cover
        if typ is None:
            continue
        q = (min(sh.qty, available.get(typ, 0)) // lot) * lot
        if q > 0:
            capped.append(replace(sh, qty=q))
            available[typ] -= q
    return capped


def sequence_exit_legs(
    open_legs: Sequence[SpreadLegIntent],
) -> tuple[list[SpreadLegIntent], list[SpreadLegIntent]]:
    """Order exit legs so short risk legs are bought back *first*, long wings sold *second*."""
    close_shorts_first: list[SpreadLegIntent] = []
    close_longs_second: list[SpreadLegIntent] = []
    for lg in open_legs:
        if lg.side.upper() == "SELL":
            close_shorts_first.append(replace(lg, side="BUY", is_protective_hedge=False))
        else:
            close_longs_second.append(replace(lg, side="SELL", is_protective_hedge=False))
    return close_shorts_first, close_longs_second


def emergency_flatten_filled_hedges(
    filled_hedges: Sequence[SpreadLegIntent],
) -> list[SpreadLegIntent]:
    """``MARKET_PROTECTED`` exits for filled hedges when the stage-2 shorts fail or time out."""
    return [
        replace(
            lg,
            side="SELL" if lg.side.upper() == "BUY" else "BUY",
            order_type="MARKET_PROTECTED",
            is_protective_hedge=False,
        )
        for lg in filled_hedges
        if lg.qty > 0
    ]
