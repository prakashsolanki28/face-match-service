# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/).

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
