"""Synthetic instrument master for recorder tests (no network, no real market data)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from engine.core.rules import IST
from engine.data.reference.instrument_master import Instrument, InstrumentMaster, Kind

DAY = date(2026, 11, 11)  # a Wednesday trading day in the 2026 calendar
WEEKLY = date(2026, 11, 17)
MONTHLY = date(2026, 11, 23)  # holiday-shifted monthly (24 Nov is a holiday)


def ins(
    symbol: str,
    kind: Kind,
    *,
    underlying: str = "NIFTY",
    expiry: date | None = None,
    strike: float | None = None,
    lot: int = 65,
    exchange: str = "NSE",
) -> Instrument:
    return Instrument(
        symbol=symbol,
        broker="fyers",
        exchange=exchange,
        segment="cm" if kind in (Kind.INDEX, Kind.EQUITY) else "fo",
        token=symbol,
        exchange_token="0",
        underlying=underlying,
        kind=kind,
        expiry=expiry,
        expiry_ts=datetime(expiry.year, expiry.month, expiry.day, 15, 40, tzinfo=IST)
        if expiry
        else None,
        strike=strike,
        lot_size=lot if kind not in (Kind.INDEX,) else 0,
        tick_size=0.05,
        isin="",
        description=symbol,
    )


def opt_symbol(expiry: date, strike: float, kind: Kind) -> str:
    return f"NSE:NIFTY{expiry:%y%m%d}{int(strike)}{kind.value}"


def build_master(strikes: range = range(24000, 26050, 50)) -> InstrumentMaster:
    items = [
        ins("NSE:NIFTY50-INDEX", Kind.INDEX),
        ins("NSE:INDIAVIX-INDEX", Kind.INDEX, underlying="INDIAVIX"),
        ins("NSE:NIFTY26NOVFUT", Kind.FUTURE, expiry=MONTHLY),
    ]
    for exp in (WEEKLY, MONTHLY):
        for k in strikes:
            for kind in (Kind.CALL, Kind.PUT):
                items.append(ins(opt_symbol(exp, k, kind), kind, expiry=exp, strike=float(k)))
    return InstrumentMaster(items, as_of=DAY, source="test")


@pytest.fixture
def master() -> InstrumentMaster:
    return build_master()
