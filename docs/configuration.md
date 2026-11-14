# Configuration

FaceMatch reads environment variables, and also a `.env` file in the working directory (real environment variables
take precedence). Copy [`.env.example`](../.env.example) to get started. Every setting is optional.

## Server and security

| Variable | Default | Notes |
|---|---|---|
| `HOST` | `127.0.0.1` | Interface to bind. The Docker image uses `0.0.0.0` |
| `PORT` | `8000` | |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `LOG_FORMAT` | `text` | `json` writes one JSON object per line (timestamp, level, request id, method, path, status, duration), for Loki, ELK or CloudWatch |
| `API_KEY` | *(empty)* | The **admin** key (all permissions). Clients send it as `X-API-Key` or `Authorization: Bearer …`. Create limited device keys with it (see [api.md](api.md#authentication)). An API key, or at least one device key, is **required whenever `HOST` is not localhost** |
| `ALLOW_NO_AUTH` | `false` | Set `true` to run an open API on a network on purpose (e.g. behind a private gateway) |
| `CORS_ORIGINS` | `*` | Comma-separated origins allowed to call the API from a browser. Restrict in production |
| `API_DOCS` | `false` | Serve interactive API docs at `/docs` and `/redoc`. The OpenAPI spec is always at `/openapi.json` |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-For` header is trusted for the client IP (used by rate limiting). Set it to your reverse proxy's address |

## Abuse protection

| Variable | Default | Notes |
|---|---|---|
| `RATE_LIMIT_PER_MINUTE` | `600` | Requests per API key (or per IP on an open server). Over the limit: `429` with `Retry-After`. `0` disables. A kiosk sends about 100 frames per minute |
| `AUTH_FAILURES_PER_MINUTE` | `20` | Wrong or missing keys per IP before `429` |
| `MAX_CONCURRENT_IMAGES` | `8` | Images processed at once. More requests wait up to 10 s, then get `503` with `Retry-After` |

## Storage

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | *(empty)* | `postgresql://user:pass@host:5432/db` to share data between several servers (needs the `[postgres]` extra, included in the Docker image). Empty uses SQLite in `DATA_DIR` |
| `GALLERY_SYNC_SECONDS` | `2` | How often each server picks up enrollments made by other servers |
| `DATA_DIR` | `./data` | SQLite database (`facematch.db`) and backups. Back it up |
| `MODELS_DIR` | `./models` | ONNX models |
| `AUTO_DOWNLOAD_MODELS` | `true` | Download missing models at start-up (SHA-256 verified). The Docker image bakes them in |

## Recognition

| Variable | Default | Notes |
|---|---|---|
| `MATCH_THRESHOLD` | `0.40` | Cosine similarity needed to accept a match. The lowest value with no wrong names in our evaluation. Use `0.45` for stricter matching once people have 3+ photos |
| `MATCH_MARGIN` | `0.05` | The best match must beat the next-best **other** person by this much, otherwise the face is `ambiguous` |
| `DUPLICATE_THRESHOLD` | `0.50` | At enrollment, a photo this similar to someone else is rejected as a duplicate. Stricter than the match threshold because 0.40 would wrongly flag about 30% of new photos with 400 people enrolled (measured) |
| `DETECTION_THRESHOLD` | `0.60` | Detector confidence for a face. 0.9 missed 16% of ordinary portraits; 0.5 still gave no false detections |
| `MIN_FACE_SIZE` | `40` | Minimum face width in real pixels to attempt recognition. At 24 px wrong names appear |
| `MIN_ENROLL_FACE_SIZE` | `40` | Same, for enrollment photos. Thumbnails of about 100×125 px pass (small images are upscaled for detection) |
| `MAX_IMAGE_SIDE` | `1600` | Larger images are downscaled first (bigger images cost much more CPU) |
| `MAX_UPLOAD_MB` | `8` | Upload size limit per image |

## Attendance and integration

| Variable | Default | Notes |
|---|---|---|
| `ATTENDANCE_COOLDOWN_SECONDS` | `300` | Repeat sightings of the same person inside this window aren't logged again |
| `TIMEZONE` | server local | IANA name such as `Asia/Kolkata`. Decides which day an event belongs to |
| `WEBHOOK_URL` | – | Each new attendance event is POSTed here as JSON |
| `WEBHOOK_SECRET` | – | When set, requests carry `X-Signature-256: sha256=<HMAC of the raw body>` |
| `WEBHOOK_MAX_ATTEMPTS` | `20` | Deliveries are retried with exponential backoff (5 s, 10 s, 20 s … capped at 1 h), about 10 hours in total, then marked `failed` |
| `WEBHOOK_RETRY_BASE_SECONDS` | `5` | First retry delay |

## Liveness (anti-spoofing)

| Variable | Default | Notes |
|---|---|---|
| `LIVENESS` | `on` | Check recognised faces on `/attendance` for photo or screen spoofs. `off` disables (and skips loading the models) |
| `LIVENESS_THRESHOLD` | `0.5` | Minimum "live" probability. Higher rejects more spoofs, but also more real people. Calibrate in your pilot; Minivision's app uses 0.915. See [privacy.md](privacy.md#liveness-detection-anti-spoofing) |

## Data protection

| Variable | Default | Notes |
|---|---|---|
| `BACKUP_INTERVAL_HOURS` | `24` | Automatic consistent backups into `DATA_DIR/backups/` (owner-only files). `0` disables |
| `BACKUP_KEEP` | `7` | Number of backups to keep |
| `AUDIT_RETENTION_DAYS` | `0` | Delete audit entries older than this. `0` keeps them forever |
| `ATTENDANCE_RETENTION_DAYS` | `0` | Delete attendance events older than this (checked hourly). `0` keeps them forever. Delivered webhooks are always purged after 30 days |

See [accuracy.md](accuracy.md) for how the recognition defaults were chosen.
