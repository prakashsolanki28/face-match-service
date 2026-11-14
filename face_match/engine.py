"""Face engine: detection (YuNet) and embedding extraction (SFace), both via OpenCV DNN.

The service depends only on the `FaceEngine` protocol, so a heavier backend
(e.g. InsightFace/ArcFace) can be swapped in without touching the API layer.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Protocol, Tuple

import cv2
import numpy as np

cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)


@dataclass
class Face:
    """A detected face, in the coordinates of the image passed to `detect`."""

    box: Tuple[float, float, float, float]  # x, y, width, height
    score: float
    # Five points: right eye, left eye, nose tip, right mouth corner, left mouth corner.
    landmarks: List[Tuple[float, float]]
    raw: np.ndarray = field(repr=False)  # engine-specific detection row, used for alignment

    @property
    def size(self) -> float:
        return min(self.box[2], self.box[3])


class FaceEngine(Protocol):
    name: str
    embedding_size: int

    def detect(self, image: np.ndarray) -> List[Face]: ...

    def embed(self, image: np.ndarray, face: Face) -> np.ndarray:
        """Return an L2-normalised float32 embedding for `face`."""
        ...


class LivenessEngine(Protocol):
    """Presentation-attack detection: is this a live face, or a photo/screen held up to the camera?"""

    name: str

    def score(self, image: np.ndarray, face: Face) -> float:
        """Probability (0-1) that `face` in `image` is a real, live face."""
        ...


class OpenCVFaceEngine:
    name = "opencv-yunet-sface"
    embedding_size = 128

    def __init__(
        self,
        detector_path: Path,
        recognizer_path: Path,
        score_threshold: float = 0.9,
        nms_threshold: float = 0.3,
    ) -> None:
        self._detector = cv2.FaceDetectorYN.create(
            str(detector_path), "", (320, 320), score_threshold, nms_threshold, 5000
        )
        self._recognizer = cv2.FaceRecognizerSF.create(str(recognizer_path), "")
        # OpenCV DNN objects are stateful (input size, buffers), so serialise access.
        self._detect_lock = threading.Lock()
        self._embed_lock = threading.Lock()

    def detect(self, image: np.ndarray) -> List[Face]:
        height, width = image.shape[:2]
        with self._detect_lock:
            self._detector.setInputSize((width, height))
            _, rows = self._detector.detect(image)
        if rows is None:
            return []
        faces = [
            Face(
                box=(float(r[0]), float(r[1]), float(r[2]), float(r[3])),
                score=float(r[14]),
                landmarks=[(float(r[4 + 2 * i]), float(r[5 + 2 * i])) for i in range(5)],
                raw=r.copy(),
            )
            for r in rows
        ]
        faces.sort(key=lambda f: f.box[2] * f.box[3], reverse=True)  # largest first
        return faces

    def embed(self, image: np.ndarray, face: Face) -> np.ndarray:
        with self._embed_lock:
            aligned = self._recognizer.alignCrop(image, face.raw)
            feature = self._recognizer.feature(aligned)
        vector = feature.ravel().astype(np.float32)
        return vector / (np.linalg.norm(vector) + 1e-10)
