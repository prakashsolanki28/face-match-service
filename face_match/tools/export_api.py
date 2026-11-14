"""Write the API description files that other tools import (the api/ folder of the repository).

    face-match export-api               # write api/openapi.json and the Postman files
    face-match export-api --out ./x     # somewhere else
    face-match export-api --check       # exit 1 if the files are out of date (for CI)

Files:
  openapi.json                         OpenAPI 3.1: Postman, Insomnia, Bruno, Hoppscotch, Apidog,
                                       Swagger UI/Editor, and client code generators
  FaceMatch.postman_collection.json    Postman collection v2.1, every endpoint ready to send
  FaceMatch.postman_environment.json   Postman environment with `baseUrl` and a secret `apiKey`

Everything is generated from the code, so the files always match the server version they come from.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import __version__
from ..openapi import REPO, TAGS

DEFAULT_BASE_URL = "http://localhost:8000"
POSTMAN_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
COLLECTION_FILE = "FaceMatch.postman_collection.json"
ENVIRONMENT_FILE = "FaceMatch.postman_environment.json"
OPENAPI_FILE = "openapi.json"

# Postman scripts that keep the ids of things just created, so the next requests can use them.
_SAVE_ID = {
    "enroll_person": ("personId", "person.id", 201),
    "create_key": ("keyId", "key.id", 201),
}


class _NoModels:
    """Stands in for the face models: describing the API doesn't need them."""

    name = "none"
    embedding_size = 128

    def detect(self, image):
        raise RuntimeError("Face models are not loaded while exporting the API description.")

    def embed(self, image, face):
        raise RuntimeError("Face models are not loaded while exporting the API description.")


def build_openapi() -> Dict[str, Any]:
    """The same spec the server serves at /openapi.json, plus a `servers` entry for client tools."""
    from ..config import Settings
    from ..main import create_app

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Settings(data_dir=Path(tmp), log_level="warning"), engine=_NoModels())
        try:
            spec = copy.deepcopy(app.openapi())
        finally:
            app.state.service.db.close()
    servers = [
        {
            "url": DEFAULT_BASE_URL,
            "description": "Local server. Change it to yours, e.g. https://face.example.com",
        }
    ]
    ordered = {"openapi": spec.pop("openapi"), "info": spec.pop("info"), "servers": servers}
    ordered.update(spec)
    return ordered


# ---- Postman ---------------------------------------------------------------------------------------


def _variable_name(param: str) -> str:
    head, *rest = param.split("_")
    return head + "".join(word.capitalize() for word in rest)


def _example(schema: Dict[str, Any]) -> Optional[Any]:
    for candidate in (schema, *schema.get("anyOf", [])):
        if candidate.get("examples"):
            return candidate["examples"][0]
    return None


def _text(value: Any) -> str:
    return json.dumps(value) if isinstance(value, (bool, list, dict)) else str(value)


