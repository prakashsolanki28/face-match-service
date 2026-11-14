# Privacy and security

Face data is **biometric personal data** under most privacy laws, including the GDPR (EU/UK), India's DPDP Act and
several US state laws such as BIPA. If you deploy FaceMatch, you are responsible for using it lawfully.
This page explains what the service stores and what you need to add.

## What FaceMatch stores

| Data | Stored? | Where |
|---|---|---|
| Uploaded photos and frames | **No.** Processed in memory and discarded | – |
| Face embeddings (128 numbers per enrollment photo) | Yes | `DATA_DIR/facematch.db` |
| Name and `external_id` | Yes | same database |
| Attendance events (who, when, device, similarity) | Yes, until erased or past `ATTENDANCE_RETENTION_DAYS` | same database |
| Webhook queue (event payloads) | Delivered/failed entries are purged after 30 days | same database |
| Backups | Copies of all of the above | `DATA_DIR/backups/` |

Embeddings can't be turned back into a photo, but they are still biometric identifiers. Protect the database
like any other personal data.

## Built-in data-protection controls

| Need | How |
|---|---|
| **Right to erasure** | `DELETE /api/v1/persons/{id}?erase_history=true` removes the face data, the attendance history and any queued webhooks for that person. Without the flag, history is kept (a record of who attended stays useful after someone leaves). The demo UI asks which you want |
| **Retention limit** | `ATTENDANCE_RETENTION_DAYS=365` (for example) deletes older attendance automatically, checked hourly |
| **File protection** | The database and backups are created owner-only (mode 0600). The Docker image runs as a non-root user |
| **Encryption at rest** | Use disk or volume encryption (LUKS, BitLocker, FileVault, encrypted EBS/managed disks). FaceMatch doesn't encrypt inside the database file |
| **Backups** | Automatic, consistent and rotated (see [deployment.md](deployment.md#backups-and-upgrades)). Store off-site copies encrypted, and include them in your retention policy: erased people remain in older backups until those rotate out |
| **Least privilege** | Per-device API keys with scopes. A kiosk key can only mark attendance |

## Retention policy template

Adapt this and publish it to the people whose faces you process:

> We process face data only to record attendance at *[organisation]*. We store a numeric face template
> (not photos), your name, your *[student/employee]* ID, and the times you were recognised. Face templates are
> deleted within *[7]* days of you leaving, or on request. Attendance records are kept for *[1 year]* and then
> deleted automatically. Backups containing this data are kept for *[7]* days. To opt out, or to ask for
> erasure, contact *[contact]*.

## Your responsibilities

- **Consent and notice:** tell people their face is processed, and why. Get explicit consent where the law
  requires it (for children, usually from a parent or guardian). Offer an alternative for people who opt out.
- **Retention:** delete people who leave (`DELETE /api/v1/persons/{id}`). Decide how long attendance history is kept.
- **Access:** set `API_KEY`, use HTTPS, restrict `CORS_ORIGINS`, keep the API off the public internet where possible.
- **Never put the API key in public web pages.** Call FaceMatch from your backend or from kiosks you control.

## Built-in safeguards

- Refuses to start on a network interface without `API_KEY`.
- `/attendance` (kiosk mode) never reveals who an unknown face resembles; only `/recognize` returns `closest`.
- Prefers "unknown" to a wrong name (match threshold + margin), and doesn't judge faces that are too small.
- Signed webhooks (`WEBHOOK_SECRET`).

## Liveness detection (anti-spoofing)

`/attendance` runs a passive liveness check (`LIVENESS=on`, the default) on every recognised face before recording
it. It uses Minivision's MiniFASNet pair (two models of about 1.7 MB, about 3–5 ms per face). A face that looks like a
**printed photo or a screen held up to the camera** gets `liveness_failed`: nothing is recorded, and the name is
not returned.

On Minivision's reference captures: real person 0.9999 (live), printed photo 0.23 and phone screen 0.003 (both
rejected at the default `LIVENESS_THRESHOLD=0.5`).

**What it does not do. Read this before relying on it:**

- **It can't spot a digital photo sent directly to the API.** It detects re-capture artifacts (print texture,
  screen moiré), so an uploaded image file scores as "live". That's why attendance should only come from kiosks
  with their own `attendance`-scope key, and why the demo UI never records attendance from uploaded photos.
  Protect kiosk keys; revoke them if a device is lost.
- **It doesn't stop virtual-camera or deepfake injection,** masks, or a determined attacker. It's a deterrent
  against casual cheating (a photo of a friend on a phone), not a security boundary for doors or payments.
- **Accuracy depends on your camera, lighting and distance.** The model was trained on phone-camera frames, and
  scores also shift with the face box. **Calibrate during the pilot** ([pilot.md](pilot.md)): measure how often real
  people fail (adjust `LIVENESS_THRESHOLD`, or improve lighting) and test print, phone and tablet attacks. The vendor's
  own app uses a stricter 0.915. Kiosks send a frame every 0.6 s, so an occasional false rejection just retries
  on the next frame.
- **Licence:** the models come from [minivision-ai/Silent-Face-Anti-Spoofing](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing),
  an Apache-2.0 repository trained on Minivision's private data (not research-only datasets). The weights live in
  that repository with no separate terms, but Minivision hasn't explicitly confirmed their licence (there are open
  issues asking). Review this with your legal adviser for commercial use. `LIVENESS=off` disables it entirely.
