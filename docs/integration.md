# Integrating with a portal

FaceMatch is a separate service that only detects and matches faces. Your portal (school system, HRMS, LMS, CRM,
mobile app) keeps everything else, including attendance. They talk over HTTP, so any language works: everything is
JSON plus image uploads.

```
 ┌──────────────┐   1. photo / frame   ┌──────────────┐
 │ Camera / app │ ───────────────────▶ │   FaceMatch  │
 │  or portal   │ ◀─────────────────── │ detect·match │
 └──────┬───────┘   2. person ids      └──────────────┘
        │ 3. mark present, log, notify
        ▼
 ┌───────────────┐
 │  Your portal  │
 └───────────────┘
```

## 1. Link people to your records

Pick one of these (or use both):

- **Store FaceMatch's id.** Enroll with photos only and save the returned `person.id` in your portal, for example
  in a `face_id` column next to the student. Every match carries it as `person_id`, so FaceMatch never needs a name
  or any other personal data.
- **Send your own ID.** Enroll with `external_id` set to your primary key (student number, employee code). Every
  match carries it back, so you don't have to store FaceMatch's ids. To look someone up:
  `GET /api/v1/persons?external_id=EMP-1024`.

`name` is optional either way. To keep extra fields with a person, such as class and section for a kiosk to show,
send `metadata` (see [api.md](api.md#your-own-fields-metadata)).

## 2. Keep the people in sync

| Portal event | Call |
|---|---|
| Person added | `POST /api/v1/persons` with photo(s), optionally with name, `external_id` and `metadata` |
| Details changed (new class, corrected name) | `PATCH /api/v1/persons/{id}` with only the fields that changed. `metadata` is replaced as a whole |
| Extra photo (e.g. a live photo from the kiosk) | `POST /api/v1/persons/{id}/faces` |
| Photo replaced (new year, new ID card) | `PUT /api/v1/persons/{id}/faces`. All-or-nothing: old photos are kept if the new ones fail |
| Person leaves | `DELETE /api/v1/persons/{id}`. Removes the person and their face data |

### Bulk import

To enroll many people at once, export a CSV and a photo folder from your portal:

```bash
face-match import --dry-run --csv people.csv --photos ./photos                # check files, no server needed
face-match import --csv people.csv --photos ./photos --api-key "$API_KEY"     # enroll
face-match import --csv people.csv --photos ./photos --replace                # later: replace photos
```

- Recognised column names: ID (`external_id`, `student_id`, `admission_no`, `roll_no`, `employee_id`, `id`),
  name (`name`, `full_name`, `student_name`), photo (`photo`, `photos`, `image`, `filename`, `file`;
  separate several photos with `;`). Other columns are ignored. Example:

  ```csv
  admission_no,student_name,class,photo
  STU0001,Asha Verma,7-A,STU0001.jpg
  STU0002,Ravi Kumar,7-A,STU0002.jpg;STU0002_live.jpg
  ```
- Without a CSV, file names are used: `EMP-1024_Asha Verma.jpg`, or a sub-folder per person.
- Every run writes a CSV report with one row per person and the reason for any failure. Re-running skips people
  already enrolled. Use `--add-photos` / `--replace` to update them, and `--allow-duplicates` for twins.
- The importer ([face_match/tools/bulk_import.py](../face_match/tools/bulk_import.py)) uses only the Python
  standard library. You can copy that single file to any server with Python 3.9+ and run it with `python bulk_import.py`.

## 3. Send photos, get ids back

Your kiosk, mobile app, or a backend job that reads an IP camera posts each image to `POST /api/v1/recognize`.
The answer comes back in the same request; FaceMatch records nothing about it.

| In the response | What to do with it |
|---|---|
| `faces[].match.person_id` | The person is recognised. Look them up by this id (or by `match.external_id`) |
| `faces[].match` is `null` | Not recognised. `unknown_reason` says why (`below_threshold`, `ambiguous`, `too_small`, `nobody_enrolled`, `liveness_failed`) |
| `faces[].closest` | Only with `?closest=true`, for testing. **Never act on it** |

A camera can send a frame about once a second. The same person is recognised in many frames in a row, and can
appear twice in one frame, so your portal decides what counts as a new event.

Liveness is checked on recognised faces by default, so a printed photo or a phone screen held up to the camera
gets `liveness_failed` instead of a match. Use `?liveness=false` only for photos that don't come from a live camera
(see [api.md](api.md#liveness)).

## 4. Mark attendance in your portal

FaceMatch tells you *who*; your portal decides what that means. A simple attendance rule: the first time a person
is recognised on a given day, mark them present; ignore later sightings that day. Faces without a match are left
for a teacher or receptionist to mark by hand.

```python
import datetime
import requests

FACEMATCH = "http://localhost:8000"


def handle_frame(jpeg_bytes, camera_id):
    r = requests.post(
        f"{FACEMATCH}/api/v1/recognize",
        headers={"X-API-Key": API_KEY},
        files={"image": ("frame.jpg", jpeg_bytes, "image/jpeg")},
        timeout=10,
    )
    r.raise_for_status()
    today = datetime.date.today().isoformat()
    for face in r.json()["faces"]:
        match = face["match"]  # ignore face["closest"]: it is never a match
        if match is None:
            continue  # unknown: leave it for manual marking
        # Your own table, with a unique key on (person_id, day): repeats are ignored.
        mark_present_once(
            person_id=match["person_id"], day=today, camera=camera_id, similarity=match["similarity"]
        )
```

```js
// Node.js 18+ (fetch, FormData and Blob are built in)
async function handleFrame(jpegBuffer, cameraId) {
  const form = new FormData();
  form.append("image", new Blob([jpegBuffer], { type: "image/jpeg" }), "frame.jpg");
  const res = await fetch("http://localhost:8000/api/v1/recognize", {
    method: "POST", headers: { "X-API-Key": process.env.FACEMATCH_API_KEY }, body: form,
  });
  if (!res.ok) throw new Error(`FaceMatch ${res.status}`);
  const today = new Date().toISOString().slice(0, 10);
  for (const face of (await res.json()).faces) {
    if (!face.match) continue; // unknown: leave it for manual marking; never use face.closest
    // Your own table, with a unique key on (person_id, day): repeats are ignored.
    await markPresentOnce({ personId: face.match.person_id, day: today, camera: cameraId });
  }
}
```

- Check-in and check-out: keep the first and the last sighting of the day instead of only the first.
- Absentees: everyone in your own roster without a mark for the day.
- Time zones: use your portal's time zone for "the day". FaceMatch's responses don't depend on it.
- Handle `429` and `503` by waiting for `Retry-After` and dropping or retrying the frame.

## Calling the API from code

```python
import requests

r = requests.post(
    "http://localhost:8000/api/v1/recognize",
    headers={"X-API-Key": API_KEY},
    files={"image": open("photo.jpg", "rb")},
)
for face in r.json()["faces"]:
    print(face["match"]["person_id"] if face["match"] else f"unknown ({face['unknown_reason']})")
```

```js
const form = new FormData();
form.append("image", fileInput.files[0]);
const res = await fetch("https://face.example.com/api/v1/recognize", {
  method: "POST", headers: { "X-API-Key": API_KEY }, body: form,
});
const { faces } = await res.json();
```

Don't put the API key in public browser code: it gives full access. Call FaceMatch from your backend, or from a
kiosk you control (see [api-keys.md](api-keys.md)).
