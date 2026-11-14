"""Passive liveness (anti-spoofing): Minivision's MiniFASNet pair on OpenCV DNN.

Detects a face *re-captured* from a print or a screen held up to the camera. It does not
detect a digital photo sent straight to the API (that looks "live"), so attendance must come
from a trusted camera. See docs/privacy.md.

Pre- and post-processing follow the original implementation exactly
(minivision-ai/Silent-Face-Anti-Spoofing @ b6d5f04):
  * crop: src/generate_patches.py (scale x box around its centre, shifted back inside the
    image rather than clipped, then resized to 80x80)
  * tensor: src/data_io/functional.py (BGR, float 0-255, no normalisation, NCHW)
  * output: 3 logits, softmax, index 1 = real; the two models' P(real) are averaged
    (as in Minivision's Android app, whose default threshold is 0.915).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

import cv2
import numpy as np

from .config import Settings
from .engine import Face, LivenessEngine

# (model key, crop scale relative to the face box)
_MODELS: Sequence[Tuple[str, float]] = (("liveness_v2", 2.7), ("liveness_v1se", 4.0))
_INPUT = 80


def crop_for_liveness(
    image: np.ndarray, box: Sequence[float], scale: float, size: int = _INPUT
) -> np.ndarray:
    """Original CropImage: scale x box around its centre, capped to the image, shifted inside it."""
    src_h, src_w = image.shape[:2]
    x, y, box_w, box_h = (int(v) for v in box[:4])
    box_w, box_h = max(box_w, 1), max(box_h, 1)
    scale = min((src_h - 1) / box_h, (src_w - 1) / box_w, scale)
    new_w, new_h = box_w * scale, box_h * scale
    center_x, center_y = x + box_w / 2, y + box_h / 2
    x1, y1 = center_x - new_w / 2, center_y - new_h / 2
    x2, y2 = center_x + new_w / 2, center_y + new_h / 2
    if x1 < 0:
        x2 -= x1
        x1 = 0
    if y1 < 0:
        y2 -= y1
        y1 = 0
    if x2 > src_w - 1:
        x1 -= x2 - src_w + 1
        x2 = src_w - 1
    if y2 > src_h - 1:
        y1 -= y2 - src_h + 1
        y2 = src_h - 1
    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    return cv2.resize(image[y1 : y2 + 1, x1 : x2 + 1], (size, size))


class MiniFASNetLiveness:
    name = "minifasnet-v2+v1se"

    def __init__(self, model_paths: Dict[str, Path]) -> None:
        self._nets = [(cv2.dnn.readNetFromONNX(str(model_paths[key])), scale) for key, scale in _MODELS]
        self._lock = threading.Lock()  # cv2.dnn.Net is not thread-safe

    def score(self, image: np.ndarray, face: Face) -> float:
        p_real = []
        for net, scale in self._nets:
            patch = crop_for_liveness(image, face.box, scale)
            blob = patch.astype(np.float32).transpose(2, 0, 1)[None]  # BGR, 0-255, NCHW
            with self._lock:
                net.setInput(blob)
                logits = net.forward().reshape(-1)
            exp = np.exp(logits - logits.max())
            p_real.append(exp[1] / exp.sum())
        return float(np.mean(p_real))


def load_liveness(settings: Settings, model_paths: Dict[str, Path]) -> Optional[LivenessEngine]:
    """Return the liveness engine, or None when liveness is off."""
    if settings.liveness != "on":
        return None
    return MiniFASNetLiveness(model_paths)
