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
(or localhost), so you need it for the demo UI on phones and tablets too. Caddy example:

```
face.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

## Resources

Measured on an Apple M-series laptop:

| | |
|---|---|
| Memory | about 110 MB RSS while serving, with 33 people enrolled |
| Disk | about 140 MB of Python packages (mostly OpenCV) + 39 MB of models |
| Latency | about 10 ms per 640 px camera frame; about 150 ms median for full-size photos (up to 1600 px after downscaling) |
| Matching | 0.02 ms per search with 2,000 embeddings, 0.5 ms with 10,000 |

One instance comfortably serves several kiosks. Requests are handled in a thread pool, and the detector and
recognizer are each serialised, so CPU time per frame is what limits throughput. The compose file caps memory at 512 MB.

## Scaling and storage

- **Single instance** (the default): SQLite + in-memory index. Fine for thousands of people.
- **Several instances:** each one holds its own in-memory index, so they need a shared database and an index
  refresh on enrollment changes. That isn't built in yet; Postgres + pgvector is the planned path.
  Contributions are welcome.

## Backups and upgrades

- Back up `DATA_DIR`. With SQLite in WAL mode, use `sqlite3 facematch.db ".backup backup.db"` for a consistent copy.
- Upgrades keep the same database. Databases from before v1.0 (`attendance.db`) are picked up automatically.
- Pin an image tag (e.g. `:1.0`) in production. `:latest` follows the newest release.
