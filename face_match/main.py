"""Application factory. Run with `face-match serve` (see cli.py)."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import require, router
from .config import STATIC_DIR, Settings
from .engine import OpenCVFaceEngine
from .liveness import load_liveness
from .maintenance import MaintenanceWorker, Periodic
from .model_store import ensure_models
from .observability import Metrics, configure_logging, process_gauges, request_id_var
from .schemas import Health
from .security import ImageGate, KeyStore, RateLimiter
from .service import FaceService, ServiceError
from .storage import open_database
from .webhook import WebhookDispatcher

log = logging.getLogger("face_match")
access_log = logging.getLogger("face_match.access")


def _error(status: int, code: str, message: str, details=None, headers=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details}}
    return JSONResponse(status_code=status, content=jsonable_encoder(body), headers=headers)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level, settings.log_format)

    models = ensure_models(settings.models_dir, download=settings.auto_download_models)
    engine = OpenCVFaceEngine(
        models["detector"], models["recognizer"], score_threshold=settings.detection_threshold
    )
    liveness = load_liveness(settings, models)
    db = open_database(settings)
    metrics = Metrics()

    dispatcher = None
    if settings.webhook_url:
        dispatcher = WebhookDispatcher(
            db,
            settings.webhook_url,
            settings.webhook_secret,
            max_attempts=settings.webhook_max_attempts,
            base_delay=settings.webhook_retry_base_seconds,
            on_result=lambda outcome: metrics.inc("facematch_webhook_deliveries_total", outcome=outcome),
        )
    service = FaceService(
        settings,
        engine,
        db,
        metrics=metrics,
        liveness=liveness,
        on_event_queued=dispatcher.notify if dispatcher else None,
    )
    keys = KeyStore(db, settings.api_key)
    maintenance = MaintenanceWorker(db, settings, metrics)
    # Picks up enrollments made by other servers sharing the database (cheap when idle).
    gallery_sync = Periodic("gallery-sync", settings.gallery_sync_seconds, service.sync_gallery)

    stats = db.stats()
    log.info(
        "FaceMatch %s ready: engine %s, liveness %s, %d people / %d embeddings, auth %s",
        __version__,
        engine.name,
        liveness.name if liveness and settings.liveness == "on" else "off",
        stats["persons"],
        stats["embeddings"],
        "on" if keys.auth_enabled() else "OFF (localhost only)",
    )

    def gauges():
        current = db.stats()
        queue = db.webhook_counts()
        return {
            f'facematch_build_info{{version="{__version__}"}}': 1,
            "facematch_enrolled_persons": current["persons"],
            "facematch_enrolled_embeddings": current["embeddings"],
            "facematch_liveness_enabled": 1 if liveness and settings.liveness == "on" else 0,
            **{f'facematch_webhook_queue{{status="{k}"}}': v for k, v in queue.items()},
            **process_gauges(),
        }

    metrics.gauge_source(gauges)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if dispatcher:
            dispatcher.start()
        maintenance.start()
        gallery_sync.start()
        yield
        gallery_sync.stop()
        maintenance.stop()
        if dispatcher:
            dispatcher.stop()
        db.close()

    app = FastAPI(
        title="FaceMatch",
        version=__version__,
        description=(
            "Self-hosted face detection, face matching and attendance. Send images as multipart/form-data. "
            "Authenticate with the `X-API-Key` header (or `Authorization: Bearer <key>`)."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.metrics = metrics
    app.state.keys = keys
    app.state.limiter = RateLimiter(settings.rate_limit_per_minute, settings.auth_failures_per_minute)
    app.state.image_gate = ImageGate(settings.max_concurrent_images)
    app.state.dispatcher = dispatcher
    app.state.maintenance = maintenance

    @app.middleware("http")
    async def observe(request: Request, call_next):
        request_id = (request.headers.get("x-request-id") or uuid.uuid4().hex[:16])[:64]
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            elapsed = time.perf_counter() - start
            route = getattr(request.scope.get("route"), "path", None) or "ui"
            metrics.inc(
                "facematch_http_requests_total", method=request.method, route=route, status=str(status)
            )
            metrics.observe("facematch_http_request_duration_seconds", elapsed, route=route)
            access_log.info(
                "%s %s %d %.1fms",
                request.method,
                request.url.path,
                status,
                elapsed * 1000,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 1),
                    "client": request.client.host if request.client else None,
                },
            )
            request_id_var.reset(token)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError):
        return _error(exc.status_code, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        return _error(422, "validation_error", "Request validation failed.", exc.errors())

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        return _error(exc.status_code, "http_error", str(exc.detail))

    @app.get("/health", response_model=Health, tags=["system"])
    def health():
        """Liveness/readiness probe: also checks that the database answers."""
        current = db.stats()
        return Health(
            status="ok",
            version=__version__,
            engine=engine.name,
            persons=current["persons"],
            embeddings=current["embeddings"],
            match_threshold=settings.match_threshold,
            min_face_size=settings.min_face_size,
            liveness=bool(liveness and settings.liveness == "on"),
            auth_required=keys.auth_enabled(),
            login=db.has_active_users(),
        )

    @app.get(
        "/metrics",
        tags=["system"],
        response_class=PlainTextResponse,
        dependencies=[require("metrics")],
    )
    def prometheus_metrics():
        """Prometheus metrics. Needs a key with the `metrics` scope when auth is on."""
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    app.include_router(router)

    # Demo web UI. Mounted last so it never shadows API routes.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
