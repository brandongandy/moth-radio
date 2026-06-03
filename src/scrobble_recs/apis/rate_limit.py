from __future__ import annotations

import time
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        rate_limit_per_second: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_limit_per_second <= 0:
            raise ValueError("rate_limit_per_second must be greater than zero")
        self._minimum_interval = 1.0 / rate_limit_per_second
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request_at: float | None = None

    def wait(self) -> None:
        now = self._monotonic()
        if self._last_request_at is None:
            self._last_request_at = now
            return

        elapsed = now - self._last_request_at
        wait_seconds = self._minimum_interval - elapsed
        if wait_seconds > 0:
            self._sleep(wait_seconds)
            self._last_request_at = now + wait_seconds
            return

        self._last_request_at = now
