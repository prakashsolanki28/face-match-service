# Running a pilot before go-live

Lab numbers don't replace a pilot. Your people, cameras, lighting and photos decide real accuracy. Run FaceMatch
**alongside** your normal attendance process for 1–2 weeks with one class or team (30–50 people), then decide with data.

## 1. Set up

1. Deploy FaceMatch with `API_KEY`, `TIMEZONE` and liveness on (the default). See [deployment.md](deployment.md).
2. Enroll the group with `face-match import` from your portal photos, then fix every row the import report flags.
3. Add **one live photo per person** with the kiosk camera (Enroll tab → "Add these photos to …").
4. Create one key per kiosk: `face-match keys create --name pilot-kiosk --scopes attendance`.
5. Keep taking attendance the usual way (paper or portal). That register is your ground truth.

## 2. Measure every day

Export the day's register as CSV (`external_id,status`, with `present`/`absent` or `P`/`A`), then:

```bash
face-match pilot-report --register register-2026-11-14.csv --date 2026-11-14 --api-key "$READ_KEY"
```

```
Pilot report for 2026-11-14: 42 people in the register, 39 present
  confirmed          37  (94.9% of those present were marked)
  missed              2
  not enrolled        0
  not in register     0
  FALSE PRESENT       0  (must be 0: wrong name, spoof or register error)
```

The command exits with status 1 if there is any `FALSE_PRESENT`, and writes a per-person CSV.

Also watch `/metrics` (or the demo UI's Activity list):

- `facematch_faces_total{endpoint="attendance",outcome="liveness_failed"}`: if real people often fail
  liveness, check lighting and camera placement, or lower `LIVENESS_THRESHOLD` carefully.
- `outcome="below_threshold"` / `"ambiguous"`: people who need better or extra photos.

## 3. Test spoofing on purpose

On a few days, ask volunteers to try to cheat the kiosk: hold up a printed photo, a photo on a phone, and a video
on a tablet of an enrolled person. Write down each attempt and whether it was marked (`pilot-report` shows it as
`FALSE_PRESENT` if the person was absent, or check the Activity list).

## 4. Go-live criteria (suggested)

| Measure | Target |
|---|---|
| False presents (wrong name or spoof) | **0** across the whole pilot |
| Confirmed / present | ≥ 95% after the first week (once live photos are added) |
| Spoof attempts accepted | 0 for printed photos; investigate any phone/screen success before go-live |
| Real people rejected by liveness | < 2% of check-ins |

If a target is missed, fix the cause (photos, lighting, camera height, thresholds) and run another week.
Record the final numbers. They're your evidence that the system is fit for purpose.
