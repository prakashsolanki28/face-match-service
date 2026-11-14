"""Business logic: enrollment, recognition and attendance marking."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from .config import Settings
from .engine import Face, FaceEngine
from .gallery import FaceGallery
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
from .storage import Database, Row

# When a photo has several faces, the largest is used for enrollment only if
# the next one is smaller than this fraction of it (i.e. a bystander).
_DOMINANT_FACE_RATIO = 0.5
# Small images (e.g. passport-photo thumbnails from a school portal) where no
# face is found are retried after upscaling to this many pixels on the long side.
_UPSCALE_TARGET = 480


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


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


@dataclass
class _Decoded:
    image: np.ndarray  # possibly downscaled working copy
    scale: float  # working size / original size
    size: ImageSize  # original size


class FaceService:
    def __init__(self, settings: Settings, engine: FaceEngine, db: Database) -> None:
        self.settings = settings
        self.engine = engine
        self.db = db
        self.gallery = FaceGallery(engine.embedding_size)
        self.gallery.load(db.all_embeddings())
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
            results.append(AnalyzedFace(fields=self._face_fields(face, decoded), embedding=embedding))
        return decoded.size, results

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

    def recognize(self, data: bytes) -> RecognizeResponse:
        start = time.perf_counter()
        size, faces = self.analyze(data)
        results = []
        for face in faces:
            if face.embedding is None:
                results.append(RecognizedFace(**face.fields, match=None, unknown_reason="too_small"))
                continue
            found = self.match_embedding(face.embedding)
            results.append(
                RecognizedFace(
                    **face.fields, match=found.match, unknown_reason=found.reason, closest=found.closest
                )
            )
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
                )
                outcome = AttendanceResult(
                    status="marked" if created else "already_marked",
                    event_id=row["id"],
                    timestamp=row["created_at"],
                )
                if created:
                    new_events.append(self._event(row))
            results.append(AttendanceFace(**fields, match=match, attendance=outcome))

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
        self, day: Optional[str], person_id: Optional[str], limit: int
    ) -> List[AttendanceEvent]:
        return [self._event(r) for r in self.db.list_attendance(day, person_id, limit)]

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

    def delete_person(self, person_id: str) -> None:
        if not self.db.delete_person(person_id):
            raise ServiceError(404, "person_not_found", "Person not found.")
        self.gallery.remove(person_id)
