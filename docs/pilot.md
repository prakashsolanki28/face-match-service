# Running a pilot before go-live

Lab numbers don't replace a pilot. Your people, cameras, lighting and photos decide real accuracy. Run FaceMatch
**alongside** your normal process for 1–2 weeks with one class or team (30–50 people), then decide with data.
The example below is attendance, which your portal builds from FaceMatch's matches; the same steps work for
access control or anything else.

## 1. Set up

1. Deploy FaceMatch with `API_KEY` and liveness on (the default). See [deployment.md](deployment.md).
2. Enroll the group with `face-match import` from your portal photos, then fix every row the import report flags.
3. Add **one live photo per person** taken with the kiosk camera (`POST /api/v1/persons/{id}/faces`, or
   `face-match import --add-photos`).
4. Point your kiosk app at `/api/v1/recognize` with the API key, and have your portal mark the matched people
   present (see [integration.md](integration.md#4-mark-attendance-in-your-portal) and
   [keys-and-audit.md](keys-and-audit.md)). Keep the similarity and camera of each mark, so you can investigate.
5. Keep taking attendance the usual way (paper or portal). That register is your ground truth.

## 2. Measure every day

Compare your portal's marks for the day with the register, person by person:

| Outcome | Meaning |
|---|---|
| Confirmed | Present in the register and marked from a match |
| Missed | Present in the register, not marked (unknown, too small, or skipped the kiosk) |
| **False present** | Absent in the register but marked: a wrong name, a spoof, or a register mistake. Investigate every one |
| Not enrolled | Present in the register but not enrolled in FaceMatch |

Before the pilot, measure accuracy offline with `face-match evaluate` on kiosk photos of the group (see
[accuracy.md](accuracy.md)): it reports correct, unknown and wrong answers without involving your portal.

Also watch `/metrics`:

- `facematch_faces_total{endpoint="recognize",outcome="liveness_failed"}`: if real people often fail
  liveness, check lighting and camera placement, or lower `LIVENESS_THRESHOLD` carefully.
- `outcome="below_threshold"` / `"ambiguous"`: people who need better or extra photos.

## 3. Test spoofing on purpose

On a few days, ask volunteers to try to cheat the kiosk: hold up a printed photo, a photo on a phone, and a video
on a tablet of an enrolled person. Write down each attempt and whether it was marked. A blocked attempt gets
`unknown_reason: "liveness_failed"` from `/recognize`, so your portal doesn't mark it.

## 4. Go-live criteria (suggested)

| Measure | Target |
|---|---|
| False presents (wrong name or spoof) | **0** across the whole pilot |
| Confirmed / present | ≥ 95% after the first week (once live photos are added) |
| Spoof attempts accepted | 0 for printed photos; investigate any phone/screen success before go-live |
| Real people rejected by liveness | < 2% of check-ins |

If a target is missed, fix the cause (photos, lighting, camera height, thresholds) and run another week.
Record the final numbers. They're your evidence that the system is fit for purpose.
