"""Upstox V3 feed: protobuf decoding, socket flow (fake socket), master parsing, login, tokens."""

from __future__ import annotations

import json
import logging
import struct
import threading
import time
from collections.abc import Mapping
from datetime import date, time as dtime
from pathlib import Path
from typing import Any

import pytest

from engine.core.config import load_settings
from engine.core.env import Secret
from engine.data.adapters.base import Depth, FeedStatus, Quote, StatusKind
from engine.data.adapters.protowire import ProtoDecodeError, decode, iter_fields
from engine.data.adapters.upstox import (
    MODE_FULL,
    UpstoxAuthError,
    UpstoxFeed,
    decode_feed_response,
    exchange_upstox_code,
    extract_upstox_code,
    upstox_login_url,
)
from engine.data.recorder.run import RecorderSetupError, load_recorder_settings, upstox_token
from engine.data.reference.instrument_master import Kind, parse_upstox_master

TOKEN = "eyJ0eXAiOiJKV1Qi.UPSTOX-SECRET.sig"
WSS = "wss://wsfeeder-api.upstox.com/market-data-feeder/v3/upstox-developer-api/feeds?requestId=abc&code=ONE-TIME-CODE"

# ------------------------------------------------------------------ tiny protobuf encoder


def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def vint(num: int, v: int) -> bytes:
    return _varint(num << 3) + _varint(v)


def dbl(num: int, v: float) -> bytes:
    return _varint(num << 3 | 1) + struct.pack("<d", v)


def msg(num: int, body: bytes) -> bytes:
    return _varint(num << 3 | 2) + _varint(len(body)) + body


def text(num: int, s: str) -> bytes:
    return msg(num, s.encode())


def option_frame(key: str, *, ltp: float, oi: float, ts_ms: int, live: bool = True) -> bytes:
    ltpc = dbl(1, ltp) + vint(2, ts_ms - 500) + vint(3, 65) + dbl(4, ltp - 5)
    levels = b"".join(
        msg(
            1,
            vint(1, 650 * (i + 1))
            + dbl(2, ltp - 0.5 * (i + 1))
            + vint(3, 130)
            + dbl(4, ltp + 0.5 * (i + 1)),
        )
        for i in range(5)
    )
    greeks = dbl(1, 0.52) + dbl(2, -11.0) + dbl(3, 0.0009) + dbl(4, 9.5)
    ohlc = msg(
        1, text(1, "1d") + dbl(2, ltp - 10) + dbl(3, ltp + 10) + dbl(4, ltp - 20) + dbl(5, ltp)
    )
    market_ff = (
        msg(1, ltpc)
        + msg(2, levels)
        + msg(3, greeks)
        + msg(4, ohlc)
        + dbl(5, ltp - 1)
        + vint(6, 1_300_000)
        + dbl(7, oi)
        + dbl(8, 0.1325)
        + dbl(9, 50_000.0)
        + dbl(10, 70_000.0)
    )
    feed = msg(2, msg(1, market_ff)) + vint(4, 1)
    entry = text(1, key) + msg(2, feed)
    head = vint(1, 1) if live else b""  # proto3 omits 0 (initial_feed)
    return head + msg(2, entry) + vint(3, ts_ms)


def index_frame(key: str, ltp: float, ts_ms: int) -> bytes:
    feed = msg(2, msg(2, msg(1, dbl(1, ltp) + vint(2, ts_ms) + dbl(4, ltp - 50))))
    return vint(1, 1) + msg(2, text(1, key) + msg(2, feed)) + vint(3, ts_ms)


TS_MS = 1_794_900_000_123


def test_protowire_basics() -> None:
    assert list(iter_fields(vint(1, 150) + dbl(2, 1.5))) == [
        (1, 0, 150),
        (2, 1, struct.pack("<d", 1.5)),
    ]
    with pytest.raises(ProtoDecodeError):
        list(iter_fields(b"\x0a\x05ab"))  # length 5 but only 2 bytes
    from engine.data.adapters.protowire import F

    assert decode(vint(1, (1 << 64) - 3), {1: F("x", "int64")}) == {"x": -3}  # negative int64


