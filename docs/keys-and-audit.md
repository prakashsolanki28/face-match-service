# Keys, kiosks and the audit log

FaceMatch is an API-only service. Every caller (your portal backend, each kiosk, monitoring) authenticates
with an API key.

## API keys

| Key | What it is |
|---|---|
| **Admin key** | The `API_KEY` setting. Can do everything, including creating and revoking other keys. Keep it on the server side only |
| **Device keys** | Keys you create per kiosk, integration or monitoring system, each limited to the permissions it needs. Only a SHA-256 hash is stored; the token is shown once |

| Scope | Allows |
|---|---|
| `attendance` | `POST /api/v1/attendance`. Give kiosks **only** this |
| `recognize` | `POST /api/v1/detect`, `POST /api/v1/recognize` |
| `read` | List people, attendance and summaries |
| `enroll` | Create, update and delete people and photos |
| `metrics` | `GET /metrics` |
| `admin` | Everything, including keys, webhook deliveries and the audit log |

```bash
# with the API
curl -X POST localhost:8000/api/v1/keys -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "gate-1 kiosk", "scopes": ["attendance"]}'
curl localhost:8000/api/v1/keys -H "X-API-Key: $API_KEY"
curl -X DELETE localhost:8000/api/v1/keys/<id> -H "X-API-Key: $API_KEY"

# or on the server
face-match keys create --name "gate-1 kiosk" --scopes attendance
face-match keys create --name "portal sync" --scopes read,enroll
face-match keys list
face-match keys revoke <id>
```

`GET /api/v1/auth/me` returns the name and scopes of the key you call it with, which is handy for checking a
device's setup. Revoked keys stop working within 30 seconds on every server.

## Building a kiosk

A kiosk is your own app (tablet, laptop or small PC with a camera) that sends camera frames to
`POST /api/v1/attendance` with an `attendance`-only device key, and shows the result. A minimal headless client
in Python with OpenCV:

```python
import time, cv2, requests

API, KEY, DEVICE = "https://face.example.com", "fm_...", "gate-1"
camera = cv2.VideoCapture(0)
while True:
    ok, frame = camera.read()
    if not ok:
        continue
    frame = cv2.resize(frame, (640, int(frame.shape[0] * 640 / frame.shape[1])))
    jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
    res = requests.post(
        f"{API}/api/v1/attendance",
        headers={"X-API-Key": KEY},
        files={"image": ("frame.jpg", jpeg, "image/jpeg")},
        data={"device_id": DEVICE},
        timeout=10,
    )
    if res.status_code in (401, 403):
        raise SystemExit("Key revoked or lacks the attendance scope")
    if res.status_code in (429, 503):
        time.sleep(int(res.headers.get("Retry-After", 2)))
        continue
    for face in res.json()["faces"][:1]:  # the largest face is the person in front of the camera
        status = face["attendance"]["status"]
        if status == "marked":
            print("Welcome,", face["match"]["name"])
        elif status == "already_marked":
            print("Already checked in:", face["match"]["name"])
        elif status == "liveness_failed":
            print("Please look at the camera (photos and screens aren't accepted)")
        elif status == "too_small":
            print("Please come closer")
    time.sleep(0.7)
```

Guidelines for production kiosks:

- **Send live camera frames only.** Never let a kiosk upload stored photos: liveness detects photos held up to a
  camera, not image files sent directly.
- Send about one frame every 0.5–1 s. The cooldown (`ATTENDANCE_COOLDOWN_SECONDS`) prevents duplicates.
- Show "Not recognised" only after a few unknown frames in a row, to avoid flicker.
- Honour `Retry-After` on `429`/`503`, back off on network errors, and stop if the key is rejected.
- A browser-based kiosk needs HTTPS (or localhost) for camera access, and should keep its key in a backend or
  device-protected storage. Kiosk keys should only ever have the `attendance` scope.

## Audit log

`GET /api/v1/audit` (admin) records who did what: enrollments, photo changes, deletions (including whether history
was erased), device keys created or revoked, and webhook retries. Each entry has the actor (`admin_key` or
`key:<name>#<id>`), time, IP and request id. Filter with `?action=person.*` or `?actor=key:portal-sync#3f9c…`.
Entries are kept forever by default; set `AUDIT_RETENTION_DAYS` to limit that.
