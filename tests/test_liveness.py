"""Liveness (anti-spoofing): preprocessing geometry and the service rules. No face images."""

from __future__ import annotations

import numpy as np
import pytest
from fakes import Spec, face_png

from face_match.liveness import crop_for_liveness


def _recognize(client, person=1, query=""):
    files = {"image": ("f.png", face_png(Spec(person)), "image/png")}
    return client.post(f"/api/v1/recognize{query}", files=files).json()["faces"][0]


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


def test_spoofed_face_is_returned_without_its_match(client):
    _enroll(client)
    client.app.state.service.liveness.value = 0.1
    face = _recognize(client, query="?closest=true")
    assert face["unknown_reason"] == "liveness_failed" and face["match"] is None
    assert face["closest"] is None  # don't confirm who a suspected spoof shows
    assert face["liveness_score"] == pytest.approx(0.1)


def test_live_face_is_matched_with_its_score(client):
    _enroll(client)
    face = _recognize(client)
    assert face["match"]["name"] == "Asha" and face["liveness_score"] == pytest.approx(0.99)


def test_unknown_faces_are_not_liveness_checked(client):
    _enroll(client)
    client.app.state.service.liveness.value = 0.1
    face = _recognize(client, person=2)
    assert face["unknown_reason"] == "below_threshold" and face["liveness_score"] is None


def test_threshold_is_configurable(make_client):
    with make_client(liveness_threshold=0.995) as client:
        _enroll(client)
        assert _recognize(client)["unknown_reason"] == "liveness_failed"


def test_check_can_be_skipped_per_request(client):
    """For uploaded photos, which aren't re-captured from a camera."""
    _enroll(client)
    client.app.state.service.liveness.value = 0.1
    face = _recognize(client, query="?liveness=false")
    assert face["match"]["name"] == "Asha" and face["liveness_score"] is None


def test_liveness_off_skips_the_check(make_client):
    with make_client(liveness="off") as client:
        assert client.get("/health").json()["liveness"] is False
        _enroll(client)
        for query in ("", "?liveness=true"):
            face = _recognize(client, query=query)
            assert face["match"]["name"] == "Asha" and face["liveness_score"] is None
