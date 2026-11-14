# FaceMatch

[![CI](https://github.com/prakashsolanki28/face-match-service/actions/workflows/ci.yml/badge.svg)](https://github.com/prakashsolanki28/face-match-service/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

**A lightweight, self-hosted face detection and face matching service.** Enroll people, then send any photo or
camera frame and get back *who is in it*, over a simple REST API that any portal, app or backend can call.
Built-in attendance logging, bulk import and signed webhooks cover the common "who arrived today?" use case.

- **Light:** about 110 MB of RAM, about 10 ms per frame on CPU, 43 MB of models. No GPU, no compiler, no database server.
- **Safe by design:** it refuses to guess between look-alikes, tells you *why* a face is unknown, rejects photos
  and screens held up to the camera (liveness), and stores embeddings rather than photos.
- **Production features:** per-device API keys with permissions, rate limiting, webhooks that retry until
  delivered, Prometheus metrics, JSON logs, automatic backups, retention and right-to-erasure.
- **Commercially usable:** Apache-2.0 code, with permissively licensed models (YuNet: MIT, SFace: Apache-2.0).
- **Measured:** 97.9% correct with 5 photos per person and **no wrong names** on a 31-person, 2,500-photo
  evaluation. See [docs/accuracy.md](docs/accuracy.md).

## Quick start

### Docker (recommended)

```bash
docker run -d -p 8000:8000 -v face-match-data:/data \
  -e API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  ghcr.io/prakashsolanki28/face-match-service:latest
```

Images are published for `linux/amd64` and `linux/arm64` (Raspberry Pi 4/5, Apple Silicon, AWS Graviton).
Or use [docker-compose.yml](docker-compose.yml).

### pip

```bash
pip install "git+https://github.com/prakashsolanki28/face-match-service.git"
face-match serve            # downloads the models on first run → http://127.0.0.1:8000
```

| URL | What |
|---|---|
| `http://localhost:8000` | Demo UI. Paste your API key at the top right |
| `http://localhost:8000/docs` | Interactive API docs (OpenAPI / Swagger) |
| `http://localhost:8000/health` | Health check |

## Use it from your app

```bash
# 1. Enroll a person (3–5 photos give the best accuracy; one passport photo also works)
curl -X POST localhost:8000/api/v1/persons -H "X-API-Key: $API_KEY" \
  -F name="Asha Verma" -F external_id="EMP-1024" -F images=@front.jpg -F images=@left.jpg

# 2. Who is in this photo?
curl -X POST localhost:8000/api/v1/recognize -H "X-API-Key: $API_KEY" -F image=@frame.jpg
```

```json
{
  "faces": [{
    "box": { "x": 512, "y": 180, "width": 210, "height": 262 },
    "match": { "external_id": "EMP-1024", "name": "Asha Verma", "similarity": 0.71 },
    "unknown_reason": null
  }],
  "processing_ms": 9.3
}
```

`external_id` is *your* ID for the person, such as an employee code or student number, so results map straight
back to your records.

## Features

| | |
|---|---|
| **Detection** | Boxes and 5 landmarks for every face (`/detect`) |
| **Matching** | 1:N identification against everyone enrolled (`/recognize`), with a reason for every unknown face |
| **Enrollment** | Create, add or replace photos. Quality checks reject photos with no face, several faces, a face too small, inconsistent photos or duplicates |
| **Attendance** | `/attendance` logs check-ins, with liveness (anti-spoofing), a cooldown, daily present/absent summary and webhooks that retry until delivered |
| **Security** | Admin key plus scoped device keys (a kiosk key can only mark attendance), rate limiting, overload protection, secure defaults |
| **Operations** | Prometheus `/metrics`, JSON logs with request ids, automatic backups, retention, `face-match pilot-report` for go-live validation |
| **Bulk import** | `face-match import` enrolls hundreds of people from a CSV roster and photo folder, with a report |
| **Evaluation** | `face-match evaluate` measures accuracy on your own photos before you go live |
| **Integration** | REST + OpenAPI, API-key auth, CORS, HMAC-signed webhooks, stable error codes |

## Documentation

- [API reference](docs/api.md): endpoints, responses, error codes
- [Configuration](docs/configuration.md): every environment variable
- [Deployment](docs/deployment.md): Docker, HTTPS, resources, backups
- [Integrating with a portal](docs/integration.md): IDs, webhooks, bulk import, keeping photos up to date
- [Attendance guide](docs/attendance.md): schools and offices with hundreds of people
- [Pilot guide](docs/pilot.md): validate on your own people before go-live
- [Accuracy](docs/accuracy.md): how it's measured, results, tuning
- [Privacy & security](docs/privacy.md): biometric data, consent, liveness

## How it works

```
image ─▶ YuNet (detect + landmarks) ─▶ align ─▶ SFace (128-d embedding) ─▶ cosine match vs. enrolled
                                                                            threshold + margin ─▶ name | unknown
                          /attendance only: MiniFASNet liveness ─▶ live? record : liveness_failed
```

Detection and recognition models come from the [OpenCV Model Zoo](https://github.com/opencv/opencv_zoo); the
liveness models from [Minivision](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing) (see the licence note in
[docs/privacy.md](docs/privacy.md#liveness-detection-anti-spoofing)). All run on OpenCV's DNN module.
They're downloaded on first run and verified against pinned SHA-256 hashes. Embeddings live in SQLite and an
in-memory index, which searches 10,000 faces in about 0.5 ms.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Please report security issues
privately ([SECURITY.md](SECURITY.md)).

## License

[Apache-2.0](LICENSE). Model credits are in [NOTICE](NOTICE).
