# Deployment

## Docker

```bash
docker run -d --name face-match -p 8000:8000 \
  -v face-match-data:/data \
  -e API_KEY=change-me -e TIMEZONE=Asia/Kolkata \
  --restart unless-stopped \
  ghcr.io/prakashsolanki28/face-match-service:latest
```

- Multi-arch: `linux/amd64` and `linux/arm64`.
- Runs as a non-root user (uid 10001). Data lives in the `/data` volume.
- The models are baked in, so containers start offline.
- A health check calls `/health`.
- The container listens on `0.0.0.0`, so **`API_KEY` is required** (or `ALLOW_NO_AUTH=true`).

With compose: create a `.env` containing `API_KEY=...`, then run `docker compose up -d`
(see [docker-compose.yml](../docker-compose.yml)).

## Without Docker

```bash
pip install "git+https://github.com/prakashsolanki28/face-match-service.git"
API_KEY=change-me face-match serve --host 0.0.0.0 --port 8000
```

Run it under a process manager (systemd, supervisord) in production. Example systemd unit:

```ini
[Unit]
Description=FaceMatch
After=network.target

[Service]
User=facematch
WorkingDirectory=/var/lib/face-match
Environment=API_KEY=change-me HOST=127.0.0.1 PORT=8000
ExecStart=/opt/face-match/venv/bin/face-match serve
Restart=always

[Install]
WantedBy=multi-user.target
```

## HTTPS

Put FaceMatch behind a reverse proxy that terminates TLS. Browsers only allow camera access on HTTPS
(or localhost), so browser-based kiosk apps need it too. Caddy example:

```
face.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

## Kiosks and integrations

Every client (your portal's backend, each kiosk, monitoring) uses the same `API_KEY`, which gives full access.
Keep it on devices you control, lock kiosks down, and change the key on the server and every client if a device
is lost (see [api-keys.md](api-keys.md#3-change-rotate-the-key)).

## Monitoring

`GET /metrics` serves Prometheus metrics: request counts and latency by route, faces by outcome (matched,
below_threshold, ambiguous, too_small, liveness_failed), rate-limited requests, backup runs, enrolled people, and
process memory.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: face-match
    authorization:
      credentials_file: /etc/prometheus/facematch-api-key   # file holding the API key
    static_configs:
      - targets: ["face-match:8000"]
```

Suggested alerts:
- A rising rate of `5xx` in `facematch_http_requests_total`, or of `facematch_rate_limited_total{reason="busy"}`.
- `facematch_maintenance_runs_total{kind="backup",outcome="error"}` increasing.

**Logs:** set `LOG_FORMAT=json` for one JSON object per line. Each request is logged once with its request id,
method, path, status, duration and client. `/health` checks that the database answers; use it for container and
load-balancer health checks.

## Resources

Measured on an Apple M-series laptop:

| | |
|---|---|
| Memory | about 110 MB RSS while serving, with 33 people enrolled |
| Disk | about 140 MB of Python packages (mostly OpenCV) + 43 MB of models (including liveness) |
| Latency | about 10 ms per 640 px camera frame; about 150 ms median for full-size photos (up to 1600 px after downscaling) |
| Matching | 0.02 ms per search with 2,000 embeddings, 0.5 ms with 10,000 |

One instance comfortably serves several kiosks. Requests are handled in a thread pool, and the detector and
recognizer are each serialised, so CPU time per frame is what limits throughput. The compose file caps memory at 512 MB.

## Capacity

Measured with `face-match loadtest` on an Apple M4 laptop (one server, 33 people enrolled, liveness on, a 640×480
frame with one face):

| Parallel clients | Requests/s | Median latency | p95 | ≈ kiosks |
|---|---|---|---|---|
| 1 | 40 | 24 ms | 34 ms | 26 |
| 4 | 41 | 94 ms | 131 ms | 27 |
| 16 | 43 | 371 ms | 432 ms | 28 |

Throughput is bound by face detection and recognition, which each process one image at a time per server, so more
clients add latency rather than throughput. A kiosk sends about 1.5 frames per second, so **one server serves about
25–35 kiosks**; a school with 400 students arriving in 20 minutes needs 2–4 kiosks. Measure your own hardware:

```bash
face-match loadtest --image a-real-camera-frame.jpg --api-key "$KEY" --concurrency 1,4,8 --duration 15
```

Memory: about 110 MB idle and about 250 MB under sustained load.

## Several servers (high availability)

Point every server at one PostgreSQL database and put a load balancer in front:

```bash
pip install "face-match-service[postgres]"     # already included in the Docker image
DATABASE_URL=postgresql://facematch:secret@db.internal:5432/facematch face-match serve --host 0.0.0.0
```

What is shared, and how:

| Concern | How it works with several servers |
|---|---|
| People, embeddings, audit log | In PostgreSQL. Any server can handle any request; no sticky sessions needed |
| New enrollments | Each server polls a change feed every `GALLERY_SYNC_SECONDS` (default 2 s) and updates its in-memory index |
| Key revocation | Takes effect within 30 s everywhere (key lookups are cached briefly) |
| Rate limits | Counted per server: the effective limit is `RATE_LIMIT_PER_MINUTE` × number of servers |
| Backups | Use `pg_dump` or your provider's managed backups (the built-in backups are for SQLite) |

Example with Docker Compose (two FaceMatch servers, PostgreSQL, and Caddy as an HTTPS load balancer):

```yaml
services:
  db:
    image: postgres:16
    environment: { POSTGRES_USER: facematch, POSTGRES_PASSWORD: change-me, POSTGRES_DB: facematch }
    volumes: [pgdata:/var/lib/postgresql/data]
  app:
    image: ghcr.io/prakashsolanki28/face-match-service:latest
    environment:
      DATABASE_URL: postgresql://facematch:change-me@db:5432/facematch
      API_KEY: ${API_KEY:?set API_KEY}
      FORWARDED_ALLOW_IPS: "*"   # trust the proxy's X-Forwarded-For (only reachable through Caddy)
    deploy: { replicas: 2 }
    depends_on: [db]
  proxy:
    image: caddy:2
    command: caddy reverse-proxy --from face.example.com --to app:8000
    ports: ["80:80", "443:443"]
volumes: { pgdata: {} }
```

## Backups and upgrades

- **Automatic backups:** every `BACKUP_INTERVAL_HOURS` (default 24) a consistent online copy is written to
  `DATA_DIR/backups/facematch-<time>.db`, keeping the newest `BACKUP_KEEP` (default 7). Files are owner-only (0600).
  **Copy them off the server** (e.g. a nightly `rsync`/`rclone` of `DATA_DIR/backups/` to encrypted storage).
- **On demand:** `face-match backup` (or `docker exec face-match face-match backup`), safe while the service runs.
- **Restore:** stop the service, replace `DATA_DIR/facematch.db` with a backup file (remove any `facematch.db-wal`
  and `-shm` next to it), and start it again.
- Upgrades keep the same database. Databases from before v1.0 (`attendance.db`) are picked up automatically.
- The database remembers which recognition model made its face embeddings (shown as `embedding_model` in
  `/health`). If an upgrade changes the model, the server refuses to start rather than return wrong matches.
  Photos are never stored, so embeddings can't be converted: stay on the previous version, or enroll everyone
  again into a new, empty database (a new `DATA_DIR` or `DATABASE_URL`), for example with `face-match import`.
- Pin an image tag (e.g. `:1.0`) in production. `:latest` follows the newest release.
