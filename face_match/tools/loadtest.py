#!/usr/bin/env python3
"""Measure how much traffic a FaceMatch server handles, for capacity planning.

    face-match loadtest --image frame.jpg --concurrency 4 --duration 30 --api-key "$KEY"

Sends the same image from N parallel clients for a fixed time, like N kiosks sending frames
without pause, and reports throughput, latency percentiles and status codes. Use a realistic
frame from your camera (e.g. 640x480 with one face). Run it against a test server, not production.

Rule of thumb: a kiosk sends about 1.5 frames per second, so a server sustaining R requests per
second supports roughly R / 1.5 kiosks running at the same time.
Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from pathlib import Path
from typing import List, Optional, Sequence


def _body(image: bytes) -> tuple:
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="frame.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n".encode()
        + image
        + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def run(url: str, api_key: str, image: bytes, concurrency: int, duration: float, endpoint: str) -> dict:
    body, content_type = _body(image)
    target = f"{url.rstrip('/')}/api/v1/{endpoint}"
    latencies: List[float] = []
    statuses: Counter = Counter()
    lock = threading.Lock()
    stop_at = time.perf_counter() + duration

    def worker() -> None:
        while time.perf_counter() < stop_at:
            request = urllib.request.Request(target, data=body, method="POST")
            request.add_header("Content-Type", content_type)
            if api_key:
                request.add_header("X-API-Key", api_key)
            start = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    response.read()
                    status = response.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            except Exception:  # noqa: BLE001 - connection errors count as failures
                status = 0
            elapsed = time.perf_counter() - start
            with lock:
                statuses[status] += 1
                if status == 200:
                    latencies.append(elapsed)

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.perf_counter() - started

    latencies.sort()

    def pct(p: float) -> float:
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))] * 1000 if latencies else 0.0

    ok = statuses.get(200, 0)
    return {
        "concurrency": concurrency,
        "requests": sum(statuses.values()),
        "ok": ok,
        "throughput": ok / wall if wall else 0.0,
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "mean_ms": statistics.mean(latencies) * 1000 if latencies else 0.0,
        "statuses": dict(statuses),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="face-match loadtest", description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", type=Path, required=True, help="A JPEG frame to send (ideally one face).")
    parser.add_argument("--url", default=os.environ.get("FACE_SERVICE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=os.environ.get("FACE_API_KEY") or os.environ.get("API_KEY", ""))
    parser.add_argument("--endpoint", choices=["recognize", "detect"], default="recognize")
    parser.add_argument("--concurrency", default="1,4,8", help="Comma-separated client counts to try.")
    parser.add_argument("--duration", type=float, default=15.0, help="Seconds per concurrency level.")
    args = parser.parse_args(argv)

    image = args.image.read_bytes()
    print(
        f"POST {args.url}/api/v1/{args.endpoint}  image {len(image) // 1024} KB  "
        f"{args.duration:g}s per level\n"
    )
    print(" clients   req/s   p50 ms   p95 ms   p99 ms   ~kiosks   statuses")
    worst = 0
    for level in [int(c) for c in args.concurrency.split(",")]:
        result = run(args.url, args.api_key, image, level, args.duration, args.endpoint)
        kiosks = int(result["throughput"] / 1.5)
        print(
            f" {level:>7} {result['throughput']:>7.1f} {result['p50_ms']:>8.1f} {result['p95_ms']:>8.1f}"
            f" {result['p99_ms']:>8.1f} {kiosks:>9}   {result['statuses']}"
        )
        if any(code != 200 for code in result["statuses"]):
            worst = 1
    return worst


if __name__ == "__main__":
    sys.exit(main())
