"""Shared fixtures. Tests use generated images and model-free fakes (see fakes.py): no photos of people."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
from fakes import FakeEngine, FakeLiveness
from fastapi.testclient import TestClient

from face_match.config import Settings
from face_match.main import create_app

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


def make_test_app(tmp_path: Path, **overrides):
    liveness = None if overrides.get("liveness") == "off" else FakeLiveness()
    return create_app(make_test_settings(tmp_path, **overrides), engine=FakeEngine(), liveness=liveness)


@pytest.fixture
def make_client(tmp_path):
    """Build a client on a fresh database. Pass Settings overrides as kwargs."""
    reset_test_database()

    def factory(**overrides) -> TestClient:
        return TestClient(make_test_app(tmp_path, **overrides))

    return factory


@pytest.fixture
def client(make_client):
    with make_client() as c:
        yield c
