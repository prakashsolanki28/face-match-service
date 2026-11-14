"""HTTP routes for /api/v1. Handlers are sync, so FastAPI runs them in a thread pool.

Every route needs the API key (the API_KEY setting), which gives full access.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Annotated, Iterator, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, Header, Path, Query, Request, Response, UploadFile
from fastapi.security import APIKeyHeader

from .observability import request_id_var
from .schemas import (
    METADATA_DESCRIPTION,
    METADATA_EXAMPLE,
    AuditEntry,
    AuthCheck,
    DetectResponse,
    EnrollResponse,
    ErrorResponse,
    Person,
    PersonUpdate,
    RecognizeResponse,
)
from .security import Busy, RateLimited
from .service import FaceService, ImageInput, ServiceError, parse_metadata

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_service(request: Request) -> FaceService:
    return request.app.state.service


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limited(request: Request, exc: RateLimited) -> ServiceError:
    request.app.state.metrics.inc("facematch_rate_limited_total", reason="rate")
    return ServiceError(
        429,
        "rate_limited",
        f"Too many requests. Retry in {exc.retry_after} s.",
        headers={"Retry-After": str(exc.retry_after)},
    )


def authenticate(
    request: Request,
    x_api_key: Optional[str] = Depends(_api_key_header),
    authorization: Optional[str] = Header(None, include_in_schema=False),
) -> None:
    """Check the API key (`X-API-Key` or `Authorization: Bearer <key>`), then rate-limit the client IP."""
    state = request.app.state
    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    ip = client_ip(request)

    if state.auth.enabled and not state.auth.check(token):
        try:
            state.limiter.auth_failure(ip)
        except RateLimited as exc:
            raise _rate_limited(request, exc) from None
        raise ServiceError(401, "unauthorized", "Missing or invalid API key (X-API-Key header).")
    # Per client IP: every client shares the one key, so a per-key limit would throttle them all together.
    try:
        state.limiter.check(ip)
    except RateLimited as exc:
        raise _rate_limited(request, exc) from None


def audit(
    request: Request,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Record a change. The actor is `api_key`, or `open` on a local server without a key."""
    request.app.state.service.db.add_audit(
        "api_key" if request.app.state.auth.enabled else "open",
        action,
        target_type,
        target_id,
        details,
        client_ip(request),
        request_id_var.get(),
    )


@contextmanager
def image_slot(request: Request) -> Iterator[None]:
    try:
        with request.app.state.image_gate.slot():
            yield
    except Busy:
        request.app.state.metrics.inc("facematch_rate_limited_total", reason="busy")
        raise ServiceError(
            503, "busy", "Server is busy. Retry shortly.", headers={"Retry-After": "1"}
        ) from None


def _read(upload: UploadFile, service: FaceService) -> ImageInput:
    # Read one byte past the limit so oversize files are detected without loading them fully.
    data = upload.file.read(service.settings.max_upload_bytes + 1)
    return ImageInput(data=data, filename=upload.filename)


def _check_count(images: List[UploadFile]) -> None:
    if len(images) > 10:
        raise ServiceError(422, "too_many_images", "Send at most 10 images per request.")


_errors = {code: {"model": ErrorResponse} for code in (400, 401, 413, 422, 429, 503)}

# Shared parameter descriptions (they appear in /openapi.json and the Postman collection).
_PHOTO = "A photo: JPEG, PNG, WebP or BMP."
_PHOTOS = "1-10 photos: JPEG, PNG, WebP or BMP."
PersonId = Annotated[str, Path(description="The person's id, returned when they were enrolled.")]
Offset = Annotated[int, Query(ge=0, description="Skip this many results (for paging).")]

# Every /api/v1 route needs the API key.
router = APIRouter(prefix="/api/v1", responses=_errors, dependencies=[Depends(authenticate)])


# ---- faces -------------------------------------------------------------------


