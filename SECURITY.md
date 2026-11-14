# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through
[GitHub security advisories](https://github.com/prakashsolanki28/face-match-service/security/advisories/new).
You'll get a reply within 7 days. Please allow time for a fix before disclosing it publicly.

## Supported versions

Security fixes go into the latest minor release.

## Running FaceMatch safely

FaceMatch stores biometric data (face embeddings). Treat a deployment like any system holding personal data:

- **Always set `API_KEY`.** The service refuses to listen on a network interface without one, unless you set
  `ALLOW_NO_AUTH=true` on purpose.
- Serve it over **HTTPS**, behind a reverse proxy (Caddy, nginx, Traefik). Browsers only allow the camera on
  HTTPS or localhost anyway.
- Restrict `CORS_ORIGINS` to your own front-end origins.
- Set `WEBHOOK_SECRET` and verify `X-Signature-256` on the receiving side.
- Back up and protect `DATA_DIR` (the SQLite database). It holds embeddings and attendance history.
- **Liveness is not included.** A printed photo or a screen can pass as a person. Add presentation-attack
  detection before using FaceMatch for access control. See [docs/privacy.md](docs/privacy.md).
