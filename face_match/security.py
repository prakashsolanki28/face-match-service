"""Authentication (one API key), rate limiting and overload protection."""

from __future__ import annotations

import hmac
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Tuple


class ApiKeyAuth:
    """The single API key (the API_KEY setting) gives full access to the API.

    With no key configured the API is open, which `face-match serve` only allows on localhost
    (or with ALLOW_NO_AUTH=true).
    """

    def __init__(self, api_key: str) -> None:
        self._key = api_key.encode()

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def check(self, token: Optional[str]) -> bool:
        """Constant-time comparison, so response timing doesn't reveal the key."""
        return bool(self._key) and bool(token) and hmac.compare_digest(token.encode(), self._key)


class RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after:.1f}s")
        self.retry_after = max(1, int(retry_after + 0.999))


class RateLimiter:
    """Token buckets per client IP: `per_minute` requests and `failures_per_minute` wrong keys."""

    def __init__(self, per_minute: int, failures_per_minute: int) -> None:
        self._rate = per_minute
        self._failure_rate = failures_per_minute
        self._lock = threading.Lock()
        self._buckets: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, last refill time)
        self._calls = 0

    def _take(self, key: str, capacity: int) -> None:
        if capacity <= 0:
            return
        refill_per_second = capacity / 60.0
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(capacity), now))
            tokens = min(float(capacity), tokens + (now - last) * refill_per_second)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                raise RateLimited((1.0 - tokens) / refill_per_second)
            self._buckets[key] = (tokens - 1.0, now)
            self._calls += 1
            if self._calls % 5000 == 0:  # forget idle clients
                cutoff = now - 600
                self._buckets = {k: v for k, v in self._buckets.items() if v[1] > cutoff}

    def check(self, client: str) -> None:
        self._take("req:" + client, self._rate)

    def auth_failure(self, ip: str) -> None:
        self._take("fail:" + ip, self._failure_rate)


class Busy(Exception):
    """All image-processing slots stayed busy for the whole wait."""


class ImageGate:
    """Caps concurrent image processing so a burst queues briefly instead of exhausting memory/CPU."""

    def __init__(self, slots: int, wait_seconds: float = 10.0) -> None:
        self._semaphore = threading.BoundedSemaphore(max(1, slots))
        self._wait = wait_seconds

    @contextmanager
    def slot(self) -> Iterator[None]:
        if not self._semaphore.acquire(timeout=self._wait):
            raise Busy()
        try:
            yield
        finally:
            self._semaphore.release()
