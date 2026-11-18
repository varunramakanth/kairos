"""Text embeddings for semantic memory recall.

``LocalEmbedder`` runs a small open-source model on this machine (fastembed/ONNX), so
memories never leave it: ``BAAI/bge-small-en-v1.5``, 384 dimensions, about 67 MB,
MIT licensed, downloaded once into a cache outside the repository. The model loads
lazily on first use, in a worker thread, so startup stays fast and the event loop is
never blocked.
"""

import asyncio
import hashlib
import math
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

DIMENSIONS = 384
"""Fixed by migration 0007 (``vector(384)``). A model with another size needs a migration."""
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class Embedder(Protocol):
    """Turns text into vectors. Documents and queries may be embedded differently
    (retrieval models often prefix queries)."""

    @property
    def name(self) -> str: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def default_cache() -> Path:
    """Outside the repository (which may be iCloud-synced) and shared across checkouts."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "kairos" / "models"


class LocalEmbedder:
    def __init__(self, model: str = DEFAULT_MODEL, *, cache_dir: Path | None = None) -> None:
        self._model_name = model
        self._cache = cache_dir or default_cache()
        self._model: Any = None
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._model_name

    async def _load(self) -> Any:
        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._create)
        return self._model

    def _create(self) -> Any:
        from fastembed import TextEmbedding

        self._cache.mkdir(parents=True, exist_ok=True)
        return TextEmbedding(self._model_name, cache_dir=str(self._cache))

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        model = await self._load()
        vectors = await asyncio.to_thread(lambda: list(model.embed(list(texts))))
        return [[float(x) for x in v] for v in vectors]

    async def embed_query(self, text: str) -> list[float]:
        model = await self._load()
        vectors = await asyncio.to_thread(lambda: list(model.query_embed(text)))
        return [float(x) for x in vectors[0]]


class HashEmbedder:
    """A deterministic stand-in for tests: hashed bag of words, unit length.

    Lexical, not semantic, but it exercises the whole vector path without a model.
    """

    name = "hash-384"

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [_hash_vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return _hash_vector(text)


def _hash_vector(text: str) -> list[float]:
    vector = [0.0] * DIMENSIONS
    for word in text.lower().split():
        digest = hashlib.sha256(word.strip(".,!?;:").encode()).digest()
        vector[int.from_bytes(digest[:4], "big") % DIMENSIONS] += 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def literal(vector: Sequence[float]) -> str:
    """A vector in pgvector's text form, for ``CAST(:v AS vector)``."""
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"
