# Privacy and security

Face data is **biometric personal data** under most privacy laws, including the GDPR (EU/UK), India's DPDP Act and
several US state laws such as BIPA. If you deploy FaceMatch, you are responsible for using it lawfully.
This page explains what the service stores and what you need to add.

## What FaceMatch stores

| Data | Stored? | Where |
|---|---|---|
| Uploaded photos and frames | **No.** Processed in memory and discarded | – |
| Face embeddings (128 numbers per enrollment photo) | Yes | `DATA_DIR/facematch.db` |
| Name, `external_id` and `metadata` | Only if you send them. Photos alone are enough to enroll | same database |
| Recognition results (who was seen, when, where) | **No.** Returned in the response and not recorded. Your portal decides what to keep | – |
| Audit log (enrollments, updates, deletions, with name and `external_id`) | Yes, until past `AUDIT_RETENTION_DAYS` | same database |
| Backups | Copies of all of the above | `DATA_DIR/backups/` |

Embeddings can't be turned back into a photo, but they are still biometric identifiers. Protect the database
like any other personal data. Attendance or access records built from FaceMatch's answers live in your own portal,
so cover them in your portal's privacy policy.

**Upgraded from a version with built-in attendance?** The old `attendance` and `webhook_outbox` tables are left in
the database untouched (not used any more), so `DELETE /api/v1/persons/{id}` doesn't remove a person's old rows in
them. Export what you need, then drop them:

```bash
sqlite3 data/facematch.db "DROP TABLE IF EXISTS attendance; DROP TABLE IF EXISTS webhook_outbox;"
# PostgreSQL: run the same two DROP TABLE statements in psql
```

## Built-in data-protection controls

| Need | How |
|---|---|
| **Right to erasure** | `DELETE /api/v1/persons/{id}` removes the person, their face data and their `metadata`. The audit log keeps an entry for the deletion (with the name and `external_id`, if any) until `AUDIT_RETENTION_DAYS` |
| **Retention limit** | `AUDIT_RETENTION_DAYS=365` (for example) deletes older audit entries automatically, checked hourly |
| **File protection** | The database and backups are created owner-only (mode 0600). The Docker image runs as a non-root user |
| **Encryption at rest** | Use disk or volume encryption (LUKS, BitLocker, FileVault, encrypted EBS/managed disks). FaceMatch doesn't encrypt inside the database file |
| **Backups** | Automatic, consistent and rotated (see [deployment.md](deployment.md#backups-and-upgrades)). Store off-site copies encrypted, and include them in your retention policy: erased people remain in older backups until those rotate out |
| **Access control** | One API key protects the whole API. Keep it on servers and locked-down kiosks you control, and change it if a device is lost |

## Retention policy template

Adapt this and publish it to the people whose faces you process:

> We process face data only to recognise you for *[attendance / building access]* at *[organisation]*. The face
> recognition service stores a numeric face template (not photos), linked to an ID from our *[portal]*, and
> *[your name / no other personal data]*. Camera images are processed and discarded straight away. Face templates
> are deleted within *[7]* days of you leaving, or on request. *[Attendance]* records are kept in our *[portal]*
> for *[1 year]* and then deleted. Backups containing this data are kept for *[7]* days. To opt out, or to ask for
> erasure, contact *[contact]*.

## Your responsibilities

- **Consent and notice:** tell people their face is processed, and why. Get explicit consent where the law
  requires it (for children, usually from a parent or guardian). Offer an alternative for people who opt out.
- **Data minimisation:** you can enroll with photos only and keep the returned `person.id` in your own system. Then
  FaceMatch holds face templates but no names. Only put in `metadata` what kiosks and apps actually need: it is
  returned with every match.
- **Retention:** delete people who leave (`DELETE /api/v1/persons/{id}`). Decide how long your portal keeps the
  attendance or access records it builds from matches.
- **Access:** set `API_KEY`, use HTTPS, restrict `CORS_ORIGINS`, keep the API off the public internet where possible.
- **Never put the API key in public web pages.** Call FaceMatch from your backend or from kiosks you control.

## Built-in safeguards

- Refuses to start on a network interface without `API_KEY`.
- Never reveals who an unknown face resembles unless you ask for it with `/recognize?closest=true` (for testing).
- Prefers "unknown" to a wrong name (match threshold + margin), and doesn't judge faces that are too small.
- Doesn't record who was recognised: nothing about a `/recognize` or `/detect` request is stored.

## Liveness detection (anti-spoofing)

`/recognize` runs a passive liveness check (`LIVENESS=on`, the default) on every recognised face before returning
the match. It uses Minivision's MiniFASNet pair (two models of about 1.7 MB, about 3–5 ms per face). A face that looks
like a **printed photo or a screen held up to the camera** gets `liveness_failed`: the match (id, name and
`metadata`) is not returned. A request can skip the check with `?liveness=false`, for photos that don't come from a
live camera; kiosks and cameras should never do that.

On Minivision's reference captures: real person 0.9999 (live), printed photo 0.23 and phone screen 0.003 (both
rejected at the default `LIVENESS_THRESHOLD=0.5`).

**What it does not do. Read this before relying on it:**

- **It can't spot a digital photo sent directly to the API.** It detects re-capture artifacts (print texture,
  screen moiré), so an uploaded image file scores as "live". That's why your portal should only accept matches
  (for attendance, access and so on) from kiosks you control, and why kiosks should only ever send live camera
  frames.
  Protect the API key on kiosks; change it if a device is lost.
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
