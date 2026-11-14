"""HTTP routes for /api/v1. Handlers are sync, so FastAPI runs them in a thread pool.

Every route declares the permission (scope) it needs; see security.SCOPES.
"""

from __future__ import annotations

import hmac
import time
from contextlib import contextmanager
from datetime import date
from typing import Iterator, List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from .observability import request_id_var
from .schemas import (
    ApiKey,
    ApiKeyCreate,
    ApiKeyCreated,
    AttendanceEvent,
    AttendanceSummary,
    AuditEntry,
    DetectResponse,
    EnrollResponse,
    ErrorResponse,
    LoginRequest,
    MarkAttendanceResponse,
    Person,
    RecognizeResponse,
    User,
    UserCreate,
    UserUpdate,
    WebhookDelivery,
    WhoAmI,
)
from .security import OPEN_ACCESS, ROLES, Busy, Principal, RateLimited, check_password_with_timing_guard
from .service import FaceService, ImageInput, ServiceError

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
SESSION_COOKIE = "fm_session"
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


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
) -> Principal:
    """Resolve the caller from `X-API-Key` or `Authorization: Bearer <key>`, then rate-limit them."""
    state = request.app.state
    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    ip = client_ip(request)

    cookie = request.cookies.get(SESSION_COOKIE)

    if not state.keys.auth_enabled():
        principal = OPEN_ACCESS
    else:
        principal = state.keys.authenticate(token) if token else None
        if principal is None and not token and cookie:
            principal = state.keys.authenticate_session(cookie)
            # Cookies are sent automatically by browsers, so changes need proof the request came
            # from our own page: the per-session CSRF token in a header (synchroniser pattern).
            if principal is not None and request.method in _UNSAFE_METHODS:
                sent = request.headers.get("x-csrf-token", "")
                if not hmac.compare_digest(sent.encode(), (principal.csrf or "").encode()):
                    raise ServiceError(403, "csrf_failed", "Missing or invalid X-CSRF-Token header.")
        if principal is None:
            try:
                state.limiter.auth_failure(ip)
            except RateLimited as exc:
                raise _rate_limited(request, exc) from None
            raise ServiceError(401, "unauthorized", "Missing or invalid API key (X-API-Key header).")
    bucket = ip if principal is OPEN_ACCESS else (principal.key_id or principal.user_id or principal.name)
    try:
        state.limiter.check(bucket)
    except RateLimited as exc:
        raise _rate_limited(request, exc) from None
    request.state.principal = principal
    return principal


def require(scope: str):
    def check(principal: Principal = Depends(authenticate)) -> Principal:
        if not principal.allows(scope):
            raise ServiceError(403, "forbidden", f"This API key lacks the '{scope}' permission.")
        return principal

    return Depends(check)


