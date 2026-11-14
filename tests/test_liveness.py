"""Liveness (anti-spoofing): preprocessing geometry and the service rules. No face images."""

from __future__ import annotations

import numpy as np
import pytest
from fakes import Spec, face_png

from face_match.liveness import crop_for_liveness


def _attendance(client, person=1):
    res = client.post("/api/v1/attendance", files={"image": ("f.png", face_png(Spec(person)), "image/png")})
    return res.json()["faces"][0]


def _enroll(client):
    res = client.post(
        "/api/v1/persons",
        data={"name": "Asha", "external_id": "EMP-1"},
        files=[("images", ("f.png", face_png(Spec(1)), "image/png"))],
    )
    assert res.status_code == 201, res.text


# ---- preprocessing (must match the original implementation) -----------------------


def test_crop_is_shifted_inside_the_image_not_clipped():
    image = np.zeros((100, 200, 3), np.uint8)
    image[:, :20] = 255  # mark the left edge
    patch = crop_for_liveness(image, (0, 30, 20, 20), scale=2.7, size=80)
    assert patch.shape == (80, 80, 3)
    # 54 px wide window starting at x=0 (shifted right, not cut): the white band is ~20/54 of the width
    assert 25 <= (patch[40, :, 0] > 127).sum() <= 35


def test_crop_scale_is_capped_for_large_faces():
    image = np.arange(100 * 100 * 3, dtype=np.uint8).reshape(100, 100, 3)
    assert crop_for_liveness(image, (10, 10, 80, 80), scale=4.0).shape == (80, 80, 3)


# ---- service rules ----------------------------------------------------------------------


def test_spoofed_frame_is_not_recorded_and_name_is_withheld(client):
    _enroll(client)
    client.app.state.service.liveness.value = 0.1
    face = _attendance(client)
    assert face["attendance"]["status"] == "liveness_failed"
    assert face["unknown_reason"] == "liveness_failed" and face["match"] is None
    assert face["liveness_score"] == pytest.approx(0.1)
    assert client.get("/api/v1/attendance").json() == []


def test_live_frame_is_recorded_with_its_score(client):
    _enroll(client)
    face = _attendance(client)
    assert face["attendance"]["status"] == "marked" and face["liveness_score"] == pytest.approx(0.99)


def test_unknown_faces_are_not_liveness_checked(client):
    _enroll(client)
    client.app.state.service.liveness.value = 0.1
    face = _attendance(client, person=2)
    assert face["attendance"]["status"] == "unknown" and face["liveness_score"] is None


def test_threshold_is_configurable(make_client):
    with make_client(liveness_threshold=0.995) as client:
        _enroll(client)
        assert _attendance(client)["attendance"]["status"] == "liveness_failed"


def test_recognize_returns_score_on_request(client):
    _enroll(client)
    files = {"image": ("f.png", face_png(Spec(1)), "image/png")}
    plain = client.post("/api/v1/recognize", files=files).json()["faces"][0]
    scored = client.post("/api/v1/recognize?liveness=true", files=files).json()["faces"][0]
    assert plain["liveness_score"] is None and scored["liveness_score"] == pytest.approx(0.99)


def test_liveness_off_skips_the_check(make_client):
    with make_client(liveness="off") as client:
        assert client.get("/health").json()["liveness"] is False
        _enroll(client)
        face = _attendance(client)
        assert face["attendance"]["status"] == "marked" and face["liveness_score"] is None
