"""Liveness (anti-spoofing): preprocessing, the real model on reference captures, and service rules."""

from __future__ import annotations

import urllib.request
from pathlib import Path

import numpy as np
import pytest

from face_match.liveness import crop_for_liveness

CACHE = Path(__file__).parent / ".cache"
# Minivision's own sample captures (Apache-2.0), pinned to a commit and downloaded at test time.
_SAMPLES = (
    "https://raw.githubusercontent.com/minivision-ai/Silent-Face-Anti-Spoofing/"
    "b6d5f04ad78778917853b25c778acef6d5626d15/images/sample/{}"
)


def _sample(name: str) -> bytes:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"minivision_{name}"
    if not path.exists():
        try:
            with urllib.request.urlopen(_SAMPLES.format(name), timeout=20) as resp:
                path.write_bytes(resp.read())
        except Exception as exc:
            pytest.skip(f"could not download liveness sample {name}: {exc}")
    return path.read_bytes()


def _upload(client, path, image: bytes, **params):
    return client.post(path, params=params, files={"image": ("f.jpg", image, "image/jpeg")})


# ---- preprocessing (must match the original implementation) -----------------------


def test_crop_is_shifted_inside_the_image_not_clipped():
    image = np.zeros((100, 200, 3), np.uint8)
    image[:, :20] = 255  # mark the left edge
    patch = crop_for_liveness(image, (0, 30, 20, 20), scale=2.7, size=80)
    assert patch.shape == (80, 80, 3)
    # 54 px wide window starting at x=0 (shifted right, not cut): the white band is ~20/54 of the width
    white = (patch[40, :, 0] > 127).sum()
    assert 25 <= white <= 35


def test_crop_scale_is_capped_for_large_faces():
    image = np.arange(100 * 100 * 3, dtype=np.uint8).reshape(100, 100, 3)
    patch = crop_for_liveness(image, (10, 10, 80, 80), scale=4.0)  # 4x would exceed the image
    assert patch.shape == (80, 80, 3)


# ---- the real model on reference captures --------------------------------------------


@pytest.mark.parametrize(
    ("name", "live"),
    [("image_T1.jpg", True), ("image_F1.jpg", False), ("image_F2.jpg", False)],
    ids=["real-person", "printed-photo", "phone-screen"],
)
def test_reference_captures(client, name, live):
    face = _upload(client, "/api/v1/recognize", _sample(name), liveness="true").json()["faces"][0]
    score = face["liveness_score"]
    assert (score >= 0.5) is live, score


def test_health_reports_liveness(client, make_client):
    assert client.get("/health").json()["liveness"] is True
    with make_client(liveness="off") as off:
        assert off.get("/health").json()["liveness"] is False


# ---- service rules ----------------------------------------------------------------------


class _FixedLiveness:
    name = "fixed"

    def __init__(self, value: float) -> None:
        self.value = value

    def score(self, image, face) -> float:
        return self.value


def _enroll(client, image: bytes):
    res = client.post(
        "/api/v1/persons",
        data={"name": "Lena", "external_id": "EMP-1"},
        files=[("images", ("f.jpg", image, "image/jpeg"))],
    )
    assert res.status_code == 201, res.text


def test_spoofed_frame_is_not_recorded_and_name_is_withheld(client, lena):
    _enroll(client, lena)
    client.app.state.service.liveness = _FixedLiveness(0.1)
    face = _upload(client, "/api/v1/attendance", lena).json()["faces"][0]
    assert face["attendance"]["status"] == "liveness_failed"
    assert face["unknown_reason"] == "liveness_failed" and face["match"] is None
    assert face["liveness_score"] == pytest.approx(0.1)
    assert client.get("/api/v1/attendance").json() == []


def test_live_frame_is_recorded_with_its_score(client, lena):
    _enroll(client, lena)
    client.app.state.service.liveness = _FixedLiveness(0.97)
    face = _upload(client, "/api/v1/attendance", lena).json()["faces"][0]
    assert face["attendance"]["status"] == "marked" and face["liveness_score"] == pytest.approx(0.97)


def test_threshold_is_configurable(make_client, lena):
    with make_client(liveness_threshold=0.99) as client:
        _enroll(client, lena)
        client.app.state.service.liveness = _FixedLiveness(0.97)
        status = _upload(client, "/api/v1/attendance", lena).json()["faces"][0]["attendance"]["status"]
        assert status == "liveness_failed"


def test_liveness_off_skips_the_check(make_client, lena):
    with make_client(liveness="off") as client:
        _enroll(client, lena)
        face = _upload(client, "/api/v1/attendance", lena).json()["faces"][0]
        assert face["attendance"]["status"] == "marked" and face["liveness_score"] is None
