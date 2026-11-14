"""Accuracy regression test on the real sample dataset.

Skipped unless the sample dataset is available (see docs/accuracy.md).
Scenario: each student is enrolled from ONE 160x160 face crop (like a portal passport photo),
then recognised from different, full-size photos. Some people are never enrolled (strangers).
"""

from __future__ import annotations

import itertools
import os
import random
from collections import Counter
from pathlib import Path

import pytest

# Point FACE_MATCH_DATASET at a folder with "Faces/Faces/<Name>_<n>.jpg" crops and
# "Original Images/Original Images/<Name>/*.jpg" photos (the layout of the 31-person sample set).
DATASET = Path(os.environ.get("FACE_MATCH_DATASET", Path(__file__).resolve().parent.parent / "dataset"))
FACES = DATASET / "Faces" / "Faces"
ORIGINALS = DATASET / "Original Images" / "Original Images"

pytestmark = pytest.mark.skipif(
    not (FACES.is_dir() and ORIGINALS.is_dir()), reason="sample dataset not extracted to dataset/"
)

STUDENTS, STRANGERS, PROBES_EACH = 12, 4, 4


def _people():
    return sorted({p.stem.rsplit("_", 1)[0] for p in FACES.glob("*_0.jpg")})


def _enroll_crops(client, name: str, external_id: str, numbers=range(5), allow_duplicate=False):
    files = [
        ("images", (f"{name}_{n}.jpg", (FACES / f"{name}_{n}.jpg").read_bytes(), "image/jpeg"))
        for n in numbers
    ]
    data = {"name": name, "external_id": external_id}
    if allow_duplicate:
        data["allow_duplicate"] = "true"
    return client.post("/api/v1/persons", data=data, files=files)


def _crop_embeddings(service, name: str, numbers=range(5)):
    vectors = []
    for n in numbers:
        _, faces = service.analyze((FACES / f"{name}_{n}.jpg").read_bytes())
        if faces and faces[0].embedding is not None:
            vectors.append(faces[0].embedding)
    return vectors


def test_look_alikes_are_not_flagged_as_duplicates(client):
    """Two different people whose photos score 0.40-0.50 against each other: the old rule (duplicate at
    the 0.40 match threshold) rejected such photos; DUPLICATE_THRESHOLD=0.50 must keep them."""
    service = client.app.state.service
    embeddings = {name: _crop_embeddings(service, name) for name in _people()}
    pairs = []
    for a, b in itertools.combinations(embeddings, 2):
        sims = [float(x @ y) for x in embeddings[a] for y in embeddings[b]]
        if sims and 0.40 <= max(sims) < 0.50:
            pairs.append((max(sims), a, b))
    if not pairs:
        pytest.skip("no look-alike pair in this dataset")
    _, first, second = max(pairs)
    assert _enroll_crops(client, first, "P-1").status_code == 201
    res = _enroll_crops(client, second, "P-2")
    assert res.status_code == 201, res.text
    assert not [r for r in res.json()["rejected"] if r["reason"] == "matches_other_person"]


def test_enrollment_result_does_not_depend_on_photo_order(client):
    """The photo that agrees best with the others is the reference, so reversing the upload order
    accepts the same photos. (Before the fix, an odd first photo could reject all the good ones.)"""
    for i, name in enumerate(_people()[:8]):
        forward = _enroll_crops(client, name, f"F-{i}", numbers=range(5))
        backward = _enroll_crops(client, name, f"B-{i}", numbers=range(4, -1, -1), allow_duplicate=True)
        assert forward.status_code == backward.status_code == 201, (name, forward.text, backward.text)
        assert forward.json()["accepted"] == backward.json()["accepted"], name


def _upload(client, path: str, image: Path, **data):
    return client.post(path, data=data, files={"image": (image.name, image.read_bytes(), "image/jpeg")})


def test_one_passport_style_photo_recognises_real_photos(client):
    rng = random.Random(7)
    people = sorted(p.name for p in ORIGINALS.iterdir() if p.is_dir())
    picked = rng.sample(people, STUDENTS + STRANGERS)
    students, strangers = picked[:STUDENTS], picked[STUDENTS:]

    enrolled_from = {}
    for i, name in enumerate(students):
        # Like an admin replacing a rejected photo: try the next crop until one is accepted.
        for crop in sorted(FACES.glob(f"{name}_*.jpg"))[:5]:
            res = client.post(
                "/api/v1/persons",
                data={"name": name, "external_id": f"STU{i:04d}"},
                files=[("images", (crop.name, crop.read_bytes(), "image/jpeg"))],
            )
            if res.status_code == 201:
                enrolled_from[name] = crop.stem
                break
    assert len(enrolled_from) == STUDENTS, f"could not enroll: {set(students) - set(enrolled_from)}"

    outcome = Counter()
    wrong = []
    for name in students + strangers:
        shots = [p for p in sorted((ORIGINALS / name).glob("*.jpg")) if p.stem != enrolled_from.get(name)]
        for photo in rng.sample(shots, PROBES_EACH):
            faces = _upload(client, "/api/v1/recognize", photo).json()["faces"]
            match = faces[0]["match"] if faces else None
            if name in strangers:
                outcome["stranger_named" if match else "stranger_rejected"] += 1
            elif match is None:
                outcome["unknown"] += 1
            elif match["name"] == name:
                outcome["correct"] += 1
            else:
                outcome["wrong"] += 1
                wrong.append((photo.name, match["name"], match["similarity"]))

    total = STUDENTS * PROBES_EACH
    print(dict(outcome), wrong)
    # Bounds are set from the full-dataset evaluation, with headroom (see README).
    assert outcome["wrong"] <= 1, f"wrong names: {wrong}"
    assert outcome["stranger_named"] <= 1
    assert outcome["correct"] >= 0.70 * total, dict(outcome)
