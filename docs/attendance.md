# Attendance guide: schools and offices

How to run FaceMatch for hundreds of people, for example a school whose portal already holds one passport-size
photo per student.

## 1. Export a roster and photos

```csv
admission_no,student_name,class,photo
STU0001,Asha Verma,7-A,STU0001.jpg
STU0003,Meera Iyer,8-B,STU0003.jpg;STU0003_live.jpg
```

**Photo checklist:** one face, looking at the camera, JPG/PNG/WebP, ideally **100×125 px or larger**. Small
portal thumbnails are upscaled for detection and work down to about 80×100 px.

## 2. Import

```bash
face-match import --dry-run --csv students.csv --photos ./photos
face-match import --csv students.csv --photos ./photos --api-key "$API_KEY"
```

```
[  1/412] enrolled     STU0001        Asha Verma
[  2/412] failed       STU0002        Ravi Kumar  (no_usable_face: STU0002.jpg: no face found)
...
Done in 8.6s: 405 enrolled, 7 failed          (illustrative numbers)
```

Replace the failed photos and run the same command again; enrolled students are skipped. Twins are flagged as
matching each other. Re-run those rows with `--allow-duplicates`; when a twin is seen, the answer is `unknown`
rather than a guess.

## 3. Add one live photo per person (the biggest accuracy gain)

Portal photos are often years old and studio-lit. One photo taken with the actual kiosk camera helps more than
anything else: in our evaluation, going from 1 to 3 photos per person cut "unknown" from 13.6% to 3.0%.

- **API:** `POST /api/v1/persons/{id}/faces` with a photo from the kiosk camera (look the id up with
  `GET /api/v1/persons?external_id=…`).
- **In bulk:** `face-match import ... --add-photos`.
- **New school year:** `face-match import ... --replace`.

## 4. Daily use

- **Kiosk:** your camera app at the entrance sends frames to `/api/v1/attendance` with an attendance-only device key
  (see [keys-and-audit.md](keys-and-audit.md)). Each person needs 1–2 seconds in front of it. One server handles
  about 25–35 kiosks; if 400 people arrive within 20 minutes, use 2–4 entry points.
- **Records:** `GET /api/v1/attendance/summary?date=…` lists who was present; compare with `GET /api/v1/persons`
  for absentees.
- **Your portal:** receives each check-in by webhook or pulls the daily summary (see [integration.md](integration.md)).

## Why it doesn't guess

With hundreds of people enrolled, some will look alike to the model. FaceMatch prefers "unknown" (a teacher marks
that person by hand) to a wrong name:

- **`MATCH_MARGIN`:** the best match must clearly beat the second-best person.
- **`DUPLICATE_THRESHOLD`:** catches the same face enrolled under two IDs, without flagging classmates who merely
  look alike.
- **Consistent photo sets:** an odd photo (sunglasses, wrong person) is dropped at enrollment, whatever the upload order.
- **`MIN_FACE_SIZE`:** faces too small to judge are reported as `too_small`, not guessed.

## Before going live

- Run `face-match evaluate` on a pilot class: portal photos as the enrollment set, kiosk photos as probes
  (see [accuracy.md](accuracy.md)).
- Read [privacy.md](privacy.md): consent (for children, usually from parents), retention, and the missing
  liveness check.
