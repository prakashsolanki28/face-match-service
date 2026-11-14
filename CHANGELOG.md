# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-11-14

FaceMatch is now an API-only service.

### Removed (breaking)
- The built-in web UI (`/`, `/index.html`) and the kiosk page (`/kiosk.html`). Build kiosks and admin screens in your
  own apps against the API; docs/keys-and-audit.md includes a sample kiosk client.
- Sign-in accounts: users, roles, login/logout, session cookies and CSRF (`/api/v1/auth/login`,
  `/api/v1/auth/logout`, `/api/v1/users`, `face-match users`, `SESSION_*` settings). Access is by the admin
  `API_KEY` and scoped device keys. `/api/v1/auth/me` remains and describes the calling key.
- The `login` field from `/health`.
- Datasets, sample photos and dummy data from the project and its tests.

### Added
- docs/api-keys.md: creating keys, scopes, recommended keys per client, Postman, rotation and revocation.
- Keys created or revoked with the `face-match keys` command are now recorded in the audit log (actor `cli`).

### Changed
- Interactive API docs (`/docs`, `/redoc`) are off by default; enable them with `API_DOCS=true`. The OpenAPI spec
  is always at `/openapi.json`, and `GET /` returns a small service description.
- Tests use generated images and a model-free fake engine (`tests/fakes.py`) instead of photos of people. A
  separate smoke test checks that the real models load and run. Recognition accuracy is measured with
  `face-match evaluate` on your own photos.

## [1.2.0] - 2026-11-14

### Added
- **Sign-in accounts** for the web UI with roles `admin`, `operator` and `viewer`: scrypt password hashes, HttpOnly
  SameSite=Strict session cookies, a per-session CSRF token, throttled login attempts, and sign-out everywhere on
  password reset or disable. The last admin is protected. Endpoints `/api/v1/auth/*` and `/api/v1/users`, plus the
  CLI `face-match users`.
- **Audit log** of enrollments, deletions, key and user changes, sign-ins and webhook retries (`/api/v1/audit`,
  Admin tab, `AUDIT_RETENTION_DAYS`).
- **Kiosk page** `/kiosk.html`: full-screen check-in for tablets with an attendance-only device key. Setup link from
  the Admin tab, wake lock, automatic retry and back-off, and it notices revoked keys.
- **Admin tab** in the web UI: users, device keys (with a kiosk setup link), audit log, webhook deliveries.
- **Several servers:** optional PostgreSQL backend (`DATABASE_URL`, `[postgres]` extra, included in the Docker image).
  Enrollments propagate through a change feed (`GALLERY_SYNC_SECONDS`), check-ins use per-person locks, and webhook
  deliveries are claimed with `SKIP LOCKED`. CI runs the full suite against PostgreSQL.
- **Pagination** (`limit`/`offset`, `X-Total-Count`) for people, attendance and webhook lists.
- **`face-match loadtest`** for capacity planning, with measured numbers in docs/deployment.md.

### Changed
- Operators may also take attendance (the `attendance` scope).
- Health reports `login` (whether accounts exist).
- Memory figures in the docs now distinguish idle (about 110 MB) from sustained load (about 250 MB).

## [1.1.0] - 2026-11-14

Production hardening.

### Upgrade notes
- **Python 3.11+ is now required.** The patched FastAPI, Starlette and python-multipart releases need it.
  The Docker image is unaffected.
- **Liveness is on by default** for `/attendance`. A recognised face that looks like a photo or screen returns
  the new status `liveness_failed` instead of `marked`. Set `LIVENESS=off` to keep the 1.0 behaviour.
- Webhook bodies gained an `id` field (also sent as `X-Event-Id`) and are now retried until delivered:
  de-duplicate on it.
- The demo UI no longer records attendance from uploaded photos (live camera only).

### Anti-spoofing
- Passive liveness on `/attendance` (Minivision MiniFASNet pair, about 3–5 ms per face): photos and screens held up to
  the camera return `liveness_failed` and aren't recorded. `LIVENESS`, `LIVENESS_THRESHOLD`, and
  `/recognize?liveness=true` for testing. The demo UI never records attendance from uploaded photos.

### Security
- Admin key plus scoped device keys (`attendance`, `recognize`, `read`, `enroll`, `metrics`, `admin`) via
  `/api/v1/keys` or `face-match keys`. Only hashes are stored. `Authorization: Bearer` is supported.
- Rate limiting per key, throttling of bad keys per IP, and an overload gate (`429`/`503` with `Retry-After`).
- Secure by default: refuses to listen on a network interface without authentication. Owner-only database files.
- Requires Python 3.11+, with dependency minimums at patched releases (pip-audit clean).

### Reliability and operations
- Webhooks through a transactional outbox: retried with exponential backoff, stable event ids for de-duplication,
  admin endpoints to list and retry failed deliveries.
- Prometheus `/metrics` (requests, latency, face outcomes, liveness rejections, webhook queue, backups, memory),
  JSON logs (`LOG_FORMAT=json`) with request ids (`X-Request-ID`).
- Automatic consistent backups with rotation (`BACKUP_INTERVAL_HOURS`, `BACKUP_KEEP`), `face-match backup`.
- Retention (`ATTENDANCE_RETENTION_DAYS`) and right to erasure (`DELETE /persons/{id}?erase_history=true`).

### Tooling
- New CLI commands: `face-match keys`, `face-match backup`, `face-match pilot-report` (compare attendance with a
  manual register), plus a [pilot guide](docs/pilot.md).
- Demo UI: liveness results ("Not live?"), liveness score when testing, permission errors, erase-history option.
- Docker image installs OS security updates; releases carry an SBOM and build provenance.
- CI: tests on Python 3.11–3.13, pip-audit, CodeQL, and a Trivy scan of the image.

### Fixed
- An auth-status cache treated "never checked" as fresh for the first seconds after start-up.

## [1.0.0] - 2026-11-14

First public release.

### Added
- REST API (`/api/v1`): face detection, recognition, enrollment (create, add, replace photos), people
  management, and attendance with cooldown, daily summary and signed webhooks.
- OpenCV YuNet (detection) + SFace (recognition) engine: CPU only, about 39 MB of models, about 10 ms per frame.
- Enrollment quality checks: one dominant face, minimum size, consistent photo set (order independent),
  duplicate detection with a separate `DUPLICATE_THRESHOLD`, small-thumbnail upscaling.
- Safe identification: `MATCH_THRESHOLD` + `MATCH_MARGIN` (refuses to choose between look-alikes),
  `unknown_reason` on every unknown face, `closest` candidate on `/recognize` for tuning.
- `face-match` CLI: `serve`, `download-models`, `import` (bulk enrollment from CSV/folder), `evaluate`
  (accuracy measurement).
- Demo web UI: live camera and photo identification, enrollment, records (present/absent, search).
- Secure by default: refuses to listen on a network interface without `API_KEY`.
- Docker image (non-root, models baked in, health check) for linux/amd64 and linux/arm64; docker compose example.
