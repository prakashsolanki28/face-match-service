from __future__ import annotations

import hashlib
import os
import stat

import pytest

from face_match import model_store
from face_match.model_store import ModelFile, ensure_models


@pytest.fixture
def fake_model(tmp_path, monkeypatch):
    source = tmp_path / "source.onnx"
    source.write_bytes(b"fake model bytes")
    model = ModelFile("fake.onnx", source.as_uri(), hashlib.sha256(b"fake model bytes").hexdigest())
    monkeypatch.setattr(model_store, "MODELS", {"detector": model})
    return model


def test_downloaded_models_are_readable_by_other_users(tmp_path, fake_model):
    """Regression: temp files are 0600, which broke the non-root Docker image (downloaded as root)."""
    old = os.umask(0o022)
    try:
        path = ensure_models(tmp_path / "models")["detector"]
    finally:
        os.umask(old)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & stat.S_IROTH and mode & stat.S_IRGRP, oct(mode)
    assert path.read_bytes() == b"fake model bytes"


def test_checksum_mismatch_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "source.onnx"
    source.write_bytes(b"tampered")
    monkeypatch.setattr(model_store, "MODELS", {"detector": ModelFile("x.onnx", source.as_uri(), "0" * 64)})
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        ensure_models(tmp_path / "models")
    assert not (tmp_path / "models" / "x.onnx").exists()


def test_missing_model_without_download_explains_the_fix(tmp_path, fake_model):
    with pytest.raises(FileNotFoundError, match="face-match download-models"):
        ensure_models(tmp_path / "models", download=False)
