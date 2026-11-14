# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

FaceMatch is a self-hosted, API-only face detection and 1:N face matching service (FastAPI + OpenCV DNN, no GPU).
Python 3.11+ is required.

## Commands

```bash
make dev        # create .venv and install -e ".[dev]"
make test       # .venv/bin/pytest -q (downloads the ONNX models into ./models once)
make lint       # ruff check + ruff format --check (CI runs both)
make format     # auto-fix
make run        # face-match serve → http://127.0.0.1:8000
```

- Single test: `.venv/bin/pytest tests/test_api.py::test_name -q`
- Whole suite against PostgreSQL (CI does this too; needs the `postgres` extra):
  `FACE_MATCH_TEST_DATABASE_URL=postgresql://user@localhost/facematch_test make test`.
  Each test drops and recreates the `public` schema. Tests marked `sqlite_only` (in `conftest.py`) are skipped then.
- `tests/test_real_models.py` is the only test using the real models; everything else uses fakes.
- After any API change (routes, schemas, auth, examples): run `face-match export-api` and commit the regenerated
  `api/` files. `tests/test_api_files.py` fails if they are stale, and its `EXPECTED_AUTH` table lists every endpoint.

## Architecture

Request flow: `api.py` (routes, API-key auth, rate limiting, `image_slot` overload gate, audit) → `service.py`
`FaceService` (all business rules) → `engine.py` (detect + embed), `liveness.py`, `gallery.py`, `storage.py`.

- **`main.py` `create_app(settings, engine, liveness)`** is the single wiring point. Engines are injectable; when
  `engine` is None it downloads/verifies models (`model_store.py`, pinned SHA-256) and builds the OpenCV engine.
  Errors from `ServiceError` become `{"error": {"code", "message", "details"}}`; error codes are a stable contract.
- **Matching rules** live in `FaceService.match_embedding`: best cosine similarity must exceed `MATCH_THRESHOLD`
  *and* beat the runner-up person by `MATCH_MARGIN`, otherwise the face is unknown with an `unknown_reason`.
  Liveness only runs on recognised faces; a spoof yields `liveness_failed` instead of a match.
- **Enrollment checks** (`_collect_embeddings`): rejects no/multiple/small faces, photos inconsistent with each other
  (below match threshold vs. an anchor), and near-duplicates of other people (`DUPLICATE_THRESHOLD`).
- **Gallery** is an in-memory numpy matrix rebuilt from the DB; the DB is the source of truth. For multiple servers,
  every enrollment change or deletion appends a row to the `gallery_changes` table, and `sync_gallery()` (run every `GALLERY_SYNC_SECONDS` via
  `maintenance.Periodic`) replays changes since the last seen revision. Person details (name, metadata) on a match
  are read from the DB, not the gallery, so updates are visible immediately. Keep this invariant when adding writes.
- **`storage.Database`** supports SQLite (default, WAL, one locked connection) and PostgreSQL (`DATABASE_URL`),
  sharing one SQL schema template (`_TABLES` formatted with per-dialect types). Schema changes need migrations for
  both dialects (see the `metadata` column `ALTER TABLE` pattern) and a `SCHEMA_VERSION` bump.
- **Config**: `config.Settings` is a frozen dataclass built by `Settings.from_env()` (plus a minimal `.env`
  loader). Every variable is documented in `docs/configuration.md`; keep them in sync. `cli.check_auth` refuses to
  bind a non-loopback host without `API_KEY` unless `ALLOW_NO_AUTH=true`.
- **OpenAPI**: `openapi.py` post-processes FastAPI's spec (which endpoints need the key, file fields).
  `tools/export_api.py` builds the spec without models (`_NoModels`) and generates the Postman files from it.
- `tools/bulk_import.py` is stdlib-only by design (it talks to the HTTP API); don't add dependencies to it.

## Testing approach

`tests/fakes.py` provides `FakeEngine`/`FakeLiveness` that decode identities from generated PNGs: each coloured square
is a "face" (green channel = person id, blue = 100 + photo variant, side = face size). Ids 100–149 are near-twins of
0–49 (exercise `MATCH_MARGIN`), 150–199 are look-alikes (exercise `DUPLICATE_THRESHOLD`). Build images with
`face_png(Spec(person, ...))`. Use the `client` / `make_client(**settings_overrides)` fixtures from `conftest.py`.
`tests/test_multiserver.py` runs two app instances on one DB to test sync.

## Project rules (from CONTRIBUTING.md)

- Never commit photos of real people, datasets, `data/`, `models/` or `.db` files; tests must use generated images.
- Keep `/api/v1` backwards compatible; breaking changes need a new API version.
- Changes to recognition logic (thresholds, enrollment checks, models) need `face-match evaluate` results before and
  after. The core promise is *no wrong names*: prefer returning unknown over a wrong match.
- New face models implement the `FaceEngine` protocol in `engine.py` (`detect`, `embed`) and must have commercially
  usable weight licences (e.g. InsightFace packs are not).
- Update `docs/`, `README.md` and `CHANGELOG.md` when behaviour or configuration changes.