def test_decode_option_frame_to_quote_and_depth() -> None:
    doc = decode_feed_response(option_frame("NSE_FO|45134", ltp=120.5, oi=4_563_250.0, ts_ms=TS_MS))
    assert doc["type"] == "live_feed" and doc["currentTs"] == TS_MS
    ff = doc["feeds"]["NSE_FO|45134"]["fullFeed"]["marketFF"]
    assert ff["oi"] == 4_563_250.0 and len(ff["marketLevel"]["bidAskQuote"]) == 5
    assert decode_feed_response(option_frame("X", ltp=1, oi=0, ts_ms=1, live=False))["type"] == (
        "initial_feed"
    )
    feed = UpstoxFeed(token=Secret("T", TOKEN), reconnect=False)
    events: list[Any] = []
    feed.sink = events.append
    assert (
        feed.ingest_frame(option_frame("NSE_FO|45134", ltp=120.5, oi=4_563_250.0, ts_ms=TS_MS)) == 2
    )
    q, d = events
    assert isinstance(q, Quote) and isinstance(d, Depth)
    assert (q.ltp, q.oi, q.volume, q.ltq, q.prev_close) == (120.5, 4_563_250, 1_300_000, 65, 115.5)
    assert (q.bid, q.bid_qty, q.ask, q.ask_qty) == (120.0, 650, 121.0, 130)
    assert q.exch_ts == TS_MS // 1000 and q.ltt == (TS_MS - 500) // 1000
    assert (q.open, q.high, q.low) == (110.5, 130.5, 100.5)
    assert d.bids[0] == (120.0, 650, None) and d.asks[-1] == (123.0, 130, None)
    events.clear()
    feed.ingest_frame(index_frame("NSE_INDEX|Nifty 50", 25010.5, TS_MS))
    assert len(events) == 1 and events[0].ltp == 25010.5 and events[0].bid is None


# ------------------------------------------------------------------ socket flow with a fake


class FakeWS:
    def __init__(self, url: str, **cb: Any) -> None:
        self.url = url
        self.cb = cb
        self.sent: list[tuple[bytes, int]] = []
        self.closed = False
        self.opened = threading.Event()
        self.run_kwargs: dict[str, Any] = {}

    def run_forever(self, **kw: Any) -> None:
        self.run_kwargs = kw
        self.cb["on_open"](self)
        self.opened.set()

    def send(self, data: bytes, opcode: int) -> None:
        self.sent.append((data, opcode))

    def close(self) -> None:
        self.closed = True
        self.cb["on_close"](self, 1000, "bye")


def make_feed(sockets: list[FakeWS], auth_headers: list[Mapping[str, str]]) -> UpstoxFeed:
    def http_get(url: str, headers: Mapping[str, str]) -> bytes:
        auth_headers.append(headers)
        assert url == "https://api.upstox.com/v3/feed/market-data-feed/authorize"
        return json.dumps({"status": "success", "data": {"authorizedRedirectUri": WSS}}).encode()

    def factory(url: str, **cb: Any) -> FakeWS:
        ws = FakeWS(url, **cb)
        sockets.append(ws)
        return ws

    return UpstoxFeed(
        token=Secret("UPSTOX_ANALYTICS_TOKEN", TOKEN),
        mode=MODE_FULL,
        max_symbols=4,
        http_get=http_get,
        ws_factory=factory,
        reconnect=False,
    )


def wait(ev: threading.Event) -> None:
    assert ev.wait(2.0), "fake socket never opened"


