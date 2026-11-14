"""Business logic: enrollment, recognition and attendance marking."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from .config import Settings
from .engine import Face, FaceEngine, LivenessEngine
from .gallery import FaceGallery
from .observability import Metrics
from .schemas import (
    AttendanceEvent,
    AttendanceFace,
    AttendanceResult,
    AttendanceSummary,
    Box,
    DetectedFace,
    DetectResponse,
    EnrollResponse,
    ImageSize,
    MarkAttendanceResponse,
    Person,
    PersonMatch,
    Point,
    RecognizedFace,
    RecognizeResponse,
    RejectedImage,
)
from .security import MIN_PASSWORD_LENGTH, ROLES, SCOPES, USERNAME_PATTERN, hash_password
from .storage import Database, Row

# When a photo has several faces, the largest is used for enrollment only if
# the next one is smaller than this fraction of it (i.e. a bystander).
_DOMINANT_FACE_RATIO = 0.5
# Small images (e.g. passport-photo thumbnails from a school portal) where no
# face is found are retried after upscaling to this many pixels on the long side.
_UPSCALE_TARGET = 480


class ServiceError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


@dataclass
class ImageInput:
    data: bytes
    filename: Optional[str] = None


@dataclass
class MatchResult:
    match: Optional[PersonMatch] = None
    person: Optional[Row] = None
    closest: Optional[PersonMatch] = None  # best candidate when no match was accepted
    reason: Optional[str] = None  # why not accepted (see schemas.UnknownReason)


@dataclass
class AnalyzedFace:
    fields: Dict[str, Any]  # box, score, landmarks in original-image coordinates
    embedding: Optional[np.ndarray]  # None when the face is too small to recognise
    face: Optional[Face] = None  # detection in working-image coordinates (for liveness)
    image: Optional[np.ndarray] = None  # working image the face was found in


@dataclass
class _Decoded:
    image: np.ndarray  # possibly downscaled working copy
    scale: float  # working size / original size
    size: ImageSize  # original size


class FaceService:
    def __init__(
        self,
        settings: Settings,
        engine: FaceEngine,
        db: Database,
        metrics: Optional[Metrics] = None,
        liveness: Optional[LivenessEngine] = None,
        on_event_queued: Optional[Callable[[], None]] = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.db = db
        self.metrics = metrics or Metrics()
        self.liveness = liveness
        self._on_event_queued = on_event_queued or (lambda: None)
        self.gallery = FaceGallery(engine.embedding_size)
        # Read the change-feed position first, so nothing written during the load is missed.
        self._gallery_revision = db.gallery_revision()
        self.gallery.load(db.all_embeddings())
        self._sync_lock = threading.Lock()
        self._tz = ZoneInfo(settings.timezone) if settings.timezone else None

    # ---- time helpers --------------------------------------------------------

    def _local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self._tz) if self._tz else datetime.fromtimestamp(ts).astimezone()

    def _iso(self, ts: float) -> str:
        return self._local(ts).isoformat(timespec="seconds")

    def today(self) -> str:
        return self._local(time.time()).date().isoformat()

    # ---- image helpers -------------------------------------------------------

    def _decode(self, data: bytes) -> _Decoded:
        if len(data) > self.settings.max_upload_bytes:
            raise ServiceError(413, "image_too_large", f"Image exceeds {self.settings.max_upload_mb:g} MB.")
        image = None
        if data:
            try:
                image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            except cv2.error:
                image = None
        if image is None:
            raise ServiceError(
                400, "invalid_image", "Could not decode image. Send a JPEG, PNG, WebP or BMP file."
            )
        height, width = image.shape[:2]
        scale = min(1.0, self.settings.max_image_side / max(height, width))
        if scale < 1.0:
            image = cv2.resize(
                image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA
            )
        return _Decoded(image=image, scale=scale, size=ImageSize(width=width, height=height))

    def _detect(self, decoded: _Decoded) -> Tuple[_Decoded, List[Face]]:
        """Detect faces; for small images with no hit, retry on an upscaled copy.

        Upscaling is a fallback rather than the default because faces that fill
        the whole frame are detected better at their original size.
        """
        faces = self.engine.detect(decoded.image)
        height, width = decoded.image.shape[:2]
        if faces or max(height, width) >= _UPSCALE_TARGET:
            return decoded, faces
        factor = _UPSCALE_TARGET / max(height, width)
        image = cv2.resize(
            decoded.image, (round(width * factor), round(height * factor)), interpolation=cv2.INTER_CUBIC
        )
        upscaled = _Decoded(image=image, scale=decoded.scale * factor, size=decoded.size)
        return upscaled, self.engine.detect(image)

    @staticmethod
    def _effective_size(face: Face, decoded: _Decoded) -> float:
        """Face size in pixels of real detail: upscaling adds none, downscaling removes some."""
        return face.size / max(1.0, decoded.scale)

    @staticmethod
    def _face_fields(face: Face, decoded: _Decoded) -> Dict[str, Any]:
        """Convert a working-image face into original-image API fields."""
        inv = 1.0 / decoded.scale
        x, y, w, h = (v * inv for v in face.box)
        x0, y0 = max(0.0, x), max(0.0, y)
        x1, y1 = min(float(decoded.size.width), x + w), min(float(decoded.size.height), y + h)
        return {
            "box": Box(x=round(x0), y=round(y0), width=round(x1 - x0), height=round(y1 - y0)),
            "score": round(face.score, 4),
            "landmarks": [Point(x=round(px * inv, 1), y=round(py * inv, 1)) for px, py in face.landmarks],
        }

    def analyze(self, data: bytes) -> Tuple[ImageSize, List[AnalyzedFace]]:
        """Decode, detect (largest face first) and embed every face big enough to recognise.

        This is the shared first half of recognition; evaluation tooling uses it too.
        """
        decoded, faces = self._detect(self._decode(data))
        results = []
        for face in faces:
            usable = self._effective_size(face, decoded) >= self.settings.min_face_size
            embedding = self.engine.embed(decoded.image, face) if usable else None
            results.append(
                AnalyzedFace(
                    fields=self._face_fields(face, decoded),
                    embedding=embedding,
                    face=face,
                    image=decoded.image,
                )
            )
        return decoded.size, results

    def liveness_score(self, face: AnalyzedFace) -> Optional[float]:
        if self.liveness is None or face.face is None or face.image is None:
            return None
        return round(float(self.liveness.score(face.image, face.face)), 4)

    def match_embedding(self, vector: np.ndarray) -> MatchResult:
        """Apply the identification rules (threshold + margin) to one face embedding."""
        best = self.gallery.best_match(vector)
        if best is None:
            return MatchResult(reason="nobody_enrolled")
        person = self.db.get_person(best.person_id)
        if person is None:  # deleted between search and lookup
            return MatchResult(reason="below_threshold")
        candidate = PersonMatch(
            person_id=person["id"],
            external_id=person["external_id"],
            name=person["name"],
            similarity=round(best.similarity, 4),
        )
        if best.similarity < self.settings.match_threshold:
            return MatchResult(closest=candidate, reason="below_threshold")
        # With hundreds of people enrolled, refuse to guess between two close candidates:
        # "unknown" is recoverable (manual marking), a wrong name is not.
        if best.runner_up is not None and best.similarity - best.runner_up < self.settings.match_margin:
            return MatchResult(closest=candidate, reason="ambiguous")
        return MatchResult(match=candidate, person=person)

    @staticmethod
    def _elapsed_ms(start: float) -> float:
        return round((time.perf_counter() - start) * 1000, 1)

    # ---- detection & recognition --------------------------------------------

    def detect(self, data: bytes) -> DetectResponse:
        start = time.perf_counter()
        decoded, faces = self._detect(self._decode(data))
        return DetectResponse(
            image=decoded.size,
            faces=[DetectedFace(**self._face_fields(f, decoded)) for f in faces],
            processing_ms=self._elapsed_ms(start),
        )

    def sync_gallery(self) -> int:
        """Apply enrollments and deletions made by other servers sharing the database.

        Returns the number of people refreshed. Cheap when nothing changed (one indexed query).
        """
        with self._sync_lock:
            if self.db.gallery_revision() <= self._gallery_revision:
                return 0
            changes = self.db.gallery_changes_since(self._gallery_revision)
            latest: Dict[str, str] = {}
            for change in changes:
                latest[change["person_id"]] = change["op"]
            for person_id, op in latest.items():
                self.gallery.remove(person_id)
                if op != "delete":
                    self.gallery.add(person_id, self.db.embeddings_for(person_id))
            if changes:
                self._gallery_revision = max(c["id"] for c in changes)
            return len(latest)

    def _count_face(self, endpoint: str, outcome: str) -> None:
        self.metrics.inc("facematch_faces_total", endpoint=endpoint, outcome=outcome)

    def recognize(self, data: bytes, check_liveness: bool = False) -> RecognizeResponse:
        start = time.perf_counter()
        size, faces = self.analyze(data)
        results = []
        for face in faces:
            if face.embedding is None:
                results.append(RecognizedFace(**face.fields, match=None, unknown_reason="too_small"))
                self._count_face("recognize", "too_small")
                continue
            found = self.match_embedding(face.embedding)
            score = self.liveness_score(face) if check_liveness else None
            results.append(
                RecognizedFace(
                    **face.fields,
                    match=found.match,
                    unknown_reason=found.reason,
                    closest=found.closest,
                    liveness_score=score,
                )
            )
            self._count_face("recognize", "matched" if found.match else found.reason or "unknown")
        return RecognizeResponse(image=size, faces=results, processing_ms=self._elapsed_ms(start))

    # ---- attendance ----------------------------------------------------------

    def mark_attendance(
        self, data: bytes, device_id: Optional[str]
    ) -> Tuple[MarkAttendanceResponse, List[AttendanceEvent]]:
        """Recognise every face in the frame and log attendance for known people.

        Returns the response plus the newly created events (for webhooks).
        """
        start = time.perf_counter()
        size, faces = self.analyze(data)
        device_id = (device_id or "").strip() or None
        results: List[AttendanceFace] = []
        new_events: List[AttendanceEvent] = []
        seen_in_frame = set()

        for face in faces:
            fields = face.fields
            if face.embedding is None:
                results.append(
                    AttendanceFace(
                        **fields,
                        match=None,
                        unknown_reason="too_small",
                        attendance=AttendanceResult(status="too_small"),
                    )
                )
                self._count_face("attendance", "too_small")
                continue

            found = self.match_embedding(face.embedding)
            match, person = found.match, found.person
            if match is None or person is None:
                # No `closest` here: a public kiosk shouldn't reveal who someone resembles.
                results.append(
                    AttendanceFace(
                        **fields,
                        match=None,
                        unknown_reason=found.reason,
                        attendance=AttendanceResult(status="unknown"),
                    )
                )
                self._count_face("attendance", found.reason or "unknown")
                continue

            # Anti-spoofing: only recognised faces can mark attendance, so only they are checked.
            score = self.liveness_score(face) if self.settings.liveness == "on" else None
            if score is not None and score < self.settings.liveness_threshold:
                results.append(
                    AttendanceFace(
                        **fields,
                        match=None,  # don't confirm who a suspected spoof shows
                        unknown_reason="liveness_failed",
                        liveness_score=score,
                        attendance=AttendanceResult(status="liveness_failed"),
                    )
                )
                self._count_face("attendance", "liveness_failed")
                continue

            if match.person_id in seen_in_frame:
                outcome = AttendanceResult(status="already_marked")
            else:
                seen_in_frame.add(match.person_id)
                now = time.time()
                created, row = self.db.mark_attendance(
                    person=person,
                    similarity=match.similarity,
                    device_id=device_id,
                    ts=now,
                    day=self._local(now).date().isoformat(),
                    created_at=self._iso(now),
                    cooldown_seconds=self.settings.attendance_cooldown_seconds,
                    enqueue_webhook=bool(self.settings.webhook_url),
                )
                outcome = AttendanceResult(
                    status="marked" if created else "already_marked",
                    event_id=row["id"],
                    timestamp=row["created_at"],
                )
                if created:
                    new_events.append(self._event(row))
                    self.metrics.inc("facematch_attendance_marked_total")
            results.append(AttendanceFace(**fields, match=match, liveness_score=score, attendance=outcome))
            self._count_face("attendance", outcome.status)

        if new_events and self.settings.webhook_url:
            self._on_event_queued()

        response = MarkAttendanceResponse(
            image=size,
            device_id=device_id,
            faces=results,
            processing_ms=self._elapsed_ms(start),
        )
        return response, new_events

    @staticmethod
    def _event(row: Row) -> AttendanceEvent:
        return AttendanceEvent(
            id=row["id"],
            person_id=row["person_id"],
            external_id=row["external_id"],
            name=row["name"],
            similarity=row["similarity"],
            device_id=row["device_id"],
            timestamp=row["created_at"],
            day=row["day"],
        )

    def list_attendance(
        self, day: Optional[str], person_id: Optional[str], limit: int, offset: int = 0
    ) -> List[AttendanceEvent]:
        return [self._event(r) for r in self.db.list_attendance(day, person_id, limit, offset)]

    def attendance_summary(self, day: Optional[str]) -> List[AttendanceSummary]:
        rows = self.db.attendance_summary(day or self.today())
        return [
            AttendanceSummary(
                person_id=r["person_id"],
                external_id=r["external_id"],
                name=r["name"],
                first_seen=self._iso(r["first_ts"]),
                last_seen=self._iso(r["last_ts"]),
                events=r["events"],
            )
            for r in rows
        ]

    # ---- enrollment ----------------------------------------------------------

    def _collect_embeddings(
        self,
        images: List[ImageInput],
        person_id: Optional[str],
        allow_duplicate: bool = False,
        compare_with_existing: bool = True,
    ) -> Tuple[List[np.ndarray], List[RejectedImage], List[str]]:
        """Quality-check enrollment photos and extract one embedding from each.

        `person_id` is whose photos these are (None for a new person). With
        `compare_with_existing`, photos must match that person's current photos;
        otherwise they only have to agree with each other.
        `allow_duplicate` skips the "already enrolled as someone else" check (twins).
        Returns (accepted embeddings, rejected images, ids of other people matched).
        """
        rejected: List[RejectedImage] = []
        conflicts: List[str] = []
        candidates: List[Tuple[int, ImageInput, np.ndarray]] = []
        threshold = self.settings.match_threshold

        def reject(index: int, item: ImageInput, reason: str) -> None:
            rejected.append(RejectedImage(index=index, filename=item.filename, reason=reason))

        # 1) Per-photo quality checks and embedding.
        for index, item in enumerate(images):
            try:
                decoded = self._decode(item.data)
            except ServiceError as exc:
                reject(index, item, "too_large" if exc.status_code == 413 else "invalid_image")
                continue

            decoded, faces = self._detect(decoded)
            if not faces:
                reject(index, item, "no_face")
                continue
            if len(faces) > 1 and faces[1].size >= faces[0].size * _DOMINANT_FACE_RATIO:
                reject(index, item, "multiple_faces")
                continue
            face = faces[0]
            if self._effective_size(face, decoded) < self.settings.min_enroll_face_size:
                reject(index, item, "face_too_small")
                continue

            candidates.append((index, item, self.engine.embed(decoded.image, face)))

        # 2) Already enrolled as someone else? Uses DUPLICATE_THRESHOLD, which is stricter than
        #    the match threshold: with hundreds of similar-age students, 0.40 would wrongly flag
        #    ~30% of new photos as duplicates (measured), while 0.50 flags ~0.4%.
        unique: List[Tuple[int, ImageInput, np.ndarray]] = []
        for index, item, vector in candidates:
            best = self.gallery.best_match(vector)
            if (
                not allow_duplicate
                and best is not None
                and best.person_id != person_id
                and best.similarity >= self.settings.duplicate_threshold
            ):
                conflicts.append(best.person_id)
                reject(index, item, "matches_other_person")
            else:
                unique.append((index, item, vector))

        # 3) Every photo must show the same person.
        accepted: List[np.ndarray] = []
        if person_id is not None and compare_with_existing:  # adding photos: compare with those on file
            for index, item, vector in unique:
                reference = self.gallery.similarity_to(person_id, vector)
                if reference is not None and reference < threshold:
                    reject(index, item, "inconsistent_identity")
                else:
                    accepted.append(vector)
        elif unique:
            # The reference is the photo that agrees best with the others, so the result does
            # not depend on upload order and one odd photo (sunglasses, wrong person) is dropped.
            vectors = np.vstack([v for _, _, v in unique])
            with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
                agreement = vectors @ vectors.T
            anchor = int(np.argmax(agreement.sum(axis=1)))
            for i, (index, item, vector) in enumerate(unique):
                if i != anchor and agreement[anchor, i] < threshold:
                    reject(index, item, "inconsistent_identity")
                else:
                    accepted.append(vector)

        rejected.sort(key=lambda r: r.index)
        return accepted, rejected, conflicts

    def _person(self, row: Row) -> Person:
        return Person(**{k: row[k] for k in ("id", "external_id", "name", "face_count", "created_at")})

    def _raise_no_usable_face(self, rejected: List[RejectedImage], conflicts: List[str]) -> None:
        if conflicts:
            other = self.db.get_person(conflicts[0])
            raise ServiceError(
                409,
                "already_enrolled",
                "This face is already enrolled as another person.",
                {"person": self._person(other).model_dump() if other else None},
            )
        raise ServiceError(
            422,
            "no_usable_face",
            "None of the images contained a usable face.",
            [r.model_dump() for r in rejected],
        )

    def enroll(
        self,
        name: str,
        external_id: Optional[str],
        images: List[ImageInput],
        allow_duplicate: bool = False,
    ) -> EnrollResponse:
        name = (name or "").strip()
        external_id = (external_id or "").strip() or None
        if not name:
            raise ServiceError(422, "invalid_name", "Name must not be empty.")
        if not images:
            raise ServiceError(422, "no_images", "Provide at least one image.")
        if external_id:
            existing = self.db.find_person_by_external_id(external_id)
            if existing:
                raise ServiceError(
                    409,
                    "external_id_exists",
                    f"external_id '{external_id}' is already enrolled.",
                    {"person": self._person(existing).model_dump()},
                )

        accepted, rejected, conflicts = self._collect_embeddings(
            images, person_id=None, allow_duplicate=allow_duplicate
        )
        if not accepted:
            self._raise_no_usable_face(rejected, conflicts)

        try:
            row = self.db.create_person(name, external_id, accepted, self._iso(time.time()))
        except sqlite3.IntegrityError:  # concurrent enrollment with the same external_id
            raise ServiceError(
                409, "external_id_exists", f"external_id '{external_id}' is already enrolled."
            ) from None
        self.gallery.add(row["id"], accepted)
        return EnrollResponse(person=self._person(row), accepted=len(accepted), rejected=rejected)

    def add_faces(self, person_id: str, images: List[ImageInput]) -> EnrollResponse:
        if self.db.get_person(person_id) is None:
            raise ServiceError(404, "person_not_found", "Person not found.")
        if not images:
            raise ServiceError(422, "no_images", "Provide at least one image.")
        accepted, rejected, conflicts = self._collect_embeddings(images, person_id=person_id)
        if not accepted:
            self._raise_no_usable_face(rejected, conflicts)
        self.db.add_embeddings(person_id, accepted, self._iso(time.time()))
        self.gallery.add(person_id, accepted)
        row = self.db.get_person(person_id)
        return EnrollResponse(person=self._person(row), accepted=len(accepted), rejected=rejected)

    def replace_faces(
        self, person_id: str, images: List[ImageInput], allow_duplicate: bool = False
    ) -> EnrollResponse:
        """Replace all of a person's photos (e.g. a new school year). All-or-nothing:
        if none of the new photos is usable, the existing ones are kept."""
        if self.db.get_person(person_id) is None:
            raise ServiceError(404, "person_not_found", "Person not found.")
        if not images:
            raise ServiceError(422, "no_images", "Provide at least one image.")
        accepted, rejected, conflicts = self._collect_embeddings(
            images, person_id=person_id, allow_duplicate=allow_duplicate, compare_with_existing=False
        )
        if not accepted:
            self._raise_no_usable_face(rejected, conflicts)
        self.db.replace_embeddings(person_id, accepted, self._iso(time.time()))
        self.gallery.remove(person_id)
        self.gallery.add(person_id, accepted)
        row = self.db.get_person(person_id)
        return EnrollResponse(person=self._person(row), accepted=len(accepted), rejected=rejected)

    # ---- persons -------------------------------------------------------------

    def list_persons(self, external_id: Optional[str] = None) -> List[Person]:
        if external_id is not None:
            row = self.db.find_person_by_external_id(external_id.strip())
            return [self._person(row)] if row else []
        return [self._person(r) for r in self.db.list_persons()]

    def get_person(self, person_id: str) -> Person:
        row = self.db.get_person(person_id)
        if row is None:
            raise ServiceError(404, "person_not_found", "Person not found.")
        return self._person(row)

    def delete_person(self, person_id: str, erase_history: bool = False) -> None:
        if not self.db.delete_person(person_id, erase_history=erase_history):
            raise ServiceError(404, "person_not_found", "Person not found.")
        self.gallery.remove(person_id)

    # ---- API keys & webhooks (admin) ----------------------------------------------

    def create_api_key(self, name: str, scopes: List[str]) -> Tuple[Row, str]:
        name = (name or "").strip()
        unknown = sorted(set(scopes) - set(SCOPES))
        if not name:
            raise ServiceError(422, "invalid_name", "Name must not be empty.")
        if not scopes or unknown:
            raise ServiceError(
                422, "invalid_scopes", f"Unknown or missing scopes: {unknown}", {"allowed": sorted(SCOPES)}
            )
        return self.db.create_api_key(name, scopes, self._iso(time.time()))

    def revoke_api_key(self, key_id: str) -> None:
        if not self.db.revoke_api_key(key_id, self._iso(time.time())):
            raise ServiceError(404, "key_not_found", "API key not found or already revoked.")

    def retry_webhook(self, delivery_id: int) -> None:
        if not self.db.retry_webhook(delivery_id):
            raise ServiceError(404, "delivery_not_found", "Delivery not found or already delivered.")
        self._on_event_queued()

    # ---- admin UI users ---------------------------------------------------------------

    @staticmethod
    def _check_password(password: Optional[str]) -> None:
        if password is not None and len(password) < MIN_PASSWORD_LENGTH:
            raise ServiceError(
                422, "weak_password", f"Passwords need at least {MIN_PASSWORD_LENGTH} characters."
            )

    @staticmethod
    def _check_role(role: Optional[str]) -> None:
        if role is not None and role not in ROLES:
            raise ServiceError(422, "invalid_role", f"Role must be one of: {', '.join(ROLES)}.")

    def create_user(self, username: str, password: str, role: str) -> Row:
        username = (username or "").strip()
        if not USERNAME_PATTERN.match(username):
            raise ServiceError(
                422, "invalid_username", "Usernames are 3-64 characters: letters, digits and _ . @ -"
            )
        self._check_password(password)
        self._check_role(role)
        if self.db.find_user(username):
            raise ServiceError(409, "user_exists", f"User '{username}' already exists.")
        return self.db.create_user(username, hash_password(password), role, self._iso(time.time()))

    def _guard_last_admin(self, user: Row) -> None:
        if user["role"] == "admin" and not user["disabled_at"] and self.db.count_admins() <= 1:
            raise ServiceError(
                409, "last_admin", "This is the only active admin. Create another admin first."
            )

    def update_user(
        self,
        user_id: str,
        role: Optional[str] = None,
        password: Optional[str] = None,
        disabled: Optional[bool] = None,
    ) -> Row:
        user = self.db.get_user(user_id)
        if user is None:
            raise ServiceError(404, "user_not_found", "User not found.")
        self._check_role(role)
        self._check_password(password)
        if (role is not None and role != "admin") or disabled:
            self._guard_last_admin(user)
        fields: Dict[str, Any] = {}
        if role is not None:
            fields["role"] = role
        if password is not None:
            fields["password_hash"] = hash_password(password)
        if disabled is not None:
            fields["disabled_at"] = self._iso(time.time()) if disabled else None
        if fields:
            self.db.update_user(user_id, **fields)
        if password is not None or disabled:
            self.db.delete_sessions_for_user(user_id)  # sign out everywhere
        return self.db.get_user(user_id)  # type: ignore[return-value]

    def delete_user(self, user_id: str) -> Row:
        user = self.db.get_user(user_id)
        if user is None:
            raise ServiceError(404, "user_not_found", "User not found.")
        self._guard_last_admin(user)
        self.db.delete_user(user_id)
        return user
