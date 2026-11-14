"""SQLite persistence for people, face embeddings and attendance events.

Only embeddings are stored, never the raw photos.
"""

from __future__ import annotations

import sqlite3
import threading
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
"""

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

    def delete_person(self, person_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
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
    ) -> Tuple[bool, Row]:
        """Insert an event unless the person was seen within the cooldown window.

        Returns (created, event). When not created, `event` is the most recent one.
        The check and insert happen under one lock, so concurrent frames of the
        same person cannot produce duplicate events.
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
            event = self._conn.execute("SELECT * FROM attendance WHERE id = ?", (cur.lastrowid,)).fetchone()
        return True, dict(event)

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
