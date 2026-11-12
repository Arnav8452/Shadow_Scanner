"""P3: the cockpit gateway. Serves the page and pushes engine snapshots over a WebSocket.

It holds no trading state: it relays what the live engine (P2) and a replay engine (P4) publish,
forwards paper orders to them, starts and stops replays, and serves read-only Research data.
Restarting it never affects recording or paper trading.

Local-only by design:
- binds to a loopback address;
- the page carries a random token (kept in data/state, so open pages survive a gateway
  restart), required on the WebSocket and every API call;
- the ``Host`` and ``Origin`` headers must be this server, so other websites (or DNS
  rebinding) cannot read data or send orders.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from engine.cockpit import research
from engine.cockpit.bus import EngineLink
from engine.cockpit.config import CockpitConfig, auth_key, page_token

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static" / "cockpit.html"
SPEEDS = (0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALLOWED_CMDS = {
    "buy", "close", "flatten", "take_signal", "pause", "resume", "speed", "ping",
    "search", "watch", "unwatch", "condor_close",
}  # fmt: skip


@dataclass(eq=False)
class Client:
    ws: web.WebSocketResponse
    source: str = "live"
    symbol: str | None = None
    chain: tuple[str, str] | None = None  # (underlying, expiry ISO or "" for the nearest)
    sent: dict[str, Any] = field(default_factory=dict)


class Gateway:
    def __init__(self, ccfg: CockpitConfig, root: Path, *, ledger_file: Path) -> None:
        self.cfg = ccfg
        self.root = root
        self.ledger_file = ledger_file
        # Kept in data/state (gitignored) so an open page keeps working across gateway restarts.
        self.token = page_token(ccfg.key_file.with_name("cockpit.page_token"))
        key = auth_key(ccfg.key_file)
        self.links = {
            "live": EngineLink("live", ccfg.host, ccfg.engine_port, key),
            "replay": EngineLink("replay", ccfg.host, ccfg.replay_port, key),
        }
        self.clients: set[Client] = set()
        self.replay_proc: subprocess.Popen[bytes] | None = None
        self.replay_meta: dict[str, Any] = {}
        self._cache: dict[str, tuple[int, str, dict[str, Any], dict[str, Any]]] = {}
        self._cmd_seq = 0
        self._screener_cache: tuple[str, dict[str, Any]] | None = None
        port = ccfg.gateway_port
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}

    # ------------------------------------------------------------------ app

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._guard])
        app.router.add_get("/", self.index)
        app.router.add_get("/ws", self.ws)
        app.router.add_get("/api/research/{name}", self.api)
        app.on_startup.append(self._on_startup)  # type: ignore[arg-type]
        app.on_cleanup.append(self._on_cleanup)  # type: ignore[arg-type]
        return app

    @web.middleware
    async def _guard(
        self, request: web.Request, handler: Callable[[web.Request], Any]
    ) -> web.StreamResponse:
        if request.host not in self.allowed_hosts:
            return web.Response(status=421, text="wrong host")
        origin = request.headers.get("Origin")
        if origin is not None and origin not in self.allowed_origins:
            return web.Response(status=403, text="cross-origin request refused")
        resp: web.StreamResponse = await handler(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    def _token_ok(self, value: str | None) -> bool:
        return value is not None and secrets.compare_digest(value, self.token)

    async def index(self, request: web.Request) -> web.Response:
        html = STATIC.read_text(encoding="utf-8").replace("__COCKPIT_TOKEN__", self.token)
        return web.Response(text=html, content_type="text/html")

    async def _on_startup(self, app: web.Application) -> None:
        for link in self.links.values():
            link.start()
        app["pump"] = asyncio.create_task(self._pump())

    async def _on_cleanup(self, app: web.Application) -> None:
        app["pump"].cancel()
        for link in self.links.values():
            link.stop()
        self._stop_replay_process()

    # ------------------------------------------------------------------ research API

    async def api(self, request: web.Request) -> web.Response:
        if not self._token_ok(request.headers.get("X-Cockpit-Token")):
            return web.Response(status=403, text="token required")
        name = request.match_info["name"]
        mined = self.root / "data" / "private" / "mined"
        jobs: dict[str, Callable[[], dict[str, Any]]] = {
            "ledger": lambda: research.signal_ledger_stats(self.ledger_file),
            "study": lambda: research.study_stats(self.cfg.journal_dir / "signal_study.jsonl"),
            "screener": lambda: self._screener(),
            "context": lambda: research.context_stats(
                self.cfg.journal_dir, request.query.get("horizon", "30m")
            ),
            "paper": lambda: research.paper_history(self.cfg.journal_dir),
            "iv": lambda: research.iv_series(
                self.root / "data" / "parquet" / "eod" / "iv_history.parquet"
            ),
            "qc": lambda: research.qc_reports(self.root / "reports" / "qc"),
            "flows": lambda: research.flows(self.root / "data" / "raw"),
            "equity": lambda: research.equity(self.root / "data"),
            "swing": lambda: research.swing(self.root / "data", day=request.query.get("day", "")),
            "event_arb": lambda: research.event_arb(self.root),
            "event_arb_report": lambda: research.event_arb_report(
                self.root, request.query.get("name", "")
            ),
            "strategies": lambda: research.strategies(self.root),
            "mining": lambda: research.mining_reports(mined),
            "mining_report": lambda: research.mining_report(mined, request.query.get("name", "")),
        }
        job = jobs.get(name)
        if job is None:
            return web.Response(status=404, text="unknown")
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, job)
        except (ValueError, FileNotFoundError) as exc:
            return web.json_response({"error": str(exc)}, status=404)
        return web.json_response(data, dumps=lambda o: json.dumps(o, default=str))

    # ------------------------------------------------------------------ websocket

    async def ws(self, request: web.Request) -> web.WebSocketResponse:
        if not self._token_ok(request.query.get("token")):
            raise web.HTTPForbidden(text="token required")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
        await ws.prepare(request)
        client = Client(ws)
        self.clients.add(client)
        await self._send_status(client)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    await self._on_client(client, data)
        finally:
            self.clients.discard(client)
        return ws

    async def _on_client(self, client: Client, data: dict[str, Any]) -> None:
        kind = data.get("type")
        if kind == "select":
            client.symbol = str(data.get("symbol") or "") or None
            client.sent.pop("book", None)
        elif kind == "chain":
            und = str(data.get("underlying") or "")
            client.chain = (und, str(data.get("expiry") or "")) if und else None
            client.sent.pop("chain", None)
        elif kind == "source" and data.get("value") in self.links:
            client.source = str(data["value"])
            client.sent.clear()
        elif kind == "cmd":
            cmd = data.get("cmd")
            if not isinstance(cmd, dict) or cmd.get("cmd") not in _ALLOWED_CMDS:
                await client.ws.send_json({"type": "ack", "ok": False, "text": "unknown command"})
                return
            self._cmd_seq += 1
            cmd = {
                k: cmd[k]
                for k in ("cmd", "symbol", "lots", "stop_pct", "account", "speed", "q")
                if k in cmd
            }
            cmd["id"] = f"g{self._cmd_seq}"
            link = self.links[client.source]
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, link.send, cmd)
            if not ok:
                await client.ws.send_json(
                    {"type": "ack", "ok": False, "text": f"{client.source} engine not connected"}
                )
        elif kind == "replay_start":
            text = self._start_replay(data)
            await client.ws.send_json(
                {"type": "ack", "ok": text.startswith("started"), "text": text}
            )
        elif kind == "replay_stop":
            self.links["replay"].send({"cmd": "stop", "id": "stop"})
            await asyncio.sleep(1.0)
            self._stop_replay_process()
            await client.ws.send_json({"type": "ack", "ok": True, "text": "replay stopped"})

    def _screener(self) -> dict[str, Any]:
        """Cached per hour: the EOD screener only changes once a day."""
        key = datetime.now(UTC).strftime("%Y-%m-%d %H")
        if self._screener_cache is None or self._screener_cache[0] != key:
            data = research.screener(self.root, self.cfg.journal_dir, self.cfg.events_file)
            self._screener_cache = (key, data)
        return self._screener_cache[1]

    # ------------------------------------------------------------------ replay process

    def _start_replay(self, data: dict[str, Any]) -> str:
        d = str(data.get("date", ""))
        if not _DATE_RE.match(d):
            return "date must be YYYY-MM-DD"
        try:
            date.fromisoformat(d)
            speed = float(data.get("speed", 5.0))
        except ValueError:
            return "bad date or speed"
        if speed not in SPEEDS:
            return f"speed must be one of {SPEEDS}"
        self._stop_replay_process()
        # Forget the previous replay's last state so the page never shows it as the new one.
        link = self.links["replay"]
        link.latest, link.latest_seq = None, 0
        link.acks.clear()
        self._cache.pop("replay", None)
        for c in self.clients:
            if c.source == "replay":
                c.sent.clear()
        cmd = [
            sys.executable,
            str(self.root / "tasks.py"),
            "replay",
            f"DATE={d}",
            f"SPEED={speed:g}",
        ]
        if data.get("sim"):
            cmd.append("SIM=1")
        self.cfg.replay_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.cfg.replay_dir / "replay.log"
        flags = 0
        if sys.platform == "win32":  # replays must not compete with the live engine for CPU
            flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        with log_path.open("ab") as fh:
            self.replay_proc = subprocess.Popen(
                cmd, cwd=self.root, stdout=fh, stderr=subprocess.STDOUT, creationflags=flags
            )
        self.replay_meta = {"date": d, "speed": speed, "sim": bool(data.get("sim"))}
        return f"started replay of {d} at {'max' if speed == 0 else f'{speed:g}x'} speed"

    def _stop_replay_process(self) -> None:
        proc = self.replay_proc
        self.replay_proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ------------------------------------------------------------------ push loop

    def _status(self) -> dict[str, Any]:
        proc = self.replay_proc
        return {
            "type": "status",
            "live": self.links["live"].connected,
            "replay": self.links["replay"].connected,
            "replay_running": proc is not None and proc.poll() is None,
            "replay_exit": None if proc is None else proc.poll(),
            "replay_meta": self.replay_meta,
        }

    async def _send_status(self, client: Client) -> None:
        with contextlib.suppress(ConnectionError):
            await client.ws.send_json(self._status())

    async def _pump(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(0.1)
            tick += 1
            for name, link in self.links.items():
                snap = link.latest
                if snap is None:
                    continue
                cached = self._cache.get(name)
                if cached is None or cached[0] != link.latest_seq:
                    books = snap.get("books", {})
                    chains = snap.get("chains") or {}
                    base = {k: v for k, v in snap.items() if k not in ("books", "chains")}
                    base["type"] = "snap"
                    self._cache[name] = (
                        link.latest_seq,
                        json.dumps(base, default=str, separators=(",", ":")),
                        books,
                        chains,
                    )
            acks: dict[str, list[dict[str, Any]]] = {}
            for name, link in self.links.items():
                while link.acks:
                    acks.setdefault(name, []).append(link.acks.popleft())
            status = json.dumps(self._status()) if tick % 10 == 0 else None
            for client in list(self.clients):
                try:
                    await self._push(client, acks.get(client.source, []), status)
                except (ConnectionError, RuntimeError):
                    self.clients.discard(client)

    async def _push(self, client: Client, acks: list[dict[str, Any]], status: str | None) -> None:
        ws = client.ws
        if ws.closed:
            raise ConnectionError
        cached = self._cache.get(client.source)
        if cached is not None:
            seq, base, books, chains = cached
            if client.sent.get("snap") != seq:
                await ws.send_str(base)
                client.sent["snap"] = seq
            if client.symbol and client.sent.get("book") != seq:
                book = books.get(client.symbol)
                await ws.send_str(
                    json.dumps({"type": "book", "symbol": client.symbol, "book": book})
                )
                client.sent["book"] = seq
            if client.chain is not None:
                key = (chains.get("rev"), client.chain)
                if client.sent.get("chain") != key:
                    und, want = client.chain
                    by_exp = (chains.get("data") or {}).get(und) or {}
                    exps = sorted(by_exp)
                    exp = want if want in by_exp else (exps[0] if exps else "")
                    msg = {"type": "chain", "underlying": und, "expiry": exp,
                           "expiries": exps, "table": by_exp.get(exp)}  # fmt: skip
                    await ws.send_str(json.dumps(msg, default=str, separators=(",", ":")))
                    client.sent["chain"] = key
        for a in acks:
            await ws.send_str(json.dumps({"type": "ack", **a}, default=str))
        if status is not None:
            await ws.send_str(status)


def run_gateway(ccfg: CockpitConfig, root: Path, *, ledger_file: Path, open_browser: bool) -> None:
    gw = Gateway(ccfg, root, ledger_file=ledger_file)
    url = f"http://{ccfg.host}:{ccfg.gateway_port}/"
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"cockpit: {url}  (Ctrl+C to stop; recording and paper trading are not affected)")
    web.run_app(gw.app(), host=ccfg.host, port=ccfg.gateway_port, print=None, access_log=None)
