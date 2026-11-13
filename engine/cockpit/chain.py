"""Option chain table for the cockpit (LTP, Greeks and all-columns views).

Inputs are the recorder's 1-minute snapshots (bid, ask, LTP, previous close, volume, OI per
contract) and the previous session's NSE F&O bhavcopy for previous-day OI (published ~19:30 the
evening before: point-in-time safe). Everything else is computed here, never taken from the
broker:

- IV from each contract's own mid (Black-76 on the expiry's synthetic forward), and delta,
  gamma, theta (₹ per day per unit), vega (₹ per vol point per unit);
- intrinsic and time value against the forward;
- breakeven at expiry for a buyer at the ask, and its distance from spot;
- POP: probability a buyer at the ask finishes above (call) or below (put) breakeven, from a
  lognormal with the contract's own IV (a model number, not a forecast);
- OI change against the previous session's close, per-strike PCR and max pain (the expiry
  price at which option buyers' total payout is smallest).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from engine.analytics.greeks import compute_black76_greeks
from engine.analytics.iv import implied_vol_black76, norm_cdf

Legs = Mapping[float, Mapping[str, Mapping[str, float]]]  # strike -> CE/PE -> fields


def max_pain(legs: Legs) -> float | None:
    """Strike at which the total intrinsic value owed to option holders is smallest."""
    strikes = sorted(legs)
    if not strikes:
        return None
    ce = {k: float(legs[k].get("CE", {}).get("oi", 0) or 0) for k in strikes}
    pe = {k: float(legs[k].get("PE", {}).get("oi", 0) or 0) for k in strikes}
    if not any(ce.values()) and not any(pe.values()):
        return None

    def payout(s: float) -> float:
        return sum(ce[k] * max(0.0, s - k) + pe[k] * max(0.0, k - s) for k in strikes)

    return min(strikes, key=payout)


def buyer_pop(
    forward: float, breakeven: float, iv: float, t_var: float, *, is_call: bool
) -> float | None:
    """P(F_T beyond breakeven) under a driftless lognormal with volatility ``iv``."""
    if forward <= 0 or breakeven <= 0 or iv <= 0 or t_var <= 0:
        return None
    sd = iv * math.sqrt(t_var)
    d2 = (math.log(forward / breakeven) - 0.5 * sd * sd) / sd
    return norm_cdf(d2) if is_call else norm_cdf(-d2)


def build_chain_table(
    legs: Legs,
    *,
    forward: float,
    spot: float,
    t_var: float,
    t_cal: float,
    r: float,
    syms: Mapping[tuple[float, str], str] | None = None,
    prev_oi: Mapping[tuple[float, str], int] | None = None,
    first_oi: dict[tuple[float, str], int] | None = None,
) -> dict[str, Any]:
    syms = syms or {}
    prev_oi = prev_oi or {}
    kw: dict[str, Any] = {"t_cal": t_cal, "risk_free_rate": r}
    rows: list[dict[str, Any]] = []
    tot = {"CE": 0, "PE": 0}
    tot_chg: dict[str, int | None] = {"CE": None, "PE": None}  # over strikes with a prev OI
    vol = {"CE": 0, "PE": 0}
    for k in sorted(legs):
        row: dict[str, Any] = {"k": k}
        ivs: dict[str, float] = {}
        buys: dict[str, float] = {}
        for kind in ("CE", "PE"):
            q = legs[k].get(kind)
            if not q:
                continue
            is_call = kind == "CE"
            b, a = float(q.get("bid") or 0.0), float(q.get("ask") or 0.0)
            ltp, pc = float(q.get("ltp") or 0.0), float(q.get("prev_close") or 0.0)
            oi = int(q.get("oi") or 0)
            v = int(q.get("volume") or 0)
            tot[kind] += oi
            vol[kind] += v
            side: dict[str, Any] = {
                "sym": syms.get((k, kind)),
                "bid": b or None,
                "ask": a or None,
                "ltp": ltp or None,
                "chg": round(ltp - pc, 2) if ltp and pc else None,
                "chg_pct": round((ltp / pc - 1) * 100, 1) if ltp and pc else None,
                "volume": v,
                "oi": oi,
            }
            p_oi = prev_oi.get((k, kind))
            if p_oi is not None:
                tot_chg[kind] = (tot_chg[kind] or 0) + oi - p_oi
                side["prev_oi"] = p_oi
                side["oi_chg"] = oi - p_oi
                side["oi_chg_pct"] = round((oi / p_oi - 1) * 100, 1) if p_oi > 0 else None
            if first_oi is not None:
                side["doi"] = oi - first_oi.setdefault((k, kind), oi)
            intrinsic = max(0.0, (forward - k) if is_call else (k - forward))
            mid = (b + a) / 2 if b > 0 and a > 0 else ltp
            if mid > 0:
                side["intrinsic"] = round(intrinsic, 2)
                side["time_value"] = round(mid - intrinsic, 2)
            buy = a if a > 0 else ltp
            if buy > 0:
                be = k + buy if is_call else k - buy
                side["breakeven"] = round(be, 2)
                side["be_pct"] = round((be / spot - 1) * 100, 2) if spot > 0 else None
            if mid > 0 and t_var > 0:
                own = implied_vol_black76(mid, forward, k, t_var, is_call=is_call, **kw)
                if own:
                    side["iv_own"] = round(own * 100, 2)
                    ivs[kind] = own
            buys[kind] = buy
            row[kind.lower()] = side
        # One IV per strike, from the out-of-the-money option (calls above the forward, puts
        # below), as the market quotes it: a deep in-the-money option's own mid sits inside a
        # wide spread with little time value, so its own IV is unreliable and its greeks drift
        # (e.g. delta 0.99 where parity with the put says 0.95).
        otm = "CE" if k >= forward else "PE"
        strike_iv = ivs.get(otm) or ivs.get("PE" if otm == "CE" else "CE")
        if strike_iv and t_var > 0:
            row["iv"] = round(strike_iv * 100, 2)
            for kind in ("CE", "PE"):
                leg = row.get(kind.lower())
                if leg is None:
                    continue
                is_call = kind == "CE"
                g = compute_black76_greeks(forward, k, t_var, strike_iv, is_call=is_call, **kw)
                leg.update(
                    {
                        "iv": round(strike_iv * 100, 2),
                        "delta": round(g.delta, 3),
                        "gamma": round(g.gamma, 5),
                        "theta": round(g.theta_per_day, 2),
                        "vega": round(g.vega_per_1pct, 2),
                    }
                )
                buy = buys.get(kind) or 0.0
                if buy > 0:
                    pop = buyer_pop(
                        forward, k + buy if is_call else k - buy, strike_iv, t_var, is_call=is_call
                    )
                    leg["pop"] = round(pop * 100, 1) if pop is not None else None
        ce_oi = (row.get("ce") or {}).get("oi") or 0
        pe_oi = (row.get("pe") or {}).get("oi") or 0
        row["pcr"] = round(pe_oi / ce_oi, 2) if ce_oi else None
        rows.append(row)
    summary: dict[str, Any] = {
        "max_pain": max_pain(legs),
        "pcr_oi": round(tot["PE"] / tot["CE"], 2) if tot["CE"] else None,
        "pcr_volume": round(vol["PE"] / vol["CE"], 2) if vol["CE"] else None,
        "call_oi": tot["CE"],
        "put_oi": tot["PE"],
        "call_oi_chg": tot_chg["CE"],
        "put_oi_chg": tot_chg["PE"],
    }
    return {"rows": rows, "summary": summary}


def load_prev_oi(
    raw_dir: Any, before: Any, underlyings: Any, max_back_days: int = 7
) -> dict[str, dict[tuple[str, float, str], int]]:
    """OI per (expiry ISO, strike, CE/PE) for each index, from the newest NSE F&O bhavcopy dated
    before ``before`` (a previous session's file, published the evening before). BSE contracts
    (Sensex) are not in NSE's file and come back empty."""
    from datetime import timedelta

    from engine.data.eod.bhavcopy import parse_fno_bhavcopy_csv
    from engine.data.eod.download import eod_path, read_eod_text

    for back in range(1, max_back_days + 1):
        d = before - timedelta(days=back)
        path = eod_path(raw_dir, "fo", d)
        if not path.exists():
            continue
        out: dict[str, dict[tuple[str, float, str], int]] = {u: {} for u in underlyings}
        for row in parse_fno_bhavcopy_csv(read_eod_text(path)):
            if row.trade_date != d or row.symbol not in out or row.option_type not in ("CE", "PE"):
                continue
            out[row.symbol][(row.expiry.isoformat(), float(row.strike), row.option_type)] = row.oi
        return out
    return {}
