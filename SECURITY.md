# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through
[GitHub security advisories](https://github.com/prakashsolanki28/face-match-service/security/advisories/new).
You'll get a reply within 7 days. Please allow time for a fix before disclosing it publicly.

## Supported versions

Security fixes go into the latest minor release.

## Running FaceMatch safely

FaceMatch stores biometric data (face embeddings). Treat a deployment like any system holding personal data:

- **Always set `API_KEY`** (the admin key). The service refuses to listen on a network interface without
  authentication, unless you set `ALLOW_NO_AUTH=true` on purpose.
- **Give every kiosk and integration its own scoped key** (`face-match keys create --scopes attendance`) and
  revoke lost devices. Never put the admin key on a kiosk.
- Rate limiting (`RATE_LIMIT_PER_MINUTE`), bad-key throttling and an overload gate are on by default. Behind a
  reverse proxy, set `FORWARDED_ALLOW_IPS` so limits apply per real client.
- Serve it over **HTTPS**, behind a reverse proxy (Caddy, nginx, Traefik). Browsers only allow the camera on
  HTTPS or localhost anyway.
- Restrict `CORS_ORIGINS` to your own front-end origins.
- Set `WEBHOOK_SECRET` and verify `X-Signature-256` on the receiving side.
- Protect `DATA_DIR`: the database and automatic backups are owner-only files; use disk encryption and copy
  backups off-site, encrypted.
- **Liveness is not included.** A printed photo or a screen can pass as a person. Add presentation-attack
  detection before using FaceMatch for access control. See [docs/privacy.md](docs/privacy.md).
