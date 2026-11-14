# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through
[GitHub security advisories](https://github.com/prakashsolanki28/face-match-service/security/advisories/new).
You'll get a reply within 7 days. Please allow time for a fix before disclosing it publicly.

## Supported versions

Security fixes go into the latest minor release.

## Running FaceMatch safely

FaceMatch stores biometric data (face embeddings). Treat a deployment like any system holding personal data:

- **Always set `API_KEY`**: one key with full access to the API (see [docs/api-keys.md](docs/api-keys.md)). The
  service refuses to listen on a network interface without it, unless you set `ALLOW_NO_AUTH=true` on purpose.
- Keep the key on servers and locked-down kiosks you control, never in browser or mobile app code. Change it on
  the server and every client if it leaks or a device is lost.
- Rate limiting (`RATE_LIMIT_PER_MINUTE`), bad-key throttling and an overload gate are on by default. Behind a
  reverse proxy, set `FORWARDED_ALLOW_IPS` so limits apply per real client.
- Serve it over **HTTPS**, behind a reverse proxy (Caddy, nginx, Traefik). Browsers only allow the camera on
  HTTPS or localhost anyway.
- Restrict `CORS_ORIGINS` to your own front-end origins.
- Set `WEBHOOK_SECRET` and verify `X-Signature-256` on the receiving side.
- Protect `DATA_DIR`: the database and automatic backups are owner-only files; use disk encryption and copy
  backups off-site, encrypted.
- **Liveness is a deterrent, not a guarantee.** It rejects photos and screens held up to the camera, but not
  image files sent straight to the API or deepfakes. Don't use FaceMatch alone for access control. See
  [docs/privacy.md](docs/privacy.md).
