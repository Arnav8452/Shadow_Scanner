"""Option chain table (LTP / Greeks / all columns), its live wiring and the gateway hand-off."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from engine.analytics.iv import black76_price
from engine.cockpit.chain import build_chain_table, buyer_pop, max_pain
from engine.core.rules import IST

# ---------------------------------------------------------------- pure maths


def test_max_pain_is_the_cheapest_expiry_for_option_writers() -> None:
    legs = {
        100.0: {"CE": {"oi": 0}, "PE": {"oi": 500}},
        110.0: {"CE": {"oi": 100}, "PE": {"oi": 100}},
        120.0: {"CE": {"oi": 500}, "PE": {"oi": 0}},
    }
    assert max_pain(legs) == 110.0
    assert max_pain({}) is None
    assert max_pain({100.0: {"CE": {"oi": 0}}}) is None


def test_buyer_pop_is_a_probability_and_complements() -> None:
    call = buyer_pop(100.0, 105.0, 0.2, 0.05, is_call=True)
    put = buyer_pop(100.0, 105.0, 0.2, 0.05, is_call=False)
    assert call is not None and put is not None
    assert 0 < call < 0.5 < put < 1 and call + put == pytest.approx(1.0)
    assert buyer_pop(100.0, 105.0, 0.0, 0.05, is_call=True) is None


def test_chain_table_columns() -> None:
    f, t, vol = 25000.0, 5 / 365, 0.15
    legs: dict[float, dict[str, dict[str, float]]] = {}
    for k in (24800.0, 25000.0, 25200.0):
        legs[k] = {}
        for kind in ("CE", "PE"):
            px = black76_price(f, k, t, vol, is_call=kind == "CE", t_cal=t, risk_free_rate=0.06)
            legs[k][kind] = {"bid": round(px - 0.05, 2), "ask": round(px + 0.05, 2),
                             "ltp": round(px, 2), "prev_close": round(px * 1.1, 2),
                             "oi": 1000.0 if kind == "CE" else 1500.0, "volume": 200.0}  # fmt: skip
    first: dict[tuple[float, str], int] = {}
    out = build_chain_table(
        legs, forward=f, spot=24990.0, t_var=t, t_cal=t, r=0.06,
        syms={(25000.0, "CE"): "C25000"}, prev_oi={(25000.0, "CE"): 800}, first_oi=first,
    )  # fmt: skip
    row = next(r for r in out["rows"] if r["k"] == 25000.0)
    ce, pe = row["ce"], row["pe"]
    assert ce["sym"] == "C25000" and pe["sym"] is None
    assert ce["iv"] == pytest.approx(15.0, abs=0.3) and pe["iv"] == pytest.approx(15.0, abs=0.3)
    assert 0.45 < ce["delta"] < 0.6 and -0.55 < pe["delta"] < -0.4
    assert ce["theta"] < 0 and ce["vega"] > 0 and ce["gamma"] > 0
    assert ce["oi_chg"] == 200 and ce["oi_chg_pct"] == 25.0 and "oi_chg" not in pe
    assert ce["chg_pct"] == pytest.approx(-9.1, abs=0.1)
    assert ce["breakeven"] == pytest.approx(25000.0 + legs[25000.0]["CE"]["ask"])
    assert ce["intrinsic"] == 0.0 and ce["time_value"] > 0
    assert 0 < ce["pop"] < 50 and 0 < pe["pop"] < 50  # buyers of ATM options usually lose
    assert row["pcr"] == 1.5 and ce["doi"] == 0
    s = out["summary"]
    assert s["pcr_oi"] == 1.5 and s["max_pain"] is not None and s["call_oi_chg"] == 200


# ---------------------------------------------------------------- live wiring


def test_live_chains_from_recorder_snapshots(repo_root: Path, tmp_path: Path) -> None:
    from engine.cockpit.analytics_live import LiveAnalytics
    from engine.core.calendar import TradingCalendar
    from engine.core.rules import RuleBook
    from engine.data.reference.instrument_master import Kind
    from engine.data.schema import QUOTES_SCHEMA
    from engine.data.store import DatasetWriter, Store
    from tests.data.conftest import MONTHLY, WEEKLY, build_master, opt_symbol
    from tests.data.test_shadow_live import _quote_row

    book = RuleBook.load(repo_root / "config" / "rules")
    master = build_master(range(24800, 25250, 50))
    now = datetime(2026, 11, 11, 11, 0, 30, tzinfo=IST)
    snap_t = datetime(2026, 11, 11, 11, 0, tzinfo=IST)
    w = DatasetWriter(tmp_path, "quotes", QUOTES_SCHEMA)
    for exp, t in ((WEEKLY, 6 / 365), (MONTHLY, 12 / 365)):
        for k in range(24800, 25250, 50):
            for kind in (Kind.CALL, Kind.PUT):
                px = black76_price(25000.0, k, t, 0.15, is_call=kind is Kind.CALL, t_cal=t,
                                   risk_free_rate=0.06)  # fmt: skip
                w.append(_quote_row(snap_t, opt_symbol(exp, k, kind), kind.value, expiry=exp,
                                    strike=float(k), bid=round(px - 0.05, 2),
                                    ask=round(px + 0.05, 2), ltp=round(px, 2), oi=1000,
                                    prev_close=round(px, 2), volume=50))  # fmt: skip
    w.flush()
    la = LiveAnalytics(
        book=book, calendar=TradingCalendar.from_rulebook(book), master=master,
        underlyings={"NIFTY": "NSE:NIFTY50-INDEX"}, iv_history={}, chain_dir=tmp_path,
        prev_oi={"NIFTY": {(WEEKLY.isoformat(), 25000.0, "CE"): 900}},
    )  # fmt: skip
    la.strip["NIFTY"] = {"spot": 25000.0, "forward": 25000.0, "expiry": WEEKLY.isoformat()}
    store = Store(tmp_path)
    near = la.chain_snapshot(store, "NIFTY", WEEKLY, now)
    assert near is not None and near["rows"] and near["max_pain"] is not None
    ce = next(r["ce"] for r in near["table"]["rows"] if r["k"] == 25000.0)
    assert ce["oi_chg"] == 100 and ce["sym"] == opt_symbol(WEEKLY, 25000, Kind.CALL)
    assert la._recorded_expiries(store, "NIFTY", WEEKLY, now) == [MONTHLY]
    later = la.chain_table(store, "NIFTY", MONTHLY, now)
    assert later is not None and later["forward"] == pytest.approx(25000.0, abs=2.0)
    assert later["prev_oi_available"] is False and later["lot"] == 65
    la.chain["NIFTY"] = near
    view = la.view()["NIFTY"]["chain"]
    assert "rows" not in view and "table" not in view and view["max_pain"] == near["max_pain"]


# ---------------------------------------------------------------- gateway hand-off


class _WS:
    closed = False

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_str(self, s: str) -> None:
        self.sent.append(json.loads(s))


def test_gateway_sends_a_chain_only_on_request_and_change(repo_root: Path, tmp_path: Path) -> None:
    from engine.cockpit.config import CockpitConfig
    from engine.cockpit.gateway import Client, Gateway

    ccfg = CockpitConfig(key_file=tmp_path / "cockpit.key", journal_dir=tmp_path,
                         replay_dir=tmp_path / "replay")  # fmt: skip
    gw = Gateway(ccfg, repo_root, ledger_file=tmp_path / "ledger.jsonl")
    again = Gateway(ccfg, repo_root, ledger_file=tmp_path / "ledger.jsonl")
    assert gw.token == again.token  # open pages survive a gateway restart
    chains = {"rev": 1, "data": {"NIFTY": {"2026-11-17": {"rows": [1]}, "2026-11-24": {"rows": [2]}}}}
    gw._cache["live"] = (1, json.dumps({"type": "snap"}), {}, chains)
    ws = _WS()
    c = Client(ws)  # type: ignore[arg-type]

    async def run() -> None:
        await gw._push(c, [], None)
        assert [m["type"] for m in ws.sent] == ["snap"]  # no chain until asked
        await gw._on_client(c, {"type": "chain", "underlying": "NIFTY", "expiry": ""})
        await gw._push(c, [], None)
        msg = ws.sent[-1]
        assert msg["type"] == "chain" and msg["expiry"] == "2026-11-17"
        assert msg["expiries"] == ["2026-11-17", "2026-11-24"] and msg["table"] == {"rows": [1]}
        n = len(ws.sent)
        await gw._push(c, [], None)
        assert len(ws.sent) == n  # unchanged: nothing re-sent
        await gw._on_client(c, {"type": "chain", "underlying": "NIFTY", "expiry": "2026-11-24"})
        await gw._push(c, [], None)
        assert ws.sent[-1]["table"] == {"rows": [2]}

    asyncio.run(run())
