from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from engine.core.clock import ManualClock, market_session
from engine.core.config import load_settings
from engine.core.env import Secret, parse_dotenv, update_dotenv
from engine.core.rules import IST, RuleBook
from engine.data.adapters.base import Depth, FeedStatus, Quote, StatusKind
from engine.data.adapters.fyers import (
    FyersAuthError,
    FyersFeed,
    app_id_hash,
    exchange_auth_code,
    extract_auth_code,
    login_url,
    normalize,
)
from engine.data.adapters.sim import SimFeed, bachelier
from engine.data.recorder.core import Recorder
from engine.data.recorder.run import (
    RecorderRunner,
    RecorderSettings,
    StickyStore,
    load_recorder_settings,
)
from engine.data.reference.instrument_master import InstrumentMaster
from engine.data.schema import EVENTS_SCHEMA, QUOTES_SCHEMA
from engine.data.store import DatasetWriter
from tests.data.conftest import DAY

TOKEN = "eyJhbGciOi.FAKE-ACCESS-TOKEN.sig"

# ------------------------------------------------------------------ normalisation


def test_normalize_scrip_index_depth_and_status() -> None:
    sf = {
        "type": "sf",
        "symbol": "NSE:NIFTY26N1725000CE",
        "ltp": 120.5,
        "vol_traded_today": 1300,
        "last_traded_time": 1794900000,
        "exch_feed_time": 1794900001,
        "bid_size": 650,
        "ask_size": 325,
        "bid_price": 120.0,
        "ask_price": 121.0,
        "last_traded_qty": 65,
        "tot_buy_qty": 10,
        "tot_sell_qty": 20,
        "avg_trade_price": 119.8,
        "low_price": 100.0,
        "high_price": 130.0,
        "lower_ckt": 0,
        "upper_ckt": 0,
        "open_price": 110.0,
        "prev_close_price": 115.0,
    }
    q = normalize(sf, 5)
    assert isinstance(q, Quote) and q.recv_ns == 5
    assert (q.bid, q.bid_qty, q.ask, q.ask_qty, q.volume) == (120.0, 650, 121.0, 325, 1300)
    assert q.exch_ts == 1794900001 and q.ltt == 1794900000
    assert q.oi is None  # SDK 3.2.1 strips OI
    assert q.upper_ckt is None and q.lower_ckt is None  # SDK sends 0 = unknown
    idx = normalize({"type": "if", "symbol": "NSE:NIFTY50-INDEX", "ltp": 25010.5}, 6)
    assert isinstance(idx, Quote) and idx.ltp == 25010.5 and idx.bid is None
    dp: dict[str, Any] = {"type": "dp", "symbol": "NSE:NIFTY26N1725000CE"}
    for i in range(1, 6):
        dp |= {f"bid_price{i}": 120 - i, f"bid_size{i}": 65 * i, f"bid_order{i}": i}
        dp |= {f"ask_price{i}": 120 + i, f"ask_size{i}": 65, f"ask_order{i}": 1}
    d = normalize(dp, 7)
    assert isinstance(d, Depth) and d.bids[0] == (119.0, 65, 1) and len(d.asks) == 5
    cn = normalize({"type": "cn", "code": 200, "message": "Authentication done", "s": "ok"}, 8)
    assert isinstance(cn, FeedStatus) and cn.kind is StatusKind.CONNECTED
    bad = normalize({"type": "cn", "code": 11001, "message": "Authentication failed"}, 8)
    assert isinstance(bad, FeedStatus) and bad.kind is StatusKind.AUTH_FAILED
    lim = normalize({"code": 51001, "message": "Symbol limit exceeded", "s": "error"}, 9)
    assert isinstance(lim, FeedStatus) and lim.kind is StatusKind.LIMIT
    assert normalize({"type": "ful", "s": "ok"}, 9) is None


# ------------------------------------------------------------------ login helpers


