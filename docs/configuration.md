# Configuration

FaceMatch reads environment variables, and also a `.env` file in the working directory (real environment variables
take precedence). Copy [`.env.example`](../.env.example) to get started. Every setting is optional.

## Server and security

| Variable | Default | Notes |
|---|---|---|
| `HOST` | `127.0.0.1` | Interface to bind. The Docker image uses `0.0.0.0` |
| `PORT` | `8000` | |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `API_KEY` | *(empty)* | Clients send it as `X-API-Key`. **Required whenever `HOST` is not localhost** |
| `ALLOW_NO_AUTH` | `false` | Set `true` to run an open API on a network on purpose (e.g. behind a private gateway) |
| `CORS_ORIGINS` | `*` | Comma-separated origins allowed to call the API from a browser. Restrict in production |

## Storage

| Variable | Default | Notes |
|---|---|---|
| `DATA_DIR` | `./data` | SQLite database (`facematch.db`): people, embeddings, attendance. Back it up |
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

See [accuracy.md](accuracy.md) for how the recognition defaults were chosen.
