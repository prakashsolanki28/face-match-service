# Accounts, roles, kiosks and the audit log

## Sign-in accounts

The web UI supports user accounts with roles. Kiosks don't use accounts; they use device keys (below).

| Role | Can |
|---|---|
| `admin` | Everything: users, device keys, audit log, webhook deliveries, plus all operator rights |
| `operator` | Enroll and edit people, identify faces, take attendance with the camera, view records (e.g. teachers, front office) |
| `viewer` | View records only (e.g. management reports) |

**Create the first admin** on the server (the password is asked twice and never shown):

```bash
face-match users create --username admin --role admin
# Docker: docker exec -it face-match face-match users create --username admin --role admin
```

Then open the UI and sign in. Add more people in **Admin → Users**, or with the CLI:
`face-match users create|list|set-password|disable|enable|delete`.

How sessions are protected:

- Passwords are hashed with scrypt; at least 10 characters.
- Sessions use an HttpOnly, SameSite=Strict cookie (Secure on HTTPS; see `SESSION_COOKIE_SECURE`) and last
  `SESSION_HOURS` (default 12).
- Every change also needs a per-session **CSRF token** that only the FaceMatch page can read.
- Wrong passwords are throttled per IP and per username. Resetting a password or disabling a user signs them out
  everywhere at once.
- The last active admin can't be deleted, disabled or demoted.

Once any account or device key exists, the whole API requires authentication. The admin `API_KEY` keeps working for
scripts and recovery.

## Kiosks

A kiosk is a tablet or laptop at an entrance running the full-screen check-in page `/kiosk.html`. It has no tabs and
no admin functions. It can only mark attendance, because it uses a device key with just the `attendance` permission.

1. **Admin → Device keys**: name it (e.g. `gate-1 kiosk`), keep only **attendance** ticked, and create it.
2. Click **open kiosk** next to the new key on the tablet (or paste the key into `/kiosk.html`'s setup screen). The key
   is stored on the device and removed from the address bar.
3. Allow camera access. Put the browser in full-screen or kiosk mode (Android "screen pinning", iPad Guided
   Access, or Chrome's `--kiosk` flag).

What the kiosk does:

- Captures a frame about every 0.7 s and shows **Welcome, Name**, **Already checked in at 08:12**, **Please come
  closer**, or **Not recognised** (only after several unknown frames, to avoid flicker).
- Liveness is on: a photo or screen held up shows **Please look directly at the camera**, and nothing is recorded.
- Keeps the screen awake, retries automatically when the network drops, and backs off when the server is busy.
- If the key is revoked, the kiosk stops within about 30 seconds and asks for a new key.
- **Hidden reset:** tap the top-right corner three times within two seconds.

The page needs HTTPS (or localhost) for camera access. See [deployment.md](deployment.md#https).

## Audit log

**Admin → Audit log** (or `GET /api/v1/audit`) records who did what: enrollments, photo changes, deletions (including
whether history was erased), device keys created or revoked, users created, changed or deleted, sign-ins, failed
sign-ins and sign-outs, and webhook retries.

Each entry has the actor (`user:asha`, `key:portal-sync#3f9c…`, `admin_key`), time, IP and request id. Filter with
`?action=person.*` or `?actor=user:asha`. Keep entries forever (default), or set `AUDIT_RETENTION_DAYS`.
