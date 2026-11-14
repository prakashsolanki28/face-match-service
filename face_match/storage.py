"""Persistence for people, embeddings, attendance, keys, users, sessions, audit and webhooks.

Two backends share one implementation:
  * SQLite (default): a single file in DATA_DIR, ideal for one server.
  * PostgreSQL (DATABASE_URL=postgresql://...): lets several FaceMatch servers share data.
    Requires `pip install "face-match-service[postgres]"`.

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
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

import numpy as np

SCHEMA_VERSION = 3

_TABLES = """
CREATE TABLE IF NOT EXISTS persons (
    id          TEXT PRIMARY KEY,
    external_id TEXT UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS face_embeddings (
    id         {serial},
    person_id  TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    embedding  {blob} NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embeddings_person ON face_embeddings(person_id);

-- Person details are copied onto each event so history survives deleting a person.
CREATE TABLE IF NOT EXISTS attendance (
    id          {serial},
    person_id   TEXT NOT NULL,
    external_id TEXT,
    name        TEXT NOT NULL,
    similarity  {real} NOT NULL,
    device_id   TEXT,
    ts          {real} NOT NULL,
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
    id              {serial},
    event_id        TEXT NOT NULL UNIQUE,
    event           TEXT NOT NULL,
    person_id       TEXT,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at {real} NOT NULL,
    last_error      TEXT,
    created_at      {real} NOT NULL,
    delivered_at    {real}
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON webhook_outbox(status, next_attempt_at);

-- Admin UI users. Passwords are stored as scrypt hashes.
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT,
    disabled_at   TEXT
);

-- Login sessions. The id is a SHA-256 hash of the cookie value.
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf       TEXT NOT NULL,
    created_at {real} NOT NULL,
    expires_at {real} NOT NULL,
    ip         TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

-- Who changed what, and when.
CREATE TABLE IF NOT EXISTS audit_log (
    id          {serial},
    ts          {real} NOT NULL,
    created_at  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    details     TEXT,
    ip          TEXT,
    request_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- Change feed of enrolled faces, so every server keeps its in-memory index in sync.
CREATE TABLE IF NOT EXISTS gallery_changes (
    id        {serial},
    person_id TEXT NOT NULL,
    op        TEXT NOT NULL,
    ts        {real} NOT NULL
);
"""

_SQLITE_TYPES = {"serial": "INTEGER PRIMARY KEY AUTOINCREMENT", "blob": "BLOB", "real": "REAL"}
_POSTGRES_TYPES = {"serial": "BIGSERIAL PRIMARY KEY", "blob": "BYTEA", "real": "DOUBLE PRECISION"}

Row = Dict[str, Any]


def is_postgres_url(target: Union[str, Path]) -> bool:
    return isinstance(target, str) and target.startswith(("postgres://", "postgresql://"))


class Database:
    """Thread-safe data access. One connection, serialised by a lock (the workload is light)."""

    def __init__(self, target: Union[Path, str]) -> None:
        self._lock = threading.RLock()
        self.dialect = "postgres" if is_postgres_url(target) else "sqlite"
        if self.dialect == "postgres":
            self._open_postgres(str(target))
        else:
            self._open_sqlite(Path(target))

    # ---- connection & helpers ------------------------------------------------------

    def _open_sqlite(self, path: Path) -> None:
        if sqlite3.sqlite_version_info < (3, 35):
            raise RuntimeError(f"SQLite 3.35+ is required (found {sqlite3.sqlite_version}).")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path: Optional[Path] = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_TABLES.format(**_SQLITE_TYPES))
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # Biometric data: owner-only. SQLite gives the -wal/-shm files the same mode.
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    def _open_postgres(self, url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise RuntimeError(
                'PostgreSQL support needs: pip install "face-match-service[postgres]"'
            ) from exc
        self.path = None
        self._conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        with self._lock, self._conn.transaction():
            # Serialise schema creation when several servers start at once.
            self._conn.execute("SELECT pg_advisory_xact_lock(727274)")
            for statement in _TABLES.format(**_POSTGRES_TYPES).split(";"):
                code = [line for line in statement.splitlines() if not line.strip().startswith("--")]
                if "".join(code).strip():  # skip chunks that are only comments/whitespace
                    self._conn.execute(statement)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def _execute(self, sql: str, params: Sequence[Any] = ()):
        return self._conn.execute(self._sql(sql), tuple(params))

    def _one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Row]:
        with self._lock:
            row = self._execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: Sequence[Any] = ()) -> List[Row]:
        with self._lock:
            rows = self._execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _value(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self._one(sql, params)
        return next(iter(row.values())) if row else None

    @contextlib.contextmanager
    def _tx(self) -> Iterator[None]:
        """One atomic transaction (BEGIN IMMEDIATE on SQLite, so writers queue up)."""
        with self._lock:
            if self.dialect == "postgres":
                with self._conn.transaction():
                    yield
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- persons -------------------------------------------------------------

    def _log_gallery(self, person_id: str, op: str) -> None:
        self._execute(
            "INSERT INTO gallery_changes (person_id, op, ts) VALUES (?, ?, ?)", (person_id, op, time.time())
        )

    def create_person(
        self, name: str, external_id: Optional[str], embeddings: Iterable[np.ndarray], created_at: str
    ) -> Row:
        person_id = uuid.uuid4().hex
        with self._tx():
            self._execute(
                "INSERT INTO persons (id, external_id, name, created_at) VALUES (?, ?, ?, ?)",
                (person_id, external_id, name, created_at),
            )
            self._insert_embeddings(person_id, embeddings, created_at)
            self._log_gallery(person_id, "upsert")
        return self.get_person(person_id)  # type: ignore[return-value]

    def add_embeddings(self, person_id: str, embeddings: Iterable[np.ndarray], created_at: str) -> None:
        with self._tx():
            self._insert_embeddings(person_id, embeddings, created_at)
            self._log_gallery(person_id, "upsert")

    def replace_embeddings(self, person_id: str, embeddings: Iterable[np.ndarray], created_at: str) -> None:
        """Swap all of a person's embeddings in one transaction."""
        with self._tx():
            self._execute("DELETE FROM face_embeddings WHERE person_id = ?", (person_id,))
            self._insert_embeddings(person_id, embeddings, created_at)
            self._log_gallery(person_id, "upsert")

    def _insert_embeddings(self, person_id: str, embeddings: Iterable[np.ndarray], created_at: str) -> None:
        for vector in embeddings:
            self._execute(
                "INSERT INTO face_embeddings (person_id, embedding, created_at) VALUES (?, ?, ?)",
                (person_id, np.asarray(vector, dtype=np.float32).tobytes(), created_at),
            )

    _PERSON_SELECT = """
        SELECT p.id, p.external_id, p.name, p.created_at, COUNT(e.id) AS face_count
        FROM persons p LEFT JOIN face_embeddings e ON e.person_id = p.id
    """

    def get_person(self, person_id: str) -> Optional[Row]:
        return self._one(self._PERSON_SELECT + " WHERE p.id = ? GROUP BY p.id", (person_id,))

    def find_person_by_external_id(self, external_id: str) -> Optional[Row]:
        return self._one(self._PERSON_SELECT + " WHERE p.external_id = ? GROUP BY p.id", (external_id,))

    def list_persons(self, limit: Optional[int] = None, offset: int = 0) -> List[Row]:
        sql = self._PERSON_SELECT + " GROUP BY p.id ORDER BY lower(p.name), p.id"
        params: List[Any] = []
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return self._all(sql, params)

    def count_persons(self) -> int:
        return int(self._value("SELECT COUNT(*) AS n FROM persons") or 0)

    def delete_person(self, person_id: str, erase_history: bool = False) -> bool:
        """Delete a person and their embeddings. With erase_history, also delete their
        attendance events and queued webhooks (right to erasure)."""
        with self._tx():
            deleted = self._execute("DELETE FROM persons WHERE id = ?", (person_id,)).rowcount
            if deleted:
                self._log_gallery(person_id, "delete")
            if erase_history:
                self._execute("DELETE FROM attendance WHERE person_id = ?", (person_id,))
                self._execute("DELETE FROM webhook_outbox WHERE person_id = ?", (person_id,))
        return deleted > 0

    def all_embeddings(self) -> List[Tuple[str, np.ndarray]]:
        rows = self._all("SELECT person_id, embedding FROM face_embeddings ORDER BY id")
        return [(r["person_id"], np.frombuffer(bytes(r["embedding"]), dtype=np.float32)) for r in rows]

    def embeddings_for(self, person_id: str) -> List[np.ndarray]:
        rows = self._all(
            "SELECT embedding FROM face_embeddings WHERE person_id = ? ORDER BY id", (person_id,)
        )
        return [np.frombuffer(bytes(r["embedding"]), dtype=np.float32) for r in rows]

    def gallery_revision(self) -> int:
        return int(self._value("SELECT COALESCE(MAX(id), 0) AS rev FROM gallery_changes") or 0)

    def gallery_changes_since(self, revision: int) -> List[Row]:
        return self._all(
            "SELECT id, person_id, op FROM gallery_changes WHERE id > ? ORDER BY id", (revision,)
        )

    def purge_gallery_changes_before(self, ts: float) -> int:
        with self._tx():
            return self._execute("DELETE FROM gallery_changes WHERE ts < ?", (ts,)).rowcount

    def stats(self) -> Row:
        return {
            "persons": int(self._value("SELECT COUNT(*) AS n FROM persons") or 0),
            "embeddings": int(self._value("SELECT COUNT(*) AS n FROM face_embeddings") or 0),
        }

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
        The check and insert are atomic, also across servers (PostgreSQL takes a per-person
        advisory lock), so concurrent frames can't create duplicates. With enqueue_webhook,
        the webhook is queued in the same transaction, so no event is ever lost.
        """
        with self._tx():
            if self.dialect == "postgres":
                self._execute("SELECT pg_advisory_xact_lock(hashtext(?))", (person["id"],))
            last = self._execute(
                "SELECT * FROM attendance WHERE person_id = ? ORDER BY ts DESC LIMIT 1", (person["id"],)
            ).fetchone()
            if last is not None and ts - last["ts"] < cooldown_seconds:
                return False, dict(last)
            event = dict(
                self._execute(
                    """INSERT INTO attendance
                       (person_id, external_id, name, similarity, device_id, ts, day, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
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
                ).fetchall()[0]  # read RETURNING fully before COMMIT
            )
            if enqueue_webhook:
                self._enqueue("attendance.marked", event["person_id"], attendance_payload(event))
        return True, event

    def list_attendance(
        self, day: Optional[str], person_id: Optional[str], limit: int, offset: int = 0
    ) -> List[Row]:
        clauses, params = [], []  # type: List[str], List[Any]
        if day:
            clauses.append("day = ?")
            params.append(day)
        if person_id:
            clauses.append("person_id = ?")
            params.append(person_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._all(
            f"SELECT * FROM attendance {where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )

    def attendance_summary(self, day: str) -> List[Row]:
        return self._all(
            """SELECT person_id,
                      MAX(external_id) AS external_id,
                      MAX(name)        AS name,
                      MIN(ts)          AS first_ts,
                      MAX(ts)          AS last_ts,
                      COUNT(*)         AS events
               FROM attendance WHERE day = ?
               GROUP BY person_id ORDER BY first_ts""",
            (day,),
        )

    def purge_attendance_before(self, ts: float) -> int:
        with self._tx():
            return self._execute("DELETE FROM attendance WHERE ts < ?", (ts,)).rowcount

    # ---- API keys --------------------------------------------------------------

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_api_key(self, name: str, scopes: Iterable[str], created_at: str) -> Tuple[Row, str]:
        """Create a key. Returns (row, token); the token is not stored and can't be shown again."""
        token = "fm_" + secrets.token_urlsafe(32)
        key_id = uuid.uuid4().hex[:12]
        with self._tx():
            self._execute(
                """INSERT INTO api_keys (id, name, prefix, key_hash, scopes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (key_id, name, token[:10], self.hash_token(token), " ".join(sorted(set(scopes))), created_at),
            )
        return self.get_api_key(key_id), token  # type: ignore[return-value]

    def get_api_key(self, key_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM api_keys WHERE id = ?", (key_id,))

    def find_api_key(self, token: str) -> Optional[Row]:
        return self._one(
            "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (self.hash_token(token),)
        )

    def list_api_keys(self) -> List[Row]:
        return self._all("SELECT * FROM api_keys ORDER BY created_at, id")

    def revoke_api_key(self, key_id: str, revoked_at: str) -> bool:
        with self._tx():
            return (
                self._execute(
                    "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (revoked_at, key_id),
                ).rowcount
                > 0
            )

    def touch_api_key(self, key_id: str, used_at: str) -> None:
        with self._tx():
            self._execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (used_at, key_id))

    def has_active_api_keys(self) -> bool:
        return self._one("SELECT 1 AS x FROM api_keys WHERE revoked_at IS NULL LIMIT 1") is not None

    # ---- users & sessions ----------------------------------------------------------

    def create_user(self, username: str, password_hash: str, role: str, created_at: str) -> Row:
        user_id = uuid.uuid4().hex[:12]
        with self._tx():
            self._execute(
                "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, password_hash, role, created_at),
            )
        return self.get_user(user_id)  # type: ignore[return-value]

    def get_user(self, user_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    def find_user(self, username: str) -> Optional[Row]:
        return self._one("SELECT * FROM users WHERE lower(username) = lower(?)", (username,))

    def list_users(self) -> List[Row]:
        return self._all("SELECT * FROM users ORDER BY lower(username)")

    def update_user(self, user_id: str, **fields: Any) -> bool:
        allowed = {"password_hash", "role", "disabled_at", "last_login_at"}
        changes = {k: v for k, v in fields.items() if k in allowed}
        if not changes:
            return False
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self._tx():
            return (
                self._execute(
                    f"UPDATE users SET {assignments} WHERE id = ?", (*changes.values(), user_id)
                ).rowcount
                > 0
            )

    def delete_user(self, user_id: str) -> bool:
        with self._tx():
            self._execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return self._execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount > 0

    def count_admins(self) -> int:
        return int(
            self._value("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled_at IS NULL") or 0
        )

    def has_active_users(self) -> bool:
        return self._one("SELECT 1 AS x FROM users WHERE disabled_at IS NULL LIMIT 1") is not None

    def create_session(
        self, user_id: str, ttl_seconds: float, ip: Optional[str], user_agent: Optional[str]
    ) -> Tuple[str, str, float]:
        """Returns (cookie token, csrf token, expiry). Only a hash of the token is stored."""
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        now = time.time()
        with self._tx():
            self._execute(
                """INSERT INTO sessions (id, user_id, csrf, created_at, expires_at, ip, user_agent)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self.hash_token(token), user_id, csrf, now, now + ttl_seconds, ip, (user_agent or "")[:300]),
            )
        return token, csrf, now + ttl_seconds

    def get_session(self, token: str, now: float) -> Optional[Row]:
        """Active session joined with its (enabled) user."""
        return self._one(
            """SELECT s.id AS session_id, s.csrf, s.expires_at, u.id AS user_id, u.username, u.role
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.id = ? AND s.expires_at > ? AND u.disabled_at IS NULL""",
            (self.hash_token(token), now),
        )

    def delete_session(self, token: str) -> None:
        with self._tx():
            self._execute("DELETE FROM sessions WHERE id = ?", (self.hash_token(token),))

    def delete_sessions_for_user(self, user_id: str) -> None:
        with self._tx():
            self._execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def purge_expired_sessions(self, now: float) -> int:
        with self._tx():
            return self._execute("DELETE FROM sessions WHERE expires_at <= ?", (now,)).rowcount

    # ---- audit log -------------------------------------------------------------------

    def add_audit(
        self,
        actor: str,
        action: str,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        details: Optional[Row] = None,
        ip: Optional[str] = None,
        request_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        now = time.time()
        with self._tx():
            self._execute(
                """INSERT INTO audit_log
                   (ts, created_at, actor, action, target_type, target_id, details, ip, request_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now,
                    created_at or time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
                    actor,
                    action,
                    target_type,
                    target_id,
                    json.dumps(details) if details else None,
                    ip,
                    request_id,
                ),
            )

    def list_audit(
        self, limit: int, offset: int = 0, action: Optional[str] = None, actor: Optional[str] = None
    ) -> List[Row]:
        clauses, params = [], []  # type: List[str], List[Any]
        if action:
            clauses.append("action LIKE ?")
            params.append(action.replace("*", "%"))
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)
        )
        for row in rows:
            row["details"] = json.loads(row["details"]) if row["details"] else None
        return rows

    def purge_audit_before(self, ts: float) -> int:
        with self._tx():
            return self._execute("DELETE FROM audit_log WHERE ts < ?", (ts,)).rowcount

    # ---- webhook outbox ----------------------------------------------------------

    def _enqueue(self, event: str, person_id: Optional[str], payload: Row) -> None:
        now = time.time()
        self._execute(
            """INSERT INTO webhook_outbox (event_id, event, person_id, payload, next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uuid.uuid4().hex, event, person_id, json.dumps(payload), now, now),
        )

    def claim_due_webhooks(self, now: float, lease_seconds: float = 60.0, limit: int = 20) -> List[Row]:
        """Claim due deliveries by pushing their next attempt `lease_seconds` ahead.

        Another server (or worker) won't pick them up meanwhile; on PostgreSQL rows locked by a
        concurrent claim are skipped. The returned rows keep their original values.
        """
        lock = " FOR UPDATE SKIP LOCKED" if self.dialect == "postgres" else ""
        with self._tx():
            rows = [
                dict(r)
                for r in self._execute(
                    "SELECT * FROM webhook_outbox WHERE status = 'pending' AND next_attempt_at <= ?"
                    f" ORDER BY id LIMIT ?{lock}",
                    (now, limit),
                ).fetchall()
            ]
            for row in rows:
                self._execute(
                    "UPDATE webhook_outbox SET next_attempt_at = ? WHERE id = ?",
                    (now + lease_seconds, row["id"]),
                )
        return rows

    def due_webhooks(self, now: float, limit: int = 20) -> List[Row]:
        """Read-only view of due deliveries (the dispatcher uses claim_due_webhooks)."""
        return self._all(
            "SELECT * FROM webhook_outbox WHERE status = 'pending' AND next_attempt_at <= ?"
            " ORDER BY id LIMIT ?",
            (now, limit),
        )

    def webhook_delivered(self, row_id: int, attempts: int, ts: float) -> None:
        with self._tx():
            self._execute(
                "UPDATE webhook_outbox SET status='delivered', attempts=?, delivered_at=?, last_error=NULL"
                " WHERE id=?",
                (attempts, ts, row_id),
            )

    def webhook_attempt_failed(
        self, row_id: int, attempts: int, error: str, next_at: Optional[float]
    ) -> None:
        """Record a failed attempt; next_at=None gives up (status 'failed')."""
        with self._tx():
            if next_at is None:
                self._execute(
                    "UPDATE webhook_outbox SET status='failed', attempts=?, last_error=? WHERE id=?",
                    (attempts, error[:500], row_id),
                )
            else:
                self._execute(
                    "UPDATE webhook_outbox SET attempts=?, last_error=?, next_attempt_at=? WHERE id=?",
                    (attempts, error[:500], next_at, row_id),
                )

    def list_webhooks(self, status: Optional[str], limit: int, offset: int = 0) -> List[Row]:
        where, params = ("WHERE status = ?", [status]) if status else ("", [])
        return self._all(
            f"SELECT * FROM webhook_outbox {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )

    def retry_webhook(self, row_id: int) -> bool:
        with self._tx():
            return (
                self._execute(
                    "UPDATE webhook_outbox SET status='pending', next_attempt_at=?"
                    " WHERE id=? AND status != 'delivered'",
                    (time.time(), row_id),
                ).rowcount
                > 0
            )

    def webhook_counts(self) -> Dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM webhook_outbox GROUP BY status")
        counts = {"pending": 0, "delivered": 0, "failed": 0}
        counts.update({r["status"]: int(r["n"]) for r in rows})
        return counts

    def purge_webhooks_before(self, ts: float) -> int:
        with self._tx():
            return self._execute(
                "DELETE FROM webhook_outbox WHERE status != 'pending' AND created_at < ?", (ts,)
            ).rowcount

    # ---- backups -------------------------------------------------------------------

    def backup_to(self, target: Path) -> Path:
        """Consistent online copy of the database (safe while the service is running)."""
        if self.dialect != "sqlite":
            raise RuntimeError(
                "Built-in backups are for SQLite. For PostgreSQL use pg_dump or managed backups."
            )
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


def open_database(settings) -> Database:
    """Database for these settings: PostgreSQL when DATABASE_URL is set, else SQLite in DATA_DIR."""
    return Database(settings.database_url or settings.db_path)


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