@router.post(
    "/detect",
    summary="Detect faces",
    response_model=DetectResponse,
    tags=["faces"],
)
def detect_faces(
    request: Request,
    image: UploadFile = File(..., description=_PHOTO),
    service: FaceService = Depends(get_service),
):
    """Find faces in an image (no identity lookup)."""
    with image_slot(request):
        return service.detect(_read(image, service).data)


@router.post(
    "/recognize",
    summary="Recognize faces",
    response_model=RecognizeResponse,
    tags=["faces"],
)
def recognize_faces(
    request: Request,
    image: UploadFile = File(..., description=_PHOTO),
    liveness: Optional[bool] = Query(
        None,
        description="Check recognised faces for a printed photo or a screen held up to the camera; "
        "one that fails is returned without its match (unknown_reason liveness_failed). Defaults to "
        "the server's LIVENESS setting. Use false for uploaded photos, which aren't re-captured.",
    ),
    closest: bool = Query(
        False,
        description="For testing and tuning: also return the nearest enrolled person for unknown faces.",
    ),
    service: FaceService = Depends(get_service),
):
    """Find faces and identify enrolled people. Does not record anything.

    Each recognised face has `match.person_id` (and your `external_id`): use these to act on the
    result, for example to mark people present in your own system.
    """
    with image_slot(request):
        return service.recognize(_read(image, service).data, check_liveness=liveness, include_closest=closest)


# ---- persons -----------------------------------------------------------------


@router.post(
    "/persons",
    summary="Enroll a person",
    response_model=EnrollResponse,
    status_code=201,
    tags=["persons"],
    responses={409: {"model": ErrorResponse}},
)
def enroll_person(
    request: Request,
    images: List[UploadFile] = File(..., description="1-10 clear, front-facing photos."),
    name: Optional[str] = Form(
        None, max_length=200, description="Full name. Optional.", examples=["Asha Verma"]
    ),
    external_id: Optional[str] = Form(
        None,
        max_length=200,
        description="Your system's ID for this person, e.g. admission number. Optional.",
        examples=["STU0001"],
    ),
    metadata: Optional[str] = Form(
        None,
        description="Optional. " + METADATA_DESCRIPTION + " Send it as JSON text in this form field.",
        examples=[json.dumps(METADATA_EXAMPLE)],
    ),
    allow_duplicate: bool = Form(
        False, description="Enroll even if the face resembles someone already enrolled (twins)."
    ),
    service: FaceService = Depends(get_service),
):
    """Enroll a person. Only photos are required: the response's `person.id` is returned as
    `person_id` in every match, so store it in your system. 3-5 photos (slightly different
    angles/lighting) give the best accuracy."""
    _check_count(images)
    fields = parse_metadata(metadata)  # check before the slower photo processing
    with image_slot(request):
        result = service.enroll(
            name,
            external_id,
            [_read(f, service) for f in images],
            allow_duplicate=allow_duplicate,
            metadata=fields,
        )
    audit(
        request,
        "person.create",
        "person",
        result.person.id,
        {"name": result.person.name, "external_id": result.person.external_id, "photos": result.accepted},
    )
    return result


@router.get(
    "/persons",
    summary="List people",
    response_model=List[Person],
    tags=["persons"],
)
def list_persons(
    response: Response,
    external_id: Optional[str] = Query(
        None, description="Look up one person by your system's ID.", examples=["EMP-001"]
    ),
    limit: Optional[int] = Query(None, ge=1, le=10000, description="Page size (default: everyone)."),
    offset: Offset = 0,
    service: FaceService = Depends(get_service),
):
    """People sorted by name. `X-Total-Count` holds the total for paging."""
    response.headers["X-Total-Count"] = str(service.db.count_persons())
    if external_id is not None:
        return service.list_persons(external_id)
    return [service._person(r) for r in service.db.list_persons(limit, offset)]


@router.get(
    "/persons/{person_id}",
    summary="Get a person",
    response_model=Person,
    tags=["persons"],
)
def get_person(person_id: PersonId, service: FaceService = Depends(get_service)):
    """One person, by the id returned at enrollment."""
    return service.get_person(person_id)


