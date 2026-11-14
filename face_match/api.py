"""HTTP routes for /api/v1. Handlers are sync, so FastAPI runs them in a thread pool."""

from __future__ import annotations

import hmac
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.security import APIKeyHeader

from . import webhook
from .schemas import (
    AttendanceEvent,
    AttendanceSummary,
    DetectResponse,
    EnrollResponse,
    ErrorResponse,
    MarkAttendanceResponse,
    Person,
    RecognizeResponse,
)
from .service import FaceService, ImageInput, ServiceError

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_service(request: Request) -> FaceService:
    return request.app.state.service


def require_api_key(request: Request, api_key: Optional[str] = Depends(_api_key_header)) -> None:
    expected = request.app.state.settings.api_key
    if expected and not hmac.compare_digest(api_key or "", expected):
        raise ServiceError(401, "unauthorized", "Missing or invalid X-API-Key header.")


def _read(upload: UploadFile, service: FaceService) -> ImageInput:
    # Read one byte past the limit so oversize files are detected without loading them fully.
    data = upload.file.read(service.settings.max_upload_bytes + 1)
    return ImageInput(data=data, filename=upload.filename)


_errors = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)], responses=_errors)


# ---- faces -------------------------------------------------------------------


@router.post("/detect", response_model=DetectResponse, tags=["faces"])
def detect_faces(image: UploadFile = File(...), service: FaceService = Depends(get_service)):
    """Find faces in an image (no identity lookup)."""
    return service.detect(_read(image, service).data)


@router.post("/recognize", response_model=RecognizeResponse, tags=["faces"])
def recognize_faces(image: UploadFile = File(...), service: FaceService = Depends(get_service)):
    """Find faces and identify enrolled people. Does not record attendance."""
    return service.recognize(_read(image, service).data)


# ---- persons -----------------------------------------------------------------


@router.post(
    "/persons",
    response_model=EnrollResponse,
    status_code=201,
    tags=["persons"],
    responses={409: {"model": ErrorResponse}},
)
def enroll_person(
    name: str = Form(..., max_length=200),
    external_id: Optional[str] = Form(
        None, max_length=200, description="Your system's ID for this person, e.g. employee code."
    ),
    images: List[UploadFile] = File(..., description="1-10 clear, front-facing photos."),
    allow_duplicate: bool = Form(
        False, description="Enroll even if the face resembles someone already enrolled (twins)."
    ),
    service: FaceService = Depends(get_service),
):
    """Enroll a person. 3-5 photos (slightly different angles/lighting) give the best accuracy."""
    if len(images) > 10:
        raise ServiceError(422, "too_many_images", "Send at most 10 images per request.")
    return service.enroll(
        name, external_id, [_read(f, service) for f in images], allow_duplicate=allow_duplicate
    )


@router.get("/persons", response_model=List[Person], tags=["persons"])
def list_persons(
    external_id: Optional[str] = Query(None, description="Look up one person by your system's ID."),
    service: FaceService = Depends(get_service),
):
    return service.list_persons(external_id)


@router.get("/persons/{person_id}", response_model=Person, tags=["persons"])
def get_person(person_id: str, service: FaceService = Depends(get_service)):
    return service.get_person(person_id)


@router.post(
    "/persons/{person_id}/faces",
    response_model=EnrollResponse,
    tags=["persons"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def add_faces(
    person_id: str,
    images: List[UploadFile] = File(...),
    service: FaceService = Depends(get_service),
):
    """Add more photos to an existing person to improve recognition."""
    if len(images) > 10:
        raise ServiceError(422, "too_many_images", "Send at most 10 images per request.")
    return service.add_faces(person_id, [_read(f, service) for f in images])


@router.put(
    "/persons/{person_id}/faces",
    response_model=EnrollResponse,
    tags=["persons"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def replace_faces(
    person_id: str,
    images: List[UploadFile] = File(...),
    allow_duplicate: bool = Form(False),
    service: FaceService = Depends(get_service),
):
    """Replace all photos of a person (e.g. new school-year photos).

    All-or-nothing: if none of the new photos is usable, the current photos are kept.
    """
    if len(images) > 10:
        raise ServiceError(422, "too_many_images", "Send at most 10 images per request.")
    return service.replace_faces(
        person_id, [_read(f, service) for f in images], allow_duplicate=allow_duplicate
    )


@router.delete(
    "/persons/{person_id}",
    status_code=204,
    tags=["persons"],
    responses={404: {"model": ErrorResponse}},
)
def delete_person(person_id: str, service: FaceService = Depends(get_service)):
    """Delete a person and their face data. Past attendance events are kept."""
    service.delete_person(person_id)
    return Response(status_code=204)


# ---- attendance --------------------------------------------------------------


@router.post("/attendance", response_model=MarkAttendanceResponse, tags=["attendance"])
def mark_attendance(
    background: BackgroundTasks,
    image: UploadFile = File(...),
    device_id: Optional[str] = Form(None, max_length=100, description="Camera/kiosk identifier."),
    service: FaceService = Depends(get_service),
):
    """Recognise everyone in the frame and record attendance.

    Repeat sightings within ATTENDANCE_COOLDOWN_SECONDS return `already_marked`.
    """
    response, new_events = service.mark_attendance(_read(image, service).data, device_id)
    settings = service.settings
    if settings.webhook_url:
        for event in new_events:
            background.add_task(
                webhook.send,
                settings.webhook_url,
                settings.webhook_secret,
                "attendance.marked",
                event.model_dump(),
            )
    return response


@router.get("/attendance", response_model=List[AttendanceEvent], tags=["attendance"])
def list_attendance(
    day: Optional[date] = Query(None, alias="date", description="YYYY-MM-DD; omit for all days."),
    person_id: Optional[str] = None,
    limit: int = Query(200, ge=1, le=5000),
    service: FaceService = Depends(get_service),
):
    """Raw attendance events, newest first."""
    return service.list_attendance(day.isoformat() if day else None, person_id, limit)


@router.get("/attendance/summary", response_model=List[AttendanceSummary], tags=["attendance"])
def attendance_summary(
    day: Optional[date] = Query(None, alias="date", description="YYYY-MM-DD; defaults to today."),
    service: FaceService = Depends(get_service),
):
    """One row per person for the day: first seen (check-in), last seen (check-out)."""
    return service.attendance_summary(day.isoformat() if day else None)
