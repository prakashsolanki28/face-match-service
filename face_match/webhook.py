"""Reliable outbound webhooks: events are queued in the database (transactional outbox)
and a background worker delivers them with exponential backoff until the receiver
answers 2xx or the attempt limit is reached."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from . import __version__
from .storage import Database

log = logging.getLogger(__name__)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_body(event_id: str, event: str, data: Dict[str, Any]) -> bytes:
    return json.dumps({"id": event_id, "event": event, "data": data}).encode()


def post(
    url: str, secret: str, event_id: str, event: str, body: bytes, timeout: float = 5.0
) -> Tuple[bool, str]:
    """One delivery attempt. Returns (delivered, error message)."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"face-match/{__version__}",
        "X-Event-Id": event_id,  # stable across retries: receivers use it to ignore duplicates
        "X-Event-Type": event,
    }
    if secret:
        headers["X-Signature-256"] = sign(secret, body)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - network errors of any kind mean "retry later"
        return False, f"{type(exc).__name__}: {exc}"


class WebhookDispatcher:
    """Background worker that drains the webhook outbox."""

    def __init__(
        self,
        db: Database,
        url: str,
        secret: str,
        max_attempts: int = 20,
        base_delay: float = 5.0,
        max_delay: float = 3600.0,
        poll_interval: float = 2.0,
        on_result=None,
    ) -> None:
        self.db = db
        self.url = url
        self.secret = secret
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.poll_interval = poll_interval
        self._on_result = on_result or (lambda outcome: None)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="webhook-dispatcher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def notify(self) -> None:
        """Deliver now instead of waiting for the next poll (called when an event is queued)."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.deliver_due()
            except Exception:  # pragma: no cover - keep the worker alive on unexpected errors
                log.exception("Webhook dispatcher error")
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def backoff(self, attempts: int) -> float:
        return min(self.max_delay, self.base_delay * (2 ** (attempts - 1)))

    def deliver_due(self, now: Optional[float] = None) -> int:
        """Attempt every due event once. Returns how many were delivered."""
        delivered = 0
        # Claiming (not just reading) makes sure two servers never send the same delivery at once.
        for row in self.db.claim_due_webhooks(time.time() if now is None else now):
            if self._stop.is_set():
                break
            body = build_body(row["event_id"], row["event"], json.loads(row["payload"]))
            ok, info = post(self.url, self.secret, row["event_id"], row["event"], body)
            attempts = row["attempts"] + 1
            if ok:
                self.db.webhook_delivered(row["id"], attempts, time.time())
                delivered += 1
                self._on_result("delivered")
            elif attempts >= self.max_attempts:
                self.db.webhook_attempt_failed(row["id"], attempts, info, None)
                log.error("Webhook %s gave up after %d attempts: %s", row["event_id"], attempts, info)
                self._on_result("failed")
            else:
                retry_at = time.time() + self.backoff(attempts)
                self.db.webhook_attempt_failed(row["id"], attempts, info, retry_at)
                log.warning("Webhook %s attempt %d failed (%s); retrying", row["event_id"], attempts, info)
                self._on_result("retry")
        return delivered
