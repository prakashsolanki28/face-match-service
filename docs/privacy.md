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
| Attendance events (who, when, device, similarity) | Yes; kept after a person is deleted | same database |

Embeddings can't be turned back into a photo, but they are still biometric identifiers. Protect the database
like any other personal data.

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

## Known limitation: no liveness detection

FaceMatch checks *who* a face looks like, not whether it's a live person. **A printed photo or a phone screen can
pass.** That's acceptable for low-stakes attendance with a supervised kiosk, but not for access control or payments.
Before such uses, add presentation-attack detection, for example a passive liveness model (MiniFASNet-style ONNX
models exist) or an active challenge (blink or head turn), in front of identification.
