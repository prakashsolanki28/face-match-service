"""The real ONNX models load and run (on generated images: no faces, no photos of people).

Recognition quality itself isn't covered by automated tests; measure it on your own
photos with `face-match evaluate` (see docs/accuracy.md).
"""

from __future__ import annotations

import numpy as np
import pytest
from fakes import blank_png

from face_match.config import Settings
from face_match.engine import Face, OpenCVFaceEngine
from face_match.liveness import MiniFASNetLiveness
from face_match.main import create_app
from face_match.model_store import ensure_models


@pytest.fixture(scope="module")
def models():
    return ensure_models(Settings().models_dir)


def test_detector_finds_no_face_in_a_blank_image(models):
    engine = OpenCVFaceEngine(models["detector"], models["recognizer"])
    assert engine.detect(np.full((480, 640, 3), 30, np.uint8)) == []


def test_recognizer_produces_a_unit_embedding(models):
    engine = OpenCVFaceEngine(models["detector"], models["recognizer"])
    image = np.random.default_rng(0).integers(0, 255, (480, 640, 3), dtype=np.uint8)
    landmarks = [(300.0, 220.0), (340.0, 220.0), (320.0, 245.0), (305.0, 265.0), (335.0, 265.0)]
    raw = np.array([270, 180, 100, 120, *[v for point in landmarks for v in point], 0.99], np.float32)
    face = Face(box=(270.0, 180.0, 100.0, 120.0), score=0.99, landmarks=landmarks, raw=raw)
    vector = engine.embed(image, face)
    assert vector.shape == (128,) and abs(float(np.linalg.norm(vector)) - 1.0) < 1e-4


def test_liveness_models_return_a_probability(models):
    liveness = MiniFASNetLiveness(models)
    image = np.random.default_rng(1).integers(0, 255, (480, 640, 3), dtype=np.uint8)
    face = Face(box=(270.0, 180.0, 100.0, 120.0), score=0.99, landmarks=[], raw=np.zeros(15, np.float32))
    assert 0.0 <= liveness.score(image, face) <= 1.0


def test_real_service_starts_and_handles_a_blank_image(tmp_path):
    from fastapi.testclient import TestClient

    settings = Settings(data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as client:
        health = client.get("/health").json()
        assert health["engine"] == "opencv-yunet-sface" and health["liveness"] is True
        res = client.post("/api/v1/recognize", files={"image": ("b.png", blank_png(), "image/png")})
        assert res.status_code == 200 and res.json()["faces"] == []
