"""Service configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


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
    # Security: requests must send X-API-Key. Without a key the API is open, which the
    # CLI only allows on localhost unless allow_no_auth is set (see cli.check_auth).
    api_key: str = ""
    allow_no_auth: bool = False
    cors_origins: List[str] = field(default_factory=lambda: ["*"])
    # Storage (relative paths resolve against the working directory)
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

    @property
    def db_path(self) -> Path:
        legacy = self.data_dir / "attendance.db"  # name used before v1.0
        return legacy if legacy.exists() else self.data_dir / "facematch.db"

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
            api_key=_str("API_KEY", ""),
            allow_no_auth=_bool("ALLOW_NO_AUTH", False),
            cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
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
        )
