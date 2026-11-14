"""SQLite persistence for people, face embeddings and attendance events.

Only embeddings are stored, never the raw photos.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
    id          TEXT PRIMARY KEY,
    external_id TEXT UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS face_embeddings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    embedding  BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embeddings_person ON face_embeddings(person_id);

-- Person details are copied onto each event so history survives deleting a person.
CREATE TABLE IF NOT EXISTS attendance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   TEXT NOT NULL,
    external_id TEXT,
    name        TEXT NOT NULL,
    similarity  REAL NOT NULL,
    device_id   TEXT,
    ts          REAL NOT NULL,
    day         TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attendance_day ON attendance(day);
CREATE INDEX IF NOT EXISTS idx_attendance_person_ts ON attendance(person_id, ts);

-- Scoped API keys. Only a SHA-256 hash of each token is stored.
CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    prefix       TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    scopes       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

-- Transactional outbox: webhook events are stored with the attendance event and
-- delivered (with retries) by a background worker.
CREATE TABLE IF NOT EXISTS webhook_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    event           TEXT NOT NULL,
    person_id       TEXT,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error      TEXT,
    created_at      REAL NOT NULL,
    delivered_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON webhook_outbox(status, next_attempt_at);
"""

SCHEMA_VERSION = 2

Row = Dict[str, Any]


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # Biometric data: owner-only. SQLite gives the -wal/-shm files the same mode.
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- persons -------------------------------------------------------------

    def create_person(
        self,
        name: str,
        external_id: Optional[str],
        embeddings: Iterable[np.ndarray],
        created_at: str,
    ) -> Row:
        person_id = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO persons (id, external_id, name, created_at) VALUES (?, ?, ?, ?)",
                (person_id, external_id, name, created_at),
            )
            self._insert_embeddings(person_id, embeddings, created_at)
        return self.get_person(person_id)  # type: ignore[return-value]

    def add_embeddings(self, person_id: str, embeddings: Iterable[np.ndarray], created_at: str) -> None:
        with self._lock, self._conn:
            self._insert_embeddings(person_id, embeddings, created_at)

    def replace_embeddings(self, person_id: str, embeddings: Iterable[np.ndarray], created_at: str) -> None:
        """Swap all of a person's embeddings in one transaction."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM face_embeddings WHERE person_id = ?", (person_id,))
            self._insert_embeddings(person_id, embeddings, created_at)

    def _insert_embeddings(self, person_id: str, embeddings: Iterable[np.ndarray], created_at: str) -> None:
        self._conn.executemany(
            "INSERT INTO face_embeddings (person_id, embedding, created_at) VALUES (?, ?, ?)",
            [(person_id, np.asarray(e, dtype=np.float32).tobytes(), created_at) for e in embeddings],
        )

    _PERSON_SELECT = """
        SELECT p.id, p.external_id, p.name, p.created_at, COUNT(e.id) AS face_count
        FROM persons p LEFT JOIN face_embeddings e ON e.person_id = p.id
    """

    def get_person(self, person_id: str) -> Optional[Row]:
        with self._lock:
            row = self._conn.execute(
                self._PERSON_SELECT + " WHERE p.id = ? GROUP BY p.id", (person_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_person_by_external_id(self, external_id: str) -> Optional[Row]:
        with self._lock:
            row = self._conn.execute(
                self._PERSON_SELECT + " WHERE p.external_id = ? GROUP BY p.id", (external_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_persons(self) -> List[Row]:
        with self._lock:
            rows = self._conn.execute(
                self._PERSON_SELECT + " GROUP BY p.id ORDER BY p.name COLLATE NOCASE"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_person(self, person_id: str, erase_history: bool = False) -> bool:
        """Delete a person and their embeddings. With erase_history, also delete their
        attendance events and queued webhooks (right to erasure)."""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
            if erase_history:
                self._conn.execute("DELETE FROM attendance WHERE person_id = ?", (person_id,))
                self._conn.execute("DELETE FROM webhook_outbox WHERE person_id = ?", (person_id,))
        return cur.rowcount > 0

    def all_embeddings(self) -> List[Tuple[str, np.ndarray]]:
        with self._lock:
            rows = self._conn.execute("SELECT person_id, embedding FROM face_embeddings").fetchall()
        return [(r["person_id"], np.frombuffer(r["embedding"], dtype=np.float32)) for r in rows]

    def stats(self) -> Row:
        with self._lock:
            persons = self._conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
            faces = self._conn.execute("SELECT COUNT(*) FROM face_embeddings").fetchone()[0]
        return {"persons": persons, "embeddings": faces}

    # ---- attendance ----------------------------------------------------------

    def mark_attendance(
        self,
        person: Row,
        similarity: float,
        device_id: Optional[str],
        ts: float,
        day: str,
        created_at: str,
        cooldown_seconds: int,
        enqueue_webhook: bool = False,
    ) -> Tuple[bool, Row]:
        """Insert an event unless the person was seen within the cooldown window.

        Returns (created, event). When not created, `event` is the most recent one.
        The check and insert happen under one lock, so concurrent frames of the
        same person cannot produce duplicate events. With enqueue_webhook, the
        webhook is queued in the same transaction, so no event is ever lost.
        """
        with self._lock, self._conn:
            last = self._conn.execute(
                "SELECT * FROM attendance WHERE person_id = ? ORDER BY ts DESC LIMIT 1",
                (person["id"],),
            ).fetchone()
            if last is not None and ts - last["ts"] < cooldown_seconds:
                return False, dict(last)
            cur = self._conn.execute(
                """INSERT INTO attendance
                   (person_id, external_id, name, similarity, device_id, ts, day, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    person["id"],
                    person["external_id"],
                    person["name"],
                    similarity,
                    device_id,
                    ts,
                    day,
                    created_at,
                ),
            )
            event = dict(
                self._conn.execute("SELECT * FROM attendance WHERE id = ?", (cur.lastrowid,)).fetchone()
            )
            if enqueue_webhook:
                self._enqueue("attendance.marked", event["person_id"], attendance_payload(event))
        return True, event

    def list_attendance(self, day: Optional[str], person_id: Optional[str], limit: int) -> List[Row]:
        clauses, params = [], []  # type: List[str], List[Any]
        if day:
            clauses.append("day = ?")
            params.append(day)
        if person_id:
            clauses.append("person_id = ?")
            params.append(person_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM attendance {where} ORDER BY ts DESC LIMIT ?", (*params, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def attendance_summary(self, day: str) -> List[Row]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT person_id,
                          MAX(external_id) AS external_id,
                          MAX(name)        AS name,
                          MIN(ts)          AS first_ts,
                          MAX(ts)          AS last_ts,
                          COUNT(*)         AS events
                   FROM attendance WHERE day = ?
                   GROUP BY person_id ORDER BY first_ts""",
                (day,),
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_attendance_before(self, ts: float) -> int:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM attendance WHERE ts < ?", (ts,)).rowcount

    # ---- API keys --------------------------------------------------------------

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_api_key(self, name: str, scopes: Iterable[str], created_at: str) -> Tuple[Row, str]:
        """Create a key. Returns (row, token); the token is not stored and can't be shown again."""
        token = "fm_" + secrets.token_urlsafe(32)
        key_id = uuid.uuid4().hex[:12]
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO api_keys (id, name, prefix, key_hash, scopes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (key_id, name, token[:10], self.hash_token(token), " ".join(sorted(set(scopes))), created_at),
            )
        return self.get_api_key(key_id), token  # type: ignore[return-value]

    def get_api_key(self, key_id: str) -> Optional[Row]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return dict(row) if row else None

    def find_api_key(self, token: str) -> Optional[Row]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (self.hash_token(token),)
            ).fetchone()
        return dict(row) if row else None

    def list_api_keys(self) -> List[Row]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM api_keys ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def revoke_api_key(self, key_id: str, revoked_at: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (revoked_at, key_id)
            )
        return cur.rowcount > 0

    def touch_api_key(self, key_id: str, used_at: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (used_at, key_id))

    def has_active_api_keys(self) -> bool:
        with self._lock:
            return (
                self._conn.execute("SELECT 1 FROM api_keys WHERE revoked_at IS NULL LIMIT 1").fetchone()
                is not None
            )

    # ---- webhook outbox ----------------------------------------------------------

    def _enqueue(self, event: str, person_id: Optional[str], payload: Row) -> None:
        now = time.time()
        self._conn.execute(
            """INSERT INTO webhook_outbox (event_id, event, person_id, payload, next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uuid.uuid4().hex, event, person_id, json.dumps(payload), now, now),
        )

    def due_webhooks(self, now: float, limit: int = 20) -> List[Row]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM webhook_outbox WHERE status = 'pending' AND next_attempt_at <= ?
                   ORDER BY id LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def webhook_delivered(self, row_id: int, attempts: int, ts: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE webhook_outbox SET status='delivered', attempts=?, delivered_at=?, last_error=NULL"
                " WHERE id=?",
                (attempts, ts, row_id),
            )

    def webhook_attempt_failed(
        self, row_id: int, attempts: int, error: str, next_at: Optional[float]
    ) -> None:
        """Record a failed attempt; next_at=None gives up (status 'failed')."""
        with self._lock, self._conn:
            if next_at is None:
                self._conn.execute(
                    "UPDATE webhook_outbox SET status='failed', attempts=?, last_error=? WHERE id=?",
                    (attempts, error[:500], row_id),
                )
            else:
                self._conn.execute(
                    "UPDATE webhook_outbox SET attempts=?, last_error=?, next_attempt_at=? WHERE id=?",
                    (attempts, error[:500], next_at, row_id),
                )

    def list_webhooks(self, status: Optional[str], limit: int) -> List[Row]:
        where, params = ("WHERE status = ?", [status]) if status else ("", [])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM webhook_outbox {where} ORDER BY id DESC LIMIT ?", (*params, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def retry_webhook(self, row_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE webhook_outbox SET status='pending', next_attempt_at=?"
                " WHERE id=? AND status != 'delivered'",
                (time.time(), row_id),
            )
        return cur.rowcount > 0

    def webhook_counts(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM webhook_outbox GROUP BY status"
            ).fetchall()
        counts = {"pending": 0, "delivered": 0, "failed": 0}
        counts.update({r[0]: r[1] for r in rows})
        return counts

    def purge_webhooks_before(self, ts: float) -> int:
        with self._lock, self._conn:
            return self._conn.execute(
                "DELETE FROM webhook_outbox WHERE status != 'pending' AND created_at < ?", (ts,)
            ).rowcount

    # ---- backups -------------------------------------------------------------------

    def backup_to(self, target: Path) -> Path:
        """Consistent online copy of the database (safe while the service is running)."""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        dest = sqlite3.connect(str(tmp))
        try:
            with self._lock:
                self._conn.backup(dest)
        finally:
            dest.close()
        os.chmod(tmp, 0o600)
        tmp.replace(target)
        return target


def attendance_payload(row: Row) -> Row:
    """Public JSON shape of an attendance event (API responses and webhooks)."""
    return {
        "id": row["id"],
        "person_id": row["person_id"],
        "external_id": row["external_id"],
        "name": row["name"],
        "similarity": row["similarity"],
        "device_id": row["device_id"],
        "timestamp": row["created_at"],
        "day": row["day"],
    }
