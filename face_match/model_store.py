"""Downloads and verifies the ONNX model files used by the face engine."""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

log = logging.getLogger(__name__)

_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"


@dataclass(frozen=True)
class ModelFile:
    filename: str
    url: str
    sha256: str


MODELS: Dict[str, ModelFile] = {
    # YuNet: face detector, ~230 KB.
    "detector": ModelFile(
        filename="face_detection_yunet_2023mar.onnx",
        url=f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    # SFace: face recognizer producing 128-d embeddings, ~37 MB.
    "recognizer": ModelFile(
        filename="face_recognition_sface_2021dec.onnx",
        url=f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(model: ModelFile, target: Path) -> None:
    log.info("Downloading %s ...", model.filename)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False, suffix=".part") as tmp:
        tmp_path = Path(tmp.name)
        try:
            with urllib.request.urlopen(model.url, timeout=60) as resp:
                shutil.copyfileobj(resp, tmp)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    actual = _sha256(tmp_path)
    if actual != model.sha256:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Checksum mismatch for {model.filename}: expected {model.sha256}, got {actual}")
    tmp_path.replace(target)


def ensure_models(models_dir: Path, download: bool = True) -> Dict[str, Path]:
    """Return local paths for every model, downloading missing ones when allowed."""
    models_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, Path] = {}
    for key, model in MODELS.items():
        path = models_dir / model.filename
        if not path.is_file():
            if not download:
                raise FileNotFoundError(
                    f"Model {path} is missing. Run `face-match download-models` "
                    "or set AUTO_DOWNLOAD_MODELS=true."
                )
            _download(model, path)
        paths[key] = path
    return paths
