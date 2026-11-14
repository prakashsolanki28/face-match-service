"""Prometheus metrics (no extra dependency), request IDs and structured logging."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import threading
import time
from typing import Callable, Dict, List, Tuple

try:
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore[assignment]

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

Labels = Tuple[Tuple[str, str], ...]

# Seconds; covers fast camera frames (~10 ms) to large photos (~1 s).
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_HELP = {
    "facematch_http_requests_total": ("counter", "HTTP requests by route and status."),
    "facematch_http_request_duration_seconds": ("histogram", "HTTP request latency."),
    "facematch_faces_total": ("counter", "Faces processed, by endpoint and outcome."),
    "facematch_attendance_marked_total": ("counter", "New attendance events."),
    "facematch_webhook_deliveries_total": ("counter", "Webhook delivery attempts by outcome."),
    "facematch_rate_limited_total": ("counter", "Requests rejected by rate limiting or overload."),
    "facematch_maintenance_runs_total": ("counter", "Backup and retention jobs by kind and outcome."),
}


def _labels(**labels: str) -> Labels:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


def _fmt(labels: Labels) -> str:
    if not labels:
        return ""
    escaped = (f'{k}="{v}"'.replace("\n", " ") for k, v in labels)
    return "{" + ",".join(escaped) + "}"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[Tuple[str, Labels], float] = {}
        self._hist: Dict[Tuple[str, Labels], List[float]] = {}  # bucket counts + [sum, count]
        self._gauges: List[Callable[[], Dict[str, float]]] = []

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, _labels(**labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, _labels(**labels))
        with self._lock:
            data = self._hist.setdefault(key, [0.0] * (len(_BUCKETS) + 2))
            for i, bound in enumerate(_BUCKETS):
                if value <= bound:
                    data[i] += 1
            data[-2] += value
            data[-1] += 1

    def gauge_source(self, fn: Callable[[], Dict[str, float]]) -> None:
        """Register a callback returning {metric_name_with_labels: value} at scrape time."""
        self._gauges.append(fn)

    def render(self) -> str:
        lines: List[str] = []
        with self._lock:
            counters = dict(self._counters)
            hists = {k: list(v) for k, v in self._hist.items()}
        seen = set()
        for (name, labels), value in sorted(counters.items()):
            if name not in seen:
                kind, help_text = _HELP.get(name, ("counter", name))
                lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
                seen.add(name)
            lines.append(f"{name}{_fmt(labels)} {value:g}")
        for (name, labels), data in sorted(hists.items()):
            if name not in seen:
                lines += [f"# HELP {name} {_HELP.get(name, ('', name))[1]}", f"# TYPE {name} histogram"]
                seen.add(name)
            for bound, count in zip(_BUCKETS, data, strict=False):  # data has 2 extra slots
                lines.append(f"{name}_bucket{_fmt(labels + (('le', f'{bound:g}'),))} {count:g}")
            lines.append(f"{name}_bucket{_fmt(labels + (('le', '+Inf'),))} {data[-1]:g}")
            lines.append(f"{name}_sum{_fmt(labels)} {data[-2]:.6f}")
            lines.append(f"{name}_count{_fmt(labels)} {data[-1]:g}")
        for source in self._gauges:
            try:
                values = source()
            except Exception:  # noqa: BLE001 - a failing gauge must not break the scrape
                continue
            for name, value in values.items():
                base = name.split("{", 1)[0]
                if base not in seen:
                    lines.append(f"# TYPE {base} gauge")
                    seen.add(base)
                lines.append(f"{name} {value:g}")
        return "\n".join(lines) + "\n"


def process_gauges() -> Dict[str, float]:
    if resource is None:
        return {}
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = peak if sys.platform == "darwin" else peak * 1024  # macOS reports bytes, Linux KiB
    values = {"process_peak_resident_memory_bytes": float(peak_bytes)}
    try:  # current RSS on Linux
        with open("/proc/self/statm") as fh:
            pages = int(fh.read().split()[1])
        values["process_resident_memory_bytes"] = float(pages * resource.getpagesize())
    except OSError:
        pass
    return values


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    _SKIP = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in self._SKIP and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str, fmt: str) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