def _resolve(spec: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    ref = schema.get("$ref")
    return spec["components"]["schemas"][ref.rsplit("/", 1)[1]] if ref else schema


def _is_file(schema: Dict[str, Any]) -> bool:
    return schema.get("items", schema).get("format") == "binary"


def _field(
    name: str, schema: Dict[str, Any], required: bool, enabled_if_example: bool = True
) -> Dict[str, Any]:
    """One query parameter or form field. Optional ones start disabled unless they have an example."""
    example = _example(schema)
    value = example if example is not None else schema.get("default")
    entry: Dict[str, Any] = {"key": name, "value": "" if value is None else _text(value)}
    description = schema.get("description", "")
    entry["description"] = f"(Required) {description}".strip() if required else description
    if not required and (example is None or not enabled_if_example):
        entry["disabled"] = True
    return entry


def _url(path: str, op: Dict[str, Any]) -> Dict[str, Any]:
    segments = [f":{s[1:-1]}" if s.startswith("{") else s for s in path.strip("/").split("/") if s]
    query = [
        _field(
            p["name"],
            {**p["schema"], "description": p.get("description", "")},
            p.get("required", False),
            enabled_if_example=False,  # filters are off until you want them
        )
        for p in op.get("parameters", [])
        if p["in"] == "query"
    ]
    enabled = "&".join(f"{q['key']}={q['value']}" for q in query if not q.get("disabled"))
    url: Dict[str, Any] = {
        "raw": "{{baseUrl}}/" + "/".join(segments) + (f"?{enabled}" if enabled else ""),
        "host": ["{{baseUrl}}"],
        "path": segments,
    }
    if query:
        url["query"] = query
    variables = [
        {
            "key": p["name"],
            "value": "{{" + _variable_name(p["name"]) + "}}",
            "description": p.get("description", ""),
        }
        for p in op.get("parameters", [])
        if p["in"] == "path"
    ]
    if variables:
        url["variable"] = variables
    return url


def _body(spec: Dict[str, Any], op: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    content = op.get("requestBody", {}).get("content", {})
    if "multipart/form-data" in content:
        schema = _resolve(spec, content["multipart/form-data"]["schema"])
        required = set(schema.get("required", []))
        fields = []
        for name, prop in schema["properties"].items():
            if _is_file(prop):
                note = " Add one row per photo." if prop.get("type") == "array" else ""
                description = (prop.get("description", "") + note).strip()
                # No "src": an empty list makes Postman keep a blank entry next to the chosen file,
                # which it then reads as a folder (EISDIR). Postman's own importer leaves it out too.
                fields.append(
                    {"key": name, "type": "file", "description": f"(Required) {description}".strip()}
                )
            else:
                fields.append({**_field(name, prop, name in required), "type": "text"})
        return {"mode": "formdata", "formdata": fields}
    if "application/json" in content:
        schema = _resolve(spec, content["application/json"]["schema"])
        example = {name: _example(prop) for name, prop in schema["properties"].items()}
        return {
            "mode": "raw",
            "raw": json.dumps(example, indent=2),
            "options": {"raw": {"language": "json"}},
        }
    return None


def _request(spec: Dict[str, Any], path: str, method: str, op: Dict[str, Any]) -> Dict[str, Any]:
    request: Dict[str, Any] = {"method": method.upper(), "header": [], "url": _url(path, op)}
    body = _body(spec, op)
    if body:
        request["body"] = body
        if body["mode"] == "raw":
            request["header"].append({"key": "Content-Type", "value": "application/json"})
    if not op.get("security"):
        request["auth"] = {"type": "noauth"}
    request["description"] = op.get("description", "")
    item: Dict[str, Any] = {"name": op["summary"], "request": request, "response": []}
    if op["operationId"] in _SAVE_ID:
        variable, field, status = _SAVE_ID[op["operationId"]]
        item["event"] = [
            {
                "listen": "test",
                "script": {
                    "type": "text/javascript",
                    "exec": [
                        f"// Saves the new id in the collection variable `{variable}` for the next requests.",
                        f"if (pm.response.code === {status}) {{",
                        f'    pm.collectionVariables.set("{variable}", pm.response.json().{field});',
                        "}",
                    ],
                },
            }
        ]
    return item


def build_postman_collection(spec: Dict[str, Any]) -> Dict[str, Any]:
    folders = {
        tag["name"]: {"name": tag["name"].capitalize(), "description": tag["description"], "item": []}
        for tag in TAGS
    }
    path_variables: List[str] = []
    for path, operations in spec["paths"].items():
        for method, op in operations.items():
            folders[op["tags"][0]]["item"].append(_request(spec, path, method, op))
            for p in op.get("parameters", []):
                name = _variable_name(p["name"])
                if p["in"] == "path" and name not in path_variables:
                    path_variables.append(name)
    for folder in folders.values():  # deletes last, so running a folder top to bottom works
        folder["item"].sort(key=lambda item: item["request"]["method"] == "DELETE")
    description = "\n".join(
        [
            f"Every FaceMatch {__version__} endpoint, ready to send.",
            "",
            "1. Import `FaceMatch.postman_environment.json` too and select the **FaceMatch** environment. "
            "Set `baseUrl` to your server and `apiKey` to a key: a device key with only the scopes you need.",
            "2. Send **System → Health check**, then **Auth → Who am I** to check the key.",
            "3. For photos, open **Body → form-data** and choose a file in each `image` / `images` row.",
            "",
            "Enrolling a person or creating a key saves the new id in `personId` / `keyId`, so the requests "
            "that need an id work straight away. Each request says which permission it needs.",
            "",
            f"Keys and permissions: {REPO}/blob/main/docs/api-keys.md",
            "",
            "Generated from the OpenAPI spec by `face-match export-api`.",
        ]
    )
    return {
        "info": {
            "_postman_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{REPO}#postman-collection")),
            "name": "FaceMatch API",
            "description": description,
            "schema": POSTMAN_SCHEMA,
        },
        "auth": {
            "type": "apikey",
            "apikey": [
                {"key": "key", "value": "X-API-Key", "type": "string"},
                {"key": "value", "value": "{{apiKey}}", "type": "string"},
                {"key": "in", "value": "header", "type": "string"},
            ],
        },
        "item": [folder for folder in folders.values() if folder["item"]],
        "variable": [
            {"key": "baseUrl", "value": DEFAULT_BASE_URL, "description": "Your FaceMatch server."},
            *(
                {"key": name, "value": "", "description": "Set by earlier requests, or by hand."}
                for name in path_variables
            ),
        ],
    }


def build_postman_environment() -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{REPO}#postman-environment")),
        "name": "FaceMatch",
        "values": [
            {"key": "baseUrl", "value": DEFAULT_BASE_URL, "type": "default", "enabled": True},
            {"key": "apiKey", "value": "", "type": "secret", "enabled": True},
        ],
        "_postman_variable_scope": "environment",
    }


def render() -> Dict[str, str]:
    """File name -> contents, exactly as written to disk."""
    spec = build_openapi()
    files = {
        OPENAPI_FILE: spec,
        COLLECTION_FILE: build_postman_collection(spec),
        ENVIRONMENT_FILE: build_postman_environment(),
    }
    return {name: json.dumps(data, indent=2, ensure_ascii=False) + "\n" for name, data in files.items()}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="face-match export-api",
        description="Write openapi.json and a Postman collection and environment for this API.",
    )
    parser.add_argument("--out", default="api", help="Output folder (default: api).")
    parser.add_argument(
        "--check", action="store_true", help="Don't write; exit 1 if the files are missing or out of date."
    )
    args = parser.parse_args(argv)
    out = Path(args.out)
    files = render()
    if args.check:
        stale = [n for n, text in files.items() if not (out / n).exists() or (out / n).read_text() != text]
        if stale:
            print(f"Out of date in {out}/: {', '.join(stale)}. Run: face-match export-api --out {out}")
            return 1
        print(f"{out}/ is up to date.")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text)
        print(f"Wrote {out / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
