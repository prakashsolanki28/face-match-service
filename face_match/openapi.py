"""The OpenAPI description of the API, served at /openapi.json and exported by `face-match export-api`.

FastAPI generates the spec from the routes; this module adds what it can't infer: the permission
(scope) each endpoint needs, Bearer auth as an alternative to `X-API-Key`, and `format: binary` on
file fields so every client tool (Postman, Insomnia, Swagger UI, code generators) shows a file picker.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.routing import APIRoute, APIRouter

REPO = "https://github.com/prakashsolanki28/face-match-service"

TAGS = [
    {"name": "system", "description": "Service info, health check and metrics."},
    {"name": "auth", "description": "Check which API key you are using and what it may do."},
    {"name": "faces", "description": "Detect and identify faces. Nothing is recorded."},
    {"name": "persons", "description": "Enroll, update and remove people and their photos."},
    {"name": "attendance", "description": "Mark attendance from camera frames and read it back."},
    {"name": "admin", "description": "API keys, webhook deliveries and the audit log (admin key only)."},
]

INFO: Dict[str, Any] = {
    "license_info": {"name": "Apache-2.0", "identifier": "Apache-2.0"},
    "contact": {"name": "FaceMatch on GitHub", "url": REPO},
    "openapi_tags": TAGS,
}

_ANY_KEY = "any"


def _required_scope(route: APIRoute) -> Optional[str]:
    for dependency in route.dependencies:
        scope = getattr(dependency.dependency, "required_scope", None)
        if scope:
            return scope
    return None


def _mark_files(node: Any) -> None:
    """Add `format: binary` next to OpenAPI 3.1's `contentMediaType`, which many tools don't read yet."""
    if isinstance(node, dict):
        if node.get("type") == "string" and "contentMediaType" in node:
            node.setdefault("format", "binary")
        for value in node.values():
            _mark_files(value)
    elif isinstance(node, list):
        for value in node:
            _mark_files(value)


def _enrich(spec: Dict[str, Any], routes: Dict[str, APIRoute]) -> None:
    schemes = spec.setdefault("components", {}).setdefault("securitySchemes", {})
    if "APIKeyHeader" in schemes:
        schemes["APIKeyHeader"]["description"] = (
            "The admin key (`API_KEY` setting) or a device key (`fm_…`). See docs/api-keys.md."
        )
        schemes["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "The same key sent as `Authorization: Bearer <key>`.",
        }

    for operations in spec["paths"].values():
        for op in operations.values():
            if "security" in op:
                route = routes.get(op["operationId"])
                scope = (_required_scope(route) if route else None) or _ANY_KEY
                op["security"] = [{"APIKeyHeader": []}, {"BearerAuth": []}]
                op["x-required-scope"] = scope
                note = (
                    "**Permission:** any valid API key."
                    if scope == _ANY_KEY
                    else f"**Permission:** an API key with the `{scope}` scope (or the admin key)."
                )
            else:
                op["security"] = []
                note = "**Permission:** none (public)."
            op["description"] = f"{op.get('description', '').strip()}\n\n{note}".strip()
            # Errors use one shape everywhere ({"error": {...}}), including validation errors.
            for response in op.get("responses", {}).values():
                schema = response.get("content", {}).get("application/json", {}).get("schema", {})
                if schema.get("$ref") == "#/components/schemas/HTTPValidationError":
                    schema["$ref"] = "#/components/schemas/ErrorResponse"

    schemas = spec["components"].get("schemas", {})
    if "HTTPValidationError" not in str(spec["paths"]):
        schemas.pop("HTTPValidationError", None)
        schemas.pop("ValidationError", None)
    _mark_files(spec)


def install_openapi(app: FastAPI, *routers: APIRouter) -> None:
    """Make `app.openapi()` (and so /openapi.json) return the enriched spec.

    Pass the routers included in `app`. Operation ids are route names (see main.py), which is how
    each operation is matched to the route that declares its scope.
    """
    routes = {
        route.name: route
        for route in [*app.routes, *(r for router in routers for r in router.routes)]
        if isinstance(route, APIRoute)
    }
    generate = app.openapi

    def openapi() -> Dict[str, Any]:
        if app.openapi_schema is None:
            _enrich(generate(), routes)  # generate() caches its result in app.openapi_schema
        return app.openapi_schema

    app.openapi = openapi
