"""Business logic: enrollment and recognition."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from .config import Settings
from .engine import Face, FaceEngine, LivenessEngine
from .gallery import FaceGallery
from .observability import Metrics
from .schemas import (
    Box,
    DetectedFace,
    DetectResponse,
    EnrollResponse,
    ImageSize,
    Person,
    PersonMatch,
    Point,
    RecognizedFace,
    RecognizeResponse,
    RejectedImage,
)
from .storage import INTEGRITY_ERRORS, Database, Row

# When a photo has several faces, the largest is used for enrollment only if
# the next one is smaller than this fraction of it (i.e. a bystander).
_DOMINANT_FACE_RATIO = 0.5
# Small images (e.g. passport-photo thumbnails from a school portal) where no
# face is found are retried after upscaling to this many pixels on the long side.
_UPSCALE_TARGET = 480

# Limits for a person's own fields (metadata).
METADATA_MAX_KEYS = 50
METADATA_MAX_KEY_LENGTH = 64
METADATA_MAX_TEXT_LENGTH = 1000


EMBEDDING_MODEL_KEY = "embedding_model"


def embedding_model_id(engine: FaceEngine) -> str:
    return getattr(engine, "model_id", None) or engine.name


class EmbeddingModelMismatch(RuntimeError):
    """The database holds embeddings from a different recognition model than the one loaded."""

    def __init__(self, stored: str, loaded: str, embeddings: int) -> None:
        super().__init__(
            f"The database holds {embeddings} face embeddings made by the model '{stored}', but this "
            f"server runs '{loaded}'. Embeddings from different models can't be compared: matching "
            "would give wrong results, so FaceMatch won't start. Either run the version of FaceMatch "
            "(and model) that made them, or enroll everyone again into a new, empty database (a new "
            "DATA_DIR or DATABASE_URL), for example with `face-match import`. Photos are never stored, "
            "so existing embeddings can't be converted."
        )
        self.stored, self.loaded = stored, loaded


def check_embedding_model(db: Database, engine: FaceEngine) -> str:
    """Record which model makes this database's embeddings, and refuse a different one.

    The first server to start claims the database for its model. Databases from before schema 6
    have no record; their embeddings were made by the only model those versions shipped, so they
    are claimed by the current one. An empty database can switch models freely.
    """
    loaded = embedding_model_id(engine)
    db.set_info(EMBEDDING_MODEL_KEY, loaded, overwrite=False)
    stored = db.get_info(EMBEDDING_MODEL_KEY)
    if stored != loaded:
        embeddings = db.stats()["embeddings"]
        if embeddings:
            raise EmbeddingModelMismatch(stored or "unknown", loaded, embeddings)
        db.set_info(EMBEDDING_MODEL_KEY, loaded)
    return loaded


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


def _invalid_metadata(message: str) -> ServiceError:
    return ServiceError(422, "invalid_metadata", message)


def _reject_constant(value: str) -> Any:
    raise _invalid_metadata(f"metadata can't contain {value}.")


def parse_metadata(text: Optional[str]) -> Dict[str, Any]:
    """Parse the `metadata` form field: a JSON object. Empty or missing means none."""
    if text is None or not text.strip():
        return {}
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError:
        raise _invalid_metadata(
            'metadata must be a JSON object, e.g. {"class": "7-A", "roll_no": 12}.'
        ) from None
    return clean_metadata(value)


def clean_metadata(value: Any) -> Dict[str, Any]:
    """Check a person's own fields: a flat JSON object of text, numbers, true/false or null."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _invalid_metadata('metadata must be a JSON object, e.g. {"class": "7-A", "roll_no": 12}.')
    if len(value) > METADATA_MAX_KEYS:
        raise _invalid_metadata(f"metadata can have at most {METADATA_MAX_KEYS} keys.")
    for key, item in value.items():
        if not key.strip() or len(key) > METADATA_MAX_KEY_LENGTH:
            raise _invalid_metadata(f"metadata keys must be 1-{METADATA_MAX_KEY_LENGTH} characters.")
        if isinstance(item, str):
            if len(item) > METADATA_MAX_TEXT_LENGTH:
                raise _invalid_metadata(
                    f"metadata['{key}'] is longer than {METADATA_MAX_TEXT_LENGTH} characters."
                )
        elif isinstance(item, float) and not math.isfinite(item):
            raise _invalid_metadata(f"metadata['{key}'] must be a finite number.")
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise _invalid_metadata(
                f"metadata['{key}'] must be text, a number, true/false or null (no nested objects or lists)."
            )
    return dict(value)


def _clean_name(name: Optional[str]) -> Optional[str]:
    return (name or "").strip() or None


@dataclass
class ImageInput:
    data: bytes
    filename: Optional[str] = None


