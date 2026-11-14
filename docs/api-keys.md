# API keys and permissions

FaceMatch is API-only, and every request is authenticated with an API key. This guide covers how to create
keys, which permissions (scopes) to give each client, and how to use, rotate and revoke them safely.

## Two kinds of key

| Key | What it can do | Where it lives | Who should have it |
|---|---|---|---|
| **Admin key** | Everything, including creating and revoking other keys | The `API_KEY` setting on the server | Only the people who run the server |
| **Device keys** (`fm_…`) | Only the scopes chosen when the key was created | The FaceMatch database (as a hash) | One per client: your portal, each kiosk, monitoring |

Always use a **device key** in applications. Keep the admin key on the server for administration only.

Endpoints that need no key: `GET /`, `GET /health` and `GET /openapi.json` (the endpoint list, no data).

---

## 1. Set the admin key

Generate a long random value:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set it as `API_KEY` on the server. Never put it in source code or git.

```bash
# .env file in the directory you run face-match from (keep it private: chmod 600 .env)
API_KEY=paste-the-generated-value

# or Docker
docker run -d -e API_KEY="paste-the-generated-value" ... ghcr.io/prakashsolanki28/face-match-service:latest

# or docker compose: put API_KEY=... in the .env file next to docker-compose.yml
```

On a network interface (anything other than localhost), FaceMatch **refuses to start** without authentication.
That means `API_KEY` must be set, or at least one device key must exist (or you set `ALLOW_NO_AUTH=true` on purpose).

---

## 2. Create device keys

### With the command line (on the server)

```bash
face-match keys create --name "school-portal" --scopes read,enroll,recognize
face-match keys create --name "gate-1-kiosk"  --scopes attendance
face-match keys create --name "prometheus"    --scopes metrics
```

Output:

```
Created key 61b499cd4e50 (gate-1-kiosk) with scopes: attendance
Token (shown once, store it securely): fm_kVgDbAI…
```

In Docker, run the same command inside the container:

```bash
docker exec face-match face-match keys create --name "gate-1-kiosk" --scopes attendance
```

The command uses the same database as the server (`DATA_DIR`, or `DATABASE_URL` for PostgreSQL), so run it where
those settings apply. New keys work immediately, on every server sharing the database.

### With the API (admin key required)

```bash
curl -X POST https://face.example.com/api/v1/keys \
  -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"name": "gate-1-kiosk", "scopes": ["attendance"]}'
```

```json
{
  "key": {
    "id": "61b499cd4e50",
    "name": "gate-1-kiosk",
    "prefix": "fm_kVgDbAI",
    "scopes": ["attendance"],
    "created_at": "2026-11-14T13:55:02+05:30",
    "last_used_at": null,
    "revoked_at": null
  },
  "token": "fm_kVgDbAI…"
}
```

> **The token is shown only once.** FaceMatch stores just a SHA-256 hash of it. Copy it straight into the
> client's configuration or your secret manager. If it's lost, create a new key and revoke the old one.

Rules:

- `name` is required (up to 100 characters). Use names that say where the key is used, such as
  `school-portal-prod` or `gate-2-kiosk`.
- `scopes` must contain at least one valid scope. Unknown scopes are rejected:
  `Unknown or missing scopes ['root']. Choose from: admin, enroll, recognize, attendance, read, metrics`.

---

## 3. Permissions (scopes)

| Scope | Allows | Typical client |
|---|---|---|
| `attendance` | Mark attendance from camera frames | Kiosks |
| `recognize` | Detect and identify faces; nothing is recorded | Verification screens, photo tagging |
| `read` | List people, attendance and daily summaries | Portal sync, reports |
| `enroll` | Create, update and delete people and their photos | Portal admin backend, bulk import |
| `metrics` | Read Prometheus metrics | Monitoring |
| `admin` | Everything: all of the above, plus keys, webhook deliveries and the audit log | Server administrators only |

A key can have several scopes (`--scopes read,enroll`). `admin` includes all others.

### Which scope each endpoint needs

| Endpoint | Scope |
|---|---|
| `POST /api/v1/attendance` | `attendance` |
| `POST /api/v1/detect`, `POST /api/v1/recognize` | `recognize` |
| `GET /api/v1/persons`, `GET /api/v1/persons/{id}` | `read` |
| `GET /api/v1/attendance`, `GET /api/v1/attendance/summary` | `read` |
| `POST /api/v1/persons`, `POST`/`PUT /api/v1/persons/{id}/faces`, `DELETE /api/v1/persons/{id}` | `enroll` |
| `GET /metrics` | `metrics` |
| `POST`/`GET /api/v1/keys`, `DELETE /api/v1/keys/{id}` | `admin` |
| `GET /api/v1/webhooks`, `POST /api/v1/webhooks/{id}/retry`, `GET /api/v1/audit` | `admin` |
| `GET /api/v1/auth/me` | any valid key |
| `GET /`, `GET /health`, `GET /openapi.json` | none |

---

## 4. Recommended keys

Give every client its own key, with the fewest scopes it needs:

