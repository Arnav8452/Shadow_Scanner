"""Orders-Per-Second (OPS) Rate Limiter (SPEC §7, CLAUDE.md Rule 2).

Hard-caps order dispatch strictly below the SEBI retail-algo threshold (< 10 OPS; default 5 OPS).
"""

from __future__ import annotations

from collections import deque
from datetime import date
from pathlib import Path


class OPSRateLimiter:
    """Sliding 1-second window rate limiter."""

    def __init__(self, max_orders_per_second: int = 5, *, sebi_threshold: int = 10) -> None:
        """``sebi_threshold`` comes from ``config/rules/sebi_algo_rules.csv`` via
        :meth:`from_rules`; the default only guards direct construction in tests."""
        if not 1 <= max_orders_per_second < sebi_threshold:
            raise ValueError(
                f"max_orders_per_second must be in [1, {sebi_threshold - 1}] "
                f"(SEBI < {sebi_threshold}), got {max_orders_per_second}"
            )
        self.max_ops = max_orders_per_second
        self._timestamps_ns: deque[int] = deque()

    @classmethod
    def from_rules(cls, root: Path, on: date, max_orders_per_second: int) -> OPSRateLimiter:
        from engine.core.config import sebi_ops_threshold

        return cls(max_orders_per_second, sebi_threshold=sebi_ops_threshold(root, on))

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
