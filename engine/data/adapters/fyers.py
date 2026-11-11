"""Fyers API v3 market-data adapter (Data Socket) and the daily login helpers.

Facts checked on 10 Nov 2026 (Fyers docs "Plans & Limits", "Data Socket", SDK fyers-apiv3
3.2.1 source):

* Standard (free) plan: **100 symbols per user across all connections**; Prime
  (₹499 + GST / month, chosen by the owner on 10 Nov 2026): 5,000. Over the cap the connection
  is not established.
* ``SymbolUpdate`` (``sf`` scrip / ``if`` index messages) and ``DepthUpdate`` (``dp``, five
  levels) are separate subscriptions. Whether a depth subscription uses a second slot is not
  documented — we assume it does (``depth_counts_separately``).
* The SDK removes ``OI`` from scrip messages before calling us. :func:`with_open_interest`
  restores it from the SDK's raw per-symbol dict (tested against the 3.2.1 layout; confirm on
  the first live day via the QC report's ``oi_present``). OI is passed through unscaled.
  Depth messages carry no exchange timestamp.
* The SDK pins old libraries (requests 2.31, aiohttp 3.9.3, asyncio 3.4.3 backport,
  setuptools 68). It is an optional extra (``uv sync --extra fyers``) and imported lazily.
* Updating symbols: Fyers recommends unsubscribing the old list and subscribing the new one.

**Not yet run against the live feed** (no API keys). Treat the first live session as a test.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from engine.core.clock import Clock, SystemClock
from engine.core.env import Secret
from engine.data.adapters.base import Depth, FeedEvent, FeedStatus, Level, Quote, Sink, StatusKind

log = logging.getLogger(__name__)

API_BASE = "https://api-t1.fyers.in/api/v3"
QUOTE_CHANNEL = 11
DEPTH_CHANNEL = 12


class FyersAuthError(RuntimeError):
    pass


# --------------------------------------------------------------------------- login


def login_url(app_id: str, redirect_uri: str, state: str) -> str:
    q = urllib.parse.urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{API_BASE}/generate-authcode?{q}"


def app_id_hash(app_id: str, secret: Secret) -> str:
    return hashlib.sha256(f"{app_id}:{secret.reveal()}".encode()).hexdigest()


def extract_auth_code(pasted: str, expected_state: str | None = None) -> str:
    """Accept either the full redirect URL or the bare auth code."""
    text = pasted.strip()
    if "auth_code=" not in text:
        if not text or any(c.isspace() for c in text):
            raise FyersAuthError("paste the redirect URL or the auth_code")
        return text
    params = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
    if expected_state is not None and params.get("state", [""])[0] != expected_state:
        raise FyersAuthError("state mismatch in redirect URL (possible stale or foreign login)")
    code = params.get("auth_code", [""])[0]
    if not code:
        raise FyersAuthError("no auth_code in redirect URL")
    return code


Poster = Callable[[str, bytes], Mapping[str, Any]]


def _post_json(url: str, body: bytes) -> Mapping[str, Any]:
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data: Mapping[str, Any] = json.loads(resp.read().decode("utf-8"))
    return data


def exchange_auth_code(
    app_id: str, secret: Secret, auth_code: str, *, poster: Poster = _post_json
) -> Secret:
    """Swap the browser auth code for today's access token (POST /validate-authcode)."""
    body = json.dumps(
        {
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash(app_id, secret),
            "code": auth_code,
        }
    ).encode()
    try:
        resp = poster(f"{API_BASE}/validate-authcode", body)
    except OSError as exc:
        raise FyersAuthError(f"token request failed: {type(exc).__name__}") from None
    token = resp.get("access_token")
    if resp.get("s") != "ok" or not isinstance(token, str) or not token:
        # Never include the response body: it may echo credentials.
        raise FyersAuthError(f"login rejected (code {resp.get('code')}: {resp.get('message')})")
    return Secret("FYERS_ACCESS_TOKEN", token)


# --------------------------------------------------------------------------- normalisation


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return None if f is None else int(f)


def _band(v: Any) -> float | None:
    f = _f(v)
    return f if f else None  # the SDK sends 0 when it has no circuit band