| Client | Command |
|---|---|
| Your portal's backend (enroll, look up, identify) | `face-match keys create --name "school-portal" --scopes read,enroll,recognize` |
| Each kiosk or entrance camera | `face-match keys create --name "gate-1-kiosk" --scopes attendance` |
| Reports or dashboards (read only) | `face-match keys create --name "reports" --scopes read` |
| Bulk import (`face-match import`) | `face-match keys create --name "bulk-import" --scopes read,enroll` |
| Pilot report (`face-match pilot-report`) | `face-match keys create --name "pilot-report" --scopes read` |
| Prometheus | `face-match keys create --name "prometheus" --scopes metrics` |

One key per device means a lost tablet or leaked secret can be revoked without affecting anything else, and
the audit log shows exactly which client did what.

---

## 5. Use a key

Send it in the `X-API-Key` header, or as a Bearer token:

```bash
curl https://face.example.com/api/v1/auth/me -H "X-API-Key: fm_..."
curl https://face.example.com/api/v1/auth/me -H "Authorization: Bearer fm_..."
```

`GET /api/v1/auth/me` tells you which key you're using and what it may do:

```json
{"kind": "key", "name": "gate-1-kiosk", "scopes": ["attendance"]}
```

### Postman

1. **Import** the ready-made files from [api/](../api/README.md): `FaceMatch.postman_collection.json` and
   `FaceMatch.postman_environment.json` (drop in the files, or paste their GitHub links).
2. Select the **FaceMatch** environment and set `baseUrl` (e.g. `https://face.example.com`) and `apiKey`. The
   `apiKey` variable is a **secret**, so it's masked on screen.
3. Send **Auth → Who am I** to check the key. Every request already sends `X-API-Key: {{apiKey}}`.
4. For photos, open **Body → form-data** and choose a file in each `image` / `images` row.

Use a limited device key in Postman, not the admin key. Keep separate environments for test and live. The same
files import into Insomnia, Bruno, Hoppscotch and Thunder Client; see [api/README.md](../api/README.md).

---

## 6. List, revoke and rotate

```bash
face-match keys list
```

```
61b499cd4e50  fm_kVgDbAI…  gate-1-kiosk         [attendance]  active  last used: 2026-11-14T08:31:10+00:00
```

The list shows each key's id, prefix (to recognise it), name, scopes, status and when it was last used (updated
at most once a minute). With the API: `GET /api/v1/keys` (tokens are never returned).

**Revoke** (lost device, leaked key, client retired):

```bash
face-match keys revoke 61b499cd4e50
# or: curl -X DELETE https://face.example.com/api/v1/keys/61b499cd4e50 -H "X-API-Key: $ADMIN_KEY"
```

Every server stops accepting a revoked key within **30 seconds**. Revocation is permanent; create a new key if needed.

**Rotate a device key** (e.g. every 6–12 months, or when someone with access leaves):

1. Create a new key with the same scopes.
2. Put the new token in the client and check it with `GET /api/v1/auth/me`.
3. Confirm in `face-match keys list` that the new key is being used.
4. Revoke the old key.

**Rotate the admin key:** set a new `API_KEY` value on every server and restart them. Device keys keep working
throughout; only admin access uses the new value.

---

## 7. Keep keys safe

**Do:**
- Store tokens in environment variables or a secret manager (AWS Secrets Manager, Vault, Doppler, 1Password,
  Docker or Kubernetes secrets), and keep any `.env` file private (`chmod 600 .env`).
- Call FaceMatch only over **HTTPS**.
- Call it from your **backend servers**, and keep the API off the public internet where possible (firewall or
  private network).
- Give kiosks only `attendance`, and revoke a kiosk's key as soon as a device is lost.

**Don't:**
- Put any key in website JavaScript, a mobile app or a public repository: anyone can extract it.
- Share one key between several clients, or use the admin key in an application.
- Send keys in URLs or query strings, or write them to logs. FaceMatch only reads them from headers.

---

## 8. Errors

| Status | `error.code` | Meaning | Fix |
|---|---|---|---|
| `401` | `unauthorized` | No key, wrong key, or revoked key | Check the header name (`X-API-Key`) and the token; create a new key if it was revoked |
| `403` | `forbidden` | The key is valid but lacks the scope, e.g. `This API key lacks the 'read' permission.` | Use a key with that scope, or create one |
| `429` | `rate_limited` | Too many requests from this key (`RATE_LIMIT_PER_MINUTE`, default 600), or too many failed keys from this IP (`AUTH_FAILURES_PER_MINUTE`, default 20) | Wait for the `Retry-After` seconds; check the client isn't looping on a bad key |

---

## 9. How keys are protected

- **Format:** `fm_` followed by 43 random URL-safe characters (256 bits of randomness), generated with Python's
  `secrets` module.
- **Storage:** only a SHA-256 hash of each device key is stored. A database leak doesn't reveal usable tokens. The
  admin key isn't stored at all; it's compared in constant time against `API_KEY`.
- **Sharing between servers:** with PostgreSQL (`DATABASE_URL`) every server uses the same keys. Lookups are cached
  for up to 30 seconds, which is why revocation can take that long.
- **Brute force:** repeated wrong keys from one IP get `429`. Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` so
  this applies to the real client IP.
- **Audit trail:** creating and revoking keys is recorded in the audit log, as `admin_key` or `cli` (for the
  command line), along with every change made with each key (`key:<name>#<id>`). View it with
  `GET /api/v1/audit`, using the admin key.
