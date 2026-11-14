"""Downloads and verifies the ONNX model files used by the face engine."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

log = logging.getLogger(__name__)

_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"


@dataclass(frozen=True)
class ModelFile:
    filename: str
    url: str
    sha256: str
    # Fallback (url, sha256) pairs, tried in order if the primary download fails.
    mirrors: Tuple[Tuple[str, str], ...] = ()

    def sources(self) -> Tuple[Tuple[str, str], ...]:
        return ((self.url, self.sha256), *self.mirrors)


_MINIFAS = "https://github.com/yakhyo/face-anti-spoofing/releases/download/weights"
_MINIFAS_MIRROR = (
    "https://github.com/QingHeYang/Silent-Face-Anti-Spoofing-onnx/raw/"
    "584d4421d7ac42c59e640796f46e886b0095367a/onnx"
)


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
    # Liveness (anti-spoofing): Minivision MiniFASNet pair, Apache-2.0
    # (minivision-ai/Silent-Face-Anti-Spoofing). ONNX exports verified to contain the original
    # weights unchanged. ~1.7 MB each.
    "liveness_v2": ModelFile(
        filename="minifasnet_v2_2.7_80x80.onnx",
        url=f"{_MINIFAS}/MiniFASNetV2.onnx",
        sha256="b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907",
        mirrors=(
            (
                f"{_MINIFAS_MIRROR}/2.7_80x80_MiniFASNetV2.onnx",
                "0cbe5caec95c31de9d2ef845cb85407d76aecd1b6a2c0e343f7d35306bfbccb8",
            ),
        ),
    ),
    "liveness_v1se": ModelFile(
        filename="minifasnet_v1se_4.0_80x80.onnx",
        url=f"{_MINIFAS}/MiniFASNetV1SE.onnx",
        sha256="ebab7f90c7833fbccd46d3a555410e78d969db5438e169b6524be444862b3676",
        mirrors=(
            (
                f"{_MINIFAS_MIRROR}/4_0_0_80x80_MiniFASNetV1SE.onnx",
                "a25886a85cdcfa2c4ea23edb71de35f250c17827b4cadd253a972b28c80fdf1e",
            ),
        ),
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(model: ModelFile, target: Path) -> None:
    errors = []
    for url, sha256 in model.sources():
        try:
            _download_one(model.filename, url, sha256, target)
            return
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            log.warning("Download of %s from %s failed: %s", model.filename, url, exc)
            errors.append(str(exc))
    raise RuntimeError(f"Could not download {model.filename}: " + " | ".join(errors))


def _download_one(filename: str, url: str, sha256: str, target: Path) -> None:
    log.info("Downloading %s ...", filename)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False, suffix=".part") as tmp:
        tmp_path = Path(tmp.name)
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                shutil.copyfileobj(resp, tmp)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    actual = _sha256(tmp_path)
    if actual != sha256:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Checksum mismatch for {filename}: expected {sha256}, got {actual}")
    # Temp files are created owner-only (0600). Models are public data and are often downloaded by one
    # user (e.g. root during `docker build`) and read by another, so apply normal file permissions.
    umask = os.umask(0)
    os.umask(umask)
    tmp_path.chmod(0o666 & ~umask)
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
