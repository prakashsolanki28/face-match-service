"""Background data-protection jobs: scheduled backups and retention purges."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import List, Optional

from .config import Settings
from .storage import Database

log = logging.getLogger(__name__)

_DELIVERED_WEBHOOK_RETENTION_DAYS = 30


def list_backups(backup_dir: Path) -> List[Path]:
    return sorted(backup_dir.glob("facematch-*.db"))


def make_backup(db: Database, backup_dir: Path, keep: int) -> Path:
    """Write a timestamped online backup and delete the oldest beyond `keep`."""
    target = backup_dir / f"facematch-{time.strftime('%Y%m%d-%H%M%S')}.db"
    db.backup_to(target)
    backups = list_backups(backup_dir)
    for old in backups[: max(0, len(backups) - max(1, keep))]:
        old.unlink(missing_ok=True)
    return target


def apply_retention(db: Database, settings: Settings, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    removed = {"attendance": 0, "webhooks": 0}
    if settings.attendance_retention_days > 0:
        removed["attendance"] = db.purge_attendance_before(now - settings.attendance_retention_days * 86400)
    removed["webhooks"] = db.purge_webhooks_before(now - _DELIVERED_WEBHOOK_RETENTION_DAYS * 86400)
    return removed


class MaintenanceWorker:
    """Checks every few minutes whether a backup or retention purge is due."""

    def __init__(self, db: Database, settings: Settings, metrics=None, check_every: float = 300.0) -> None:
        self.db = db
        self.settings = settings
        self.metrics = metrics
        self.check_every = check_every
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_retention = 0.0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="maintenance", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _count(self, kind: str, outcome: str) -> None:
        if self.metrics:
            self.metrics.inc("facematch_maintenance_runs_total", kind=kind, outcome=outcome)

    def run_once(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        interval = self.settings.backup_interval_hours * 3600
        if interval > 0:
            backups = list_backups(self.settings.backup_dir)
            last = backups[-1].stat().st_mtime if backups else 0.0
            if now - last >= interval:
                try:
                    path = make_backup(self.db, self.settings.backup_dir, self.settings.backup_keep)
                    log.info("Backup written: %s", path)
                    self._count("backup", "ok")
                except Exception:
                    log.exception("Backup failed")
                    self._count("backup", "error")
        if now - self._last_retention >= 3600:
            self._last_retention = now
            try:
                removed = apply_retention(self.db, self.settings, now)
                if any(removed.values()):
                    log.info("Retention purge removed %s", removed)
                self._count("retention", "ok")
            except Exception:
                log.exception("Retention purge failed")
                self._count("retention", "error")

    def _run(self) -> None:
        while not self._stop.wait(5.0 if self._last_retention == 0 else self.check_every):
            self.run_once()
