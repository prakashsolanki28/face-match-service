"""Application factory. Run with `face-match serve` (see cli.py)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import router
from .config import STATIC_DIR, Settings
from .engine import OpenCVFaceEngine
from .model_store import ensure_models
from .schemas import Health
from .service import FaceService, ServiceError
from .storage import Database

log = logging.getLogger("app")


def _error(status: int, code: str, message: str, details=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details}}
    return JSONResponse(status_code=status, content=jsonable_encoder(body))


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    models = ensure_models(settings.models_dir, download=settings.auto_download_models)
    engine = OpenCVFaceEngine(
        models["detector"], models["recognizer"], score_threshold=settings.detection_threshold
    )
    db = Database(settings.db_path)
    service = FaceService(settings, engine, db)
    stats = db.stats()
    log.info(
        "Engine %s ready; %d people / %d face embeddings loaded; auth %s",
        engine.name,
        stats["persons"],
        stats["embeddings"],
        "ON" if settings.api_key else "OFF (demo mode)",
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        db.close()

    app = FastAPI(
        title="FaceMatch",
        version=__version__,
        description=(
            "Face detection, recognition and attendance logging. "
            "Send images as multipart/form-data. Authenticate with the `X-API-Key` header."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError):
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        return _error(422, "validation_error", "Request validation failed.", exc.errors())

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        return _error(exc.status_code, "http_error", str(exc.detail))

    @app.get("/health", response_model=Health, tags=["system"])
    def health():
        stats = db.stats()
        return Health(
            status="ok",
            version=__version__,
            engine=engine.name,
            persons=stats["persons"],
            embeddings=stats["embeddings"],
            match_threshold=settings.match_threshold,
            min_face_size=settings.min_face_size,
            auth_required=bool(settings.api_key),
        )

    app.include_router(router)

    # Demo web UI. Mounted last so it never shadows API routes.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
