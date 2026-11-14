"""Minimal HTTP GET (stdlib only). Generic User-Agent; never sends owner identity."""

from __future__ import annotations

import http.client
import urllib.request

USER_AGENT = "quant-engine/0.1 (personal research; low frequency)"


def http_get_bytes(url: str, timeout: float = 60.0) -> bytes:
    """GET ``url``. A connection cut off mid-body (``IncompleteRead`` and other
    ``http.client`` protocol errors) is raised as ``ConnectionError``, an ``OSError``, so
    callers that already handle network errors handle it too."""
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body: bytes = resp.read()
    except http.client.HTTPException as exc:
        raise ConnectionError(f"{type(exc).__name__} from {url}") from exc
    return body
