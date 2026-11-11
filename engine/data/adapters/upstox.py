"""Upstox API V3 market-data feed: login helpers, protobuf decoding and the live socket.

Chosen as the M2 recorder feed on 11 Nov 2026 (free; the owner has not paid for any API).

Facts (Upstox docs, checked 10–11 Nov 2026):
* Endpoint https://upstox.com/developer/api-documentation/v3/get-market-data-feed/ :
  requests are binary JSON frames, responses are protobuf ``FeedResponse`` frames.
* Limits per user: 2 connections; ``full`` mode (LTPC, 5-level depth, OI, Greeks) up to 2,000
  keys when only one mode is used (1,500 when modes are mixed).
* ``full_d30`` (30 levels) needs **Upstox Plus**: 50 keys per connection, 5 connections. Plus is
  free to switch to but charges ₹30 instead of ₹20 per executed order. The recorder uses
  ``full``, which does not need Plus.
* Messages carry Upstox's server time (``currentTs``) and the last-trade time, not an exchange
  feed time; depth levels have no order counts. OI arrives as a double.
* Tokens: the daily OAuth access token (``tasks.py upstox-login``) works for the feed. Whether
  the 1-year read-only Analytics Token also opens the socket is not documented — try it first.

**Not yet run against the live feed.** Treat the first live session as the test.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from engine.core.clock import Clock, SystemClock
from engine.core.env import Secret, get_secret
from engine.core.net import USER_AGENT
from engine.data.adapters.base import Depth, FeedStatus, Level, Quote, Sink, StatusKind
from engine.data.adapters.protowire import F, ProtoDecodeError, decode

log = logging.getLogger(__name__)

UPSTOX_AUTH_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
UPSTOX_QUOTE_URL = "https://api.upstox.com/v2/market-quote/full"

# Upstox V3 subscription modes:
MODE_LTPC = "ltpc"
MODE_FULL = "full"  # 5-level depth + Greeks (up to 500 instruments on Upstox Plus)
MODE_FULL_D30 = "full_d30"  # 30-level depth + Greeks + CAS (up to 50 instruments on Upstox Plus)


def resolve_upstox_token(environ: Mapping[str, str] | None = None) -> Secret:
    """Resolve Upstox token: prefers 1-year read-only ``UPSTOX_ANALYTICS_TOKEN`` over daily token."""
    analytics = get_secret("UPSTOX_ANALYTICS_TOKEN", environ=environ)
    if analytics is not None and analytics.reveal().strip():
        return analytics
    access = get_secret("UPSTOX_ACCESS_TOKEN", environ=environ)
    if access is not None and access.reveal().strip():
        return access
    raise RuntimeError(
        "Neither UPSTOX_ANALYTICS_TOKEN nor UPSTOX_ACCESS_TOKEN is set in environment/.env"
    )


def _opt_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _opt_int(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _ms_to_epoch_s(val: Any) -> int | None:
    raw = _opt_int(val)
    if raw is None or raw <= 0:
        return None
    return raw // 1000 if raw > 10_000_000_000 else raw


def parse_upstox_v3_feed(payload: Mapping[str, Any], recv_ns: int) -> list[Quote | Depth]:
    """Translate an Upstox V3 ``market_info`` / ``feeds`` JSON frame into Quote & Depth events.

    Supports ``ltpc``, ``full`` (5-level depth), and ``full_d30`` (30-level depth + Greeks + CAS).
    """
    events: list[Quote | Depth] = []
    feeds = payload.get("feeds")
    if not isinstance(feeds, Mapping):
        return events

    for symbol, node in feeds.items():
        if not isinstance(node, Mapping):
            continue
        # Unwrap fullFeed -> marketFF / indexFF or ltpc
        ff = node.get("fullFeed")
        market_ff: Mapping[str, Any] = {}
        if isinstance(ff, Mapping):
            m_ff = ff.get("marketFF") or ff.get("indexFF")
            if isinstance(m_ff, Mapping):
                market_ff = m_ff
        elif isinstance(node.get("marketFF"), Mapping):
            market_ff = node["marketFF"]

        ltpc = market_ff.get("ltpc") if market_ff else node.get("ltpc")
        if not isinstance(ltpc, Mapping):
            ltpc = {}

        ltp = _opt_float(ltpc.get("ltp"))
        ltt = _ms_to_epoch_s(ltpc.get("ltt"))
        ltq = _opt_int(ltpc.get("ltq"))
        prev_close = _opt_float(ltpc.get("cp"))

        # Market level (5-level or 30-level bidAskQuote)
        m_level = market_ff.get("marketLevel")
        raw_quotes: Sequence[Any] = ()
        if isinstance(m_level, Mapping) and isinstance(m_level.get("bidAskQuote"), Sequence):
            raw_quotes = m_level["bidAskQuote"]
        elif isinstance(market_ff.get("bidAskQuote"), Sequence):
            raw_quotes = market_ff["bidAskQuote"]

        bids: list[Level] = []
        asks: list[Level] = []
        for item in raw_quotes:
            if not isinstance(item, Mapping):
                continue
            bp = _opt_float(item.get("bidP") if "bidP" in item else item.get("bp"))
            bq = _opt_int(item.get("bidQ") if "bidQ" in item else item.get("bq"))
            bo = _opt_int(item.get("bidO") if "bidO" in item else item.get("bo"))
            if bp is not None and bq is not None and bp > 0:
                bids.append((bp, bq, bo))

            ap = _opt_float(item.get("askP") if "askP" in item else item.get("ap"))
            aq = _opt_int(item.get("askQ") if "askQ" in item else item.get("aq"))
            ao = _opt_int(item.get("askO") if "askO" in item else item.get("ao"))
            if ap is not None and aq is not None and ap > 0:
                asks.append((ap, aq, ao))

        # Sort bids descending, asks ascending
        bids.sort(key=lambda lv: lv[0], reverse=True)
        asks.sort(key=lambda lv: lv[0])

        # Option Greeks
        greeks = market_ff.get("optionGreeks")
        if not isinstance(greeks, Mapping):
            greeks = {}
        iv = _opt_float(market_ff.get("iv") if "iv" in market_ff else greeks.get("iv"))
        delta = _opt_float(greeks.get("delta"))
        gamma = _opt_float(greeks.get("gamma"))
        theta = _opt_float(greeks.get("theta"))
        vega = _opt_float(greeks.get("vega"))
        rho = _opt_float(greeks.get("rho"))

        # OHLC (1d interval)
        ohlc_open: float | None = None
        ohlc_high: float | None = None
        ohlc_low: float | None = None
        m_ohlc = market_ff.get("marketOHLC")
        if isinstance(m_ohlc, Mapping) and isinstance(m_ohlc.get("ohlc"), Sequence):
            for bar in m_ohlc["ohlc"]:
                if isinstance(bar, Mapping) and str(bar.get("interval", "")).lower() in (
                    "1d",
                    "d1",
                    "day",
                    "",
                ):
                    ohlc_open = _opt_float(bar.get("open"))
                    ohlc_high = _opt_float(bar.get("high"))
                    ohlc_low = _opt_float(bar.get("low"))
                    break

        # Extended details & CAS (Closing Auction Session / Pre-open)
        e_feed = market_ff.get("eFeedDetails")
        if not isinstance(e_feed, Mapping):
            e_feed = market_ff

        atp = _opt_float(e_feed.get("atp") or market_ff.get("vtt_atp"))
        vtt = _opt_int(e_feed.get("vtt") or market_ff.get("vtt"))
        oi = _opt_int(e_feed.get("oi") or market_ff.get("oi"))
        tbq = _opt_int(e_feed.get("tbq") or market_ff.get("tbq"))
        tsq = _opt_int(e_feed.get("tsq") or market_ff.get("tsq"))
        upper_ckt = _opt_float(e_feed.get("uc") or market_ff.get("upper_ckt"))
        lower_ckt = _opt_float(e_feed.get("lc") or market_ff.get("lower_ckt"))
        iep = _opt_float(e_feed.get("iep") or market_ff.get("iep"))
        iiq_total = _opt_int(e_feed.get("iiqTotal") or market_ff.get("iiqTotal"))
        exch_ts = _ms_to_epoch_s(payload.get("currentTs") or ltt)

        best_bid = bids[0][0] if bids else None
        best_bid_qty = bids[0][1] if bids else None
        best_ask = asks[0][0] if asks else None
        best_ask_qty = asks[0][1] if asks else None

        events.append(
            Quote(
                symbol=str(symbol),
                recv_ns=recv_ns,
                exch_ts=exch_ts,
                ltt=ltt,
                ltp=ltp,
                ltq=ltq,
                atp=atp,
                volume=vtt,
                oi=oi,
                tbq=tbq,
                tsq=tsq,
                bid=best_bid,
                bid_qty=best_bid_qty,
                ask=best_ask,
                ask_qty=best_ask_qty,
                open=ohlc_open,
                high=ohlc_high,
                low=ohlc_low,
                prev_close=prev_close,
                upper_ckt=upper_ckt,
                lower_ckt=lower_ckt,
                iv=iv,
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                rho=rho,
                iep=iep,
                iiq_total=iiq_total,
            )
        )
        if bids or asks:
            events.append(
                Depth(
                    symbol=str(symbol),
                    recv_ns=recv_ns,
                    bids=tuple(bids),
                    asks=tuple(asks),
                    exch_ts=exch_ts,
                )
            )
    return events


def build_upstox_sub_frame(
    symbols: Sequence[str],
    *,
    mode: str = MODE_FULL_D30,
    method: str = "sub",
    guid: str = "rqe-v3-sub",
) -> bytes:
    """Build the binary/UTF-8 JSON subscription message required by Upstox V3 WebSocket."""
    msg = {
        "guid": guid,
        "method": method,
        "data": {
            "mode": mode,
            "instrumentKeys": list(symbols),
        },
    }
    return json.dumps(msg).encode("utf-8")


def _default_http_get(url: str, headers: Mapping[str, str]) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    with urllib.request.urlopen(req, timeout=30.0) as resp:
        body: bytes = resp.read()
    return body


# --------------------------------------------------------------------------- protobuf schema
# Field numbers read from Upstox's MarketDataFeedV3 descriptor (upstox-python-sdk 2.30.0, MIT),
# decoded as data with engine.data.adapters.protowire — no generated code is imported.

_LTPC: dict[int, F] = {
    1: F("ltp", "double"),
    2: F("ltt", "int64"),
    3: F("ltq", "int64"),
    4: F("cp", "double"),
    5: F("iep", {1: F("value", "double")}),
}
_QUOTE: dict[int, F] = {
    1: F("bidQ", "int64"),
    2: F("bidP", "double"),
    3: F("askQ", "int64"),
    4: F("askP", "double"),
}
_GREEKS: dict[int, F] = {
    1: F("delta", "double"),
    2: F("theta", "double"),
    3: F("gamma", "double"),
    4: F("vega", "double"),
    5: F("rho", "double"),
}
_OHLC: dict[int, F] = {
    1: F("interval", "string"),
    2: F("open", "double"),
    3: F("high", "double"),
    4: F("low", "double"),
    5: F("close", "double"),
    6: F("vol", "int64"),
    7: F("ts", "int64"),
}
_MARKET_OHLC: dict[int, F] = {1: F("ohlc", _OHLC, repeated=True)}
_MARKET_FF: dict[int, F] = {
    1: F("ltpc", _LTPC),
    2: F("marketLevel", {1: F("bidAskQuote", _QUOTE, repeated=True)}),
    3: F("optionGreeks", _GREEKS),
    4: F("marketOHLC", _MARKET_OHLC),
    5: F("atp", "double"),
    6: F("vtt", "int64"),
    7: F("oi", "double"),
    8: F("iv", "double"),
    9: F("tbq", "double"),
    10: F("tsq", "double"),
    11: F("iep", "double"),
    12: F("rp", "double"),
    13: F("ieq", "int64"),
    14: F("iiqTotal", "int64"),
    15: F("iiqM", "int64"),
    16: F("casEligible", "bool"),
}
_INDEX_FF: dict[int, F] = {1: F("ltpc", _LTPC), 2: F("marketOHLC", _MARKET_OHLC)}
_FEED: dict[int, F] = {
    1: F("ltpc", _LTPC),
    2: F("fullFeed", {1: F("marketFF", _MARKET_FF), 2: F("indexFF", _INDEX_FF)}),
    3: F(
        "firstLevelWithGreeks",
        {
            1: F("ltpc", _LTPC),
            2: F("firstDepth", _QUOTE),
            3: F("optionGreeks", _GREEKS),
            4: F("vtt", "int64"),
            5: F("oi", "double"),
            6: F("iv", "double"),
        },
    ),
    4: F("requestMode", "enum"),
}
FEED_RESPONSE: dict[int, F] = {
    1: F("type", "enum"),
    2: F("feeds", "map", map_value=_FEED),
    3: F("currentTs", "int64"),
}
_TYPE_NAMES = {0: "initial_feed", 1: "live_feed", 2: "market_info"}


def decode_feed_response(buf: bytes) -> dict[str, Any]:
    """Decode one binary ``FeedResponse`` frame into the camelCase dict that
    :func:`parse_upstox_v3_feed` reads. proto3 omits zero values, so a missing ``type`` means
    ``initial_feed`` and a missing price means 0 / not sent."""
    doc = decode(buf, FEED_RESPONSE)
    doc["type"] = _TYPE_NAMES.get(int(doc.get("type", 0)), "unknown")
    return doc


# --------------------------------------------------------------------------- daily login

UPSTOX_LOGIN_URL = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


class UpstoxAuthError(RuntimeError):
    pass


def upstox_login_url(client_id: str, redirect_uri: str, state: str) -> str:
    q = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{UPSTOX_LOGIN_URL}?{q}"


def extract_upstox_code(pasted: str, expected_state: str | None = None) -> str:
    """Accept the full redirect URL (``...?code=...&state=...``) or the bare code."""
    text = pasted.strip()
    if "code=" not in text:
        if not text or any(c.isspace() for c in text):
            raise UpstoxAuthError("paste the redirect URL or the code")
        return text
    params = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
    if expected_state is not None and params.get("state", [""])[0] != expected_state:
        raise UpstoxAuthError("state mismatch in redirect URL (stale or foreign login)")
    code = params.get("code", [""])[0]
    if not code:
        raise UpstoxAuthError("no code in redirect URL")
    return code


FormPoster = Callable[[str, bytes, Mapping[str, str]], Mapping[str, Any]]


def _post_form(url: str, body: bytes, headers: Mapping[str, str]) -> Mapping[str, Any]:
    req = urllib.request.Request(
        url, data=body, headers={"User-Agent": USER_AGENT, **headers}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data: Mapping[str, Any] = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # error bodies are JSON too
        data = json.loads(exc.read().decode("utf-8") or "{}")
    return data


def exchange_upstox_code(
    client_id: str,
    secret: Secret,
    redirect_uri: str,
    code: str,
    *,
    poster: FormPoster = _post_form,
) -> Secret:
    """Swap the browser code for today's access token (valid until about 03:30 next day)."""
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": secret.reveal(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    try:
        resp = poster(UPSTOX_TOKEN_URL, body, headers)
    except OSError as exc:
        raise UpstoxAuthError(f"token request failed: {type(exc).__name__}") from None
    token = resp.get("access_token")
    if not isinstance(token, str) or not token:
        errors = resp.get("errors")
        first = errors[0] if isinstance(errors, list) and errors else {}
        msg = first.get("message") if isinstance(first, Mapping) else None
        raise UpstoxAuthError(f"login rejected: {msg or resp.get('status') or 'no token'}")
    return Secret("UPSTOX_ACCESS_TOKEN", token)