@dataclass
class MatchResult:
    match: Optional[PersonMatch] = None
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
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.db = db
        self.metrics = metrics or Metrics()
        self.liveness = liveness
        self.embedding_model = check_embedding_model(db, engine)
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
            name=person["name"] or None,
            metadata=person["metadata"],
            similarity=round(best.similarity, 4),
        )
        if best.similarity < self.settings.match_threshold:
            return MatchResult(closest=candidate, reason="below_threshold")
        # With hundreds of people enrolled, refuse to guess between two close candidates:
        # "unknown" is recoverable (manual marking), a wrong name is not.
        if best.runner_up is not None and best.similarity - best.runner_up < self.settings.match_margin:
            return MatchResult(closest=candidate, reason="ambiguous")
        return MatchResult(match=candidate)

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

    def _count_face(self, outcome: str) -> None:
        self.metrics.inc("facematch_faces_total", endpoint="recognize", outcome=outcome)

    def recognize(
        self, data: bytes, check_liveness: Optional[bool] = None, include_closest: bool = False
    ) -> RecognizeResponse:
        """Identify every face in the image. Records nothing.

        `check_liveness` (default: the LIVENESS setting) checks each recognised face for a printed
        photo or a screen; one that fails is returned without its match (liveness_failed), so its
        id can't be used, e.g. to mark someone present. `include_closest` adds the nearest person
        for unknown faces (testing and tuning only).
        """
        start = time.perf_counter()
        if check_liveness is None:
            check_liveness = self.settings.liveness == "on"
        size, faces = self.analyze(data)
        results = []
        for face in faces:
            if face.embedding is None:
                results.append(RecognizedFace(**face.fields, match=None, unknown_reason="too_small"))
                self._count_face("too_small")
                continue

            found = self.match_embedding(face.embedding)
            if found.match is None:
                results.append(
                    RecognizedFace(
                        **face.fields,
                        match=None,
                        unknown_reason=found.reason,
                        closest=found.closest if include_closest else None,
                    )
                )
                self._count_face(found.reason or "unknown")
                continue

            # Anti-spoofing: only recognised faces return an id, so only they are checked.
            score = self.liveness_score(face) if check_liveness else None
            if score is not None and score < self.settings.liveness_threshold:
                results.append(
                    RecognizedFace(
                        **face.fields,
                        match=None,  # don't confirm who a suspected spoof shows
                        unknown_reason="liveness_failed",
                        liveness_score=score,
                    )
                )
                self._count_face("liveness_failed")
                continue

            results.append(RecognizedFace(**face.fields, match=found.match, liveness_score=score))
            self._count_face("matched")
        return RecognizeResponse(image=size, faces=results, processing_ms=self._elapsed_ms(start))

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
        return Person(
            id=row["id"],
            external_id=row["external_id"],
            name=row["name"] or None,
            metadata=row["metadata"],
            face_count=row["face_count"],
            created_at=row["created_at"],
        )

    def _external_id_taken(self, external_id: str, existing: Row) -> ServiceError:
        return ServiceError(
            409,
            "external_id_exists",
            f"external_id '{external_id}' is already enrolled.",
            {"person": self._person(existing).model_dump()},
        )

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
        name: Optional[str],
        external_id: Optional[str],
        images: List[ImageInput],
        allow_duplicate: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> EnrollResponse:
        """Enroll a new person. Only photos are required: name, external_id and metadata are optional,
        and the returned person id is what identifies them in every later response."""
        name = _clean_name(name)
        external_id = _clean_name(external_id)
        metadata = clean_metadata(metadata)
        if not images:
            raise ServiceError(422, "no_images", "Provide at least one image.")
        if external_id:
            existing = self.db.find_person_by_external_id(external_id)
            if existing:
                raise self._external_id_taken(external_id, existing)

        accepted, rejected, conflicts = self._collect_embeddings(
            images, person_id=None, allow_duplicate=allow_duplicate
        )
        if not accepted:
            self._raise_no_usable_face(rejected, conflicts)

        try:
            row = self.db.create_person(name, external_id, accepted, self._iso(time.time()), metadata)
        except INTEGRITY_ERRORS:  # concurrent enrollment with the same external_id
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

    def update_person(self, person_id: str, changes: Dict[str, Any]) -> Person:
        """Change some of name, external_id and metadata (metadata is replaced, not merged)."""
        if self.db.get_person(person_id) is None:
            raise ServiceError(404, "person_not_found", "Person not found.")
        changes = dict(changes)
        if "name" in changes:
            changes["name"] = _clean_name(changes["name"])
        if "metadata" in changes:
            changes["metadata"] = clean_metadata(changes["metadata"])
        if "external_id" in changes:
            external_id = changes["external_id"] = _clean_name(changes["external_id"])
            existing = self.db.find_person_by_external_id(external_id) if external_id else None
            if existing and existing["id"] != person_id:
                raise self._external_id_taken(external_id, existing)  # type: ignore[arg-type]
        try:
            updated = self.db.update_person(person_id, changes)
        except INTEGRITY_ERRORS:  # concurrent change to the same external_id
            raise ServiceError(
                409, "external_id_exists", f"external_id '{changes['external_id']}' is already enrolled."
            ) from None
        if not updated:
            raise ServiceError(404, "person_not_found", "Person not found.")
        return self.get_person(person_id)

    def delete_person(self, person_id: str) -> None:
        if not self.db.delete_person(person_id):
            raise ServiceError(404, "person_not_found", "Person not found.")
        self.gallery.remove(person_id)
