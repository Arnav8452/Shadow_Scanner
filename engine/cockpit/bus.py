"""Engine <-> gateway link over an authenticated local socket (stdlib only).

The engine (P2/P4) owns all state. It *publishes* a whole snapshot a few times a second and
takes *commands* (paper orders, replay controls) from clients. Clients must know the shared key
in ``data/state/cockpit.key``; ``multiprocessing.connection`` checks it with an HMAC challenge
before any message is unpickled. Only loopback addresses are used.

Nothing here can block the engine loop: ``publish`` stores a reference and wakes the sender
threads; slow or dead clients are dropped by their own threads.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from multiprocessing.connection import Client, Listener
from typing import Any

log = logging.getLogger(__name__)

Message = tuple[str, Any]  # ("snap", (seq, data)) | ("ack", data) | ("cmd", data)


@dataclass
class _Peer:
    cid: int
    conn: Any  # multiprocessing Connection (PipeConnection on some platforms)
    outbox: deque[Message] = field(default_factory=lambda: deque(maxlen=100))
    sent_seq: int = -1
    alive: bool = True


class EnginePublisher:
    """Server side, inside the engine process."""

    def __init__(self, host: str, port: int, authkey: bytes) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the engine publisher only listens on loopback")
        self.address = (host, port)
        self.authkey = authkey
        self.commands: queue.Queue[tuple[int, dict[str, Any]]] = queue.Queue(maxsize=1000)
        self._cond = threading.Condition()
        self._latest: tuple[int, Any] = (0, None)
        self._peers: dict[int, _Peer] = {}
        self._next_id = 0
        self._closed = threading.Event()
        self._listener: Listener | None = None
        self._accept_thread: threading.Thread | None = None

    @property
    def clients(self) -> int:
        with self._cond:
            return sum(1 for p in self._peers.values() if p.alive)

    def start(self) -> None:
        self._listener = Listener(self.address, authkey=self.authkey)
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="cockpit-accept", daemon=True
        )
        self._accept_thread.start()
        log.info("cockpit publisher on %s:%d", *self.address)

    def publish(self, snapshot: Any) -> None:
        with self._cond:
            self._latest = (self._latest[0] + 1, snapshot)
            self._cond.notify_all()

    def reply(self, cid: int, data: dict[str, Any]) -> None:
        with self._cond:
            peer = self._peers.get(cid)
            if peer is not None:
                peer.outbox.append(("ack", data))
                self._cond.notify_all()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._cond:
            self._cond.notify_all()
            peers = list(self._peers.values())
        for p in peers:
            with contextlib.suppress(OSError):
                p.conn.close()
        if self._listener is not None:
            # accept() does not wake on close() on every platform: poke it with one connection.
            with contextlib.suppress(OSError):
                Client(self.address, authkey=self.authkey).close()
            with contextlib.suppress(OSError):
                self._listener.close()

    # ------------------------------------------------------------------ threads

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._closed.is_set():
            try:
                conn = self._listener.accept()
            except Exception as exc:  # wrong key, aborted handshake, listener closed
                if self._closed.is_set():
                    return
                log.warning("cockpit client refused: %s", type(exc).__name__)
                continue
            if self._closed.is_set():
                conn.close()
                return
            with self._cond:
                self._next_id += 1
                peer = _Peer(self._next_id, conn)
                self._peers[peer.cid] = peer
            threading.Thread(
                target=self._send_loop, args=(peer,), name=f"cockpit-tx-{peer.cid}", daemon=True
            ).start()
            threading.Thread(
                target=self._recv_loop, args=(peer,), name=f"cockpit-rx-{peer.cid}", daemon=True
            ).start()
            log.info("cockpit client %d connected", peer.cid)

    def _drop(self, peer: _Peer) -> None:
        with self._cond:
            peer.alive = False
            self._peers.pop(peer.cid, None)
        with contextlib.suppress(OSError):
            peer.conn.close()

    def _send_loop(self, peer: _Peer) -> None:
        while not self._closed.is_set() and peer.alive:
            with self._cond:
                while (
                    not self._closed.is_set()
                    and peer.alive
                    and not peer.outbox
                    and self._latest[0] == peer.sent_seq
                ):
                    self._cond.wait(timeout=1.0)
                outbox = list(peer.outbox)
                peer.outbox.clear()
                seq, snap = self._latest
            try:
                for msg in outbox:
                    peer.conn.send(msg)
                if seq != peer.sent_seq and snap is not None:
                    peer.conn.send(("snap", (seq, snap)))
                    peer.sent_seq = seq
            except (OSError, EOFError, ValueError):
                self._drop(peer)
                return

    def _recv_loop(self, peer: _Peer) -> None:
        while not self._closed.is_set() and peer.alive:
            try:
                msg = peer.conn.recv()
            except (OSError, EOFError, ValueError):
                self._drop(peer)
                return
            if (
                isinstance(msg, tuple)
                and len(msg) == 2
                and msg[0] == "cmd"
                and isinstance(msg[1], dict)
            ):
                try:
                    self.commands.put_nowait((peer.cid, msg[1]))
                except queue.Full:
                    self.reply(peer.cid, {"id": msg[1].get("id"), "ok": False, "text": "busy"})


class EngineLink:
    """Client side, inside the gateway. Reconnects by itself; never raises to the caller."""

    def __init__(self, name: str, host: str, port: int, authkey: bytes) -> None:
        self.name = name
        self.address = (host, port)
        self.authkey = authkey
        self.connected = False
        self.latest_seq = 0
        self.latest: Any = None
        self.acks: deque[dict[str, Any]] = deque(maxlen=200)
        self._conn: Any = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"link-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        conn = self._conn
        if conn is not None:
            with contextlib.suppress(OSError):
                conn.close()

    def send(self, cmd: dict[str, Any]) -> bool:
        conn = self._conn
        if conn is None or not self.connected:
            return False
        try:
            with self._send_lock:
                conn.send(("cmd", cmd))
            return True
        except (OSError, ValueError):
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn = Client(self.address, authkey=self.authkey)
            except Exception:  # engine not running yet, or wrong key
                self._stop.wait(1.0)
                continue
            self._conn = conn
            self.connected = True
            try:
                while not self._stop.is_set():
                    kind, payload = conn.recv()
                    if kind == "snap":
                        self.latest_seq, self.latest = payload
                    elif kind == "ack" and isinstance(payload, dict):
                        self.acks.append(payload)
            except (OSError, EOFError, ValueError, TypeError):
                pass
            finally:
                self.connected = False
                self._conn = None
                with contextlib.suppress(OSError):
                    conn.close()
            self._stop.wait(0.5)
