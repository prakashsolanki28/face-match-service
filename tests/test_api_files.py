"""The importable API files in api/ (OpenAPI spec, Postman collection and environment)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from face_match import __version__
from face_match.tools import export_api

API_DIR = Path(__file__).resolve().parent.parent / "api"

# Every endpoint, and whether it needs the API key (one key gives full access).
EXPECTED_AUTH = {
    ("get", "/health"): False,
    ("get", "/"): False,
    ("get", "/metrics"): True,
    ("get", "/api/v1/auth/check"): True,
    ("post", "/api/v1/detect"): True,
    ("post", "/api/v1/recognize"): True,
    ("post", "/api/v1/persons"): True,
    ("get", "/api/v1/persons"): True,
    ("get", "/api/v1/persons/{person_id}"): True,
    ("patch", "/api/v1/persons/{person_id}"): True,
    ("delete", "/api/v1/persons/{person_id}"): True,
    ("post", "/api/v1/persons/{person_id}/faces"): True,
    ("put", "/api/v1/persons/{person_id}/faces"): True,
    ("get", "/api/v1/audit"): True,
}


@pytest.fixture(scope="module")
def files():
    return {name: json.loads(text) for name, text in export_api.render().items()}


def _operations(spec):
    return {(m, p): op for p, ops in spec["paths"].items() for m, op in ops.items()}


def _spec_shape(spec):
    """What matters to an API client, ignoring cosmetic differences between library versions."""
    shape = {"version": spec["info"]["version"], "schemas": {}}
    for (method, path), op in _operations(spec).items():
        body = op.get("requestBody", {}).get("content", {})
        fields = sorted(
            name
            for media in body.values()
            for name in export_api._resolve(spec, media["schema"]).get("properties", {})
        )
        shape[f"{method} {path}"] = (
            op["operationId"],
            op["summary"],
            bool(op["security"]),
            sorted(p["name"] for p in op.get("parameters", [])),
            fields,
        )
    for name, schema in spec["components"]["schemas"].items():
        shape["schemas"][name] = sorted(schema.get("properties", {}))
    return shape


def _requests(collection):
    for folder in collection["item"]:
        for item in folder["item"]:
            yield folder["name"], item


def _collection_shape(collection):
    return [
        (
            folder,
            item["name"],
            item["request"]["method"],
            item["request"]["url"]["raw"],
            [q["key"] for q in item["request"]["url"].get("query", [])],
            [f["key"] for f in item["request"].get("body", {}).get("formdata", [])],
        )
        for folder, item in _requests(collection)
    ]


def test_committed_files_are_up_to_date(files):
    """Fails after an API change until the files are regenerated: `face-match export-api`."""
    committed = {name: json.loads((API_DIR / name).read_text()) for name in files}
    assert _spec_shape(committed["openapi.json"]) == _spec_shape(files["openapi.json"])
    assert _collection_shape(committed[export_api.COLLECTION_FILE]) == _collection_shape(
        files[export_api.COLLECTION_FILE]
    )
    assert committed[export_api.ENVIRONMENT_FILE] == files[export_api.ENVIRONMENT_FILE]


def test_every_endpoint_documents_its_auth(files):
    spec = files["openapi.json"]
    key_auth = [{"APIKeyHeader": []}, {"BearerAuth": []}]
    assert set(_operations(spec)) == set(EXPECTED_AUTH)
    for key, op in _operations(spec).items():
        assert op["security"] == (key_auth if EXPECTED_AUTH[key] else []), key
        assert "**Auth:**" in op["description"], key
        assert "x-required-scope" not in op and "scope" not in op["description"], key


def test_exported_spec_is_what_the_server_serves(client, files):
    served = client.get("/openapi.json").json()
    exported = dict(files["openapi.json"])
    assert exported.pop("servers")[0]["url"] == export_api.DEFAULT_BASE_URL
    assert served == exported
    assert served["info"]["version"] == __version__


def test_file_fields_are_marked_binary_for_client_tools(files):
    spec = files["openapi.json"]
    body = spec["components"]["schemas"]["Body_enroll_person"]["properties"]
    assert body["images"]["items"]["format"] == "binary"
    assert spec["components"]["schemas"]["Body_recognize_faces"]["properties"]["image"]["format"] == "binary"
    assert "HTTPValidationError" not in json.dumps(spec)  # errors have one shape: ErrorResponse


def test_postman_collection_is_ready_to_send(files):
    collection = files[export_api.COLLECTION_FILE]
    assert collection["info"]["schema"] == export_api.POSTMAN_SCHEMA
    assert collection["auth"]["type"] == "apikey"
    assert {"key": "value", "value": "{{apiKey}}", "type": "string"} in collection["auth"]["apikey"]
    requests = list(_requests(collection))
    assert len(requests) == len(EXPECTED_AUTH)
    assert len({item["name"] for _, item in requests}) == len(requests)
    public = {"Health check", "Service info"}
    for _, item in requests:
        request = item["request"]
        assert request["url"]["raw"].startswith("{{baseUrl}}/")
        assert (request.get("auth") == {"type": "noauth"}) == (item["name"] in public)
        for field in request.get("body", {}).get("formdata", []):
            if field["key"] in ("image", "images"):
                assert field["type"] == "file"


def test_postman_saves_new_ids_and_runs_deletes_last(files):
    items = {item["name"]: item for _, item in _requests(files[export_api.COLLECTION_FILE])}
    script = "\n".join(items["Enroll a person"]["event"][0]["script"]["exec"])
    assert 'pm.collectionVariables.set("personId", pm.response.json().person.id)' in script
    assert items["Get a person"]["request"]["url"]["variable"][0]["value"] == "{{personId}}"
    for folder in files[export_api.COLLECTION_FILE]["item"]:
        methods = [item["request"]["method"] for item in folder["item"]]
        assert methods == sorted(methods, key=lambda m: m == "DELETE"), folder["name"]


def test_postman_environment_keeps_the_key_secret(files):
    values = {v["key"]: v for v in files[export_api.ENVIRONMENT_FILE]["values"]}
    assert values["apiKey"] == {"key": "apiKey", "value": "", "type": "secret", "enabled": True}
    assert values["baseUrl"]["value"] == export_api.DEFAULT_BASE_URL


def test_export_command_writes_and_checks(tmp_path, capsys):
    assert export_api.main(["--out", str(tmp_path), "--check"]) == 1
    assert export_api.main(["--out", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(export_api.render())
    assert export_api.main(["--out", str(tmp_path), "--check"]) == 0
    assert "up to date" in capsys.readouterr().out