# --------------------------------------------------------------------------- live feed

_URL_QUERY = re.compile(r"(wss?://[^\s?]+)\?\S*")


def _scrub(text: str) -> str:
    """Drop query strings from URLs: the authorised socket URL carries a one-time code."""
    return _URL_QUERY.sub(r"\1?<redacted>", text)[:300]


def _default_ws_factory(url: str, **callbacks: Any) -> Any:
    try:
        import websocket
    except ImportError as exc:  # pragma: no cover - core dependency
        raise RuntimeError("websocket-client is not installed; run `uv sync`") from exc
    return websocket.WebSocketApp(url, **callbacks)


def _binary_opcode() -> int:
    try:
        import websocket

        return int(websocket.ABNF.OPCODE_BINARY)
    except ImportError:  # pragma: no cover
        return 0x2


class UpstoxFeed:
    """Upstox V3 market-data feed over our own WebSocket client (TLS verified).

    Flow: ``GET /v3/feed/market-data-feed/authorize`` (Bearer token) returns a single-use
    ``wss://`` URL; we connect to it, send subscription requests as **binary** JSON frames, and
    decode protobuf ``FeedResponse`` frames. Every instrument is subscribed in one mode so the
    single-category limits apply (``full``: 2,000 keys; Upstox Plus ``full_d30``: 50).

    Upstox's own Python SDK disables certificate checks for this socket; we do not.
    """

    supports_depth = True
    depth_counts_separately = False
    CHUNK = 500  # keys per subscription request (Upstox documents no per-request maximum)

    def __init__(
        self,
        *,
        token: Secret | None = None,
        mode: str = MODE_FULL,
        max_symbols: int | None = None,
        clock: Clock | None = None,
        http_get: Callable[[str, Mapping[str, str]], bytes] = _default_http_get,
        ws_factory: Callable[..., Any] = _default_ws_factory,
        ssl_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
        reconnect: bool = True,
    ) -> None:
        if mode not in {MODE_LTPC, MODE_FULL, MODE_FULL_D30}:
            raise ValueError(f"Unsupported Upstox V3 mode: {mode!r}")
        self.mode = mode
        self.name = "upstox_d30" if mode == MODE_FULL_D30 else "upstox"
        default_cap = {MODE_FULL_D30: 50, MODE_FULL: 2000, MODE_LTPC: 5000}[mode]
        self.max_symbols = max_symbols or default_cap
        self._token = token
        self.clock: Clock = clock or SystemClock()
        self._http_get = http_get
        self._ws_factory = ws_factory
        self._ssl_context_factory = ssl_context_factory
        self._reconnect = reconnect
        self.sink: Sink | None = None
        self.symbols: set[str] = set()
        self.depth: set[str] = set()
        self._lock = threading.Lock()
        self._ws: Any = None
        self._connected = False
        self._stopping = False
        self._backoff_s = 1.0
        self.connects = 0
        self.frames = 0
        self.decode_errors = 0

    # ------------------------------------------------------------------ helpers

    def _status(self, kind: StatusKind, detail: str) -> None:
        if self.sink is not None:
            self.sink(FeedStatus(kind, self.clock.now_ns(), _scrub(detail)))

    def authorize_ws_url(self) -> str:
        """Request the single-use authorised WebSocket URI from Upstox V3 (never logged)."""
        tok = self._token or resolve_upstox_token()
        raw_body = self._http_get(
            UPSTOX_AUTH_URL,
            {"Accept": "application/json", "Authorization": f"Bearer {tok.reveal()}"},
        )
        doc = json.loads(raw_body.decode("utf-8"))
        data = doc.get("data") if isinstance(doc, Mapping) else None
        uri = None
        if isinstance(data, Mapping):
            uri = data.get("authorizedRedirectUri") or data.get("authorized_redirect_uri")
        if not uri or not isinstance(uri, str) or not uri.startswith("wss://"):
            raise RuntimeError("Upstox authorize response has no wss:// authorizedRedirectUri")
        return uri

    def _send(self, symbols: Sequence[str], method: str) -> None:
        ws = self._ws
        if ws is None or not self._connected or not symbols:
            return
        by_mode: dict[str, list[str]] = {}
        for key in sorted(symbols):
            by_mode.setdefault(self.mode_for(key), []).append(key)
        for mode, keys in by_mode.items():
            for i in range(0, len(keys), self.CHUNK):
                frame = build_upstox_sub_frame(
                    keys[i : i + self.CHUNK],
                    mode=mode,
                    method=method,
                    guid=f"qe-{method}-{mode}-{self.connects}-{i}",
                )
                ws.send(frame, opcode=_binary_opcode())

    def mode_for(self, key: str) -> str:
        """Indices have no order book: in ``full_d30`` Upstox answers them with an empty
        ``fullFeed`` (seen live, 11 Nov 2026), so they are subscribed in ``full`` instead."""
        if self.mode == MODE_FULL_D30 and "_INDEX|" in key:
            return MODE_FULL
        return self.mode

    # ------------------------------------------------------------------ connection

    def _connect(self) -> None:
        try:
            url = self.authorize_ws_url()
        except Exception as exc:
            kind = StatusKind.AUTH_FAILED if "401" in str(exc) else StatusKind.ERROR
            self._status(kind, f"authorize failed: {type(exc).__name__}: {exc}")
            self._schedule_reconnect()
            return
        ws = self._ws_factory(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws
        self.connects += 1
        threading.Thread(
            target=ws.run_forever,
            kwargs={
                "sslopt": {"context": self._ssl_context_factory()},
                "ping_interval": 20,
                "ping_timeout": 10,
            },
            name="upstox-ws",
            daemon=True,
        ).start()

    def _schedule_reconnect(self) -> None:
        if self._stopping or not self._reconnect:
            return
        delay = self._backoff_s
        self._backoff_s = min(30.0, self._backoff_s * 2)
        timer = threading.Timer(delay, self._connect)
        timer.daemon = True
        timer.start()

    def _on_open(self, ws: Any) -> None:
        if ws is not self._ws:
            return
        self._connected = True
        self._backoff_s = 1.0
        self._status(StatusKind.CONNECTED, f"{self.name} socket open ({self.mode})")
        with self._lock:
            keys = list(self.symbols)
        self._send(keys, "sub")

    def _on_message(self, ws: Any, message: Any) -> None:
        recv = self.clock.now_ns()
        if self.sink is None:
            return
        try:
            if isinstance(message, (bytes, bytearray)):
                doc = decode_feed_response(bytes(message))
            else:
                doc = json.loads(message)
        except (ValueError, ProtoDecodeError) as exc:
            self.decode_errors += 1
            if self.decode_errors in (1, 100, 10_000):
                self._status(StatusKind.ERROR, f"undecodable frame: {type(exc).__name__}")
            return
        self.frames += 1
        if isinstance(doc, Mapping) and doc.get("status") == "error":
            self._status(StatusKind.ERROR, json.dumps(doc)[:300])
            return
        for ev in parse_upstox_v3_feed(doc, recv) if isinstance(doc, Mapping) else ():
            self.sink(ev)

    def _on_error(self, ws: Any, error: Any) -> None:
        if ws is self._ws:
            self._status(StatusKind.ERROR, f"{type(error).__name__}: {error}")

    def _on_close(self, ws: Any, code: Any = None, reason: Any = None) -> None:
        if ws is not self._ws:
            return  # an old socket closing after a restart
        self._connected = False
        self._status(StatusKind.DISCONNECTED, f"closed ({code}) {reason or ''}")
        self._schedule_reconnect()

    # ------------------------------------------------------------------ adapter API

    def start(self, sink: Sink) -> None:
        self.sink = sink
        self._stopping = False
        self._connect()

    def subscribe(self, symbols: Sequence[str], *, depth: bool) -> None:
        with self._lock:
            new = [s for s in symbols if s not in self.symbols]
            combined = self.symbols | set(symbols)
            if len(combined) > self.max_symbols:
                self._status(
                    StatusKind.LIMIT,
                    f"{self.name} cap {self.max_symbols} exceeded ({len(combined)})",
                )
                return
            self.symbols = combined
            if depth:
                self.depth |= set(symbols)
        self._send(new, "sub")

    def unsubscribe(self, symbols: Sequence[str], *, depth: bool) -> None:
        with self._lock:
            gone = [s for s in symbols if s in self.symbols]
            self.symbols -= set(symbols)
            self.depth -= set(symbols)
        self._send(gone, "unsub")

    def ingest_frame(self, payload: Mapping[str, Any] | bytes | str) -> int:
        """Parse and push one frame (protobuf bytes, JSON text or dict) — used by tests."""
        if self.sink is None:
            return 0
        now_ns = self.clock.now_ns()
        if isinstance(payload, (bytes, bytearray)):
            doc: Mapping[str, Any] = decode_feed_response(bytes(payload))
        elif isinstance(payload, str):
            doc = json.loads(payload)
        else:
            doc = payload
        events = parse_upstox_v3_feed(doc, now_ns)
        for ev in events:
            self.sink(ev)
        return len(events)

    def restart(self) -> None:
        old = self._ws
        self._ws = None
        self._connected = False
        if old is not None:
            try:
                old.close()
            except Exception as exc:  # best effort
                log.warning("upstox close failed: %s", type(exc).__name__)
        self._status(StatusKind.DISCONNECTED, f"{self.name} restart")
        self._backoff_s = 1.0
        if not self._stopping:
            self._connect()

    def stop(self) -> None:
        self._stopping = True
        old, self._ws = self._ws, None
        self._connected = False
        if old is not None:
            try:
                old.close()
            except Exception as exc:
                log.warning("upstox close failed: %s", type(exc).__name__)
        self._status(StatusKind.DISCONNECTED, f"{self.name} stop")