def normalize(msg: Mapping[str, Any], recv_ns: int) -> FeedEvent | None:
    """Translate one SDK message dict into a feed event (None for messages we ignore)."""
    typ = msg.get("type")
    sym = msg.get("symbol")
    if typ == "sf" and isinstance(sym, str):
        return Quote(
            sym,
            recv_ns,
            exch_ts=_i(msg.get("exch_feed_time")),
            ltt=_i(msg.get("last_traded_time")),
            ltp=_f(msg.get("ltp")),
            ltq=_i(msg.get("last_traded_qty")),
            atp=_f(msg.get("avg_trade_price")),
            volume=_i(msg.get("vol_traded_today")),
            oi=_i(msg.get("OI")),  # restored by with_open_interest (SDK 3.2.1 pops it)
            tbq=_i(msg.get("tot_buy_qty")),
            tsq=_i(msg.get("tot_sell_qty")),
            bid=_f(msg.get("bid_price")),
            bid_qty=_i(msg.get("bid_size")),
            ask=_f(msg.get("ask_price")),
            ask_qty=_i(msg.get("ask_size")),
            open=_f(msg.get("open_price")),
            high=_f(msg.get("high_price")),
            low=_f(msg.get("low_price")),
            prev_close=_f(msg.get("prev_close_price")),
            upper_ckt=_band(msg.get("upper_ckt")),
            lower_ckt=_band(msg.get("lower_ckt")),
        )
    if typ == "if" and isinstance(sym, str):
        return Quote(
            sym,
            recv_ns,
            exch_ts=_i(msg.get("exch_feed_time")),
            ltp=_f(msg.get("ltp")),
            open=_f(msg.get("open_price")),
            high=_f(msg.get("high_price")),
            low=_f(msg.get("low_price")),
            prev_close=_f(msg.get("prev_close_price")),
        )
    if typ == "dp" and isinstance(sym, str):

        def side(name: str) -> tuple[Level, ...]:
            levels: list[Level] = []
            for i in range(1, 6):
                px, qty = _f(msg.get(f"{name}_price{i}")), _i(msg.get(f"{name}_size{i}"))
                if px is None or qty is None:
                    continue
                levels.append((px, qty, _i(msg.get(f"{name}_order{i}"))))
            return tuple(levels)

        return Depth(sym, recv_ns, side("bid"), side("ask"))
    code = msg.get("code")
    text = str(msg.get("message", ""))[:200]
    if typ == "cn":
        ok = msg.get("s") == "ok"
        return FeedStatus(StatusKind.CONNECTED if ok else StatusKind.AUTH_FAILED, recv_ns, text)
    if code in (-99, 51001) or "limit" in text.lower():
        return FeedStatus(StatusKind.LIMIT, recv_ns, f"{code}: {text}")
    if typ == "sub":
        kind = StatusKind.SUBSCRIBED if msg.get("s") == "ok" else StatusKind.ERROR
        return FeedStatus(kind, recv_ns, text)
    if msg.get("s") == "error":
        return FeedStatus(StatusKind.ERROR, recv_ns, f"{code}: {text}")
    return None


# --------------------------------------------------------------------------- feed

SocketFactory = Callable[..., Any]


def with_open_interest(base: Any) -> Any:
    """Subclass the SDK socket so open interest survives.

    In fyers-apiv3 3.2.1 the private ``FyersDataSocket.__response_output(data, data_type)``
    receives the raw per-symbol dict, which still holds ``"OI"`` as an unscaled integer. It
    builds a new dict for the callback and pops ``"OI"`` from it. We remember the raw OI for
    the duration of that call and put it back in ``On_message``. If the SDK layout changes,
    the base class is returned unchanged, OI stays missing, and the day's QC report flags it.
    """
    original = getattr(base, "__dict__", {}).get("_FyersDataSocket__response_output")
    if original is None or not callable(getattr(base, "On_message", None)):
        log.warning("fyers SDK layout not recognised; open interest cannot be recovered")
        return base
    shape: Callable[..., Any] = original

    class OIPreservingDataSocket(base):  # type: ignore[misc]
        # The SDK class is a singleton (``cls._instance``). A slot of our own means we never
        # inherit an un-hooked base instance, and each feed (re)start gets a fresh socket.
        _instance: Any = None
        _qe_pending_oi: Any = None

        def _FyersDataSocket__response_output(self, data: Any, data_type: Any) -> Any:
            raw = data.get("OI") if data_type == "scrips" and isinstance(data, dict) else None
            self._qe_pending_oi = raw
            try:
                return shape(self, data, data_type)
            finally:
                self._qe_pending_oi = None

        def On_message(self, message: Any) -> None:  # noqa: N802 (SDK method name)
            oi = self._qe_pending_oi
            if oi is not None and isinstance(message, dict) and "OI" not in message:
                message = {**message, "OI": oi}
            super().On_message(message)

    OIPreservingDataSocket.__name__ = f"OIPreserving{getattr(base, '__name__', 'Socket')}"
    return OIPreservingDataSocket


def _sdk_socket_factory(**kwargs: Any) -> Any:
    try:
        from fyers_apiv3.FyersWebsocket import data_ws
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise RuntimeError(
            "fyers-apiv3 is not installed; run `uv sync --extra fyers` (see docs/RUNBOOK.md)"
        ) from exc
    return with_open_interest(data_ws.FyersDataSocket)(**kwargs)


