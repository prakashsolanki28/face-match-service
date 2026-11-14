# API reference

FaceMatch is a face detection and face matching service, and it is API-only. You enroll people and get an id for
each; then you send photos or camera frames and get back the ids of the people in them. What you do with a match,
such as marking attendance, happens in your own system (see [integration.md](integration.md)).

The OpenAPI 3.1 specification is always at `/openapi.json` (generate clients from it).
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
| `POST` | `/api/v1/recognize?liveness=&closest=` | Identify every face and return who they are. Records nothing |
| `POST` | `/api/v1/persons` | Enroll: 1–10 `images`, plus optional `name`, `external_id`, `metadata` and `allow_duplicate` |
| `GET` | `/api/v1/persons?external_id=&limit=&offset=` | List people (sorted by name, people without a name last; total in `X-Total-Count`). `?external_id=` looks one up by your ID |
| `GET` | `/api/v1/persons/{id}` | One person |
| `PATCH` | `/api/v1/persons/{id}` | Change `name`, `external_id` or `metadata` (JSON body). Only the fields you send change |
| `POST` | `/api/v1/persons/{id}/faces` | Add photos (they must match the person's existing photos) |
| `PUT` | `/api/v1/persons/{id}/faces` | Replace all photos. All-or-nothing: if no new photo is usable, nothing changes |
| `DELETE` | `/api/v1/persons/{id}` | Delete a person and their face data. Nothing else is kept about them |
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
      "match": { "person_id": "9f1c…", "external_id": "EMP-1024", "name": "Asha Verma",
                 "metadata": { "class": "7-A" }, "similarity": 0.71 },
      "unknown_reason": null,
      "closest": null,
      "liveness_score": 0.98
    }
  ],
  "processing_ms": 9.3
}
```

Faces are ordered largest first. `landmarks` are: right eye, left eye, nose tip, right mouth corner, left mouth corner.
`match.person_id` is the id returned at enrollment: use it to look the person up in your own records. If two faces
in one image match the same person, both are returned with that `person_id`, so de-duplicate on your side.

| Query parameter | Default | Meaning |
|---|---|---|
| `liveness` | The server's `LIVENESS` setting (on) | Check that each recognised face is live (see [Liveness](#liveness)). `false` skips the check |
| `closest` | `false` | `true` adds `closest` for unknown faces, for testing and tuning |

### Unknown faces

When `match` is `null`, `unknown_reason` explains why:

| `unknown_reason` | Meaning | What to do |
|---|---|---|
| `below_threshold` | The closest person scored under `MATCH_THRESHOLD` | Not enrolled, or this photo differs a lot from the enrolled ones (add a similar photo) |
| `ambiguous` | Two enrolled people scored almost the same (`MATCH_MARGIN`) | Look-alikes or twins. Add more photos of both |
| `too_small` | Face narrower than `MIN_FACE_SIZE` real pixels | Move closer or use a larger photo |
| `nobody_enrolled` | The gallery is empty | Enroll someone |
| `liveness_failed` | Recognised, but the face looks like a printed photo or a screen | Someone held up a photo or phone. The match isn't returned |

With `?closest=true`, unknown faces also get `closest`: the nearest person and their similarity, to help testing and
tuning. It is off by default so that a kiosk can't learn who a stranger resembles. **Never treat `closest` as a
match** (for example, never mark someone present from it). It is never set for `liveness_failed` faces.

### Liveness

Liveness (anti-spoofing) catches a face **re-captured from a print or a screen** held up to the camera. When it is
on, every recognised face is checked. A face that fails gets `match: null`, `unknown_reason: "liveness_failed"` and
its `liveness_score`, so the person's id isn't returned. Faces that aren't recognised aren't checked and have
`liveness_score: null`.

- It is on by default (`LIVENESS=on`, threshold `LIVENESS_THRESHOLD`). With `LIVENESS=off` on the server there is
  no check and `liveness_score` is always `null`.
- Use `?liveness=false` for photos that were not taken by a live camera, such as tagging uploaded photos.
- It can't detect a digital photo sent straight to the API, so frames must come from a camera you control.
  See [privacy.md](privacy.md#liveness-detection-anti-spoofing).

## Enroll

Only the photos are required. The simplest call sends one photo and nothing else:

```bash
curl -X POST localhost:8000/api/v1/persons -H "X-API-Key: $API_KEY" -F images=@front.jpg
```

```json
{
  "person": { "id": "9f1c2a7e4b0d4e6f8a1b3c5d7e9f0a2b", "external_id": null, "name": null, "metadata": {},
              "face_count": 1, "created_at": "2026-11-14T09:22:17+05:30" },
  "accepted": 1,
  "rejected": []
}
```

**Store `person.id` in your system** (for example in a `face_id` column next to the student). Every match carries
it as `person_id`, so it is all you need to link a face back to your records, and FaceMatch doesn't have to hold any
other personal data.

All the optional fields together:

```bash
curl -X POST localhost:8000/api/v1/persons -H "X-API-Key: $API_KEY" \
  -F name="Asha Verma" -F external_id="STU0001" \
  -F 'metadata={"class": "7-A", "section": "B", "roll_no": 12}' \
  -F images=@front.jpg -F images=@left.jpg -F images=@right.jpg
```

| Field | Required | Meaning |
|---|---|---|
| `images` | Yes, 1–10 | Clear photos with one face each. 3–5 photos give the best accuracy |
| `name` | No | Full name. `null` in responses when not sent |
| `external_id` | No | Your own ID (admission number, employee code). Must be unique |
| `metadata` | No | Your own fields, as JSON text (see below) |
| `allow_duplicate` | No | `true` to enroll a face that resembles someone already enrolled (twins) |

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

### Your own fields (`metadata`)

`metadata` holds any fields you want to keep with a person, under your own keys:

```json
{ "class": "7-A", "section": "B", "roll_no": 12, "bus_route": "R4", "hostel": false }
```

- A flat JSON object: values are text, numbers, `true`/`false` or `null`. Nested objects and lists are rejected.
- Up to 50 keys of 1–64 characters each; text values up to 1000 characters.
- Returned with the person (`GET /persons`, `GET /persons/{id}`) and in `match` (and `closest`) from `/recognize`,
  so a kiosk can show it.
- Returned to kiosks too, so don't store secrets in it.
- Anything else fails with `422 invalid_metadata`, and nobody is enrolled.

## Update a person

`PATCH /api/v1/persons/{id}` takes a JSON body. Only the fields you send change:

```bash
curl -X PATCH localhost:8000/api/v1/persons/9f1c2a7e4b0d4e6f8a1b3c5d7e9f0a2b -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d '{"metadata": {"class": "8-A", "section": "B", "roll_no": 7}}'
```

The response is the updated person. `metadata` **replaces** all of the person's fields (it isn't merged), so send
the full set. `{}` or `null` removes them, and `"name": null` or `"external_id": null` removes those. Unknown fields
fail with `422 validation_error`, and an `external_id` used by someone else with `409 external_id_exists`. To change
photos, use `/persons/{id}/faces`.

## Delete a person

```bash
curl -X DELETE localhost:8000/api/v1/persons/9f1c2a7e4b0d4e6f8a1b3c5d7e9f0a2b -H "X-API-Key: $API_KEY"
```

Returns `204`. The person, their face data and their `metadata` are deleted. The only trace left is the audit log
entry for the deletion, which records the name and `external_id` (see [keys-and-audit.md](keys-and-audit.md#audit-log)).

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
| 422 | `validation_error`, `no_usable_face` (details list each rejected photo), `invalid_metadata`, `no_images`, `too_many_images` |
