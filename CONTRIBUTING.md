# Contributing to FaceMatch

Thanks for helping! Bug reports, docs fixes, and pull requests are all welcome.

## Development setup

```bash
git clone https://github.com/prakashsolanki28/face-match-service.git
cd face-match-service
make dev            # creates .venv and installs FaceMatch with test/lint tools
make test           # runs the test suite (downloads models and two sample images once)
make run            # http://127.0.0.1:8000
```

Python 3.9 or newer is required. No GPU, compiler, or system libraries are needed.

## Before opening a pull request

- `make lint` and `make test` pass. CI runs the same checks on Python 3.9, 3.11 and 3.13 and builds the Docker image.
- New behaviour has a test. API changes keep `/api/v1` backwards compatible. Breaking changes need a new API version.
- **Changes to recognition logic** (thresholds, enrollment checks, models) must come with evidence. Run
  `face-match evaluate` on a labelled photo set before and after, and paste both results into the PR.
  The service's promise is *no wrong names*: a change that adds wrong-name errors won't be merged.
- Update `docs/`, `README.md` and `CHANGELOG.md` when behaviour or configuration changes.

## Privacy rules for contributors

- **Never commit photos of real people**, datasets, `data/` folders or `.db` files. Tests download public
  OpenCV sample images at run time instead.
- Never paste real face images into issues. Describe the image instead (size, number of faces, lighting).

## Project layout

```
face_match/
  cli.py          `face-match` command (serve, download-models, import, evaluate)
  main.py         FastAPI app factory: wiring, error handlers, CORS, static UI
  api.py          /api/v1 routes and API-key auth
  service.py      business rules: enrollment checks, matching, attendance
  engine.py       FaceEngine interface + OpenCV YuNet/SFace implementation
  gallery.py      in-memory embedding index (cosine search)
  storage.py      SQLite repository
  model_store.py  model download + SHA-256 verification
  webhook.py      signed outbound webhooks
  schemas.py      request/response models (the API contract)
  config.py       settings from environment / .env
  static/         demo web UI (single file, no build step)
  tools/          bulk_import.py (stdlib only), evaluate.py
tests/            API, bulk-import and dataset accuracy tests
docs/             user documentation
```

## Adding a different model

Implement the `FaceEngine` protocol in `face_match/engine.py` (`detect` and `embed`), wire it in `main.py`, and
include `face-match evaluate` results. Check the licence of the model weights first: many popular face models
(for example InsightFace's pretrained packs) are for non-commercial research only and can't be bundled.
