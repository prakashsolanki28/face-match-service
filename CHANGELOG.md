# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-14

First public release.

### Recognition
- REST API (`/api/v1`): face detection, 1:N recognition, enrollment (create, add or replace photos), people
  management, attendance with cooldown and a daily summary.
- OpenCV YuNet (detection) + SFace (recognition): CPU only, about 10 ms per frame.
- Enrollment quality checks: one dominant face, minimum size, consistent photo set (order independent),
  duplicate detection (`DUPLICATE_THRESHOLD`), small-thumbnail upscaling.
- Safe identification: `MATCH_THRESHOLD` + `MATCH_MARGIN` (no guessing between look-alikes), `unknown_reason` on every
  unknown face, `closest` candidate on `/recognize` for tuning.

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
- `face-match` CLI: `serve`, `download-models`, `keys`, `backup`, `import` (bulk enrollment), `evaluate`
  (accuracy measurement), `pilot-report` (compare with a manual register).
- Demo web UI: live camera and photo identification, enrollment, records (present/absent, search).
- Docker image (non-root, models baked in, OS security updates, health check) for linux/amd64 and linux/arm64,
  with SBOM and provenance. CI: lint, tests on Python 3.11–3.13, pip-audit, CodeQL, Trivy image scan.