def test_login_url_hash_and_auth_code() -> None:
    url = login_url("ABC-100", "https://127.0.0.1/cb", "s123")
    assert url.startswith("https://api-t1.fyers.in/api/v3/generate-authcode?")
    assert "client_id=ABC-100" in url and "response_type=code" in url
    secret = Secret("FYERS_SECRET_KEY", "shh")
    assert app_id_hash("ABC-100", secret) == hashlib.sha256(b"ABC-100:shh").hexdigest()
    redirect = "https://127.0.0.1/cb?s=ok&code=200&auth_code=AUTH.CODE.X&state=s123"
    assert extract_auth_code(redirect, "s123") == "AUTH.CODE.X"
    assert extract_auth_code("AUTH.CODE.X") == "AUTH.CODE.X"
    with pytest.raises(FyersAuthError, match="state"):
        extract_auth_code(redirect, "other")
    with pytest.raises(FyersAuthError):
        extract_auth_code("not a code")


def test_exchange_auth_code_never_leaks(caplog: pytest.LogCaptureFixture) -> None:
    sent: list[dict[str, Any]] = []

    def ok(url: str, body: bytes) -> Mapping[str, Any]:
        sent.append(json.loads(body))
        return {"s": "ok", "code": 200, "access_token": TOKEN}

    tok = exchange_auth_code("ABC-100", Secret("S", "shh"), "AUTH", poster=ok)
    assert tok.reveal() == TOKEN and TOKEN not in repr(tok)
    assert sent[0]["grant_type"] == "authorization_code" and sent[0]["code"] == "AUTH"
    assert "shh" not in json.dumps(sent[0])  # only the hash is sent

    def rejected(url: str, body: bytes) -> Mapping[str, Any]:
        return {"s": "error", "code": -16, "message": "invalid auth code", "echo": TOKEN}

    with pytest.raises(FyersAuthError) as err:
        exchange_auth_code("ABC-100", Secret("S", "shh"), "AUTH", poster=rejected)
    assert TOKEN not in str(err.value) and "invalid auth code" in str(err.value)


