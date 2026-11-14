"""Persistence for people, embeddings and the audit log.

Two backends share one implementation:
  * SQLite (default): a single file in DATA_DIR, ideal for one server.
  * PostgreSQL (DATABASE_URL=postgresql://...): lets several FaceMatch servers share data.
    Requires `pip install "face-match-service[postgres]"`.

Only embeddings are stored, never the raw photos.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

import numpy as np

SCHEMA_VERSION = 6

_TABLES = """
-- name is '' when none was given (the API returns null). metadata is a JSON object of the
-- caller's own fields, or NULL.
CREATE TABLE IF NOT EXISTS persons (
    id          TEXT PRIMARY KEY,
    external_id TEXT UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    metadata    TEXT
);

CREATE TABLE IF NOT EXISTS face_embeddings (
    id         {serial},
    person_id  TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    embedding  {blob} NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embeddings_person ON face_embeddings(person_id);

-- Attendance and its webhook outbox were removed in 3.0: attendance is kept by the caller's own
-- system. Tables left by older versions are not dropped automatically, so no history is lost.

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

-- Facts about the stored data, e.g. which recognition model produced the embeddings (schema 6).
CREATE TABLE IF NOT EXISTS service_info (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Scoped device keys were removed in 2.0: the single API_KEY setting gives full access.
DROP TABLE IF EXISTS api_keys;
"""

_SQLITE_TYPES = {"serial": "INTEGER PRIMARY KEY AUTOINCREMENT", "blob": "BLOB", "real": "REAL"}
_POSTGRES_TYPES = {"serial": "BIGSERIAL PRIMARY KEY", "blob": "BYTEA", "real": "DOUBLE PRECISION"}

Row = Dict[str, Any]

# Unique-constraint violations (a concurrent write took the same external_id), for both backends.
try:  # pragma: no cover - depends on the optional extra
    import psycopg as _psycopg

    INTEGRITY_ERRORS: Tuple[type, ...] = (sqlite3.IntegrityError, _psycopg.IntegrityError)
except ImportError:
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)


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
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(persons)")}
            if "metadata" not in columns:  # databases created before schema 5
                self._conn.execute("ALTER TABLE persons ADD COLUMN metadata TEXT")
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
            # Databases created before schema 5.
            self._conn.execute("ALTER TABLE persons ADD COLUMN IF NOT EXISTS metadata TEXT")

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
        self,
        name: Optional[str],
        external_id: Optional[str],
        embeddings: Iterable[np.ndarray],
        created_at: str,
        metadata: Optional[Row] = None,
    ) -> Row:
        person_id = uuid.uuid4().hex
        with self._tx():
            self._execute(
                "INSERT INTO persons (id, external_id, name, created_at, metadata) VALUES (?, ?, ?, ?, ?)",
                (person_id, external_id, name or "", created_at, _metadata_json(metadata)),
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
        SELECT p.id, p.external_id, p.name, p.created_at, p.metadata, COUNT(e.id) AS face_count
        FROM persons p LEFT JOIN face_embeddings e ON e.person_id = p.id
    """

    @staticmethod
    def _person_row(row: Optional[Row]) -> Optional[Row]:
        """Decode the metadata JSON (an empty dict when there is none)."""
        if row is not None:
            row["metadata"] = json.loads(row["metadata"]) if row["metadata"] else {}
        return row

    def get_person(self, person_id: str) -> Optional[Row]:
        return self._person_row(
            self._one(self._PERSON_SELECT + " WHERE p.id = ? GROUP BY p.id", (person_id,))
        )

    def find_person_by_external_id(self, external_id: str) -> Optional[Row]:
        return self._person_row(
            self._one(self._PERSON_SELECT + " WHERE p.external_id = ? GROUP BY p.id", (external_id,))
        )

    def list_persons(self, limit: Optional[int] = None, offset: int = 0) -> List[Row]:
        # By name; people enrolled without a name come last, oldest first.
        sql = self._PERSON_SELECT + " GROUP BY p.id ORDER BY p.name = '', lower(p.name), p.created_at, p.id"
        params: List[Any] = []
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return [self._person_row(r) for r in self._all(sql, params)]  # type: ignore[misc]

    def update_person(self, person_id: str, changes: Row) -> bool:
        """Set some of name, external_id and metadata. Returns False if the person doesn't exist."""
        values = dict(changes)
        if "name" in values:
            values["name"] = values["name"] or ""
        if "metadata" in values:
            values["metadata"] = _metadata_json(values["metadata"])
        columns = [c for c in ("name", "external_id", "metadata") if c in values]
        if not columns:
            return self.get_person(person_id) is not None
        assignments = ", ".join(f"{c} = ?" for c in columns)  # fixed column names, never user input
        params = (*[values[c] for c in columns], person_id)
        with self._tx():
            return self._execute(f"UPDATE persons SET {assignments} WHERE id = ?", params).rowcount > 0

    def count_persons(self) -> int:
        return int(self._value("SELECT COUNT(*) AS n FROM persons") or 0)

    def delete_person(self, person_id: str) -> bool:
        """Delete a person and their embeddings."""
        with self._tx():
            deleted = self._execute("DELETE FROM persons WHERE id = ?", (person_id,)).rowcount
            if deleted:
                self._log_gallery(person_id, "delete")
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

    # ---- service info ----------------------------------------------------------------

    def get_info(self, key: str) -> Optional[str]:
        return self._value("SELECT value FROM service_info WHERE key = ?", (key,))

    def set_info(self, key: str, value: str, overwrite: bool = True) -> None:
        """Store a value. With overwrite=False an existing value is kept (first writer wins)."""
        action = "UPDATE SET value = excluded.value" if overwrite else "NOTHING"
        with self._tx():
            self._execute(
                f"INSERT INTO service_info (key, value) VALUES (?, ?) ON CONFLICT (key) DO {action}",
                (key, value),
            )

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


def _metadata_json(metadata: Optional[Row]) -> Optional[str]:
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) if metadata else None
