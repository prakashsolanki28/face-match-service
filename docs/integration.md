# Integrating with a portal

FaceMatch is a separate service. Your portal (school system, HRMS, LMS, CRM, mobile app) talks to it over HTTP.
It works with any language: everything is JSON plus image uploads.

```
 ┌──────────────┐   photo / frame    ┌──────────────┐
 │ Camera / app │ ─────────────────▶ │   FaceMatch  │
 └──────────────┘ ◀───────────────── │ detect·match │
                   who + status       └──────┬───────┘
                                             │ webhook (signed)
                                             ▼
                                     ┌───────────────┐
                                     │  Your portal  │
                                     └───────────────┘
```

## 1. Map identities with `external_id`

Enroll each person with `external_id` set to your own primary key (student number, employee code). Every match,
attendance event and webhook carries it back, so you never have to store FaceMatch's internal IDs.
To look someone up: `GET /api/v1/persons?external_id=EMP-1024`.

## 2. Keep the people in sync

| Portal event | Call |
|---|---|
| Person added | `POST /api/v1/persons` with name, `external_id` and photo(s) |
| Extra photo (e.g. a live photo from the kiosk) | `POST /api/v1/persons/{id}/faces` |
| Photo replaced (new year, new ID card) | `PUT /api/v1/persons/{id}/faces`. All-or-nothing: old photos are kept if the new ones fail |
| Person leaves | `DELETE /api/v1/persons/{id}`. Removes face data; attendance history stays |

### Bulk import

To enroll many people at once, export a CSV and a photo folder from your portal:

```bash
face-match import --dry-run --csv people.csv --photos ./photos                # check files, no server needed
face-match import --csv people.csv --photos ./photos --api-key "$API_KEY"     # enroll
face-match import --csv people.csv --photos ./photos --replace                # later: replace photos
```

- Recognised column names: ID (`external_id`, `student_id`, `admission_no`, `roll_no`, `employee_id`, `id`),
  name (`name`, `full_name`, `student_name`), photo (`photo`, `photos`, `image`, `filename`, `file`;
  separate several photos with `;`). Other columns are ignored. See [examples/students_template.csv](../examples/students_template.csv).
- Without a CSV, file names are used: `EMP-1024_Asha Verma.jpg`, or a sub-folder per person.
- Every run writes a CSV report with one row per person and the reason for any failure. Re-running skips people
  already enrolled. Use `--add-photos` / `--replace` to update them, and `--allow-duplicates` for twins.
- The importer ([face_match/tools/bulk_import.py](../face_match/tools/bulk_import.py)) uses only the Python
  standard library. You can copy that single file to any server with Python 3.9+ and run it with `python bulk_import.py`.

## 3. Send photos

Your kiosk, mobile app or a backend job that reads an IP camera posts images:

- `/api/v1/recognize`: identify only (e.g. photo tagging, verification screens).
- `/api/v1/attendance`: identify and log (once a second is fine; the cooldown prevents duplicates).

## 4. Receive results

### Webhooks

Set `WEBHOOK_URL` (and `WEBHOOK_SECRET`). Every new attendance event is POSTed:

```json
{ "id": "5b0c3c1e9a7f4f0c8f4c2b1a6d9e8f70",
  "event": "attendance.marked",
  "data": { "id": 42, "person_id": "9f1c…", "external_id": "EMP-1024", "name": "Asha Verma",
            "similarity": 0.71, "device_id": "main-gate", "timestamp": "2026-11-14T09:22:17+05:30", "day": "2026-11-14" } }
```

Verify the signature against the **raw** request body:

```js
// Node.js (Express: use express.raw() for this route)
const expected = "sha256=" + crypto.createHmac("sha256", process.env.WEBHOOK_SECRET).update(req.body).digest("hex");
const valid = crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(req.get("X-Signature-256") || ""));
```

```python
# Python
expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
valid = hmac.compare_digest(expected, request.headers.get("X-Signature-256", ""))
```

```php
// PHP
$expected = 'sha256=' . hash_hmac('sha256', file_get_contents('php://input'), getenv('WEBHOOK_SECRET'));
$valid = hash_equals($expected, $_SERVER['HTTP_X_SIGNATURE_256'] ?? '');
```

**Reliable delivery:** events are stored in the same database transaction as the attendance record, then
delivered by a background worker. Anything other than a `2xx` response is retried with exponential backoff
(`WEBHOOK_MAX_ATTEMPTS`, about 10 hours by default), and deliveries survive restarts.

- Every delivery of the same event carries the same `id` (also in the `X-Event-Id` header). **Store it and ignore
  repeats**, because a retry can arrive after your endpoint processed the first attempt but timed out replying.
- Respond quickly with `2xx` and process asynchronously if your handling is slow (the timeout is 5 s).
- Failed deliveries are listed at `GET /api/v1/webhooks?status=failed` and can be retried with
  `POST /api/v1/webhooks/{id}/retry`.

### Polling

`GET /api/v1/attendance/summary?date=YYYY-MM-DD` returns everyone seen that day. Anyone missing from it,
compared with `GET /api/v1/persons`, was absent.

## Calling the API from code

```python
import requests

r = requests.post(
    "http://localhost:8000/api/v1/recognize",
    headers={"X-API-Key": API_KEY},
    files={"image": open("photo.jpg", "rb")},
)
for face in r.json()["faces"]:
    print(face["match"]["external_id"] if face["match"] else f"unknown ({face['unknown_reason']})")
```

```js
const form = new FormData();
form.append("image", fileInput.files[0]);
const res = await fetch("https://face.example.com/api/v1/recognize", {
  method: "POST", headers: { "X-API-Key": API_KEY }, body: form,
});
const { faces } = await res.json();
```

Don't put the API key in public browser code. Call FaceMatch from your backend, or from a kiosk you control, and
give every client its own key with only the scopes it needs (see [api.md](api.md#authentication)).
