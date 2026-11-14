from __future__ import annotations

import dataclasses
import os
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from face_match.config import Settings
from face_match.main import create_app

CACHE = Path(__file__).parent / ".cache"

# Run the whole suite against PostgreSQL by setting this, e.g.
#   FACE_MATCH_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/facematch_test pytest
TEST_DATABASE_URL = os.environ.get("FACE_MATCH_TEST_DATABASE_URL", "")
sqlite_only = pytest.mark.skipif(bool(TEST_DATABASE_URL), reason="SQLite-specific behaviour")


def reset_test_database() -> None:
    """Give each test an empty PostgreSQL schema (no-op on SQLite, which uses a temp dir)."""
    if TEST_DATABASE_URL:
        import psycopg

        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")


def make_test_settings(tmp_path: Path, **overrides) -> Settings:
    base = dataclasses.replace(Settings(), data_dir=tmp_path / "data", database_url=TEST_DATABASE_URL)
    return dataclasses.replace(base, **overrides)


SAMPLES = {
    # Public OpenCV sample images (BSD licence), used only for tests.
    "lena": "https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/lena.jpg",
    "messi": "https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/messi5.jpg",
}


def _sample(name: str) -> bytes:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{name}.jpg"
    if not path.exists():
        try:
            with urllib.request.urlopen(SAMPLES[name], timeout=20) as resp:
                path.write_bytes(resp.read())
        except Exception as exc:  # offline CI, etc.
            pytest.skip(f"could not download sample image {name}: {exc}")
    return path.read_bytes()


def _encode(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", image)
    assert ok
    return buf.tobytes()


@pytest.fixture
def make_client(tmp_path):
    """Build a client on a fresh database. Pass Settings overrides as kwargs."""

    reset_test_database()

    def factory(**overrides) -> TestClient:
        return TestClient(create_app(make_test_settings(tmp_path, **overrides)))

    return factory


@pytest.fixture
def client(make_client):
    with make_client() as c:
        yield c


@pytest.fixture(scope="session")
def lena() -> bytes:
    return _sample("lena")


@pytest.fixture(scope="session")
def lena_variant(lena) -> bytes:
    """Same person, different-looking photo: mirrored, darker and lower contrast."""
    image = cv2.imdecode(np.frombuffer(lena, np.uint8), cv2.IMREAD_COLOR)
    image = cv2.convertScaleAbs(cv2.flip(image, 1), alpha=0.75, beta=15)
    return _encode(image)


@pytest.fixture(scope="session")
def messi() -> bytes:
    """A different person. Upscaled because the face is only ~31 px wide in the original."""
    image = cv2.imdecode(np.frombuffer(_sample("messi"), np.uint8), cv2.IMREAD_COLOR)
    return _encode(cv2.resize(image, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC))


@pytest.fixture(scope="session")
def messi_small() -> bytes:
    return _sample("messi")


@pytest.fixture(scope="session")
def passport_thumb(lena) -> bytes:
    """A 100x125 passport-style crop (head and shoulders), as school portals often store."""
    image = cv2.imdecode(np.frombuffer(lena, np.uint8), cv2.IMREAD_COLOR)
    x, y, w, h = 208, 183, 146, 207  # Lena's face box
    crop = image[int(y - 0.35 * h) : int(y + 1.45 * h), int(x - 0.35 * w) : int(x + 1.35 * w)]
    return _encode(cv2.resize(crop, (100, 125), interpolation=cv2.INTER_AREA))


@pytest.fixture(scope="session")
def blank() -> bytes:
    return _encode(np.full((480, 640, 3), 200, np.uint8))
