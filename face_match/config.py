"""Service configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (KEY=VALUE per line), read from the working directory.
    Real environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Server
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"
    log_format: str = "text"  # "text" or "json" (one JSON object per line, for log shippers)
    # Security: requests must send X-API-Key. Without a key the API is open, which the
    # CLI only allows on localhost unless allow_no_auth is set (see cli.check_auth).
    api_key: str = ""
    allow_no_auth: bool = False
    api_docs: bool = (
        False  # serve interactive API docs at /docs and /redoc (the spec is always at /openapi.json)
    )
    cors_origins: List[str] = field(default_factory=lambda: ["*"])
    # Abuse protection
    rate_limit_per_minute: int = 600  # requests per API key (or per IP when open); 0 disables
    auth_failures_per_minute: int = 20  # failed authentications per IP before 429
    max_concurrent_images: int = 8  # image requests processed at once; more wait, then get 503
    # Storage (relative paths resolve against the working directory)
    database_url: str = ""  # postgresql://... to share data between several servers; empty = SQLite
    gallery_sync_seconds: float = 2.0  # how often each server picks up enrollments made by others
    data_dir: Path = Path("data")
    models_dir: Path = Path("models")
    auto_download_models: bool = True
    # Face pipeline
    # YuNet confidence needed to count as a face. 0.6 measured best on real photos: at 0.9,
    # 16% of ordinary portraits were missed; down to 0.5 gave no false detections.
    detection_threshold: float = 0.60
    match_threshold: float = 0.40  # SFace cosine similarity needed to accept a match
    match_margin: float = 0.05  # best match must beat the next-best *other* person by this much
    duplicate_threshold: float = 0.50  # enrollment: photo this similar to someone else = duplicate
    min_face_size: int = 40  # px of real detail, smallest face used for recognition
    min_enroll_face_size: int = 40  # px of real detail; passport thumbnails of ~100x125 px pass
    max_image_side: int = 1600  # larger images are downscaled before processing
    max_upload_mb: float = 8.0
    # Attendance
    attendance_cooldown_seconds: int = 300  # ignore repeat sightings inside this window
    timezone: str = ""  # IANA name such as "Asia/Kolkata"; empty means server local time
    # Integration
    webhook_url: str = ""  # POSTed to on every new attendance event
    webhook_secret: str = ""  # signs webhook bodies (HMAC-SHA256) when set
    webhook_max_attempts: int = 20  # exponential backoff (5 s doubling, capped at 1 h): about 10 hours
    webhook_retry_base_seconds: float = 5.0
    # Data protection
    backup_interval_hours: float = 24.0  # automatic SQLite backups into DATA_DIR/backups; 0 disables
    backup_keep: int = 7  # newest backups to keep
    attendance_retention_days: int = 0  # delete attendance events older than this; 0 keeps forever
    audit_retention_days: int = 0  # delete audit entries older than this; 0 keeps forever
    # Liveness (anti-spoofing) for /attendance: "on" or "off"
    liveness: str = "on"
    liveness_threshold: float = 0.5  # minimum "real face" probability

    @property
    def db_path(self) -> Path:
        legacy = self.data_dir / "attendance.db"  # name used before v1.0
        return legacy if legacy.exists() else self.data_dir / "facematch.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        defaults = cls()
        origins = _str("CORS_ORIGINS", "*")
        return cls(
            host=_str("HOST", defaults.host),
            port=_int("PORT", defaults.port),
            log_level=_str("LOG_LEVEL", defaults.log_level).lower(),
            log_format=_str("LOG_FORMAT", defaults.log_format).lower(),
            api_key=_str("API_KEY", ""),
            allow_no_auth=_bool("ALLOW_NO_AUTH", False),
            api_docs=_bool("API_DOCS", False),
            database_url=_str("DATABASE_URL", ""),
            gallery_sync_seconds=_float("GALLERY_SYNC_SECONDS", defaults.gallery_sync_seconds),
            cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
            rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", defaults.rate_limit_per_minute),
            auth_failures_per_minute=_int("AUTH_FAILURES_PER_MINUTE", defaults.auth_failures_per_minute),
            max_concurrent_images=_int("MAX_CONCURRENT_IMAGES", defaults.max_concurrent_images),
            data_dir=Path(_str("DATA_DIR", str(defaults.data_dir))),
            models_dir=Path(_str("MODELS_DIR", str(defaults.models_dir))),
            auto_download_models=_bool("AUTO_DOWNLOAD_MODELS", True),
            detection_threshold=_float("DETECTION_THRESHOLD", defaults.detection_threshold),
            match_threshold=_float("MATCH_THRESHOLD", defaults.match_threshold),
            match_margin=_float("MATCH_MARGIN", defaults.match_margin),
            duplicate_threshold=_float("DUPLICATE_THRESHOLD", defaults.duplicate_threshold),
            min_face_size=_int("MIN_FACE_SIZE", defaults.min_face_size),
            min_enroll_face_size=_int("MIN_ENROLL_FACE_SIZE", defaults.min_enroll_face_size),
            max_image_side=_int("MAX_IMAGE_SIDE", defaults.max_image_side),
            max_upload_mb=_float("MAX_UPLOAD_MB", defaults.max_upload_mb),
            attendance_cooldown_seconds=_int(
                "ATTENDANCE_COOLDOWN_SECONDS", defaults.attendance_cooldown_seconds
            ),
            timezone=_str("TIMEZONE", ""),
            webhook_url=_str("WEBHOOK_URL", ""),
            webhook_secret=_str("WEBHOOK_SECRET", ""),
            webhook_max_attempts=_int("WEBHOOK_MAX_ATTEMPTS", defaults.webhook_max_attempts),
            webhook_retry_base_seconds=_float(
                "WEBHOOK_RETRY_BASE_SECONDS", defaults.webhook_retry_base_seconds
            ),
            backup_interval_hours=_float("BACKUP_INTERVAL_HOURS", defaults.backup_interval_hours),
            backup_keep=_int("BACKUP_KEEP", defaults.backup_keep),
            attendance_retention_days=_int("ATTENDANCE_RETENTION_DAYS", defaults.attendance_retention_days),
            audit_retention_days=_int("AUDIT_RETENTION_DAYS", defaults.audit_retention_days),
            liveness=_str("LIVENESS", defaults.liveness).lower(),
            liveness_threshold=_float("LIVENESS_THRESHOLD", defaults.liveness_threshold),
        )