class FyersFeed:
    name = "fyers"
    supports_depth = True

    def __init__(
        self,
        app_id: str,
        access_token: Secret,
        *,
        max_symbols: int,
        log_dir: Path,
        depth_counts_separately: bool = True,
        clock: Clock | None = None,
        socket_factory: SocketFactory = _sdk_socket_factory,
    ) -> None:
        if not app_id or not access_token:
            raise FyersAuthError("FYERS_APP_ID and today's FYERS_ACCESS_TOKEN are required")
        self.app_id = app_id
        self._token = access_token
        self.max_symbols = max_symbols
        self.depth_counts_separately = depth_counts_separately
        self.log_dir = log_dir
        self.clock = clock or SystemClock()
        self._factory = socket_factory
        self._sink: Sink | None = None
        self._sock: Any = None
        self._lock = threading.Lock()
        self.quote_symbols: list[str] = []
        self.depth_symbols: list[str] = []

    # SDK callbacks run on SDK threads; they only stamp time, translate and hand over.
    def _on_message(self, msg: Any) -> None:
        recv = self.clock.now_ns()
        if self._sink is None or not isinstance(msg, Mapping):
            return
        ev = normalize(msg, recv)
        if ev is not None:
            self._sink(ev)

    def _on_error(self, msg: Any) -> None:
        if self._sink is not None:
            ev = normalize(msg, self.clock.now_ns()) if isinstance(msg, Mapping) else None
            self._sink(ev or FeedStatus(StatusKind.ERROR, self.clock.now_ns(), str(msg)[:200]))

    def _on_close(self, msg: Any) -> None:
        if self._sink is not None:
            self._sink(FeedStatus(StatusKind.DISCONNECTED, self.clock.now_ns(), str(msg)[:200]))

    def _on_open(self) -> None:
        with self._lock:
            sock, quotes, depth = self._sock, list(self.quote_symbols), list(self.depth_symbols)
        if self._sink is not None:
            self._sink(FeedStatus(StatusKind.CONNECTED, self.clock.now_ns(), "fyers socket open"))
        if sock is None:
            return
        if quotes:
            sock.subscribe(symbols=quotes, data_type="SymbolUpdate", channel=QUOTE_CHANNEL)
        if depth:
            sock.subscribe(symbols=depth, data_type="DepthUpdate", channel=DEPTH_CHANNEL)
        sock.keep_running()

    def _new_socket(self) -> Any:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        return self._factory(
            access_token=f"{self.app_id}:{self._token.reveal()}",
            log_path=str(self.log_dir),
            litemode=False,
            write_to_file=False,
            reconnect=True,
            reconnect_retry=50,
            on_connect=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
        )

    def start(self, sink: Sink) -> None:
        self._sink = sink
        with self._lock:
            self._sock = self._new_socket()
            sock = self._sock
        threading.Thread(target=sock.connect, name="fyers-connect", daemon=True).start()

    def _resubscribe(self, data_type: str, channel: int, old: list[str], new: list[str]) -> None:
        sock = self._sock
        if sock is None or not sock.is_connected():
            return  # _on_open subscribes the full lists
        if old:
            sock.unsubscribe(symbols=old, data_type=data_type, channel=channel)
        if new:
            sock.subscribe(symbols=new, data_type=data_type, channel=channel)

    def subscribe(self, symbols: Sequence[str], *, depth: bool) -> None:
        with self._lock:
            old_q, old_d = list(self.quote_symbols), list(self.depth_symbols)
            self.quote_symbols = list(dict.fromkeys([*old_q, *symbols]))
            if depth:
                self.depth_symbols = list(dict.fromkeys([*old_d, *symbols]))
        self._resubscribe("SymbolUpdate", QUOTE_CHANNEL, old_q, self.quote_symbols)
        if depth:
            self._resubscribe("DepthUpdate", DEPTH_CHANNEL, old_d, self.depth_symbols)

    def unsubscribe(self, symbols: Sequence[str], *, depth: bool) -> None:
        drop = set(symbols)
        with self._lock:
            old_q, old_d = list(self.quote_symbols), list(self.depth_symbols)
            self.quote_symbols = [s for s in old_q if s not in drop]
            self.depth_symbols = [s for s in old_d if s not in drop]
        self._resubscribe("SymbolUpdate", QUOTE_CHANNEL, old_q, self.quote_symbols)
        self._resubscribe("DepthUpdate", DEPTH_CHANNEL, old_d, self.depth_symbols)

    def restart(self) -> None:
        sink = self._sink
        self.stop()
        if sink is not None:
            self.start(sink)

    def stop(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close_connection()
            except Exception as exc:  # SDK teardown is best-effort
                log.warning("fyers close failed: %s", type(exc).__name__)
