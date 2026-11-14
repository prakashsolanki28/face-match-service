"""Model-free test doubles: generated images instead of photos of people.

A test image is a black PNG canvas with filled squares. Each square is one "face":
  * red channel 200 marks it as a face,
  * green = the person id (0-255),
  * blue = 100 + variant (which "photo" of that person),
  * its side length is the face size in pixels.
PNG is lossless, so the fake engine reads identities back exactly.

Embeddings mimic real behaviour:
  * the same person, different variant: cosine ~0.97
  * two different people (ids 0-99): cosine ~0
  * person 100+k is a "twin" of k: cosine ~0.99 (exercises MATCH_MARGIN)
  * person 150+k is a "look-alike" of k: cosine ~0.46 (exercises DUPLICATE_THRESHOLD)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

import cv2
import numpy as np

from face_match.engine import Face

DIM = 128
_FACE_RED = 200


def _unit(vector: np.ndarray) -> np.ndarray:
    return (vector / np.linalg.norm(vector)).astype(np.float32)


def _noise(seed: int) -> np.ndarray:
    return _unit(np.random.default_rng(seed).normal(size=DIM))


def _near(base: np.ndarray, cosine: float, seed: int) -> np.ndarray:
    """A unit vector with exactly `cosine` similarity to `base` (noise made perpendicular to it)."""
    noise = _noise(seed)
    noise = _unit(noise - float(noise @ base) * base)
    return _unit(base + np.sqrt(1 / cosine**2 - 1) * noise)


def person_vector(person: int) -> np.ndarray:
    if 100 <= person < 150:  # twin of person - 100
        return _near(person_vector(person - 100), 0.99, 10_000 + person)
    if 150 <= person < 200:  # look-alike of person - 150
        return _near(person_vector(person - 150), 0.46, 20_000 + person)
    return _noise(person)


def embedding(person: int, variant: int) -> np.ndarray:
    base = person_vector(person)
    return base if variant == 0 else _near(base, 0.97, 1_000 * person + variant + 7)


@dataclass
class Spec:
    person: int
    variant: int = 0
    size: int = 160


def face_png(*specs: Spec, width: int = 640, height: int = 480) -> bytes:
    """Encode the given faces side by side (largest first is up to the caller)."""
    canvas = np.zeros((height, width, 3), np.uint8)
    x = 20
    for spec in specs:
        y = max(0, (height - spec.size) // 2)
        canvas[y : y + spec.size, x : x + spec.size] = (100 + spec.variant, spec.person, _FACE_RED)
        x += spec.size + 20
    return cv2.imencode(".png", canvas)[1].tobytes()


def blank_png(width: int = 640, height: int = 480) -> bytes:
    return cv2.imencode(".png", np.full((height, width, 3), 30, np.uint8))[1].tobytes()


class FakeEngine:
    """Implements the FaceEngine protocol on generated images."""

    name = "fake"
    embedding_size = DIM

    def detect(self, image: np.ndarray) -> List[Face]:
        mask = (np.abs(image[:, :, 2].astype(int) - _FACE_RED) < 8).astype(np.uint8)
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        faces = []
        for i in range(1, count):
            x, y, w, h, area = (int(v) for v in stats[i])
            if area < 9:
                continue
            landmarks = [
                (x + w * 0.3, y + h * 0.4),
                (x + w * 0.7, y + h * 0.4),
                (x + w * 0.5, y + h * 0.6),
                (x + w * 0.35, y + h * 0.8),
                (x + w * 0.65, y + h * 0.8),
            ]
            faces.append(
                Face(
                    box=(float(x), float(y), float(w), float(h)),
                    score=0.99,
                    landmarks=landmarks,
                    raw=np.zeros(15, np.float32),
                )
            )
        faces.sort(key=lambda f: f.box[2] * f.box[3], reverse=True)
        return faces

    def embed(self, image: np.ndarray, face: Face) -> np.ndarray:
        x, y, w, h = (int(v) for v in face.box)
        blue, green, _ = (int(v) for v in image[y + h // 2, x + w // 2])
        return embedding(person=green, variant=max(0, blue - 100))


class FakeLiveness:
    """Returns a fixed 'live' probability; tests change `value`."""

    name = "fake-liveness"

    def __init__(self, value: float = 0.99) -> None:
        self.value = value

    def score(self, image: np.ndarray, face: Face) -> float:
        return self.value


def similarity(a: Sequence[float], b: Sequence[float]) -> float:
    return float(np.dot(a, b))