def test_socket_flow_subscribe_data_restart(caplog: pytest.LogCaptureFixture) -> None:
    sockets: list[FakeWS] = []
    headers: list[Mapping[str, str]] = []
    feed = make_feed(sockets, headers)
    events: list[Any] = []
    feed.subscribe(["NSE_INDEX|Nifty 50"], depth=False)  # before start: remembered
    with caplog.at_level(logging.DEBUG):
        feed.start(events.append)
        wait(sockets[0].opened)
    ws = sockets[0]
    assert ws.url == WSS and headers[0]["Authorization"] == f"Bearer {TOKEN}"
    ctx = ws.run_kwargs["sslopt"]["context"]
    assert ctx.verify_mode.name == "CERT_REQUIRED" and ctx.check_hostname  # TLS verified
    first = json.loads(ws.sent[0][0])
    assert ws.sent[0][1] == 0x2  # binary frame, as Upstox requires
    assert first["method"] == "sub" and first["data"] == {
        "mode": "full",
        "instrumentKeys": ["NSE_INDEX|Nifty 50"],
    }
    assert isinstance(events[0], FeedStatus) and events[0].kind is StatusKind.CONNECTED

    feed.subscribe(["NSE_FO|45134", "NSE_FO|45135"], depth=True)
    assert json.loads(ws.sent[-1][0])["data"]["instrumentKeys"] == ["NSE_FO|45134", "NSE_FO|45135"]
    feed.subscribe(["A", "B", "C"], depth=True)  # would exceed max_symbols=4
    assert events[-1].kind is StatusKind.LIMIT

    ws.cb["on_message"](ws, option_frame("NSE_FO|45134", ltp=99.0, oi=1000.0, ts_ms=TS_MS))
    assert any(isinstance(e, Quote) and e.oi == 1000 for e in events)
    ws.cb["on_message"](ws, b"\xff\xff")  # garbage frame
    assert feed.decode_errors == 1

    feed.unsubscribe(["NSE_FO|45135"], depth=True)
    assert json.loads(ws.sent[-1][0])["method"] == "unsub"

    feed.restart()
    wait(sockets[1].opened)
    assert ws.closed and len(sockets) == 2
    resub = json.loads(sockets[1].sent[0][0])["data"]["instrumentKeys"]
    assert resub == ["NSE_FO|45134", "NSE_INDEX|Nifty 50"]
    kinds = [e.kind for e in events if isinstance(e, FeedStatus)]
    assert kinds.count(StatusKind.DISCONNECTED) == 1  # the old socket's close is ignored

    sockets[1].cb["on_error"](sockets[1], RuntimeError(f"handshake failed for {WSS}"))
    assert "ONE-TIME-CODE" not in events[-1].detail and "<redacted>" in events[-1].detail
    feed.stop()
    assert sockets[1].closed
    blob = " ".join(str(e) for e in events) + caplog.text
    assert TOKEN not in blob and "ONE-TIME-CODE" not in blob


def test_reconnects_with_backoff_after_drop() -> None:
    sockets: list[FakeWS] = []
    feed = make_feed(sockets, [])
    feed._reconnect = True
    feed._backoff_s = 0.01
    feed.start(lambda e: None)
    wait(sockets[0].opened)
    sockets[0].cb["on_close"](sockets[0], 1006, "network")
    deadline = time.monotonic() + 2
    while len(sockets) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(sockets) == 2
    feed.stop()


# ------------------------------------------------------------------ instrument master


def test_parse_upstox_master_rows() -> None:
    rows = [
        {
            "segment": "NSE_INDEX",
            "instrument_type": "INDEX",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "exchange": "NSE",
            "trading_symbol": "NIFTY",
            "name": "Nifty 50",
        },
        {
            "segment": "NSE_INDEX",
            "instrument_type": "INDEX",
            "instrument_key": "NSE_INDEX|India VIX",
            "exchange": "NSE",
            "trading_symbol": "INDIA VIX",
            "name": "India VIX",
        },
        {
            "segment": "NSE_FO",
            "instrument_type": "CE",
            "instrument_key": "NSE_FO|45134",
            "exchange": "NSE",
            "exchange_token": "45134",
            "expiry": 1794940199000,
            "strike_price": 25000.0,
            "lot_size": 65,
            "tick_size": 5.0,
            "underlying_symbol": "NIFTY",
            "underlying_key": "NSE_INDEX|Nifty 50",
            "trading_symbol": "NIFTY 25000 CE 17 NOV 26",
        },
        {
            "segment": "NSE_FO",
            "instrument_type": "FUT",
            "instrument_key": "NSE_FO|61471",
            "exchange": "NSE",
            "exchange_token": "61471",
            "expiry": 1795458599000,
            "strike_price": 0.0,
            "lot_size": 65,
            "tick_size": 10.0,
            "underlying_symbol": "NIFTY",
            "underlying_key": "NSE_INDEX|Nifty 50",
            "trading_symbol": "NIFTY FUT 23 NOV 26",
        },
        {
            "segment": "BSE_FO",
            "instrument_type": "PE",
            "instrument_key": "BSE_FO|1163250",
            "exchange": "BSE",
            "exchange_token": "1163250",
            "expiry": 1794594599000,
            "strike_price": 80000.0,
            "lot_size": 20,
            "tick_size": 5.0,
            "underlying_symbol": "SENSEX",
            "underlying_key": "BSE_INDEX|SENSEX",
            "trading_symbol": "SENSEX 80000 PE 12 NOV 26",
        },
        {
            "segment": "NCD_FO",
            "instrument_type": "CE",
            "instrument_key": "NCD_FO|14223",
            "exchange": "NSE",
            "expiry": 1794594599000,
            "strike_price": 101.5,
            "lot_size": 1,
            "tick_size": 0.25,
        },
        {
            "segment": "NSE_EQ",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE002A01018",
            "exchange": "NSE",
            "isin": "INE002A01018",
            "lot_size": 1,
            "tick_size": 10.0,
            "trading_symbol": "RELIANCE",
        },
    ]
    items = {i.symbol: i for i in parse_upstox_master(rows, close_time=dtime(15, 40))}
    assert "NCD_FO|14223" not in items  # currency derivatives skipped
    ce = items["NSE_FO|45134"]
    assert ce.kind is Kind.CALL and ce.strike == 25000 and ce.lot_size == 65
    assert ce.tick_size == pytest.approx(0.05)  # paise -> rupees
    assert ce.expiry == date(2026, 11, 17) and ce.expiry_ts is not None
    assert ce.expiry_ts.time() == dtime(15, 40)
    assert items["NSE_FO|61471"].kind is Kind.FUTURE and items["NSE_FO|61471"].tick_size == 0.1
    assert items["NSE_INDEX|Nifty 50"].underlying == "NIFTY"  # spot lines up with the chain
    assert items["NSE_INDEX|India VIX"].underlying == "INDIAVIX"
    sensex = items["BSE_INDEX|SENSEX"]  # not in the rows: added from underlying_key
    assert sensex.kind is Kind.INDEX and sensex.underlying == "SENSEX" and sensex.exchange == "BSE"
    assert items["NSE_EQ|INE002A01018"].kind is Kind.EQUITY


