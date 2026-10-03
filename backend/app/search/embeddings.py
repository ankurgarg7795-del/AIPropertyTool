"""Embedding providers.

* ``HashingEmbedder`` – dependency-free, deterministic feature hashing with a
  small real-estate synonym map. Good enough for the demo and for tests.
* ``SentenceTransformerEmbedder`` – production default (``BAAI/bge-m3``:
  multilingual, handles Hinglish, 1024-d). Install ``sentence-transformers``.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from app.core.config import get_settings

TOKEN_RE = re.compile(r"[a-z0-9]+")
SYNONYMS = {
    "sunlit": "light", "sunny": "light", "bright": "light", "airy": "light", "daylight": "light",
    "ventilated": "light", "tech": "techpark", "it": "techpark", "office": "techpark",
    "sez": "techpark", "metro": "transit", "station": "transit", "quiet": "peaceful",
    "calm": "peaceful", "serene": "peaceful", "kids": "family", "children": "family",
    "school": "family", "maintenance": "upkeep", "cheap": "affordable", "budget": "affordable",
    "luxury": "premium", "lavish": "premium", "pool": "swimming", "flat": "apartment",
}
STOP = {"a", "an", "the", "in", "near", "with", "and", "or", "of", "for", "to", "me", "show", "i", "want", "under"}


def tokens(text: str) -> list[str]:
    out = []
    for t in TOKEN_RE.findall(text.lower()):
        if t in STOP:
            continue
        out.append(SYNONYMS.get(t, t))
    return out


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    def __init__(self, dim: int = 384):
        self.dim = dim

    def _bucket(self, feat: str) -> tuple[int, float]:
        h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
        return h % self.dim, (1.0 if (h >> 63) & 1 else -1.0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = []
        for text in texts:
            v = [0.0] * self.dim
            toks = tokens(text)
            feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
            for f in feats:
                i, s = self._bucket(f)
                v[i] += s
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs.append([x / n for x in v])
        return vecs


class SentenceTransformerEmbedder:
    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer  # type: ignore

        self._m = SentenceTransformer(model)
        self.dim = self._m.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._m.encode(texts, normalize_embeddings=True).tolist()


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        s = get_settings()
        _embedder = (SentenceTransformerEmbedder(s.embedding_model) if s.embedder == "sentence-transformers"
                     else HashingEmbedder(s.embedding_dim))
    return _embedder
