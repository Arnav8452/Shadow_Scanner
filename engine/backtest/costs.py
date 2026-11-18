"""Transaction cost model driven only by ``config/rules`` (no constants in code).

M0/M13-EA scope: equity delivery (used by the event-arbitrage sidecar). F&O costs land in M5.
Rounding to the contract note (per-component paise, STT to the rupee per scrip) is not yet
applied — golden contract-note tests in M5 will pin it down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from engine.core.rules import RuleBook, RuleRow

Side = Literal["buy", "sell"]
CRORE = 10_000_000.0


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    dp: float
    unverified: tuple[str, ...] = field(default=())  # tables that supplied UNVERIFIED rows

    @property
    def total(self) -> float:
        return (
            self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst + self.dp
        )

    def __add__(self, other: ChargeBreakdown) -> ChargeBreakdown:
        return ChargeBreakdown(
            brokerage=self.brokerage + other.brokerage,
            stt=self.stt + other.stt,
            exchange=self.exchange + other.exchange,
            sebi=self.sebi + other.sebi,
            stamp=self.stamp + other.stamp,
            gst=self.gst + other.gst,
            dp=self.dp + other.dp,
            unverified=tuple(sorted(set(self.unverified) | set(other.unverified))),
        )


ZERO = ChargeBreakdown(0, 0, 0, 0, 0, 0, 0)


def _half_up(x: float, places: int) -> float:
    from decimal import ROUND_HALF_UP, Decimal

    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


def as_contract_note(b: ChargeBreakdown) -> ChargeBreakdown:
    """Charges as a contract note prints them, for one scrip's orders on one note:
    brokerage, exchange, SEBI fee and stamp duty to the paisa; STT to the nearest rupee
    (half up); GST recomputed at the same rate on the rounded taxable value (brokerage +
    exchange + SEBI fee), to the paisa. The DP debit is a ledger charge, not on the note, and is
    left as is. Checked against a real Zerodha delivery contract note (29 Oct 2026):
    tests/golden/test_contract_notes.py, fixture kept private in data/private/golden/."""
    brok = _half_up(b.brokerage, 2)
    exch = _half_up(b.exchange, 2)
    sebi = _half_up(b.sebi, 2)
    base_raw = b.brokerage + b.exchange + b.sebi
    rate = b.gst / base_raw if base_raw > 0 else 0.0
    gst = _half_up(rate * (brok + exch + sebi), 2)
    return ChargeBreakdown(
        brokerage=brok,
        stt=_half_up(b.stt, 0),
        exchange=exch,
        sebi=sebi,
        stamp=_half_up(b.stamp, 2),
        gst=gst,
        dp=b.dp,
        unverified=b.unverified,
    )


class _Lookups:
    def __init__(self, book: RuleBook, on: date, allow_unverified: bool) -> None:
        self.book, self.on, self.allow = book, on, allow_unverified
        self.unverified: set[str] = set()

    def _note(self, table: str, rows: list[RuleRow]) -> None:
        if any(not r.verified for r in rows):
            self.unverified.add(table)

    def one(self, table: str, **keys: str) -> float:
        row = self.book.lookup(table, self.on, allow_unverified=self.allow, **keys)
        self._note(table, [row])
        return row.as_float()

    def total(self, table: str, **keys: str) -> float:
        rows = self.book[table].lookup_all(self.on, allow_unverified=self.allow, **keys)
        self._note(table, rows)
        return sum(r.as_float() for r in rows)


def equity_delivery_charges(
    book: RuleBook,
    on: date,
    *,
    side: Side,
    value_inr: float,
    broker: str = "zerodha",
    exchange: str = "NSE",
    depository: str = "CDSL",
    include_dp: bool = True,
    allow_unverified: bool = False,
    corporate_action: bool = False,
) -> ChargeBreakdown:
    """Charges for one executed delivery order of ``value_inr`` on ``on``.

    A buyback tender is a ``sell`` (sell-side STT and a DP debit apply). ``include_dp`` should
    be False for a second sell of the same scrip on the same day (DP is per scrip per day).
    ``corporate_action``: the order is a buyback / open-offer / delisting / OFS application, for
    which the broker charges its flat corporate-action fee (``brokerage`` rows, segment
    ``corporate_action``) instead of delivery brokerage.
    """
    if value_inr < 0:
        raise ValueError("value_inr must be >= 0")
    lk = _Lookups(book, on, allow_unverified)
    pct = value_inr / 100.0
    # A flat fee per order, or the lower of a percentage and a per-order cap (Upstox Plus:
    # min(₹30, 2.5%), confirmed by Upstox's brokerage API on 15 Nov 2026).
    b_rows = book["brokerage"].lookup_all(
        on,
        allow_unverified=allow_unverified,
        broker=broker,
        segment="corporate_action" if corporate_action else "equity_delivery",
    )
    lk._note("brokerage", b_rows)
    comps = {r.keys["component"]: r.as_float() for r in b_rows}
    if "flat_per_order" in comps:
        brokerage = comps["flat_per_order"]
    else:
        brokerage = min(comps["cap_per_order"], comps["pct_of_turnover"] * pct)
    stt = lk.one("stt", segment="equity_delivery", leg=side) * pct
    exch = lk.total("exchange_charges", exchange=exchange, segment="equity_delivery") * pct
    sebi = lk.one("sebi_fee", segment="equity") * value_inr / CRORE
    stamp = lk.one("stamp_duty", segment="equity_delivery", side=side) * pct
    gst = lk.one("gst", base="brokerage_exchange_sebi") / 100.0 * (brokerage + exch + sebi)
    dp = (
        lk.one("dp_charges", broker=broker, depository=depository)
        if side == "sell" and include_dp
        else 0.0
    )
    return ChargeBreakdown(brokerage, stt, exch, sebi, stamp, gst, dp, tuple(sorted(lk.unverified)))


def equity_intraday_charges(
    book: RuleBook,
    on: date,
    *,
    side: Side,
    value_inr: float,
    broker: str = "zerodha",
    exchange: str = "NSE",
    allow_unverified: bool = False,
) -> ChargeBreakdown:
    """Charges for one executed intraday (MIS, squared off the same day) cash order.

    Brokerage is the lower of a percentage and a per-order cap; STT is sell side only, stamp
    duty buy side only; no DP charge (nothing leaves the demat account).
    """
    if value_inr < 0:
        raise ValueError("value_inr must be >= 0")
    lk = _Lookups(book, on, allow_unverified)
    pct = value_inr / 100.0
    seg = "equity_intraday"
    rate = lk.one("brokerage", broker=broker, segment=seg, component="pct_of_turnover")
    cap = lk.one("brokerage", broker=broker, segment="equity_intraday", component="cap_per_order")
    brokerage = min(cap, rate * pct)
    stt = lk.one("stt", segment="equity_intraday", leg=side) * pct
    exch = lk.total("exchange_charges", exchange=exchange, segment="equity_intraday") * pct
    sebi = lk.one("sebi_fee", segment="equity") * value_inr / CRORE
    stamp = lk.one("stamp_duty", segment="equity_intraday", side=side) * pct
    gst = lk.one("gst", base="brokerage_exchange_sebi") / 100.0 * (brokerage + exch + sebi)
    return ChargeBreakdown(
        round(brokerage, 2),
        round(stt, 2),
        round(exch, 2),
        round(sebi, 2),
        round(stamp, 2),
        round(gst, 2),
        0.0,
        tuple(sorted(lk.unverified)),
    )


def fno_charges(
    book: RuleBook,
    on: date,
    *,
    segment: Literal["options", "futures"],
    side: Side,
    turnover_inr: float,
    broker: str = "zerodha",
    exchange: str = "NSE",
    exercised_intrinsic_inr: float = 0.0,
    allow_unverified: bool = True,
) -> ChargeBreakdown:
    """Charges for one executed F&O order (options premium turnover or futures notional turnover).

    All rates are looked up from ``config/rules/*.csv`` via ``book`` — zero hardcoded constants.
    """
    if turnover_inr < 0 or exercised_intrinsic_inr < 0:
        raise ValueError("turnover_inr and exercised_intrinsic_inr must be >= 0")
    lk = _Lookups(book, on, allow_unverified)
    pct = turnover_inr / 100.0

    if segment == "options":
        brokerage = lk.one(
            "brokerage", broker=broker, segment="options", component="flat_per_order"
        )
        stt = lk.one("stt", segment="options", leg="sell_premium") * pct if side == "sell" else 0.0
        if side == "buy" and exercised_intrinsic_inr > 0:
            stt += lk.one("stt", segment="options", leg="exercise_intrinsic") * (
                exercised_intrinsic_inr / 100.0
            )
    else:
        pct_rate = lk.one(
            "brokerage", broker=broker, segment="futures", component="pct_of_turnover"
        )
        cap = lk.one("brokerage", broker=broker, segment="futures", component="cap_per_order")
        brokerage = min(cap, pct_rate * pct)
        stt = lk.one("stt", segment="futures", leg="sell") * pct if side == "sell" else 0.0

    exch = lk.total("exchange_charges", exchange=exchange, segment=segment) * pct
    sebi = lk.one("sebi_fee", segment="fno") * turnover_inr / CRORE
    stamp = lk.one("stamp_duty", segment=segment, side=side) * pct
    gst = lk.one("gst", base="brokerage_exchange_sebi") / 100.0 * (brokerage + exch + sebi)
    return ChargeBreakdown(
        round(brokerage, 2),
        round(stt, 2),
        round(exch, 2),
        round(sebi, 2),
        round(stamp, 2),
        round(gst, 2),
        0.0,
        tuple(sorted(lk.unverified)),
    )
