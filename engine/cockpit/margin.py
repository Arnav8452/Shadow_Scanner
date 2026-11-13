"""Margin needed to put on a paper trade's legs in the real market (owner request, 13 Nov 2026).

- **Long options only**: the premium paid is the whole requirement (no SPAN); computed in the
  paper accounts, no network call.
- **Anything with a short option or a future**: Upstox's margin calculator
  (``POST /v2/charges/margin``) for the legs as one basket, product ``D`` (NRML/carry). It
  returns SPAN + exposure and ``final_margin`` "after margin benefit", so a hedged spread or a
  married future is priced with its hedge. Verified on 13 Nov 2026: a 25250/25000 bull put
  needed Rs 42,985 vs Rs 1,70,103 for the naked short put; a long future + ATM put Rs 64,654 vs
  Rs 1,86,644 for the future alone.

Requests run on a background thread (never the engine loop), one per new position, with the
same generic User-Agent as every other call; the token is read from the environment and never
logged. With no token, no network or a non-Upstox feed (simulation), the position keeps the
worst-case-loss proxy and says why.
"""

from __future__ import annotations

import json
import logging
import queue
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from engine.core.env import get_secret
from engine.core.net import USER_AGENT
from engine.core.rules import IST

log = logging.getLogger(__name__)

URL = "https://api.upstox.com/v2/charges/margin"
MAX_LEGS = 20  # the endpoint's limit per request

Leg = tuple[str, int, str]  # (instrument key, quantity, BUY/SELL)


def _token() -> str | None:
    for name in ("UPSTOX_ACCESS_TOKEN", "UPSTOX_ANALYTICS_TOKEN"):
        try:
            v = get_secret(name).reveal()
        except Exception:
            continue
        if v:
            return v
    return None


def fetch_margin(
    legs: Sequence[Leg], *, product: str = "D", timeout: float = 10.0
) -> dict[str, Any]:
    """One basket quote. Returns {"final", "required", "source"} or {"error": reason}."""
    if not legs:
        return {"error": "no legs"}
    if any(not str(k).startswith(("NSE_FO|", "BSE_FO|", "NSE_EQ|", "BSE_EQ|")) for k, _, _ in legs):
        return {"error": "not an Upstox instrument key (simulated feed?)"}
    if len(legs) > MAX_LEGS:
        return {"error": f"more than {MAX_LEGS} legs"}
    tok = _token()
    if not tok:
        return {"error": "no Upstox token in .env"}
    body = {
        "instruments": [
            {"instrument_key": k, "quantity": int(q), "product": product, "transaction_type": s}
            for k, q, s in legs
        ]
    }
    req = urllib.request.Request(
        URL,
        method="POST",
        data=json.dumps(body).encode(),
        headers={
            "accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Bearer " + tok,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(
            req, timeout=timeout, context=ssl.create_default_context()
        ) as r:
            doc = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return {"error": f"Upstox margin API HTTP {exc.code}"}
    except Exception as exc:  # network down, timeout: the trade still runs on paper
        return {"error": f"Upstox margin API unreachable ({type(exc).__name__})"}
    data = doc.get("data") or {}
    if doc.get("status") != "success" or "final_margin" not in data:
        return {"error": "Upstox margin API returned no margin"}
    return {
        "final": round(float(data["final_margin"]), 2),
        "required": round(float(data.get("required_margin") or data["final_margin"]), 2),
        "source": "Upstox margin calculator, NRML, hedge benefit included",
    }


class MarginQuoter:
    """Background worker: ``request(legs, on_done)`` returns at once; ``on_done(result)`` runs
    on the worker thread with the ``fetch_margin`` result plus the time it was quoted."""

    def __init__(self, fetch: Callable[[Sequence[Leg]], dict[str, Any]] = fetch_margin) -> None:
        self._fetch = fetch
        self._q: queue.Queue[tuple[list[Leg], Callable[[dict[str, Any]], None]]] = queue.Queue(
            maxsize=200
        )
        self._thread: threading.Thread | None = None

    def request(self, legs: Sequence[Leg], on_done: Callable[[dict[str, Any]], None]) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="margin-quotes", daemon=True)
            self._thread.start()
        try:
            self._q.put_nowait((list(legs), on_done))
        except queue.Full:
            on_done({"error": "margin queue full"})

    def _run(self) -> None:
        while True:
            legs, done = self._q.get()
            try:
                res = self._fetch(legs)
            except Exception as exc:  # never let one bad quote stop the worker
                res = {"error": type(exc).__name__}
            res["at"] = datetime.now(IST).strftime("%H:%M")
            try:
                done(res)
            except Exception:
                log.exception("margin callback failed")


__all__ = ["MAX_LEGS", "MarginQuoter", "fetch_margin"]
