# The API key

FaceMatch uses **one API key** for the whole service. Every request to the API sends it, and it gives full
access to every endpoint. There are no other keys and no permissions to configure.

Endpoints that need no key: `GET /`, `GET /health` and `GET /openapi.json` (the endpoint list, no data).

---

## 1. Create the key

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

Restart FaceMatch after setting or changing it.

On a network interface (anything other than localhost), FaceMatch **refuses to start** without `API_KEY`, unless
you set `ALLOW_NO_AUTH=true` on purpose. On localhost without a key the API is open, which is handy for
development.

---

## 2. Use the key

Send it in the `X-API-Key` header, or as a Bearer token:

```bash
curl https://face.example.com/api/v1/auth/check -H "X-API-Key: $API_KEY"
curl https://face.example.com/api/v1/auth/check -H "Authorization: Bearer $API_KEY"
```

`GET /api/v1/auth/check` confirms the key works:

```json
{"authenticated": true, "auth_required": true}
```

The same key works for every endpoint: enrolling and updating people, detecting and recognising faces, the audit
log and `/metrics`.

### Postman

1. **Import** the ready-made files from [api/](../api/README.md): `FaceMatch.postman_collection.json` and
   `FaceMatch.postman_environment.json` (drop in the files, or paste their GitHub links).
2. Select the **FaceMatch** environment and set `baseUrl` (e.g. `https://face.example.com`) and `apiKey`. The
   `apiKey` variable is a **secret**, so it's masked on screen.
3. Send **Auth → Check API key**. Every request already sends `X-API-Key: {{apiKey}}`.
4. For photos, open **Body → form-data** and choose a file in each `image` / `images` row.

Keep separate environments for test and live. The same files import into Insomnia, Bruno, Hoppscotch and
Thunder Client; see [api/README.md](../api/README.md).

---

## 3. Change (rotate) the key

Change it every 6–12 months, when someone who knew it leaves, or straight away if it may have leaked or a device
holding it is lost:

1. Generate a new value (step 1).
2. Set the new `API_KEY` on every FaceMatch server and restart them. From then on the old key gets `401`.
3. Put the new key in every client: your portal's backend, each kiosk, monitoring, Postman.

Every client shares the one key, so all of them need the new value at the same time. Plan a short window for it.

---

## 4. Keep the key safe

The key opens everything, including enrolling and deleting people. Treat it like a database password.

**Do:**
- Store it in environment variables or a secret manager (AWS Secrets Manager, Vault, Doppler, 1Password, Docker
  or Kubernetes secrets), and keep any `.env` file private (`chmod 600 .env`).
- Call FaceMatch only over **HTTPS**.
- Call it from your **backend servers**, and keep the API off the public internet where possible (firewall or
  private network).
- Lock down kiosks and other devices that hold the key, and change the key as soon as one is lost.

**Don't:**
- Put the key in website JavaScript, a mobile app or a public repository: anyone can extract it.
- Send it in URLs or query strings, or write it to logs. FaceMatch only reads it from headers.
- Paste it into chats, tickets or screenshots. Hide the `X-API-Key` header when you share Postman output.

---

## 5. Errors

| Status | `error.code` | Meaning | Fix |
|---|---|---|---|
| `401` | `unauthorized` | No key, or the wrong key | Check the header name (`X-API-Key`) and the value; after a key change, use the new one |
| `429` | `rate_limited` | Too many requests from this IP (`RATE_LIMIT_PER_MINUTE`, default 600), or too many wrong keys from this IP (`AUTH_FAILURES_PER_MINUTE`, default 20) | Wait for the `Retry-After` seconds; check the client isn't looping on a wrong key |

---

## 6. How the key is protected

- **Comparison:** the key is compared in constant time, so response timing doesn't reveal it. It's never stored
  in the database or written to logs.
- **Brute force:** repeated wrong keys from one IP get `429`. Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` so
  this applies to the real client IP.
- **Rate limits are per client IP**, so one busy kiosk can't use up the limit for the others.
- **Audit trail:** every change made through the API (enrollments, updates, photo changes, deletions) is
  recorded with the actor `api_key`, the client IP and the request id. View it with `GET /api/v1/audit`.
