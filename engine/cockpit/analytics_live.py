"""Market strip for the cockpit: what the analytics engine says right now.

Two speeds:
- every second, from the engine's in-memory order books: spot, futures, synthetic forward
  (parity on near-ATM call/put mids), our Black-76 ATM IV, its percentile against past EOD
  history (``iv_history.parquet``, past days only), India VIX;
- every minute, on a background thread with its own DuckDB connection, from the recorder's
  1-minute chain snapshots: OI walls, put/call OI ratio and dealer gamma (GEX).

GEX assumes dealers are long calls and short puts; nobody publishes who is short what in India,
so it is labelled HYPOTHESIS. Every number carries the time it was computed.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from engine.analytics.expiry_clock import expiry_clock
from engine.analytics.exposures import ChainGEXNode, compute_dealer_gex_profile
from engine.analytics.forward import StrikePairQuote, compute_synthetic_forward
from engine.analytics.iv import implied_vol_black76
from engine.analytics.svi import fit_svi
from engine.core.calendar import TradingCalendar
from engine.core.rules import IST, RuleBook
from engine.data.reference.instrument_master import InstrumentMaster, Kind
from engine.data.store import Store

log = logging.getLogger(__name__)


def load_iv_percentile_history(path: Path, before: date, days: int = 252) -> dict[str, list[float]]:
    """``underlying -> ATM IVs`` of the last ``days`` trade dates strictly before ``before``."""
    if not path.exists():
        return {}
    import pyarrow.parquet as pq

    rows = pq.read_table(path, columns=["trade_date", "underlying", "atm_iv"]).to_pylist()
    out: dict[str, list[tuple[date, float]]] = {}
    for r in rows:
        if r["atm_iv"] is None or r["trade_date"] >= before:
            continue
        out.setdefault(str(r["underlying"]), []).append((r["trade_date"], float(r["atm_iv"])))
    return {u: [v for _, v in sorted(xs)[-days:]] for u, xs in out.items()}


def _mid(levels: Sequence[Any]) -> float:
    return float(levels[0][0]) if levels and levels[0][0] > 0 else 0.0


class LiveAnalytics:
    def __init__(
        self,
        *,
        book: RuleBook,
        calendar: TradingCalendar | None,
        master: InstrumentMaster,
        underlyings: Mapping[str, str],  # name -> spot symbol
        iv_history: Mapping[str, Sequence[float]],
        chain_dir: Path | None,
        chain_interval_s: float = 60.0,
        constituents: Mapping[str, tuple[str, float, bool]] | None = None,
        dispersion_index: str = "NIFTY",
        dispersion_window_min: int = 60,
        prev_oi: Mapping[str, Mapping[tuple[str, float, str], int]] | None = None,
        max_expiries: int = 3,
    ) -> None:
        self.book = book
        self.calendar = calendar
        self.master = master
        self.underlyings = dict(underlyings)
        self.iv_history = {k: list(v) for k, v in iv_history.items()}
        self.chain_dir = chain_dir
        self.chain_interval_s = chain_interval_s
        self.strip: dict[str, dict[str, Any]] = {}
        self.chain: dict[str, dict[str, Any]] = {}
        # broker key -> (symbol, index weight %, weight verified?) for SPEC L9 dispersion
        self.constituents = dict(constituents or {})
        self.dispersion_index = dispersion_index
        self.dispersion_window_min = dispersion_window_min
        self.dispersion: dict[str, Any] | None = None
        self._expiry: dict[str, date] = {}
        self._oi_first: dict[tuple[str, float, str], int] = {}  # chain OI at first sight today
        self.oi_first_at: str | None = None
        self.detail: dict[str, dict[str, Any]] = {}  # live contract -> IV and greeks
        # Full chains for the page (und -> expiry ISO -> table), sent only on request.
        self.chains: dict[str, dict[str, dict[str, Any]]] = {}
        self.chain_rev = 0
        self.prev_oi = {u: dict(v) for u, v in (prev_oi or {}).items()}
        self.max_expiries = max_expiries
        self._first_oi: dict[tuple[str, str], dict[tuple[float, str], int]] = {}
        self._stop = threading.Event()
        self._now = datetime.now(IST)
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ helpers

    def _rate(self, now: datetime) -> tuple[float, bool]:
        """(r as a fraction, verified?) from config/rules risk_free_rate."""
        try:
            row = self.book.lookup("risk_free_rate", now, tenor="91d_tbill")
            return row.as_float() / 100.0, True
        except Exception:
            try:
                row = self.book.lookup(
                    "risk_free_rate", now, allow_unverified=True, tenor="91d_tbill"
                )
                return row.as_float() / 100.0, False
            except Exception:
                return 0.0, False

    def _exchange(self, underlying: str) -> str:
        spot = self.underlyings.get(underlying, "")
        if spot in self.master:
            return self.master.get(spot).exchange
        return "NSE"

    # ------------------------------------------------------------------ 1-second strip

    def update(self, now: datetime, states: Mapping[str, Any]) -> None:
        self._now = now
        options: dict[str, dict[date, dict[float, dict[str, Any]]]] = {}
        futures: dict[str, list[tuple[date, float]]] = {}
        vix = 0.0
        for sym, st in states.items():
            if sym not in self.master:
                continue
            ins = self.master.get(sym)
            if ins.kind.is_option and ins.expiry is not None and ins.strike is not None:
                slot = options.setdefault(ins.underlying, {}).setdefault(ins.expiry, {})
                slot.setdefault(float(ins.strike), {})[ins.kind.value] = st
            elif ins.kind is Kind.FUTURE and ins.expiry is not None and st.ltp > 0:
                futures.setdefault(ins.underlying, []).append((ins.expiry, st.ltp))
            elif ins.kind is Kind.INDEX and "VIX" in (sym + ins.underlying).upper() and st.ltp > 0:
                vix = st.ltp
        self._apply_breadth(states)
        r, r_ok = self._rate(now)
        for und, spot_sym in self.underlyings.items():
            spot_st = states.get(spot_sym)
            spot = float(spot_st.ltp) if spot_st is not None and spot_st.ltp > 0 else 0.0
            row: dict[str, Any] = {
                "underlying": und,
                "spot": spot or None,
                "vix": vix or None,
                "r_pct": round(r * 100, 3),
                "r_verified": r_ok,
                "at": now.strftime("%H:%M:%S"),
            }
            futs = sorted(futures.get(und, []))
            if futs:
                row["fut"] = futs[0][1]
                row["fut_expiry"] = futs[0][0].isoformat()
                if spot:
                    row["fut_basis"] = round(futs[0][1] - spot, 2)
            chain = options.get(und, {})
            today = now.astimezone(IST).date()
            expiries = sorted(e for e in chain if e >= today)
            if spot and expiries:
                exp = expiries[0]
                self._expiry[und] = exp
                row.update(self._iv_block(und, exp, chain[exp], spot, now, r))
            self.strip[und] = row
        self._contract_greeks(now, states, r)

    def _iv_block(
        self,
        und: str,
        exp: date,
        strikes: Mapping[float, Mapping[str, Any]],
        spot: float,
        now: datetime,
        r: float,
    ) -> dict[str, Any]:
        pairs = []
        for k, legs in strikes.items():
            c, p = legs.get("CE"), legs.get("PE")
            if c is None or p is None or not (c.bids and c.asks and p.bids and p.asks):
                continue
            pairs.append(StrikePairQuote(k, _mid(c.bids), _mid(c.asks), _mid(p.bids), _mid(p.asks)))
        out: dict[str, Any] = {"expiry": exp.isoformat(), "parity_strikes": 0}
        if not pairs:
            return out
        try:
            clk = expiry_clock(
                now, exp, book=self.book, calendar=self.calendar, exchange=self._exchange(und)
            )
        except Exception as exc:
            out["error"] = f"expiry clock: {type(exc).__name__}"
            return out
        fwd = compute_synthetic_forward(pairs, spot=spot, t_cal_years=clk.t_cal, risk_free_rate=r)
        out["parity_strikes"] = len(fwd.strikes_used)
        if not fwd.strikes_used:
            return out  # carry-only fallback: not a market forward, do not show IV on it
        f = fwd.forward
        atm = min(pairs, key=lambda q: abs(q.strike - f))
        kw: dict[str, Any] = {"t_cal": clk.t_cal, "risk_free_rate": r}
        civ = implied_vol_black76(atm.call_mid, f, atm.strike, clk.t_var, is_call=True, **kw)
        piv = implied_vol_black76(atm.put_mid, f, atm.strike, clk.t_var, is_call=False, **kw)
        out.update(
            {
                "forward": round(f, 2),
                "fwd_basis": round(f - spot, 2),
                "fwd_dispersion": round(fwd.dispersion_pts, 2),
                "atm_strike": atm.strike,
                "trading_minutes": round(clk.trading_minutes, 0),
            }
        )
        ivs = [v for v in (civ, piv) if v is not None]
        if ivs:
            iv = statistics.fmean(ivs)
            out["atm_iv_pct"] = round(iv * 100, 2)
            hist = self.iv_history.get(und, [])
            if len(hist) >= 20:
                out["iv_percentile"] = round(100.0 * sum(1 for h in hist if h < iv) / len(hist), 0)
                out["iv_history_days"] = len(hist)
        return out

    def _contract_greeks(self, now: datetime, states: Mapping[str, Any], r: float) -> None:
        """IV and greeks for every live option from its own mid (Black-76 on our forward for
        the nearest expiry; spot carried at r for later expiries)."""
        from engine.analytics.greeks import compute_black76_greeks

        out: dict[str, dict[str, Any]] = {}
        for sym, st in states.items():
            if sym not in self.master or not (st.bids and st.asks):
                continue
            ins = self.master.get(sym)
            if not ins.kind.is_option or ins.expiry is None or ins.strike is None:
                continue
            strip = self.strip.get(ins.underlying, {})
            spot = strip.get("spot")
            if not spot:
                continue
            try:
                clk = expiry_clock(
                    now, ins.expiry, book=self.book, calendar=self.calendar,
                    exchange=self._exchange(ins.underlying),
                )  # fmt: skip
            except Exception:
                continue
            f = strip.get("forward") if strip.get("expiry") == ins.expiry.isoformat() else None
            f = float(f or spot * math.exp(r * clk.t_cal))
            mid = 0.5 * (st.bids[0][0] + st.asks[0][0])
            is_call = ins.kind is Kind.CALL
            kw: dict[str, Any] = {"t_cal": clk.t_cal, "risk_free_rate": r}
            iv = implied_vol_black76(mid, f, float(ins.strike), clk.t_var, is_call=is_call, **kw)
            if iv is None:
                continue
            g = compute_black76_greeks(f, float(ins.strike), clk.t_var, iv, is_call=is_call, **kw)
            lot = max(1, ins.lot_size)
            intrinsic = max(0.0, (f - ins.strike) if is_call else (ins.strike - f))
            out[sym] = {
                "iv": round(iv * 100, 2),
                "delta": round(g.delta, 3),
                "gamma": round(g.gamma, 5),
                "theta_lot": round(g.theta_per_day * lot, 0),
                "vega_lot": round(g.vega_per_1pct * lot, 0),
                "intrinsic": round(intrinsic, 2),
                "time_value": round(mid - intrinsic, 2),
                "moneyness_pct": round((float(ins.strike) / f - 1) * 100, 2),
                "days": (ins.expiry - now.astimezone(IST).date()).days,
                "trading_minutes": round(clk.trading_minutes, 0),
            }
        self.detail = out

    def _apply_breadth(self, states: Mapping[str, Any]) -> None:
        """Feed constituent breadth to the scanner: an index/futures move counts as broad when
        weighted breadth is at least 25% in the same direction (SPEC L9)."""
        d = self.dispersion
        if not d or d.get("weighted_breadth_pct") is None:
            return
        wb = float(d["weighted_breadth_pct"])
        for sym, st in states.items():
            if sym not in self.master:
                continue
            ins = self.master.get(sym)
            if ins.underlying != self.dispersion_index or ins.kind not in (Kind.INDEX, Kind.FUTURE):
                continue
            move = st.ltp - st.session_open if st.ltp and st.session_open else 0.0
            st.breadth_aligned = abs(wb) >= 25.0 and move != 0 and (wb > 0) == (move > 0)

    # ------------------------------------------------------------------ 1-minute chain

    def start_chain_worker(self) -> None:
        if self.chain_dir is None:
            return
        self._thread = threading.Thread(target=self._chain_loop, name="cockpit-chain", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _chain_loop(self) -> None:
        assert self.chain_dir is not None
        store = Store(self.chain_dir)  # own connection: never shares DuckDB with the engine loop
        while not self._stop.wait(2.0 if not self.chain else self.chain_interval_s):
            changed = False
            for und in list(self.underlyings):
                exp = self._expiry.get(und)
                if exp is None:
                    continue
                try:
                    res = self.chain_snapshot(store, und, exp, self._now)
                except Exception as exc:  # the recorder may be mid-write; next minute
                    log.debug("chain analytics %s: %s", und, exc)
                    continue
                if res is not None:
                    self.chain[und] = res
                    tables = {exp.isoformat(): res.get("table", {})} if res.get("table") else {}
                    for later in self._recorded_expiries(store, und, exp, self._now):
                        try:
                            t = self.chain_table(store, und, later, self._now)
                        except Exception as exc:
                            log.debug("chain %s %s: %s", und, later, exc)
                            continue
                        if t is not None:
                            tables[later.isoformat()] = t
                    if tables:
                        self.chains[und] = tables
                        changed = True
            if changed:
                self.chain_rev += 1
            if self.constituents:
                try:
                    disp = self.dispersion_snapshot(store, self._now)
                except Exception as exc:
                    log.debug("dispersion: %s", exc)
                    disp = None
                if disp is not None:
                    self.dispersion = disp

    def chain_snapshot(
        self, store: Store, und: str, exp: date, now: datetime, lookback_s: int = 180
    ) -> dict[str, Any] | None:
        day = now.astimezone(IST).date()
        if not store.has("quotes", day):
            return None
        legs = self._legs(store, und, exp, now, lookback_s)
        if not legs:
            return None
        ce_oi = {k: int(v["CE"]["oi"]) for k, v in legs.items() if "CE" in v}
        pe_oi = {k: int(v["PE"]["oi"]) for k, v in legs.items() if "PE" in v}
        out: dict[str, Any] = {
            "at": now.astimezone(IST).strftime("%H:%M"),
            "expiry": exp.isoformat(),
        }
        if ce_oi and max(ce_oi.values()) > 0:
            out["call_wall"] = max(ce_oi, key=lambda k: ce_oi[k])
        if pe_oi and max(pe_oi.values()) > 0:
            out["put_wall"] = max(pe_oi, key=lambda k: pe_oi[k])
        if sum(ce_oi.values()) > 0:
            out["pcr_oi"] = round(sum(pe_oi.values()) / sum(ce_oi.values()), 2)
        strip = self.strip.get(und, {})
        f = strip.get("forward")
        if f:
            try:
                clk = expiry_clock(
                    now, exp, book=self.book, calendar=self.calendar, exchange=self._exchange(und)
                )
                r, _ = self._rate(now)
                nodes = []
                smile: list[tuple[float, float, float]] = []  # (strike, k, OTM iv)
                if self.oi_first_at is None:
                    self.oi_first_at = out["at"]
                table = self._table(und, exp, legs, f, float(strip.get("spot") or f), clk, r)
                table["at"] = out["at"]
                out["table"] = table
                out["rows"] = table["rows"]  # the condor's strike picker reads these
                out["forward"] = f
                out["oi_since"] = self.oi_first_at
                out["max_pain"] = table["summary"]["max_pain"]
                for k, v in legs.items():
                    if "CE" not in v or "PE" not in v:
                        continue
                    cb, ca, coi = v["CE"]["bid"], v["CE"]["ask"], int(v["CE"]["oi"])
                    pb, pa, poi = v["PE"]["bid"], v["PE"]["ask"], int(v["PE"]["oi"])
                    if min(cb, ca, pb, pa) <= 0:
                        continue
                    kw: dict[str, Any] = {"t_cal": clk.t_cal, "risk_free_rate": r}
                    civ = implied_vol_black76((cb + ca) / 2, f, k, clk.t_var, is_call=True, **kw)
                    piv = implied_vol_black76((pb + pa) / 2, f, k, clk.t_var, is_call=False, **kw)
                    if civ and piv:
                        nodes.append(ChainGEXNode(k, coi, poi, civ, piv))
                    otm = piv if k < f else civ  # out-of-the-money side is the liquid one
                    if otm:
                        smile.append((k, math.log(k / f), otm))
                if nodes:
                    gex = compute_dealer_gex_profile(
                        nodes, forward=f, t_var=clk.t_var, risk_free_rate=r
                    )
                    out["gex"] = {
                        "net_inr_per_1pct": round(gex.net_gex_inr_per_1pct, 0),
                        "flip": round(gex.gamma_flip_level, 1),
                        "regime": gex.regime,
                        "strikes": len(nodes),
                        "label": "HYPOTHESIS (assumes dealers long calls, short puts)",
                    }
                smile.sort()
                fit = fit_svi([x[1] for x in smile], [x[2] for x in smile], clk.t_var)
                if fit is not None:
                    out["smile"] = {
                        "points": [
                            [kk, round(iv * 100, 2), round(fit.iv(lk) * 100, 2)]
                            for kk, lk, iv in smile
                        ],
                        "rmse_vol_pts": fit.rmse_vol_pts,
                        "atm_iv_fit": round(fit.iv(0.0) * 100, 2),
                        "skew_pts": round(
                            (fit.iv(math.log(0.98)) - fit.iv(math.log(1.02))) * 100, 2
                        ),
                        "forward": f,
                        "params": {
                            "a": fit.a,
                            "b": fit.b,
                            "rho": fit.rho,
                            "m": fit.m,
                            "sigma": fit.sigma,
                        },
                    }
            except Exception as exc:
                out["gex_error"] = type(exc).__name__
        return out

    def _legs(
        self, store: Store, und: str, exp: date, now: datetime, lookback_s: int = 180
    ) -> dict[float, dict[str, dict[str, float]]]:
        src = store.source_sql("quotes", now.astimezone(IST).date())
        rows = store.rows(
            f"""SELECT kind, strike, bid, ask, oi, ltp, prev_close, volume FROM {src}
                WHERE capture = 'snap' AND underlying = ? AND expiry = ? AND kind IN ('CE','PE')
                  AND ts <= ? AND ts >= ?
                QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ts DESC, seq DESC) = 1""",
            [und, exp, now, now - timedelta(seconds=lookback_s)],
        )
        legs: dict[float, dict[str, dict[str, float]]] = {}
        for kind, strike, bid, ask, oi, ltp, pc, vol in rows:
            if strike is None:
                continue
            legs.setdefault(float(strike), {})[str(kind)] = {
                "bid": float(bid or 0.0),
                "ask": float(ask or 0.0),
                "oi": float(oi or 0),
                "ltp": float(ltp or 0.0),
                "prev_close": float(pc or 0.0),
                "volume": float(vol or 0),
            }
        return legs

    def _table(
        self,
        und: str,
        exp: date,
        legs: Mapping[float, Mapping[str, Mapping[str, float]]],
        forward: float,
        spot: float,
        clk: Any,
        r: float,
    ) -> dict[str, Any]:
        from engine.cockpit.chain import build_chain_table

        opts = [o for o in self.master.options(und, exp) if o.strike is not None]
        syms = {(float(o.strike or 0.0), o.kind.value): o.symbol for o in opts}
        prev_all = self.prev_oi.get(und, {})
        iso = exp.isoformat()
        prev = {(k, kind): v for (e, k, kind), v in prev_all.items() if e == iso}
        table = build_chain_table(
            legs, forward=forward, spot=spot, t_var=clk.t_var, t_cal=clk.t_cal, r=r,
            syms=syms, prev_oi=prev, first_oi=self._first_oi.setdefault((und, iso), {}),
        )  # fmt: skip
        table.update(
            {
                "underlying": und,
                "expiry": iso,
                "forward": round(forward, 2),
                "spot": spot,
                "lot": max((o.lot_size for o in opts), default=1),
                "trading_minutes": round(clk.trading_minutes, 0),
                "prev_oi_available": bool(prev),
            }
        )
        return table

    def _recorded_expiries(
        self, store: Store, und: str, nearest: date, now: datetime
    ) -> list[date]:
        src = store.source_sql("quotes", now.astimezone(IST).date())
        rows = store.rows(
            f"""SELECT DISTINCT expiry FROM {src}
                WHERE capture = 'snap' AND underlying = ? AND kind IN ('CE','PE')
                  AND expiry > ? AND ts >= ?""",
            [und, nearest, now - timedelta(seconds=180)],
        )
        return sorted(d for (d,) in rows if d is not None)[: max(0, self.max_expiries - 1)]

    def chain_table(
        self, store: Store, und: str, exp: date, now: datetime
    ) -> dict[str, Any] | None:
        """Table for a later expiry, on its own synthetic forward (put-call parity)."""
        legs = self._legs(store, und, exp, now)
        spot = float(self.strip.get(und, {}).get("spot") or 0.0)
        if not legs or spot <= 0:
            return None
        clk = expiry_clock(
            now, exp, book=self.book, calendar=self.calendar, exchange=self._exchange(und)
        )
        r, _ = self._rate(now)
        pairs = [
            StrikePairQuote(k, v["CE"]["bid"], v["CE"]["ask"], v["PE"]["bid"], v["PE"]["ask"])
            for k, v in legs.items()
            if "CE" in v
            and "PE" in v
            and min(v["CE"]["bid"], v["CE"]["ask"], v["PE"]["bid"], v["PE"]["ask"]) > 0
        ]
        fwd = compute_synthetic_forward(pairs, spot=spot, t_cal_years=clk.t_cal, risk_free_rate=r)
        if not fwd.strikes_used:
            return None
        table = self._table(und, exp, legs, fwd.forward, spot, clk, r)
        table["at"] = now.astimezone(IST).strftime("%H:%M")
        return table

    def chains_view(self) -> dict[str, Any]:
        return {"rev": self.chain_rev, "data": self.chains}

    def dispersion_snapshot(self, store: Store, now: datetime) -> dict[str, Any] | None:
        """Heavyweights' breadth and realized correlation from the recorder's 1-minute rows.

        Breadth: weight-adjusted share of members up/down on the day (vs previous close).
        Realized correlation: weight-pair average of pairwise correlations of 1-minute log
        returns over the last ``dispersion_window_min`` minutes. High = broad, macro-driven
        move; low = stock-specific moves that offset (dispersion).
        """
        from engine.data.constituents.nifty_members import compute_constituent_breadth
        from engine.data.constituents.table import log_returns, weighted_average_correlation

        day = now.astimezone(IST).date()
        if not store.has("quotes", day) or not self.constituents:
            return None
        keys = list(self.constituents)
        marks = ", ".join("?" for _ in keys)
        rows = store.rows(
            f"""SELECT symbol, ts, ltp, prev_close FROM {store.source_sql("quotes", day)}
                WHERE capture = 'snap' AND symbol IN ({marks}) AND ts <= ? AND ts >= ?
                ORDER BY ts""",
            [*keys, now, now - timedelta(minutes=self.dispersion_window_min + 2)],
        )
        series: dict[str, list[float]] = {}
        prev: dict[str, float] = {}
        for sym, _ts, ltp, pc in rows:
            if ltp:
                series.setdefault(str(sym), []).append(float(ltp))
            if pc:
                prev[str(sym)] = float(pc)
        if not series:
            return None
        names = {k: v[0] for k, v in self.constituents.items()}
        weights = {names[k]: v[1] for k, v in self.constituents.items()}
        day_ret = {
            names[k]: (px[-1] / prev[k] - 1.0) * 100.0 for k, px in series.items() if prev.get(k)
        }
        from engine.data.constituents.nifty_members import IndexConstituent

        members = [
            IndexConstituent(names[k], "?", w, day) for k, (_, w, _) in self.constituents.items()
        ]
        breadth = compute_constituent_breadth(members, day_ret)
        rets = {names[k]: log_returns(px) for k, px in series.items()}
        corr, pairs = weighted_average_correlation(rets, weights)
        return {
            "at": now.astimezone(IST).strftime("%H:%M"),
            "members": len(series),
            "of": len(self.constituents),
            "weighted_breadth_pct": breadth.weighted_breadth_pct,
            "advancing": breadth.advancing_count,
            "declining": breadth.declining_count,
            "realized_corr": corr,
            "corr_pairs": pairs,
            "window_min": self.dispersion_window_min,
            "day_returns_pct": {s: round(v, 2) for s, v in sorted(day_ret.items())},
            "weights_verified": all(v[2] for v in self.constituents.values()),
        }

    def view(self) -> dict[str, Any]:
        out = {
            u: {
                **self.strip.get(u, {"underlying": u}),
                "chain": {
                    k: v for k, v in (self.chain.get(u) or {}).items() if k not in ("rows", "table")
                }
                or None,
            }
            for u in self.underlyings
        }
        if self.dispersion is not None and self.dispersion_index in out:
            out[self.dispersion_index]["dispersion"] = self.dispersion
        return out