@router.patch(
    "/persons/{person_id}",
    summary="Update a person",
    response_model=Person,
    tags=["persons"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def update_person(
    request: Request,
    person_id: PersonId,
    changes: Annotated[PersonUpdate, Body()],
    service: FaceService = Depends(get_service),
):
    """Change a person's name, external_id or metadata. Only the fields you send change;
    `metadata` replaces all of the person's own fields. Photos are changed with /faces."""
    fields = changes.model_dump(include=changes.model_fields_set)
    person = service.update_person(person_id, fields)
    # Which fields changed, not their values: the audit log shouldn't copy personal data.
    audit(request, "person.update", "person", person_id, {"fields": sorted(fields)})
    return person


@router.post(
    "/persons/{person_id}/faces",
    summary="Add photos to a person",
    response_model=EnrollResponse,
    tags=["persons"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def add_faces(
    request: Request,
    person_id: PersonId,
    images: List[UploadFile] = File(..., description=_PHOTOS),
    service: FaceService = Depends(get_service),
):
    """Add more photos to an existing person to improve recognition."""
    _check_count(images)
    with image_slot(request):
        result = service.add_faces(person_id, [_read(f, service) for f in images])
    audit(request, "person.add_faces", "person", person_id, {"photos": result.accepted})
    return result


@router.put(
    "/persons/{person_id}/faces",
    summary="Replace a person's photos",
    response_model=EnrollResponse,
    tags=["persons"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def replace_faces(
    request: Request,
    person_id: PersonId,
    images: List[UploadFile] = File(..., description=_PHOTOS),
    allow_duplicate: bool = Form(
        False, description="Accept the photos even if they resemble someone else enrolled (twins)."
    ),
    service: FaceService = Depends(get_service),
):
    """Replace all photos of a person (e.g. new school-year photos).

    All-or-nothing: if none of the new photos is usable, the current photos are kept.
    """
    _check_count(images)
    with image_slot(request):
        result = service.replace_faces(
            person_id, [_read(f, service) for f in images], allow_duplicate=allow_duplicate
        )
    audit(request, "person.replace_faces", "person", person_id, {"photos": result.accepted})
    return result


@router.delete(
    "/persons/{person_id}",
    summary="Delete a person",
    status_code=204,
    tags=["persons"],
    responses={404: {"model": ErrorResponse}},
)
def delete_person(
    request: Request,
    person_id: PersonId,
    service: FaceService = Depends(get_service),
):
    """Delete a person and all their face data."""
    person = service.get_person(person_id)
    service.delete_person(person_id)
    audit(
        request,
        "person.delete",
        "person",
        person_id,
        {"name": person.name, "external_id": person.external_id},
    )
    return Response(status_code=204)


# ---- auth ----------------------------------------------------------------------------------------


@router.get("/auth/check", summary="Check API key", response_model=AuthCheck, tags=["auth"])
def check_api_key(request: Request):
    """Answers only when the request carries the right API key (otherwise 401)."""
    return AuthCheck(
        authenticated=request.app.state.auth.enabled, auth_required=request.app.state.auth.enabled
    )


# ---- audit log ------------------------------------------------------------------------


@router.get(
    "/audit",
    summary="Audit log",
    response_model=List[AuditEntry],
    tags=["admin"],
)
def list_audit(
    limit: int = Query(100, ge=1, le=1000, description="Page size."),
    offset: Offset = 0,
    action: Optional[str] = Query(None, description="Filter, e.g. person.* or person.update"),
    actor: Optional[str] = Query(None, description="Exact actor, e.g. api_key."),
    service: FaceService = Depends(get_service),
):
    """Who changed what: enrollments, updates, photo changes and deletions. Newest first."""
    return [AuditEntry(**row) for row in service.db.list_audit(limit, offset, action, actor)]
