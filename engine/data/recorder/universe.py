"""Which contracts to record, within the broker's symbol cap.

SPEC §3 L1: strike band = max(±25 strikes, ±3σ of the expected move), re-centred intraday;
once a strike has entered the band it keeps being recorded (sticky) so wings have continuous
history. Brokers cap subscriptions (Fyers Standard: 100 symbols per user), so every candidate
gets a priority and the list is cut at the budget — what is cut is reported, never silent.

Priority = distance from ATM in listed strikes × expiry weight (lower is more important).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from engine.data.reference.instrument_master import InstrumentMaster, Kind, classify_expiries


@dataclass(frozen=True)
class UnderlyingSpec:
    name: str  # underlying as in the master, e.g. NIFTY
    spot_symbol: str  # e.g. NSE:NIFTY50-INDEX
    expiry_roles: tuple[str, ...] = ("current_weekly", "next_weekly", "current_monthly")
    expiry_weights: Mapping[str, float] = field(default_factory=dict)  # role -> weight
    futures_months: int = 1


@dataclass(frozen=True)
class UniverseConfig:
    underlyings: tuple[UnderlyingSpec, ...]
    budget: int  # symbol slots we may use (<= broker cap)
    extra_symbols: tuple[str, ...] = ()  # e.g. India VIX, constituents
    vix_symbol: str | None = None
    min_strikes_each_side: int = 25
    buffer_strikes: int = 0  # extra strikes so price drift between re-centres keeps the minimum
    sigma_mult: float = 3.0
    fallback_iv: float = 0.15  # used for band width only, never for pricing
    tick_strikes_each_side: int = 10
    tick_expiry_roles: tuple[str, ...] = ("current_weekly",)
    depth: bool = True  # subscribe 5-level depth for futures and options


@dataclass
class Selection:
    symbols: list[str]  # within budget, priority order
    depth_symbols: set[str]
    tick_symbols: set[str]  # recorded on every message all day
    dropped: list[str]  # wanted but over budget
    expiries: dict[str, dict[str, date]]  # underlying -> role -> expiry
    atm: dict[str, float]  # "NIFTY 2026-11-17" -> ATM strike
    half_width: dict[str, float]  # "NIFTY 2026-11-17" -> ±kσ in points
    cost: int
    notes: list[str] = field(default_factory=list)

    @property
    def wanted(self) -> int:
        return len(self.symbols) + len(self.dropped)


def expected_move(spot: float, iv: float, years: float) -> float:
    """One-sigma move in points over ``years`` (calendar). Band sizing only."""
    if spot <= 0 or iv <= 0:
        raise ValueError("spot and iv must be > 0")
    return spot * iv * math.sqrt(max(years, 1.0 / 365.0))


def band_indices(
    strikes: Sequence[float], center: float, min_each_side: int, half_width: float
) -> tuple[int, int, int]:
    """(lo, atm, hi) indices into sorted ``strikes``: at least ``min_each_side`` listed strikes
    each side of ATM, widened to cover ``center ± half_width``."""
    if not strikes:
        raise ValueError("no strikes")
    i = bisect.bisect_left(strikes, center)
    if i == len(strikes) or (i > 0 and center - strikes[i - 1] <= strikes[i] - center):
        i -= 1
    atm = max(i, 0)
    lo_w = bisect.bisect_left(strikes, center - half_width)
    hi_w = bisect.bisect_right(strikes, center + half_width) - 1
    lo = max(0, min(atm - min_each_side, lo_w))
    hi = min(len(strikes) - 1, max(atm + min_each_side, hi_w))
    return lo, atm, hi


def select_universe(
    master: InstrumentMaster,
    cfg: UniverseConfig,
    *,
    now: datetime,
    spots: Mapping[str, float],
    iv: float | None,
    sticky: set[str],
    depth_counts_separately: bool,
) -> Selection:
    on = now.date()
    vol = iv if iv and iv > 0 else cfg.fallback_iv
    notes: list[str] = []
    if not (iv and iv > 0):
        notes.append(f"no live IV for band width; using fallback {cfg.fallback_iv:.0%}")

    core: list[str] = []
    for u in cfg.underlyings:
        core.append(u.spot_symbol)
    if cfg.vix_symbol:
        core.append(cfg.vix_symbol)
    core += list(cfg.extra_symbols)
    depth_ok: set[str] = set()
    for u in cfg.underlyings:
        for fut in master.futures(u.name, on)[: u.futures_months]:
            core.append(fut.symbol)
            depth_ok.add(fut.symbol)
    notes += [f"{s} not in instrument master; not recorded" for s in core if s not in master]
    core = list(dict.fromkeys(s for s in core if s in master))

    # (priority, tiebreak..., symbol)
    ranked: list[tuple[float, date, float, str, str]] = []
    tick: set[str] = set(core)
    expiries: dict[str, dict[str, date]] = {}
    atm_map: dict[str, float] = {}
    hw_map: dict[str, float] = {}
    for u in cfg.underlyings:
        roles = classify_expiries(master.option_expiries(u.name, on), u.expiry_roles)
        expiries[u.name] = roles
        spot = spots.get(u.name)
        if spot is None or spot <= 0:
            notes.append(f"{u.name}: no spot price yet; options not selected")
            continue
        weight_by_expiry: dict[date, float] = {}
        tick_expiries = {roles[r] for r in cfg.tick_expiry_roles if r in roles}
        for role, exp in roles.items():
            w = float(u.expiry_weights.get(role, 1.0))
            weight_by_expiry[exp] = min(w, weight_by_expiry.get(exp, w))
        for exp, weight in sorted(weight_by_expiry.items()):
            strikes = master.strikes(u.name, exp)
            if not strikes:
                continue
            chain = master.options(u.name, exp)
            expiry_ts = chain[0].expiry_ts
            years = ((expiry_ts - now).total_seconds() if expiry_ts else 0) / (365.0 * 86400)
            hw = cfg.sigma_mult * expected_move(spot, vol, years)
            lo, atm, hi = band_indices(
                strikes, spot, cfg.min_strikes_each_side + cfg.buffer_strikes, hw
            )
            key = f"{u.name} {exp.isoformat()}"
            atm_map[key] = strikes[atm]
            hw_map[key] = round(hw, 2)
            index_of = {k: i for i, k in enumerate(strikes)}
            for ins in chain:
                assert ins.strike is not None
                idx = index_of[ins.strike]
                in_band = lo <= idx <= hi
                if not in_band and ins.symbol not in sticky:
                    continue
                dist = abs(idx - atm)
                ranked.append((dist * weight, exp, ins.strike, ins.kind.value, ins.symbol))
                if exp in tick_expiries and dist <= cfg.tick_strikes_each_side:
                    tick.add(ins.symbol)
    ranked.sort()

    def cost(sym: str, depth: bool) -> int:
        return 1 + (1 if depth and depth_counts_separately else 0)

    chosen: list[str] = []
    depth_symbols: set[str] = set()
    used = 0
    dropped: list[str] = []
    for sym in core:
        d = cfg.depth and sym in depth_ok
        c = cost(sym, d)
        if used + c > cfg.budget:
            dropped.append(sym)
            continue
        chosen.append(sym)
        used += c
        if d:
            depth_symbols.add(sym)
    for _, _, _, _, sym in ranked:
        c = cost(sym, cfg.depth)
        if used + c > cfg.budget:
            dropped.append(sym)
            continue
        chosen.append(sym)
        used += c
        if cfg.depth:
            depth_symbols.add(sym)
    if dropped:
        notes.append(f"{len(dropped)} contracts over the {cfg.budget}-slot budget not recorded")
    kinds = {master.get(s).kind for s in chosen}
    if Kind.CALL not in kinds and any(spots.get(u.name) for u in cfg.underlyings):
        notes.append("no options selected")
    return Selection(
        symbols=chosen,
        depth_symbols=depth_symbols,
        tick_symbols=tick & set(chosen),
        dropped=dropped,
        expiries=expiries,
        atm=atm_map,
        half_width=hw_map,
        cost=used,
        notes=notes,
    )
