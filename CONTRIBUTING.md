# Contributing to FaceMatch

Thanks for helping! Bug reports, docs fixes, and pull requests are all welcome.

## Development setup

```bash
git clone https://github.com/prakashsolanki28/face-match-service.git
cd face-match-service
make dev            # creates .venv and installs FaceMatch with test/lint tools
make test           # runs the test suite (downloads the models once; no face images needed)
# against PostgreSQL too:  FACE_MATCH_TEST_DATABASE_URL=postgresql://user@localhost/facematch_test make test
make run            # http://127.0.0.1:8000
```

Python 3.11 or newer is required (patched FastAPI/Starlette releases need it). No GPU, compiler, or system libraries are needed.

## Before opening a pull request

- `make lint` and `make test` pass. CI runs the same checks on Python 3.11, 3.12 and 3.13, audits dependencies, runs CodeQL, and builds and scans the Docker image.
- New behaviour has a test. API changes keep `/api/v1` backwards compatible. Breaking changes need a new API version.
- After changing the API, run `face-match export-api` and commit the updated `api/` files (the tests check them).
- **Changes to recognition logic** (thresholds, enrollment checks, models) must come with evidence. Run
  `face-match evaluate` on a labelled photo set before and after, and paste both results into the PR.
  The service's promise is *no wrong names*: a change that adds wrong-name errors won't be merged.
- Update `docs/`, `README.md` and `CHANGELOG.md` when behaviour or configuration changes.

## Privacy rules for contributors

- **Never commit photos of real people**, datasets, `data/` folders or `.db` files. Tests use generated images
  and a fake face engine (`tests/fakes.py`), so no face images are needed or allowed in the repository.
- Never paste real face images into issues. Describe the image instead (size, number of faces, lighting).

## Project layout

```
face_match/
  cli.py          `face-match` command (serve, keys, backup, import, evaluate, export-api, …)
  main.py         FastAPI app factory: wiring, error handlers, CORS
  openapi.py      the OpenAPI spec: which endpoints need the API key, auth schemes, file fields
  api.py          /api/v1 routes and API-key auth
  service.py      business rules: enrollment checks, matching, attendance
  engine.py       FaceEngine interface + OpenCV YuNet/SFace implementation
  gallery.py      in-memory embedding index (cosine search)
  storage.py      database: SQLite, or PostgreSQL for several servers
  model_store.py  model download + SHA-256 verification
  webhook.py      signed outbound webhooks
  schemas.py      request/response models (the API contract)
  config.py       settings from environment / .env
  security.py     the API key check, rate limiting, overload gate
  liveness.py     MiniFASNet anti-spoofing
  observability.py  metrics, request ids, JSON logs
  maintenance.py  backups, retention, periodic workers
  tools/          bulk_import.py (stdlib only), evaluate.py, pilot_report.py, loadtest.py, export_api.py
api/              openapi.json and the Postman collection/environment (generated: face-match export-api)
tests/            API, production, multi-server, bulk-import and model smoke tests (fakes.py: model-free engine)
docs/             user documentation
```

## Adding a different model

Implement the `FaceEngine` protocol in `face_match/engine.py` (`detect` and `embed`), wire it in `main.py`, and
include `face-match evaluate` results. Check the licence of the model weights first: many popular face models
(for example InsightFace's pretrained packs) are for non-commercial research only and can't be bundled.
