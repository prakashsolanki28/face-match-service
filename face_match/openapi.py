"""The OpenAPI description of the API, served at /openapi.json and exported by `face-match export-api`.

FastAPI generates the spec from the routes; this module adds what it can't infer: which endpoints need the
API key, Bearer auth as an alternative to `X-API-Key`, and `format: binary` on file fields so every client
tool (Postman, Insomnia, Swagger UI, code generators) shows a file picker.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI

REPO = "https://github.com/prakashsolanki28/face-match-service"

TAGS = [
    {"name": "system", "description": "Service info, health check and metrics."},
    {"name": "auth", "description": "Check that your API key works."},
    {"name": "faces", "description": "Detect and identify faces. Nothing is recorded."},
    {"name": "persons", "description": "Enroll, update and remove people and their photos."},
    {"name": "attendance", "description": "Mark attendance from camera frames and read it back."},
    {"name": "admin", "description": "Webhook deliveries and the audit log."},
]

INFO: Dict[str, Any] = {
    "license_info": {"name": "Apache-2.0", "identifier": "Apache-2.0"},
    "contact": {"name": "FaceMatch on GitHub", "url": REPO},
    "openapi_tags": TAGS,
}


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


def _enrich(spec: Dict[str, Any]) -> None:
    schemes = spec.setdefault("components", {}).setdefault("securitySchemes", {})
    if "APIKeyHeader" in schemes:
        schemes["APIKeyHeader"]["description"] = "The API key (the `API_KEY` setting). See docs/api-keys.md."
        schemes["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "The same key sent as `Authorization: Bearer <key>`.",
        }

    for operations in spec["paths"].values():
        for op in operations.values():
            if "security" in op:
                op["security"] = [{"APIKeyHeader": []}, {"BearerAuth": []}]
                note = "**Auth:** the API key, in the `X-API-Key` header (or `Authorization: Bearer`)."
            else:
                op["security"] = []
                note = "**Auth:** none (public)."
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


def install_openapi(app: FastAPI) -> None:
    """Make `app.openapi()` (and so /openapi.json) return the enriched spec."""
    generate = app.openapi

    def openapi() -> Dict[str, Any]:
        if app.openapi_schema is None:
            _enrich(generate())  # generate() caches its result in app.openapi_schema
        return app.openapi_schema

    app.openapi = openapi
