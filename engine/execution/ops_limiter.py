"""Orders-Per-Second (OPS) Rate Limiter (SPEC §7, CLAUDE.md Rule 2).

Hard-caps order dispatch strictly below the SEBI retail-algo threshold (< 10 OPS; default 5 OPS).
"""

from __future__ import annotations

from collections import deque


class OPSRateLimiter:
    """Sliding 1-second window rate limiter."""

    def __init__(self, max_orders_per_second: int = 5) -> None:
        if not 1 <= max_orders_per_second < 10:
            raise ValueError(
                f"max_orders_per_second must be in [1, 9] (SEBI < 10), got {max_orders_per_second}"
            )
        self.max_ops = max_orders_per_second
        self._timestamps_ns: deque[int] = deque()

    def allow(self, now_ns: int) -> bool:
        """Return True and record the order if within the 1-second cap; False otherwise."""
        cutoff = now_ns - 1_000_000_000
        while self._timestamps_ns and self._timestamps_ns[0] <= cutoff:
            self._timestamps_ns.popleft()
        if len(self._timestamps_ns) >= self.max_ops:
            return False
        self._timestamps_ns.append(now_ns)
        return True

    def current_rate(self, now_ns: int) -> int:
        cutoff = now_ns - 1_000_000_000
        while self._timestamps_ns and self._timestamps_ns[0] <= cutoff:
            self._timestamps_ns.popleft()
        return len(self._timestamps_ns)
