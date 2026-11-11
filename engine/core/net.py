"""Minimal HTTP GET (stdlib only). Generic User-Agent; never sends owner identity."""

from __future__ import annotations

import urllib.request

USER_AGENT = "quant-engine/0.1 (personal research; low frequency)"


def http_get_bytes(url: str, timeout: float = 60.0) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body: bytes = resp.read()
    return body
