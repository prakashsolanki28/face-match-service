"""Authentication (admin key and scoped device keys), rate limiting and overload protection."""

from __future__ import annotations

import hmac
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Dict, FrozenSet, Iterator, Optional, Tuple

from .storage import Database

# What a key may do. "admin" implies everything.
SCOPES: Dict[str, str] = {
    "admin": "everything, including managing keys and webhooks",
    "enroll": "create, update and delete people and their photos",
    "recognize": "detect and recognise faces (no records written)",
    "attendance": "mark attendance from camera frames (kiosks)",
    "read": "list people, attendance and summaries",
    "metrics": "read /metrics",
}


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: FrozenSet[str]
    key_id: Optional[str] = None  # set for device keys
    kind: str = "admin_key"  # admin_key | key | open

    def allows(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes

    @property
    def label(self) -> str:
        """Who did it, for the audit log."""
        if self.kind == "key":
            return f"key:{self.name}#{self.key_id}"
        return self.kind


OPEN_ACCESS = Principal(name="open (no API key configured)", scopes=frozenset({"admin"}), kind="open")


class KeyStore:
    """Resolves tokens to principals. Lookups are cached briefly; revocation clears the cache."""

    _CACHE_SECONDS = 30.0
    _TOUCH_SECONDS = 60.0

    def __init__(self, db: Database, admin_key: str) -> None:
        self._db = db
        self._admin_key = admin_key
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[Optional[Principal], float]] = {}
        self._touched: Dict[str, float] = {}
        # (enabled, monotonic time of the check), None = never checked. Never use 0 as "long ago":
        # time.monotonic() can start near 0 when the process starts.
        self._enabled: Optional[Tuple[bool, float]] = None

    def auth_enabled(self) -> bool:
        if self._admin_key:
            return True
        now = time.monotonic()
        if self._enabled is None or now - self._enabled[1] > 5.0:
            self._enabled = (self._db.has_active_api_keys(), now)
        return self._enabled[0]

    def authenticate(self, token: str) -> Optional[Principal]:
        if self._admin_key and hmac.compare_digest(token.encode(), self._admin_key.encode()):
            return Principal(name="admin (API_KEY)", scopes=frozenset({"admin"}), kind="admin_key")
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(token)
        if cached and now - cached[1] < self._CACHE_SECONDS:
            principal = cached[0]
        else:
            row = self._db.find_api_key(token)
            principal = (
                Principal(
                    name=row["name"], scopes=frozenset(row["scopes"].split()), key_id=row["id"], kind="key"
                )
                if row
                else None
            )
            with self._lock:
                if len(self._cache) > 10_000:
                    self._cache.clear()
                self._cache[token] = (principal, now)
        last_touch = self._touched.get(principal.key_id) if principal and principal.key_id else None
        if principal and principal.key_id and (last_touch is None or now - last_touch > self._TOUCH_SECONDS):
            self._touched[principal.key_id] = now
            self._db.touch_api_key(principal.key_id, datetime.now(UTC).isoformat(timespec="seconds"))
        return principal

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
        self._enabled = None


class RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after:.1f}s")
        self.retry_after = max(1, int(retry_after + 0.999))


class RateLimiter:
    """Token buckets: `per_minute` requests per client key, `failures_per_minute` failed logins per IP."""

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
