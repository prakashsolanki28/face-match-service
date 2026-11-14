"""Outbound webhook so the host project learns about attendance in real time."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.request
from typing import Any, Dict

from . import __version__

log = logging.getLogger(__name__)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def send(url: str, secret: str, event: str, data: Dict[str, Any]) -> None:
    """POST {"event": ..., "data": ...}. Failures are logged, never raised."""
    body = json.dumps({"event": event, "data": data}).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"face-match/{__version__}",
    }
    if secret:
        headers["X-Signature-256"] = sign(secret, body)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            log.info("Webhook %s delivered (%s)", event, response.status)
    except Exception as exc:  # noqa: BLE001 - delivery is best effort
        log.warning("Webhook %s to %s failed: %s", event, url, exc)
