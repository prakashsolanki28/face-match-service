# API reference

FaceMatch is API-only. The OpenAPI 3.1 specification is always at `/openapi.json` (generate clients from it).
Interactive documentation pages (`/docs`, `/redoc`) are off by default; enable them with `API_DOCS=true`.

**Import the API into your tool:** the [api/](../api/README.md) folder has a ready-to-send Postman collection
and environment, plus `openapi.json`, for Postman, Insomnia, Bruno, Hoppscotch, Swagger and code generators.

- All endpoints except `/health` and `/metrics` are under `/api/v1`. See [Authentication](#authentication).
- Images are sent as `multipart/form-data` (JPEG, PNG, WebP or BMP, up to `MAX_UPLOAD_MB`). EXIF rotation is applied.
- Coordinates in responses are in pixels of the image you sent.

## Authentication

Full guide: [api-keys.md](api-keys.md) (creating the key, Postman, changing it, keeping it safe).

FaceMatch has **one API key**: the `API_KEY` setting. Send it as `X-API-Key: <key>` or
`Authorization: Bearer <key>`. It gives full access to every endpoint; there are no other keys or permissions.

```bash
curl localhost:8000/api/v1/auth/check -H "X-API-Key: $API_KEY"
# → {"authenticated": true, "auth_required": true}
```

**Limits:** each client IP gets `RATE_LIMIT_PER_MINUTE` requests. Repeated bad keys from one IP
are throttled, and when the server is saturated with images it answers `503`. All of these include a `Retry-After`
header. Every response has an `X-Request-ID` header (pass your own to correlate logs).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Status, version, engine, number of people and embeddings, thresholds, whether auth is required |
| `POST` | `/api/v1/detect` | Faces, boxes and 5 landmarks. No identity lookup |
| `POST` | `/api/v1/recognize?liveness=` | Identify every face. Records nothing. `liveness=true` adds a `liveness_score` |
| `POST` | `/api/v1/persons` | Enroll: `name`, optional `external_id`, 1–10 `images`, optional `allow_duplicate` |
| `GET` | `/api/v1/persons?external_id=&limit=&offset=` | List people (sorted by name, total in `X-Total-Count`). `?external_id=` looks one up by your ID |
| `GET` | `/api/v1/persons/{id}` | One person |
| `POST` | `/api/v1/persons/{id}/faces` | Add photos (they must match the person's existing photos) |
| `PUT` | `/api/v1/persons/{id}/faces` | Replace all photos. All-or-nothing: if no new photo is usable, nothing changes |
| `DELETE` | `/api/v1/persons/{id}?erase_history=` | Delete a person and their face data. Attendance history is kept unless `erase_history=true` (right to erasure) |
| `POST` | `/api/v1/attendance` | Identify faces **and** log attendance: `image`, optional `device_id` |
| `GET` | `/api/v1/attendance?date=YYYY-MM-DD&person_id=&limit=` | Attendance events, newest first |
| `GET` | `/api/v1/attendance/summary?date=YYYY-MM-DD` | One row per person: first seen, last seen, sightings |
| `GET` | `/api/v1/webhooks?status=pending\|delivered\|failed` | Webhook delivery queue |
| `POST` | `/api/v1/webhooks/{id}/retry` | Retry a failed delivery |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/api/v1/auth/check` | Check that your API key works |
| `GET` | `/api/v1/audit?action=&actor=&limit=&offset=` | Audit log, newest first |

## Recognize

```bash
curl -X POST localhost:8000/api/v1/recognize -H "X-API-Key: $API_KEY" -F image=@photo.jpg
```

```json
{
  "image": { "width": 1280, "height": 720 },
  "faces": [
    {
      "box": { "x": 512, "y": 180, "width": 210, "height": 262 },
      "score": 0.93,
      "landmarks": [{ "x": 570.2, "y": 280.1 }, { "x": 668.9, "y": 279.4 }, { "x": 620.5, "y": 335.0 },
                    { "x": 581.7, "y": 389.6 }, { "x": 660.3, "y": 388.8 }],
      "match": { "person_id": "9f1c…", "external_id": "EMP-1024", "name": "Asha Verma", "similarity": 0.71 },
      "unknown_reason": null,
      "closest": null
    }
  ],
  "processing_ms": 9.3
}
```

Faces are ordered largest first. `landmarks` are: right eye, left eye, nose tip, right mouth corner, left mouth corner.

### Unknown faces

When `match` is `null`, `unknown_reason` explains why:

| `unknown_reason` | Meaning | What to do |
|---|---|---|
| `below_threshold` | The closest person scored under `MATCH_THRESHOLD` | Not enrolled, or this photo differs a lot from the enrolled ones (add a similar photo) |
| `ambiguous` | Two enrolled people scored almost the same (`MATCH_MARGIN`) | Look-alikes or twins. Add more photos of both |
| `too_small` | Face narrower than `MIN_FACE_SIZE` real pixels | Move closer or use a larger photo |
| `nobody_enrolled` | The gallery is empty | Enroll someone |

`/recognize` also returns `closest` (the nearest person and their similarity) for unknown faces, to help testing
and tuning. **`/attendance` never returns `closest`**, so a public kiosk can't reveal who a stranger resembles.

## Enroll

```bash
curl -X POST localhost:8000/api/v1/persons -H "X-API-Key: $API_KEY" \
  -F name="Asha Verma" -F external_id="EMP-1024" \
  -F images=@front.jpg -F images=@left.jpg -F images=@right.jpg
```

```json
{
  "person": { "id": "9f1c…", "external_id": "EMP-1024", "name": "Asha Verma", "face_count": 3, "created_at": "2026-11-14T09:22:17+05:30" },
  "accepted": 3,
  "rejected": []
}
```

Every photo is checked. Photos that fail appear in `rejected` with a reason; the person is created if at least one
photo passes.

| Rejection reason | Meaning |
|---|---|
| `no_face` | No face found |
| `multiple_faces` | More than one similarly sized face (a small bystander is ignored) |
| `face_too_small` | Under `MIN_ENROLL_FACE_SIZE` real pixels |
| `inconsistent_identity` | Doesn't match the rest of the set. The photo that agrees best with the others is the reference, so upload order doesn't matter |
| `matches_other_person` | At least `DUPLICATE_THRESHOLD` similar to someone else already enrolled |
| `invalid_image` / `too_large` | Can't be decoded / over the size limit |

## Attendance

```bash
curl -X POST localhost:8000/api/v1/attendance -H "X-API-Key: $API_KEY" -F image=@frame.jpg -F device_id=main-gate
```

Each face gets an `attendance` object:

| `status` | Meaning |
|---|---|
| `marked` | New event created (`event_id`, `timestamp`) |
| `already_marked` | Seen within `ATTENDANCE_COOLDOWN_SECONDS`. Returns the earlier event |
| `unknown` | Not identified (see `unknown_reason`) |
| `too_small` | Face too small to identify |
| `liveness_failed` | Recognised, but the face looks like a photo or a screen. Not recorded, and the name isn't returned |

`GET /api/v1/attendance/summary?date=2026-11-14` returns one row per person seen that day, with `first_seen`
(check-in), `last_seen` and `events`. To get absentees, compare with `GET /api/v1/persons`.

## Errors

Every error has the same shape and a stable `code`:

```json
{ "error": { "code": "external_id_exists", "message": "external_id 'EMP-1024' is already enrolled.", "details": { "person": { } } } }
```

| HTTP | `code` |
|---|---|
| 400 | `invalid_image` |
| 401 | `unauthorized` |
| 429 | `rate_limited` (see `Retry-After`) |
| 503 | `busy` (see `Retry-After`) |
| 404 | `person_not_found` |
| 409 | `already_enrolled`, `external_id_exists` |
| 413 | `image_too_large` |
| 422 | `validation_error`, `no_usable_face` (details list each rejected photo), `invalid_name`, `no_images`, `too_many_images` |
