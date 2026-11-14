# Accuracy

## Measure it on your own photos

`face-match evaluate` replays the enrollment-and-recognition workflow with the service's real code:

1. Enroll each person from *K* photos, through the normal enrollment checks.
2. Try to recognise every *other* photo of them.
3. Show it photos of people who were never enrolled ("strangers").

```bash
face-match evaluate --enroll-dir ./enroll-photos --probe-dir ./other-photos --enroll-per-person 1,3,5
```

Identity comes from the folder name (`photos/Asha Verma/1.jpg`) or the file name (`Asha Verma_12.jpg`). The tool
prints results at the current settings plus a threshold/margin sweep, and writes a JSON report and a CSV of every
wrong-name case to `eval-results/`. Use your real setting: portal photos as the enrollment set, photos from the
real camera as probes.

Every probe photo gets one outcome:

| Outcome | Meaning |
|---|---|
| correct | the right name |
| wrong name | another enrolled person's name: **the costly error** |
| unknown | no name; a person handles it |
| no face | no usable face in the photo |
| stranger given a name | a never-enrolled person was named: **costly** |

## Results (v1.0 defaults)

The evaluation set has 31 people and 2,562 web photos of celebrities, with heavy variation in age, make-up, pose
and lighting. Each person was enrolled from **160×160 face crops**, which resemble passport thumbnails. They were
then recognised from **different full-size photos**: 2,016 probes of 25 enrolled people, plus 471 photos of 6 people
who were never enrolled.

| Photos per person | Correct | Wrong name | Unknown | No face | Strangers given a name |
|---|---|---|---|---|---|
| 1 | 86.2% | **0** | 13.6% | 0.2% | **0** of 471 |
| 3 | 96.8% | **0** | 3.0% | 0.2% | 1 of 471 |
| 5 | 97.9% | **0** | 1.9% | 0.2% | 1 of 471 |

The one stranger match is a genuine look-alike at similarity 0.405, just over the threshold. `MATCH_THRESHOLD=0.45`
removes it (93.6% correct with 3 photos, 96.3% with 5), at the cost of more unknowns. With only one photo per
person, 0.45 drops correct to 77%, which is why 0.40 is the default.

## How the defaults were chosen

| Setting | Finding |
|---|---|
| `DETECTION_THRESHOLD=0.60` | At 0.9, **16% of ordinary portraits had no face detected**. Down to 0.5 there were 0 false detections across 2,562 single-face crops |
| `MATCH_THRESHOLD=0.40` | The lowest value with no wrong names. At 0.35, strangers start getting names |
| `MATCH_MARGIN=0.05` | With margin 0, one wrong name appeared. With 0.05, none |
| `DUPLICATE_THRESHOLD=0.50` | Same-person vs different-person similarity over 3.2 million photo pairs: at 0.40, 0.032% of different-person pairs exceed it, which across 400 people × 3 photos is about a **30% chance per new photo** of a false "duplicate". At 0.50, about 0.4% |
| `MIN_FACE_SIZE=40` | Accuracy by face width: 24 px → 74.8% with a wrong name; 32 px → 88.7%; 40 px → 91.6%; 56 px+ → about 94% |

## Tips

- Enroll **3–5 photos** per person: front, slightly left, slightly right, ideally from the real camera.
- Front lighting. A window behind the person is the most common cause of misses.
- Faces should be **100 px or wider** in the frame. Mount the camera at face height.
- If someone is often unknown, send one of their failing frames to `POST /api/v1/recognize`: the response's
  `closest` shows who they were nearest to and the score (e.g. 0.33 against a 0.40 threshold). Then add a photo like
  the one that failed with `POST /api/v1/persons/{id}/faces`.

## Automated tests vs. accuracy

The automated test suite checks the service's logic with generated images and a fake face engine, and checks
that the real models load and run. It doesn't contain photos of people, so it doesn't measure recognition
accuracy. The results above come from a one-off evaluation; the evaluation photos aren't part of this repository.
Measure on your own photos with `face-match evaluate`.