def audit(
    request: Request,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[dict] = None,
    actor: Optional[str] = None,
) -> None:
    principal = getattr(request.state, "principal", OPEN_ACCESS)
    request.app.state.service.db.add_audit(
        actor or principal.label,
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


_errors = {code: {"model": ErrorResponse} for code in (400, 401, 403, 413, 422, 429, 503)}

router = APIRouter(prefix="/api/v1", responses=_errors)


# ---- faces -------------------------------------------------------------------


@router.post("/detect", response_model=DetectResponse, tags=["faces"], dependencies=[require("recognize")])
def detect_faces(
    request: Request, image: UploadFile = File(...), service: FaceService = Depends(get_service)
):
    """Find faces in an image (no identity lookup)."""
    with image_slot(request):
        return service.detect(_read(image, service).data)


@router.post(
    "/recognize", response_model=RecognizeResponse, tags=["faces"], dependencies=[require("recognize")]
)
def recognize_faces(
    request: Request,
    image: UploadFile = File(...),
    liveness: bool = Query(False, description="Also return liveness_score for each face."),
    service: FaceService = Depends(get_service),
):
    """Find faces and identify enrolled people. Does not record anything."""
    with image_slot(request):
        return service.recognize(_read(image, service).data, check_liveness=liveness)


# ---- persons -----------------------------------------------------------------


@router.post(
    "/persons",
    response_model=EnrollResponse,
    status_code=201,
    tags=["persons"],
    responses={409: {"model": ErrorResponse}},
    dependencies=[require("enroll")],
)
def enroll_person(
    request: Request,
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
    _check_count(images)
    with image_slot(request):
        result = service.enroll(
            name, external_id, [_read(f, service) for f in images], allow_duplicate=allow_duplicate
        )
    audit(
        request,
        "person.create",
        "person",
        result.person.id,
        {"name": result.person.name, "external_id": result.person.external_id, "photos": result.accepted},
    )
    return result


@router.get("/persons", response_model=List[Person], tags=["persons"], dependencies=[require("read")])
def list_persons(
    response: Response,
    external_id: Optional[str] = Query(None, description="Look up one person by your system's ID."),
    limit: Optional[int] = Query(None, ge=1, le=10000, description="Page size (default: everyone)."),
    offset: int = Query(0, ge=0),
    service: FaceService = Depends(get_service),
):
    """People sorted by name. `X-Total-Count` holds the total for paging."""
    response.headers["X-Total-Count"] = str(service.db.count_persons())
    if external_id is not None:
        return service.list_persons(external_id)
    return [service._person(r) for r in service.db.list_persons(limit, offset)]


@router.get("/persons/{person_id}", response_model=Person, tags=["persons"], dependencies=[require("read")])
def get_person(person_id: str, service: FaceService = Depends(get_service)):
    return service.get_person(person_id)


@router.post(
    "/persons/{person_id}/faces",
    response_model=EnrollResponse,
    tags=["persons"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    dependencies=[require("enroll")],
)
def add_faces(
    request: Request,
    person_id: str,
    images: List[UploadFile] = File(...),
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
    response_model=EnrollResponse,
    tags=["persons"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    dependencies=[require("enroll")],
)
def replace_faces(
    request: Request,
    person_id: str,
    images: List[UploadFile] = File(...),
    allow_duplicate: bool = Form(False),
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
    status_code=204,
    tags=["persons"],
    responses={404: {"model": ErrorResponse}},
    dependencies=[require("enroll")],
)
def delete_person(
    request: Request,
    person_id: str,
    erase_history: bool = Query(
        False,
        description="Also delete the person's attendance events and queued webhooks (right to erasure).",
    ),
    service: FaceService = Depends(get_service),
):
    """Delete a person and their face data. Attendance history is kept unless erase_history=true."""
    person = service.get_person(person_id)
    service.delete_person(person_id, erase_history=erase_history)
    audit(
        request,
        "person.delete",
        "person",
        person_id,
        {"name": person.name, "external_id": person.external_id, "erase_history": erase_history},
    )
    return Response(status_code=204)


# ---- attendance --------------------------------------------------------------


@router.post(
    "/attendance",
    response_model=MarkAttendanceResponse,
    tags=["attendance"],
    dependencies=[require("attendance")],
)
def mark_attendance(
    request: Request,
    image: UploadFile = File(...),
    device_id: Optional[str] = Form(None, max_length=100, description="Camera/kiosk identifier."),
    service: FaceService = Depends(get_service),
):
    """Recognise everyone in the frame and record attendance.

    Repeat sightings within ATTENDANCE_COOLDOWN_SECONDS return `already_marked`. With liveness on,
    a face that looks like a photo or screen returns `liveness_failed` and is not recorded.
    """
    with image_slot(request):
        response, _ = service.mark_attendance(_read(image, service).data, device_id)
    return response


@router.get(
    "/attendance", response_model=List[AttendanceEvent], tags=["attendance"], dependencies=[require("read")]
)
def list_attendance(
    day: Optional[date] = Query(None, alias="date", description="YYYY-MM-DD; omit for all days."),
    person_id: Optional[str] = None,
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    service: FaceService = Depends(get_service),
):
    """Raw attendance events, newest first."""
    return service.list_attendance(day.isoformat() if day else None, person_id, limit, offset)


@router.get(
    "/attendance/summary",
    response_model=List[AttendanceSummary],
    tags=["attendance"],
    dependencies=[require("read")],
)
def attendance_summary(
    day: Optional[date] = Query(None, alias="date", description="YYYY-MM-DD; defaults to today."),
    service: FaceService = Depends(get_service),
):
    """One row per person for the day: first seen (check-in), last seen (check-out)."""
    return service.attendance_summary(day.isoformat() if day else None)


# ---- admin: API keys -----------------------------------------------------------


def _key(row) -> ApiKey:
    return ApiKey(**{**row, "scopes": row["scopes"].split()})


@router.post(
    "/keys", response_model=ApiKeyCreated, status_code=201, tags=["admin"], dependencies=[require("admin")]
)
def create_key(body: ApiKeyCreate, request: Request, service: FaceService = Depends(get_service)):
    """Create a scoped API key, e.g. `{"name": "gate-1", "scopes": ["attendance"]}` for a kiosk.

    The token is returned only once.
    """
    row, token = service.create_api_key(body.name, body.scopes)
    request.app.state.keys.invalidate()
    audit(request, "key.create", "key", row["id"], {"name": row["name"], "scopes": row["scopes"].split()})
    return ApiKeyCreated(key=_key(row), token=token)


@router.get("/keys", response_model=List[ApiKey], tags=["admin"], dependencies=[require("admin")])
def list_keys(service: FaceService = Depends(get_service)):
    return [_key(r) for r in service.db.list_api_keys()]


@router.delete("/keys/{key_id}", status_code=204, tags=["admin"], dependencies=[require("admin")])
def revoke_key(key_id: str, request: Request, service: FaceService = Depends(get_service)):
    """Revoke a key immediately."""
    service.revoke_api_key(key_id)
    request.app.state.keys.invalidate()
    audit(request, "key.revoke", "key", key_id)
    return Response(status_code=204)


# ---- admin: webhook deliveries ---------------------------------------------------


@router.get(
    "/webhooks", response_model=List[WebhookDelivery], tags=["admin"], dependencies=[require("admin")]
)
def list_webhooks(
    status: Optional[Literal["pending", "delivered", "failed"]] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    service: FaceService = Depends(get_service),
):
    """Webhook delivery queue, newest first. Failed deliveries can be retried."""
    return [WebhookDelivery(**row) for row in service.db.list_webhooks(status, limit, offset)]


@router.post(
    "/webhooks/{delivery_id}/retry", status_code=202, tags=["admin"], dependencies=[require("admin")]
)
def retry_webhook(delivery_id: int, request: Request, service: FaceService = Depends(get_service)):
    service.retry_webhook(delivery_id)
    audit(request, "webhook.retry", "webhook", str(delivery_id))
    return Response(status_code=202)


# ---- auth: browser sessions for the admin UI ----------------------------------------------


def _whoami(principal: Principal, expires_at: Optional[float] = None) -> WhoAmI:
    return WhoAmI(
        kind=principal.kind,
        name=principal.name,
        role=principal.role,
        scopes=sorted(principal.scopes),
        csrf_token=principal.csrf,
        expires_at=expires_at,
    )


@router.post("/auth/login", response_model=WhoAmI, tags=["auth"])
def login(body: LoginRequest, request: Request, service: FaceService = Depends(get_service)):
    """Sign in to the admin UI. Sets an HttpOnly session cookie and returns the CSRF token."""
    state, settings = request.app.state, service.settings
    username = body.username.strip()
    user = service.db.find_user(username)
    if not check_password_with_timing_guard(body.password, user):
        audit(request, "auth.login_failed", "user", user["id"] if user else None, actor=f"user:{username}")
        try:
            state.limiter.auth_failure(client_ip(request))
            state.limiter.auth_failure("user:" + username.lower())
        except RateLimited as exc:
            raise _rate_limited(request, exc) from None
        raise ServiceError(401, "invalid_credentials", "Wrong username or password.")

    ttl = settings.session_hours * 3600
    token, csrf, expires_at = service.db.create_session(
        user["id"], ttl, client_ip(request), request.headers.get("user-agent")
    )
    service.db.update_user(user["id"], last_login_at=service._iso(time.time()))
    principal = Principal(
        name=user["username"],
        scopes=ROLES[user["role"]],
        kind="user",
        user_id=user["id"],
        role=user["role"],
        csrf=csrf,
    )
    request.state.principal = principal
    audit(request, "auth.login", "user", user["id"])
    secure = settings.session_cookie_secure == "true" or (
        settings.session_cookie_secure == "auto" and request.url.scheme == "https"
    )
    response = JSONResponse(jsonable_encoder(_whoami(principal, expires_at)))
    response.set_cookie(
        SESSION_COOKIE, token, max_age=int(ttl), httponly=True, samesite="strict", secure=secure, path="/"
    )
    return response


@router.post("/auth/logout", status_code=204, tags=["auth"])
def logout(request: Request, service: FaceService = Depends(get_service)):
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        principal = request.app.state.keys.authenticate_session(cookie)
        if principal:
            request.state.principal = principal
            audit(request, "auth.logout", "user", principal.user_id)
        service.db.delete_session(cookie)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/auth/me", response_model=WhoAmI, tags=["auth"])
def whoami(principal: Principal = Depends(authenticate)):
    """Who the current key or session belongs to, and what it may do."""
    return _whoami(principal)


# ---- admin: users ----------------------------------------------------------------------------


def _user(row) -> User:
    return User(**{k: row[k] for k in User.model_fields})


@router.get("/users", response_model=List[User], tags=["admin"], dependencies=[require("admin")])
def list_users(service: FaceService = Depends(get_service)):
    return [_user(r) for r in service.db.list_users()]


@router.post("/users", response_model=User, status_code=201, tags=["admin"], dependencies=[require("admin")])
def create_user(body: UserCreate, request: Request, service: FaceService = Depends(get_service)):
    row = service.create_user(body.username, body.password, body.role)
    request.app.state.keys.invalidate()  # auth turns on once the first user exists
    audit(request, "user.create", "user", row["id"], {"username": row["username"], "role": row["role"]})
    return _user(row)


@router.patch("/users/{user_id}", response_model=User, tags=["admin"], dependencies=[require("admin")])
def update_user(
    user_id: str, body: UserUpdate, request: Request, service: FaceService = Depends(get_service)
):
    """Change role, reset password, or disable/enable. Password resets and disabling sign the user out."""
    row = service.update_user(user_id, role=body.role, password=body.password, disabled=body.disabled)
    details = {"role": body.role, "disabled": body.disabled, "password_reset": body.password is not None}
    audit(
        request, "user.update", "user", user_id, {k: v for k, v in details.items() if v not in (None, False)}
    )
    return _user(row)


@router.delete("/users/{user_id}", status_code=204, tags=["admin"], dependencies=[require("admin")])
def delete_user(user_id: str, request: Request, service: FaceService = Depends(get_service)):
    row = service.delete_user(user_id)
    audit(request, "user.delete", "user", user_id, {"username": row["username"]})
    return Response(status_code=204)


# ---- admin: audit log ------------------------------------------------------------------------


@router.get("/audit", response_model=List[AuditEntry], tags=["admin"], dependencies=[require("admin")])
def list_audit(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    action: Optional[str] = Query(None, description="Filter, e.g. person.* or auth.login_failed"),
    actor: Optional[str] = Query(None, description="Exact actor, e.g. user:asha or key:gate-1#3f9c…"),
    service: FaceService = Depends(get_service),
):
    """Who changed what: enrollments, deletions, keys, users, logins. Newest first."""
    return [AuditEntry(**row) for row in service.db.list_audit(limit, offset, action, actor)]
