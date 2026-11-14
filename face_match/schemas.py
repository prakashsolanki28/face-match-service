"""Request/response models. These define the public API contract."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# Your own fields on a person, e.g. {"class": "7-A", "roll_no": 12}. Flat: text, numbers, true/false or null.
MetadataValue = Union[str, int, float, bool, None]
METADATA_DESCRIPTION = (
    "Your own fields for this person, as a JSON object of text, numbers, true/false or null "
    "(no nested objects or lists). Up to 50 keys of at most 64 characters; text values up to 1000 "
    "characters. Returned with the person and with every match, including to kiosks, so don't "
    "store secrets here."
)
METADATA_EXAMPLE = {"class": "7-A", "section": "B", "roll_no": 12}


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
    person_id: str = Field(description="The id returned when the person was enrolled.")
    external_id: Optional[str]
    name: Optional[str] = Field(description="Null when the person was enrolled without a name.")
    metadata: Dict[str, MetadataValue] = Field(
        default_factory=dict, description="The person's own fields (see Person.metadata)."
    )
    similarity: float = Field(description="Cosine similarity to the closest enrolled face.")


UnknownReason = Literal["too_small", "below_threshold", "ambiguous", "nobody_enrolled", "liveness_failed"]


class RecognizedFace(DetectedFace):
    match: Optional[PersonMatch] = Field(description="Null when the face is unknown.")
    unknown_reason: Optional[UnknownReason] = Field(
        default=None,
        description="Why match is null: face too small, closest person below MATCH_THRESHOLD, "
        "two people too close to call (MATCH_MARGIN), nobody enrolled, or the face looks like a "
        "photo or screen (liveness_failed).",
    )
    closest: Optional[PersonMatch] = Field(
        default=None,
        description="Only with ?closest=true, for testing and tuning: the nearest enrolled person when "
        "match is null. Never set for liveness_failed. Never treat it as a match.",
    )
    liveness_score: Optional[float] = Field(
        default=None,
        description="Probability that the face is live (not a printed photo or a screen), for recognised "
        "faces when the liveness check runs. Null for unknown faces.",
    )


class DetectResponse(BaseModel):
    image: ImageSize
    faces: List[DetectedFace]
    processing_ms: float


class RecognizeResponse(BaseModel):
    image: ImageSize
    faces: List[RecognizedFace]
    processing_ms: float


class Person(BaseModel):
    id: str = Field(
        description="FaceMatch's id for this person. Store it in your system: every match carries it "
        "as person_id."
    )
    external_id: Optional[str] = Field(description="Your system's ID, if you sent one.")
    name: Optional[str] = Field(description="Null when enrolled without a name.")
    metadata: Dict[str, MetadataValue] = Field(
        default_factory=dict, description=METADATA_DESCRIPTION, examples=[METADATA_EXAMPLE]
    )
    face_count: int
    created_at: str


class PersonUpdate(BaseModel):
    """Only the fields you send are changed."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(
        None, max_length=200, description="New name, or null to remove it.", examples=["Asha Verma"]
    )
    external_id: Optional[str] = Field(
        None,
        max_length=200,
        description="New ID from your system, or null to remove it.",
        examples=["STU0001"],
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="Replaces all of the person's own fields (it is not merged). Send {} or null to remove "
        "them. " + METADATA_DESCRIPTION.split(". ", 1)[1],
        examples=[{"class": "8-A", "section": "B", "roll_no": 12}],
    )


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
    embedding_model: str
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
