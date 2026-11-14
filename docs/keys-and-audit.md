# Kiosks and the audit log

FaceMatch is an API-only service. Every caller (your portal backend, each kiosk, monitoring) sends the one
API key, which gives full access. Setting, using and changing the key is covered in
**[api-keys.md](api-keys.md)**.

## Building a kiosk

A kiosk is your own app (tablet, laptop or small PC with a camera) that sends camera frames to
`POST /api/v1/recognize` with the API key, hands the matched ids to your portal, and shows the result. FaceMatch
only says who is in the frame; your portal decides what that means (for example, marking attendance once per
person per day). A minimal headless client in Python with OpenCV:

```python
import os, time, cv2, requests

API, DEVICE = "https://face.example.com", "gate-1"
KEY = os.environ["FACE_API_KEY"]  # the API key; keep it out of the source code
PORTAL = "https://portal.example.com/api/check-in"  # your own portal's endpoint
camera = cv2.VideoCapture(0)
while True:
    ok, frame = camera.read()
    if not ok:
        continue
    frame = cv2.resize(frame, (640, int(frame.shape[0] * 640 / frame.shape[1])))
    jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
    res = requests.post(
        f"{API}/api/v1/recognize",
        headers={"X-API-Key": KEY},
        files={"image": ("frame.jpg", jpeg, "image/jpeg")},
        timeout=10,
    )
    if res.status_code == 401:
        raise SystemExit("API key rejected (was it changed?)")
    if res.status_code in (429, 503):
        time.sleep(int(res.headers.get("Retry-After", 2)))
        continue
    for face in res.json()["faces"][:1]:  # the largest face is the person in front of the camera
        if face["match"]:
            # Your portal marks the person present and ignores repeats (the same person is in many frames).
            requests.post(
                PORTAL, json={"person_id": face["match"]["person_id"], "device": DEVICE}, timeout=10
            )
            print("Welcome,", face["match"]["name"] or face["match"]["person_id"])
        elif face["unknown_reason"] == "liveness_failed":
            print("Please look at the camera (photos and screens aren't accepted)")
        elif face["unknown_reason"] == "too_small":
            print("Please come closer")
    time.sleep(0.7)
```

Guidelines for production kiosks:

- **Send live camera frames only.** Never let a kiosk upload stored photos: liveness detects photos held up to a
  camera, not image files sent directly.
- Send about one frame every 0.5–1 s. The same person is recognised in many frames in a row, so your portal must
  ignore repeats (for example, one check-in per person per day).
- Never use `closest` (only returned with `?closest=true`) to mark anyone: it is the nearest person to an unknown
  face, not a match. Leave liveness on (don't send `?liveness=false` from a kiosk).
- Show "Not recognised" only after a few unknown frames in a row, to avoid flicker.
- Honour `Retry-After` on `429`/`503`, back off on network errors, and stop if the key is rejected.
- **The key on a kiosk opens the whole API**, so lock the device down: kiosk mode, no browsing, disk encryption.
  If a kiosk is lost or stolen, change the key straight away (see [api-keys.md](api-keys.md#3-change-rotate-the-key)).
- A browser-based kiosk needs HTTPS (or localhost) for camera access. Don't put the key in the page's
  JavaScript: let the browser talk to your own backend, and have the backend call FaceMatch with the key.

## Audit log

`GET /api/v1/audit` records every change: enrollments (`person.create`), photo changes (`person.add_faces`,
`person.replace_faces`), updates (`person.update` lists which fields changed, not their values) and deletions
(`person.delete`). Recognition requests aren't recorded. Each entry has the actor (`api_key`), time, client IP and
request id, so you can tell which device made a change by its IP. Filter with `?action=person.*`. Entries are kept forever by default;
set `AUDIT_RETENTION_DAYS` to limit that.