# ------------------------------------------------------------------ login and tokens


def test_upstox_login_helpers() -> None:
    url = upstox_login_url("KEY-1", "https://127.0.0.1/cb", "st8")
    assert url.startswith("https://api.upstox.com/v2/login/authorization/dialog?")
    assert "client_id=KEY-1" in url and "response_type=code" in url
    redirect = "https://127.0.0.1/cb?code=AbC123&state=st8"
    assert extract_upstox_code(redirect, "st8") == "AbC123"
    with pytest.raises(UpstoxAuthError, match="state"):
        extract_upstox_code(redirect, "other")
    sent: list[tuple[str, bytes, Mapping[str, str]]] = []

    def ok(url: str, body: bytes, headers: Mapping[str, str]) -> Mapping[str, Any]:
        sent.append((url, body, headers))
        return {"access_token": TOKEN, "user_id": "X"}

    tok = exchange_upstox_code(
        "KEY-1", Secret("S", "shh"), "https://127.0.0.1/cb", "AbC123", poster=ok
    )
    assert tok.reveal() == TOKEN and TOKEN not in repr(tok)
    assert sent[0][0] == "https://api.upstox.com/v2/login/authorization/token"
    assert b"grant_type=authorization_code" in sent[0][1] and b"code=AbC123" in sent[0][1]
    assert sent[0][2]["Content-Type"] == "application/x-www-form-urlencoded"

    def bad(url: str, body: bytes, headers: Mapping[str, str]) -> Mapping[str, Any]:
        return {"status": "error", "errors": [{"message": "Invalid Auth code"}]}

    with pytest.raises(UpstoxAuthError, match="Invalid Auth code"):
        exchange_upstox_code("KEY-1", Secret("S", "shh"), "https://127.0.0.1/cb", "x", poster=bad)


def test_token_choice_prefers_todays_login_then_analytics() -> None:
    day = date(2026, 11, 11)
    env = {
        "UPSTOX_ANALYTICS_TOKEN": "A" * 20,
        "UPSTOX_ACCESS_TOKEN": "D" * 20,
        "UPSTOX_TOKEN_DATE": "2026-11-11",
    }
    assert upstox_token(env, day).name == "UPSTOX_ACCESS_TOKEN"
    env["UPSTOX_TOKEN_DATE"] = "2026-11-10"  # yesterday's daily token has expired
    assert upstox_token(env, day).name == "UPSTOX_ANALYTICS_TOKEN"
    with pytest.raises(RecorderSetupError, match="no Upstox token"):
        upstox_token({}, day)


def test_recorder_settings_resolve_broker_keys(repo_root: Path) -> None:
    settings = load_settings(root=repo_root, environ={})
    assert settings.data_primary == "upstox"
    rs = load_recorder_settings(settings)
    assert rs.master_broker == "upstox" and rs.master_segments == ("NSE", "BSE")
    assert [u.spot_symbol for u in rs.universe.underlyings] == [
        "NSE_INDEX|Nifty 50",
        "BSE_INDEX|SENSEX",
    ]
    assert rs.universe.vix_symbol == "NSE_INDEX|India VIX"
    assert rs.symbol_caps["upstox"] == 2000 and rs.symbol_caps["fyers"] == 100