def test_update_dotenv_in_place(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# keys\nMODE=free\nFYERS_ACCESS_TOKEN=old\nexport OTHER=1\n", encoding="utf-8")
    update_dotenv(env, {"FYERS_ACCESS_TOKEN": "new", "FYERS_TOKEN_DATE": "2026-11-11"})
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# keys\nMODE=free\n")
    assert parse_dotenv(text) == {
        "MODE": "free",
        "FYERS_ACCESS_TOKEN": "new",
        "OTHER": "1",
        "FYERS_TOKEN_DATE": "2026-11-11",
    }
    with pytest.raises(ValueError):
        update_dotenv(env, {"BAD KEY": "x"})
    with pytest.raises(ValueError):
        update_dotenv(env, {"K": "a\nINJECTED=1"})


# ------------------------------------------------------------------ FyersFeed with a fake SDK


class FakeSocket:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.calls: list[tuple[str, str, int, list[str]]] = []
        self.connected = False
        self.closed = False

    def connect(self) -> None:
        self.connected = True
        self.kw["on_connect"]()

    def is_connected(self) -> bool:
        return self.connected

    def subscribe(self, symbols: list[str], data_type: str, channel: int) -> None:
        self.calls.append(("sub", data_type, channel, list(symbols)))

    def unsubscribe(self, symbols: list[str], data_type: str, channel: int) -> None:
        self.calls.append(("unsub", data_type, channel, list(symbols)))

    def keep_running(self) -> None:
        pass

    def close_connection(self) -> None:
        self.closed = True


def test_fyers_feed_subscription_flow(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    sockets: list[FakeSocket] = []

    def factory(**kw: Any) -> FakeSocket:
        s = FakeSocket(**kw)
        sockets.append(s)
        return s

    clock = ManualClock(datetime(2026, 11, 11, 9, 0, tzinfo=IST))
    feed = FyersFeed(
        "ABC-100",
        Secret("FYERS_ACCESS_TOKEN", TOKEN),
        max_symbols=100,
        log_dir=tmp_path / "sdk",
        clock=clock,
        socket_factory=factory,
    )
    assert TOKEN not in repr(feed.__dict__)
    events: list[Any] = []
    feed.subscribe(["NSE:NIFTY50-INDEX"], depth=False)  # before start: just remembered
    with caplog.at_level(logging.DEBUG):
        feed.start(events.append)
        for _ in range(100):
            if sockets and sockets[0].connected:
                break
            import time

            time.sleep(0.01)
    sock = sockets[0]
    assert sock.kw["access_token"] == f"ABC-100:{TOKEN}" and sock.kw["litemode"] is False
    assert sock.calls == [("sub", "SymbolUpdate", 11, ["NSE:NIFTY50-INDEX"])]
    assert isinstance(events[0], FeedStatus) and events[0].kind is StatusKind.CONNECTED
    sock.calls.clear()
    feed.subscribe(["NSE:NIFTY26N1725000CE"], depth=True)
    assert sock.calls == [
        ("unsub", "SymbolUpdate", 11, ["NSE:NIFTY50-INDEX"]),
        ("sub", "SymbolUpdate", 11, ["NSE:NIFTY50-INDEX", "NSE:NIFTY26N1725000CE"]),
        ("sub", "DepthUpdate", 12, ["NSE:NIFTY26N1725000CE"]),
    ]
    sock.kw["on_message"]({"type": "if", "symbol": "NSE:NIFTY50-INDEX", "ltp": 25000})
    assert isinstance(events[-1], Quote) and events[-1].recv_ns == clock.now_ns()
    sock.calls.clear()
    feed.unsubscribe(["NSE:NIFTY26N1725000CE"], depth=True)
    assert ("sub", "SymbolUpdate", 11, ["NSE:NIFTY50-INDEX"]) in sock.calls
    feed.stop()
    assert sock.closed
    assert TOKEN not in caplog.text
    with pytest.raises(FyersAuthError):
        FyersFeed("", Secret("T", ""), max_symbols=1, log_dir=tmp_path, socket_factory=factory)


# ------------------------------------------------------------------ simulator and runner


def test_bachelier_parity() -> None:
    c = bachelier(25000, 25100, 300, True)
    p = bachelier(25000, 25100, 300, False)
    assert c - p == pytest.approx(25000 - 25100)
    assert bachelier(25000, 24000, 0, True) == 1000


@pytest.fixture
def rs(repo_root: Path, tmp_path: Path) -> RecorderSettings:
    settings = load_settings(root=repo_root, environ={})
    base = load_recorder_settings(settings)
    universe = replace(
        base.universe,
        underlyings=(
            replace(
                base.universe.underlyings[0],
                spot_symbol="NSE:NIFTY50-INDEX",  # the synthetic master uses these tickers
                expiry_roles=("current_weekly",),
            ),
        ),
        vix_symbol="NSE:INDIAVIX-INDEX",
        min_strikes_each_side=3,
        buffer_strikes=0,
        sigma_mult=0.0,
    )
    return replace(base, universe=universe, sticky_file=tmp_path / "sticky.json", budget_margin=0)


def make_runner(
    rs: RecorderSettings, master: InstrumentMaster, tmp: Path, clock: ManualClock, cap: int
) -> tuple[RecorderRunner, SimFeed]:
    book = RuleBook.load(Path(__file__).resolve().parents[2] / "config" / "rules")
    sess = market_session(book, "NSE", "fo", DAY)
    feed = SimFeed(master, clock, spots={"NIFTY": 25010.0}, max_symbols=cap)
    rec = Recorder(
        rs.core,
        quotes=DatasetWriter(tmp, "quotes", QUOTES_SCHEMA),
        events=DatasetWriter(tmp, "recorder_events", EVENTS_SCHEMA),
        session_open=sess.open_dt,
        session_close=sess.close_dt,
        source="sim",
        start_ns=clock.now_ns(),
    )
    runner = RecorderRunner(
        rs,
        adapter=feed,
        master=master,
        clock=clock,
        recorder=rec,
        sticky=StickyStore(rs.sticky_file, DAY),
    )
    return runner, feed


def test_runner_core_first_then_band_within_cap(
    rs: RecorderSettings, master: InstrumentMaster, tmp_path: Path
) -> None:
    clock = ManualClock(datetime(2026, 11, 11, 9, 14, 0, tzinfo=IST))
    runner, feed = make_runner(rs, master, tmp_path, clock, cap=40)
    runner.start()
    assert feed.symbols == {"NSE:NIFTY50-INDEX", "NSE:INDIAVIX-INDEX", "NSE:NIFTY26NOVFUT"}
    feed.pump()
    runner.step(0)  # spot now known -> options selected immediately
    opts = {s for s in feed.symbols if master.get(s).kind.is_option}
    assert len(opts) == 14  # weekly ATM ± 3, CE+PE
    assert len(feed.symbols) <= 40 and runner.selection is not None
    for _ in range(90):
        clock.advance(1)
        feed.pump()
        runner.step(0)
    runner.finish()
    assert runner.recorder.stats.snap_rows > 0 and runner.recorder.stats.gaps == 0
    sticky = json.loads((tmp_path / "sticky.json").read_text())
    assert opts <= set(sticky)


def test_runner_respects_tight_cap_and_restarts_on_silence(
    rs: RecorderSettings, master: InstrumentMaster, tmp_path: Path
) -> None:
    clock = ManualClock(datetime(2026, 11, 11, 11, 0, 0, tzinfo=IST))
    runner, feed = make_runner(rs, master, tmp_path, clock, cap=8)
    runner.start()
    feed.pump()
    runner.step(0)
    assert len(feed.symbols) <= 8 and runner.selection is not None
    assert runner.selection.dropped
    feed.pause(60)
    for _ in range(25):
        clock.advance(1)
        feed.pump()
        runner.step(0)
    assert runner.restarts == 1 and feed.connects == 2
    runner.finish()


class FyersDataSocket:
    """Mimics the fyers-apiv3 3.2.1 layout: a singleton whose private ``__response_output``
    receives the raw per-symbol dict, builds a new dict without ``OI`` and calls ``On_message``."""

    _instance: Any = None

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, **kw: Any) -> None:
        self.OnMessage = kw["on_message"]

    def __response_output(self, data: dict[str, Any], data_type: str) -> None:
        response = {k: v for k, v in data.items() if k not in ("multiplier", "precision")}
        response.pop("OI", None)
        self.On_message(response)

    def On_message(self, message: dict[str, Any]) -> None:  # noqa: N802 (SDK name)
        self.OnMessage(message)

    def feed(self, data: dict[str, Any], data_type: str) -> None:
        self.__response_output(data, data_type)


def test_open_interest_is_restored_from_the_sdk_raw_dict() -> None:
    from engine.data.adapters.fyers import with_open_interest

    FyersDataSocket(on_message=lambda m: None)  # base singleton already exists
    got: list[dict[str, Any]] = []
    sock = with_open_interest(FyersDataSocket)(on_message=got.append)
    assert type(sock) is not FyersDataSocket  # the hooked class, not the base singleton
    raw = {"type": "sf", "symbol": "NSE:NIFTY26N1725000CE", "ltp": 120.5, "OI": 4_563_250}
    sock.feed(raw, "scrips")
    sock.feed({"type": "if", "symbol": "NSE:NIFTY50-INDEX", "ltp": 25010.0}, "index")
    assert got[0]["OI"] == 4_563_250 and "OI" not in got[1]
    assert sock._qe_pending_oi is None  # never leaks into the next message
    q = normalize(got[0], 1)
    assert isinstance(q, Quote) and q.oi == 4_563_250

    class SomethingElse:
        pass

    assert with_open_interest(SomethingElse) is SomethingElse  # unknown layout: unchanged
