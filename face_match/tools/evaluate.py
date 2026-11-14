#!/usr/bin/env python3
"""Measure recognition accuracy on a labelled photo set, e.g. a pilot class.

It replays the school workflow with the service's real code:
  1. enroll each person from K photos (like portal passport photos) through the
     normal enrollment checks;
  2. try to recognise every *other* photo of them;
  3. also show it photos of people who were never enrolled ("strangers").

For every probe photo the outcome is one of:
  correct      the right name                  (good)
  wrong name   another enrolled person's name  (the costly error)
  unknown      no name; a teacher marks it     (safe)
  no face      no usable face in the photo
and for strangers: rejected (good) or accepted as someone (costly).

Identity comes from the parent folder (photos/Asha Verma/1.jpg) or the file
name (Asha Verma_12.jpg). Photos with the same file name in --enroll-dir and
--probe-dir are treated as the same shot and never used as a probe for it.

Example (portal photos to enroll, camera photos as probes):
  face-match evaluate --enroll-dir ./enroll-photos --probe-dir ./camera-photos \\
      --enroll-per-person 1,3,5
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import random
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..config import Settings
from ..engine import OpenCVFaceEngine
from ..model_store import ensure_models
from ..service import FaceService, ImageInput, ServiceError
from ..storage import Database

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
THRESHOLDS = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55]
MARGINS = [0.0, 0.03, 0.05, 0.08, 0.10]


def scan(folder: Path) -> Dict[str, Dict[str, Path]]:
    """identity -> {photo key (file stem) -> path}"""
    photos: Dict[str, Dict[str, Path]] = defaultdict(dict)
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() not in IMAGE_EXTENSIONS or path.name.startswith("."):
            continue
        rel = path.relative_to(folder)
        if len(rel.parts) > 1:
            identity = rel.parts[0]
        else:
            head, sep, tail = path.stem.rpartition("_")
            identity = head if sep and tail.isdigit() else path.stem
        photos[identity][path.stem] = path
    return photos


def build_service(settings: Settings, engine: OpenCVFaceEngine, db_dir: Path) -> FaceService:
    db_dir.mkdir(parents=True, exist_ok=True)
    return FaceService(settings, engine, Database(db_dir / "eval.db"))


@dataclasses.dataclass
class Probe:
    path: Path
    truth: Optional[str]  # None for strangers
    embedding: Optional[np.ndarray]  # None when no usable face


def embed_probes(service: FaceService, items: List[Tuple[Path, Optional[str]]], log) -> List[Probe]:
    probes, started = [], time.perf_counter()
    for i, (path, truth) in enumerate(items, start=1):
        try:
            _, faces = service.analyze(path.read_bytes())
        except ServiceError:
            faces = []
        # Probes are single-subject photos: judge the largest face, as a kiosk would.
        embedding = faces[0].embedding if faces else None
        probes.append(Probe(path, truth, embedding))
        if i % 200 == 0 or i == len(items):
            rate = i / (time.perf_counter() - started)
            log(f"  analysed {i}/{len(items)} probe photos ({rate:.0f}/s)")
    return probes


def per_person_scores(service: FaceService, probes: List[Probe]):
    """Return (names, scores[n_probes, n_people]) with each person's best similarity."""
    rows = service.db.all_embeddings()
    person_ids = sorted({pid for pid, _ in rows})
    names = [service.db.get_person(pid)["name"] for pid in person_ids]
    column = {pid: j for j, pid in enumerate(person_ids)}
    gallery = np.vstack([v for _, v in rows])
    owner = np.array([column[pid] for pid, _ in rows])
    usable = [p for p in probes if p.embedding is not None]
    matrix = np.vstack([p.embedding for p in usable]) if usable else np.empty((0, gallery.shape[1]))
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):  # see app/gallery.py
        sims = matrix @ gallery.T
    scores = np.full((len(usable), len(person_ids)), -1.0, dtype=np.float32)
    for j in range(len(person_ids)):
        scores[:, j] = sims[:, owner == j].max(axis=1)
    return names, usable, scores


