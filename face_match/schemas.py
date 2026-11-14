"""Request/response models. These define the public API contract."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ImageSize(BaseModel):
    width: int
    height: int


class Box(BaseModel):
    x: int
    y: int
    width: int
    height: int


class Point(BaseModel):
    x: float
    y: float


class DetectedFace(BaseModel):
    box: Box = Field(description="Bounding box in original image pixels.")
    score: float = Field(description="Detector confidence, 0-1.")
    landmarks: List[Point] = Field(
        description="Right eye, left eye, nose tip, right mouth corner, left mouth corner."
    )


class PersonMatch(BaseModel):
    person_id: str
    external_id: Optional[str]
    name: str
    similarity: float = Field(description="Cosine similarity to the closest enrolled face.")


UnknownReason = Literal["too_small", "below_threshold", "ambiguous", "nobody_enrolled", "liveness_failed"]


class RecognizedFace(DetectedFace):
    match: Optional[PersonMatch] = Field(description="Null when the face is unknown.")
    unknown_reason: Optional[UnknownReason] = Field(
        default=None,
        description="Why match is null: face too small, closest person below MATCH_THRESHOLD, "
        "two people too close to call (MATCH_MARGIN), or nobody enrolled.",
    )
    closest: Optional[PersonMatch] = Field(
        default=None,
        description="/recognize only, for testing and tuning: the nearest enrolled person when "
        "match is null. Never returned by /attendance, so kiosks don't reveal near-misses.",
    )
    liveness_score: Optional[float] = Field(
        default=None,
        description="Probability that the face is live (not a photo or screen). Set on /attendance when "
        "liveness is on, and on /recognize with ?liveness=true.",
    )


class AttendanceResult(BaseModel):
    status: Literal["marked", "already_marked", "unknown", "too_small", "liveness_failed"]
    event_id: Optional[int] = None
    timestamp: Optional[str] = Field(
        default=None, description="New event time, or the earlier event when already_marked."
    )


class AttendanceFace(RecognizedFace):
    attendance: AttendanceResult


class DetectResponse(BaseModel):
    image: ImageSize
    faces: List[DetectedFace]
    processing_ms: float


class RecognizeResponse(BaseModel):
    image: ImageSize
    faces: List[RecognizedFace]
    processing_ms: float


class MarkAttendanceResponse(BaseModel):
    image: ImageSize
    device_id: Optional[str]
    faces: List[AttendanceFace]
    processing_ms: float


class Person(BaseModel):
    id: str
    external_id: Optional[str]
    name: str
    face_count: int
    created_at: str


class RejectedImage(BaseModel):
    index: int
    filename: Optional[str]
    reason: Literal[
        "invalid_image",
        "too_large",
        "no_face",
        "multiple_faces",
        "face_too_small",
        "inconsistent_identity",
        "matches_other_person",
    ]


class EnrollResponse(BaseModel):
    person: Person
    accepted: int
    rejected: List[RejectedImage]


class AttendanceEvent(BaseModel):
    id: int
    person_id: str
    external_id: Optional[str]
    name: str
    similarity: float
    device_id: Optional[str]
    timestamp: str
    day: str


class AttendanceSummary(BaseModel):
    person_id: str
    external_id: Optional[str]
    name: str
    first_seen: str
    last_seen: str
    events: int


class WebhookDelivery(BaseModel):
    id: int
    event_id: str
    event: str
    status: Literal["pending", "delivered", "failed"]
    attempts: int
    last_error: Optional[str]
    created_at: float
    delivered_at: Optional[float]
    next_attempt_at: float


class AuthCheck(BaseModel):
    authenticated: bool = Field(description="True: the request carried the correct API key.")
    auth_required: bool = Field(description="False only on a local server running without API_KEY.")


class AuditEntry(BaseModel):
    id: int
    ts: float
    created_at: str
    actor: str
    action: str
    target_type: Optional[str]
    target_id: Optional[str]
    details: Optional[dict]
    ip: Optional[str]
    request_id: Optional[str]


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    engine: str
    persons: int
    embeddings: int
    match_threshold: float
    min_face_size: int
    liveness: bool
    auth_required: bool


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Optional[object] = None


class ErrorResponse(BaseModel):
    error: ErrorBody
