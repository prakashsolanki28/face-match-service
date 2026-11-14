# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

Breaking release: FaceMatch is now a face detection and matching service only; attendance is built in your own
portal (release as 3.0.0).

### Removed (breaking)
- Built-in attendance: `POST /api/v1/attendance`, `GET /api/v1/attendance` and `GET /api/v1/attendance/summary`.
  Send frames to `POST /api/v1/recognize` instead and mark the returned `person_id`s present in your portal.
- Webhooks (their only event was `attendance.marked`): `GET /api/v1/webhooks`, `POST /api/v1/webhooks/{id}/retry`,
  the outbound delivery worker and signed requests.
- Settings: `ATTENDANCE_COOLDOWN_SECONDS`, `ATTENDANCE_RETENTION_DAYS`, `WEBHOOK_URL`, `WEBHOOK_SECRET`,
  `WEBHOOK_MAX_ATTEMPTS`, `WEBHOOK_RETRY_BASE_SECONDS`. They are ignored if still set.
- The `erase_history` parameter of `DELETE /api/v1/persons/{id}`: deleting a person now removes everything
  FaceMatch holds about them (the audit log keeps an entry for the deletion).
- `face-match pilot-report` (it compared FaceMatch's attendance with a register). Compare your portal's records
  instead; `face-match evaluate` still measures accuracy. `face-match loadtest --endpoint` is now `recognize`
  (default) or `detect`.
- Metrics: `facematch_attendance_marked_total`, `facematch_webhook_deliveries_total`, `facematch_webhook_queue`.
- The `attendance` OpenAPI tag, the `webhook.retry` audit action and docs/attendance.md (now docs/schools.md, about
  enrolling hundreds of people and marking attendance in your portal).

### Added
- Enroll with photos only: `name` is optional on `POST /api/v1/persons`. Store the returned `person.id` in your
  system; every match carries it as `person_id`.
- `metadata`: your own fields on a person (for example class, section and roll number), sent as a JSON form field
  on enrollment. A flat object of text, numbers, true/false or null; up to 50 keys. Returned with the person and
  in `match`/`closest` from `/recognize`. Invalid metadata fails with `422 invalid_metadata`.
- `PATCH /api/v1/persons/{id}`: change `name`, `external_id` or `metadata` (JSON body; only the fields you send
  change, and `metadata` is replaced). Audited as `person.update` with the names of the changed fields.

### Changed
- `/recognize` checks liveness on recognised faces by default (the server's `LIVENESS` setting, on by default).
  A face that looks like a printed photo or a screen gets `match: null`, `unknown_reason: "liveness_failed"` and
  its `liveness_score`. `?liveness=false` skips the check, for photos that don't come from a live camera.
  Previously `?liveness=true` only added a score.
- `/recognize` returns `closest` only with `?closest=true` (for testing and tuning), so a kiosk can't learn who a
  stranger resembles. It is never set for `liveness_failed` faces.
- `name` can be `null` in people and matches. A blank name is treated as no name, so the `invalid_name` error is
  no longer returned.
- `GET /api/v1/persons` lists people without a name after everyone with one, oldest first.
- Existing databases get a `metadata` column on startup (SQLite and PostgreSQL); no other migration is needed.
- A concurrent enrollment with the same `external_id` returns `409 external_id_exists` on PostgreSQL too.

### Upgrading
- **Your portal now keeps attendance.** Replace calls to `/attendance` with `/recognize`, mark each
  `match.person_id` present once per person per day (repeats and two faces of one person are yours to ignore), and
  leave unknown faces for manual marking. See docs/integration.md. Remove any webhook receiver.
- The old `attendance` and `webhook_outbox` tables are left in the database untouched, so no history is lost, but
  they are no longer used and `DELETE /api/v1/persons/{id}` doesn't remove rows in them. Export what you need, then
  drop them: `sqlite3 data/facematch.db "DROP TABLE IF EXISTS attendance; DROP TABLE IF EXISTS webhook_outbox;"`
  (PostgreSQL: the same `DROP TABLE` statements in psql).

## [2.0.0] - 2026-11-14

FaceMatch is now an API-only service.

### Removed (breaking)
- The built-in web UI (`/`, `/index.html`) and the kiosk page (`/kiosk.html`). Build kiosks and admin screens in your
  own apps against the API; docs/keys-and-audit.md includes a sample kiosk client.
- Sign-in accounts: users, roles, login/logout, session cookies and CSRF (`/api/v1/auth/login`,
  `/api/v1/auth/logout`, `/api/v1/users`, `face-match users`, `SESSION_*` settings).
- Scoped device keys and permissions: `/api/v1/keys`, `face-match keys`, the `403 forbidden` error and
  `/api/v1/auth/me`. **One API key (`API_KEY`) now gives full access to every endpoint.** Existing device keys stop
  working, and their table is dropped from the database on upgrade. Use `GET /api/v1/auth/check` to check a key.
- The `login` field from `/health`.
- Datasets, sample photos and dummy data from the project and its tests.

### Added
- docs/api-keys.md: creating, using, changing and protecting the API key, with Postman steps.
- api/: files to import the API into other tools: a Postman collection and environment (every endpoint with
  example values, file fields, auth and saved ids) and `openapi.json`. They are also attached to each release.
- `face-match export-api` regenerates them; `--check` reports when they are out of date.
- The OpenAPI spec documents which endpoints need the API key, Bearer auth, file fields as `format: binary`, and
  every parameter.

### Changed
- Rate limits (`RATE_LIMIT_PER_MINUTE`) apply per client IP, since every client shares the one key.
- Audit log entries made through the API have the actor `api_key`.
- OpenAPI operation ids are now the route names (e.g. `enroll_person` instead of
  `enroll_person_api_v1_persons_post`), and endpoints have short summaries. Regenerate API clients made from
  1.x specs.
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