def decide(scores: np.ndarray, threshold: float, margin: float) -> np.ndarray:
    """Same rule as FaceService.match_embedding. Returns the chosen column or -1."""
    if scores.shape[1] == 0:
        return np.full(scores.shape[0], -1)
    order = np.argsort(-scores, axis=1)
    best = order[:, 0]
    best_score = scores[np.arange(len(scores)), best]
    if scores.shape[1] > 1:
        runner_up = scores[np.arange(len(scores)), order[:, 1]]
    else:
        runner_up = np.full(len(scores), -np.inf)
    accept = (best_score >= threshold) & (best_score - runner_up >= margin)
    return np.where(accept, best, -1)


def evaluate(names, usable, scores, probes, threshold, margin) -> Dict[str, float]:
    choice = decide(scores, threshold, margin)
    counts = Counter()
    for probe, col in zip(usable, choice, strict=True):
        if probe.truth is None:
            counts["stranger_accepted" if col >= 0 else "stranger_rejected"] += 1
        elif col < 0:
            counts["unknown"] += 1
        elif names[col] == probe.truth:
            counts["correct"] += 1
        else:
            counts["wrong"] += 1
    for probe in probes:
        if probe.embedding is None:
            counts["stranger_no_face" if probe.truth is None else "no_face"] += 1
    known = counts["correct"] + counts["wrong"] + counts["unknown"] + counts["no_face"]
    strangers = counts["stranger_accepted"] + counts["stranger_rejected"] + counts["stranger_no_face"]
    pct = lambda n, d: round(100.0 * n / d, 2) if d else 0.0  # noqa: E731
    return {
        "threshold": threshold,
        "margin": margin,
        "known_probes": known,
        "correct_pct": pct(counts["correct"], known),
        "wrong_pct": pct(counts["wrong"], known),
        "unknown_pct": pct(counts["unknown"], known),
        "no_face_pct": pct(counts["no_face"], known),
        "stranger_probes": strangers,
        "stranger_accepted_pct": pct(counts["stranger_accepted"], strangers),
        "counts": dict(counts),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="face-match evaluate", description=__doc__.split("\n\n")[0])
    parser.add_argument("--enroll-dir", type=Path, required=True, help="Photos used for enrollment.")
    parser.add_argument("--probe-dir", type=Path, help="Photos to recognise (default: --enroll-dir).")
    parser.add_argument(
        "--enroll-per-person", default="1", help="Comma-separated photo counts to compare, e.g. 1,3."
    )
    parser.add_argument("--strangers", type=int, default=6, help="People held out as never enrolled.")
    parser.add_argument("--max-probes-per-person", type=int, default=0, help="0 = all.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=Path("eval-results"))
    args = parser.parse_args(argv)

    ks = sorted({int(k) for k in args.enroll_per_person.split(",")})
    probe_dir = args.probe_dir or args.enroll_dir
    enroll_photos, probe_photos = scan(args.enroll_dir), scan(probe_dir)
    identities = sorted(set(enroll_photos) & set(probe_photos))
    if len(identities) <= args.strangers:
        raise SystemExit(f"Need more than {args.strangers} identities; found {len(identities)}.")

    rng = random.Random(args.seed)
    strangers = set(rng.sample(identities, args.strangers))
    known = [i for i in identities if i not in strangers]
    enroll_order = {i: rng.sample(sorted(enroll_photos[i]), len(enroll_photos[i])) for i in known}
    reserved = {i: set(enroll_order[i][: max(ks)]) for i in known}  # never probe an enrollment shot

    items: List[Tuple[Path, Optional[str]]] = []
    for identity in identities:
        keys = [k for k in sorted(probe_photos[identity]) if k not in reserved.get(identity, set())]
        if args.max_probes_per_person:
            keys = rng.sample(keys, min(len(keys), args.max_probes_per_person))
        truth = None if identity in strangers else identity
        items += [(probe_photos[identity][k], truth) for k in keys]

    base = dataclasses.replace(Settings.from_env(), webhook_url="")
    models = ensure_models(base.models_dir, download=base.auto_download_models)
    engine = OpenCVFaceEngine(
        models["detector"], models["recognizer"], score_threshold=base.detection_threshold
    )
    log = lambda msg: print(msg, flush=True)  # noqa: E731

    log(
        f"{len(identities)} people: {len(known)} enrolled, {len(strangers)} strangers "
        f"({', '.join(sorted(strangers))})"
    )
    log(
        f"{len(items)} probe photos; settings: threshold {base.match_threshold}, margin {base.match_margin}\n"
    )

    with tempfile.TemporaryDirectory() as tmp:
        probes = embed_probes(build_service(base, engine, Path(tmp) / "probe"), items, log)
        report = {
            "identities": len(identities),
            "strangers": sorted(strangers),
            "probes": len(items),
            "runs": [],
        }
        failures: List[Dict[str, object]] = []

        for k in ks:
            service = build_service(base, engine, Path(tmp) / f"k{k}")
            enroll_issues: Counter = Counter()
            for identity in known:
                images = [
                    ImageInput(enroll_photos[identity][key].read_bytes(), key)
                    for key in enroll_order[identity][:k]
                ]
                try:
                    result = service.enroll(identity, identity, images)
                    enroll_issues.update(r.reason for r in result.rejected)
                except ServiceError as exc:
                    enroll_issues[exc.code] += 1
            enrolled = service.db.stats()["persons"]
            names, usable, scores = per_person_scores(service, probes)

            default = evaluate(names, usable, scores, probes, base.match_threshold, base.match_margin)
            sweep = [evaluate(names, usable, scores, probes, t, m) for t in THRESHOLDS for m in MARGINS]
            report["runs"].append(
                {
                    "photos_per_person": k,
                    "enrolled": enrolled,
                    "enroll_issues": dict(enroll_issues),
                    "default": default,
                    "sweep": sweep,
                }
            )

            log(
                f"=== {k} enrollment photo(s) per person: {enrolled}/{len(known)} enrolled"
                + (f"; rejected photos: {dict(enroll_issues)}" if enroll_issues else "")
            )
            log(
                f"At current settings: correct {default['correct_pct']}% | "
                f"wrong name {default['wrong_pct']}% | "
                f"unknown {default['unknown_pct']}% | no face {default['no_face_pct']}% "
                f"| strangers given a name {default['stranger_accepted_pct']}%"
            )
            log("  threshold margin  correct  wrong  unknown  stranger-accepted")
            for row in sweep:
                if row["margin"] in (0.0, 0.05, 0.10):
                    log(
                        f"  {row['threshold']:>9.2f} {row['margin']:>6.2f} {row['correct_pct']:>8.2f} "
                        f"{row['wrong_pct']:>6.2f} {row['unknown_pct']:>8.2f} "
                        f"{row['stranger_accepted_pct']:>12.2f}"
                    )
            log("")

            choice = decide(scores, base.match_threshold, base.match_margin)
            for probe, col, row in zip(usable, choice, scores, strict=True):
                predicted = names[col] if col >= 0 else None
                if predicted is not None and predicted != probe.truth:
                    failures.append(
                        {
                            "photos_per_person": k,
                            "path": str(probe.path),
                            "truth": probe.truth or "(stranger)",
                            "predicted": predicted,
                            "similarity": round(float(row[col]), 3),
                        }
                    )

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (args.out / f"report-{stamp}.json").write_text(json.dumps(report, indent=2))
    with (args.out / f"wrong-names-{stamp}.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["photos_per_person", "truth", "predicted", "similarity", "path"]
        )
        writer.writeheader()
        writer.writerows(failures)
    log(f"Full report: {args.out / f'report-{stamp}.json'}")
    log(f"Wrong-name cases to inspect: {args.out / f'wrong-names-{stamp}.csv'} ({len(failures)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
