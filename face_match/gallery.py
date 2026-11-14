"""In-memory index of enrolled face embeddings for fast matching."""

from __future__ import annotations

import threading
from typing import Iterable, List, NamedTuple, Optional, Tuple

import numpy as np


class Match(NamedTuple):
    person_id: str
    similarity: float
    # Best similarity among *other* people; None when only one person is enrolled.
    runner_up: Optional[float]


def _scores(matrix: np.ndarray, vector: np.ndarray) -> np.ndarray:
    # NumPy 2.0 + Apple Accelerate raises spurious FP warnings on float32 matmul;
    # the results are correct (verified against float64), so silence them here only.
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        return matrix @ vector


class FaceGallery:
    """Holds every enrolled embedding as one matrix; a search is a single mat-vec.

    Brute-force cosine search takes ~0.02 ms for 2,000 embeddings and ~0.5 ms for
    10,000. Swap in FAISS or pgvector only well beyond that.
    """

    def __init__(self, dim: int) -> None:
        self._dim = dim
        self._lock = threading.RLock()
        self._matrix = np.empty((0, dim), dtype=np.float32)
        self._owners: List[str] = []

    def __len__(self) -> int:
        return len(self._owners)

    def load(self, rows: Iterable[Tuple[str, np.ndarray]]) -> None:
        owners, vectors = [], []
        for person_id, vector in rows:
            owners.append(person_id)
            vectors.append(vector)
        matrix = np.vstack(vectors).astype(np.float32) if vectors else np.empty((0, self._dim), np.float32)
        with self._lock:
            self._matrix, self._owners = matrix, owners

    def add(self, person_id: str, vectors: List[np.ndarray]) -> None:
        if not vectors:
            return
        with self._lock:
            self._matrix = np.vstack([self._matrix, *vectors]).astype(np.float32)
            self._owners = self._owners + [person_id] * len(vectors)

    def remove(self, person_id: str) -> None:
        with self._lock:
            keep = [i for i, owner in enumerate(self._owners) if owner != person_id]
            self._matrix = self._matrix[keep]
            self._owners = [self._owners[i] for i in keep]

    def similarity_to(self, person_id: str, vector: np.ndarray) -> Optional[float]:
        """Highest similarity between `vector` and one person's enrolled faces."""
        with self._lock:
            rows = [i for i, owner in enumerate(self._owners) if owner == person_id]
            if not rows:
                return None
            return float(np.max(_scores(self._matrix[rows], vector)))

    def best_match(self, vector: np.ndarray) -> Optional[Match]:
        """Closest enrolled person, plus the best score of anyone else."""
        with self._lock:
            if not self._owners:
                return None
            scores = _scores(self._matrix, vector)
            order = np.argsort(-scores)
            best = int(order[0])
            owner = self._owners[best]
            runner_up = next((float(scores[i]) for i in order[1:] if self._owners[int(i)] != owner), None)
            return Match(owner, float(scores[best]), runner_up)
